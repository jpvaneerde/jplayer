"""Raspberry Pi OS updates, from the apps' side (the same for the player, the display and the hub).

The work is done as root by deploy/system-update.sh, through three services the apps may start with sudo
(deploy/install.sh writes the rule):
  jplayer-system-check.service    apt update: how many updates wait, is a restart needed (also daily by itself)
  jplayer-system-update.service   install them; with {"reboot": true} in system-request.json also restart when needed
  jplayer-system-reboot.service   restart the Pi
It reports in STATE_DIR/system-status.json. Automatic installs (03:00–05:00, when nothing plays) are on by default;
the setting ("system": "night" | "off") is kept with the app updates' settings in update-settings.json.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime

from .updates import NIGHT, SYSTEMCTL

STALE_AFTER = 90 * 60           # an "installing" status older than this: the updater died
AUTO_RETRY_AFTER = 20 * 3600    # at most one automatic attempt per night
UNITS = {"check": "jplayer-system-check.service", "install": "jplayer-system-update.service",
         "reboot": "jplayer-system-reboot.service"}


class SystemError_(Exception):
    pass


def reboot_needed_now() -> bool:
    """The same test as system-update.sh, for an answer between checks."""
    if os.path.exists("/run/reboot-required") or os.path.exists("/var/run/reboot-required"):
        return True
    try:
        with open("/proc/uptime") as f:
            booted = time.time() - float(f.read().split()[0])
    except OSError:
        return False
    for d in ("/boot/firmware", "/boot"):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if (n.startswith("kernel") and n.endswith(".img")) or (n.startswith("start") and n.endswith(".elf")):
                try:
                    if os.stat(os.path.join(d, n)).st_mtime > booted + 60:
                        return True
                except OSError:
                    pass
    return False


class SystemUpdates:
    def __init__(self, state_dir: str):
        self.state_dir = state_dir

    def _read(self, name: str) -> dict:
        try:
            with open(os.path.join(self.state_dir, name)) as f:
                return json.load(f)
        except Exception:
            return {}

    def _write(self, name: str, data: dict) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        path = os.path.join(self.state_dir, name)
        with open(path + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(path + ".tmp", path)

    def supported(self) -> bool:
        return os.path.exists(f"/etc/systemd/system/{UNITS['install']}")

    # ------------------------------------------------------------ settings
    def auto(self) -> str:
        return "off" if self._read("update-settings.json").get("system") == "off" else "night"

    def set_auto(self, auto: str) -> None:
        s = self._read("update-settings.json")
        s["system"] = "off" if auto == "off" else "night"
        self._write("update-settings.json", s)

    # ------------------------------------------------------------ status
    def status(self) -> dict:
        s = self._read("system-status.json")
        state = s.get("state") or "idle"
        if state in ("installing", "checking") and time.time() - (s.get("updated_at") or 0) > STALE_AFTER:
            state = "idle"
        return {
            "supported": self.supported(),
            "auto": self.auto(),
            "os": s.get("os") or "",
            "kernel": s.get("kernel") or os.uname().release,
            "updates": int(s.get("updates") or 0),
            "packages": s.get("packages") or [],
            "reboot_needed": bool(s.get("reboot_needed")) or reboot_needed_now(),
            "checked_at": s.get("checked_at"),
            "state": state,                       # idle | checking | installing | done | failed
            "in_progress": state in ("installing", "checking"),
            "step": s.get("step"),
            "message": s.get("message") or "",
            "finished_at": s.get("finished_at"),
            "log": (s.get("log") or [])[-12:],
        }

    # ------------------------------------------------------------ asking for work
    def _start(self, what: str) -> None:
        if not self.supported():
            raise SystemError_("System updates aren't set up on this Pi yet: run sudo bash /opt/jplayer/deploy/install.sh")
        res = subprocess.run(["sudo", "-n", SYSTEMCTL, "start", "--no-block", UNITS[what]],
                             capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            raise SystemError_((res.stderr or res.stdout).strip()[-300:] or f"Couldn't start {UNITS[what]}")

    def check(self) -> None:
        self._start("check")

    def install(self, reboot: bool = False) -> None:
        self._write("system-request.json", {"reboot": bool(reboot), "asked_at": time.time()})
        self._start("install")

    def reboot(self) -> None:
        self._start("reboot")

    def auto_due(self, busy: bool, now: datetime | None = None) -> bool:
        """Install now? Automatic updates on, night, nothing plays, updates (or a restart) waiting, not tried tonight."""
        if self.auto() != "night" or busy or not self.supported():
            return False
        now = now or datetime.now()
        if not (NIGHT[0] <= now.hour < NIGHT[1]):
            return False
        st = self.status()
        if st["in_progress"] or not (st["updates"] or st["reboot_needed"]):
            return False
        if time.time() - (self._read("system-auto.json").get("tried_at") or 0) < AUTO_RETRY_AFTER:
            return False
        self._write("system-auto.json", {"tried_at": time.time()})
        return True

    def run_auto(self) -> None:
        """At night: install what waits and restart when that's needed (or just restart, if only that waits)."""
        st = self.status()
        if st["updates"]:
            self.install(reboot=True)
        elif st["reboot_needed"]:
            self.reboot()


def status_for(sysu: SystemUpdates, role: str) -> dict:
    """The status, plus whether this app speaks for the Pi in Home Assistant (one of its apps does: the player
    when there is one), so a Pi with a player and a display doesn't show its system updates twice."""
    from . import system
    return {**sysu.status(), "primary": system.primary_role() == role}


def add_routes(app, sysu: SystemUpdates, prefix: str = "/api/system", role: str = "display") -> None:
    """GET {prefix} and POST check / install / reboot, PUT settings, for the display and the hub
    (the player has the same under /api/v1/system)."""
    import asyncio

    from fastapi import Body
    from fastapi.responses import JSONResponse

    async def call(fn, *args):
        try:
            await asyncio.to_thread(fn, *args)
        except SystemError_ as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        return {"ok": True}

    @app.get(prefix)
    async def system_status():
        return await asyncio.to_thread(status_for, sysu, role)

    @app.post(prefix + "/check")
    async def system_check():
        return await call(sysu.check)

    @app.post(prefix + "/install")
    async def system_install(body: dict = Body(default={})):
        return await call(sysu.install, bool((body or {}).get("reboot")))

    @app.post(prefix + "/reboot")
    async def system_reboot():
        return await call(sysu.reboot)

    @app.put(prefix + "/settings")
    async def system_settings(body: dict = Body(...)):
        auto = (body or {}).get("auto")
        if auto not in ("off", "night"):
            return JSONResponse({"error": "auto: off or night"}, status_code=422)
        await asyncio.to_thread(sysu.set_auto, auto)
        return await asyncio.to_thread(sysu.status)
