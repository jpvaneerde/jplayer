"""Spotify Connect through librespot.

librespot runs as a child process and plays straight to the DAC. Its --onevent hook tells
us what it is doing; when someone picks this player in the Spotify app, Spotify claims the
DAC (the current source stops). Switching to another source restarts librespot, which
ends the Spotify session and frees the DAC.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from ..core.models import PlayState
from .base import Source

log = logging.getLogger(__name__)


def _ms(value: str | None) -> float | None:
    try:
        return int(value) / 1000 if value not in (None, "") else None
    except ValueError:
        return None


class SpotifySource(Source):
    id = "spotify"
    name = "Spotify"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.binary = self.cfg.get("librespot_binary", "librespot")
        self.device_name = self.cfg.get("device_name") or ctx.config["server"]["name"]
        self._proc: asyncio.subprocess.Process | None = None
        self._watch: asyncio.Task[None] | None = None
        self._ticker: asyncio.Task[None] | None = None
        self._restarting = False
        self._closing = False
        self.connected = False
        self.client = ""
        self._pos_base: float | None = None
        self._pos_at = 0.0

    # ------------------------------------------------------------ process management
    async def start(self) -> None:
        if shutil.which(self.binary) is None:
            self.set_available(False, f"'{self.binary}' is not installed")
            return
        self.set_available(True)
        await self._spawn()
        self._ticker = asyncio.create_task(self._tick(), name="spotify-position")

    def _args(self) -> list[str]:
        device = self.ctx.output.lossy_device if self.ctx.output else "null"
        args = [
            self.binary,
            "--name", self.device_name,
            "--device-type", "avr",
            "--bitrate", str(self.cfg.get("bitrate", 320)),
            "--initial-volume", "100",
            "--volume-ctrl", "fixed",
            "--disable-audio-cache",
            "--cache", str(self.ctx.state_dir / "librespot"),
            "--onevent", f"{sys.executable} -m jplay.player.hooks.librespot_event",
        ]
        if device == "null":
            args += ["--backend", "pipe", "--device", "/dev/null"]
        else:
            args += ["--backend", "alsa", "--device", device]
        return args + list(self.cfg.get("extra_args", []))

    async def _spawn(self) -> None:
        port = self.ctx.config["server"]["port"]
        env = dict(os.environ)
        env["JPLAY_HOOK_URL"] = f"http://127.0.0.1:{port}/api/v1/spotify/event"
        env["JPLAY_HOOK_TOKEN"] = self.ctx.hook_token
        # Make `python -m jplay.player.hooks...` work from a source checkout too.
        pkg_root = str(Path(__file__).resolve().parents[3])
        env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
        (self.ctx.state_dir / "librespot").mkdir(parents=True, exist_ok=True)
        log.info("Starting librespot as '%s'", self.device_name)
        self._proc = await asyncio.create_subprocess_exec(
            *self._args(), env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        self._watch = asyncio.create_task(self._watch_proc(self._proc), name="librespot-watch")

    async def _watch_proc(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        async for line in proc.stderr:
            text = line.decode(errors="replace").rstrip()
            if " ERROR " in text or "error" in text.lower():
                log.warning("librespot: %s", text)
            else:
                log.debug("librespot: %s", text)
        code = await proc.wait()
        if self._closing or self._restarting:
            return
        log.warning("librespot exited (%s); restarting in 5 s", code)
        self._session_ended()
        await asyncio.sleep(5)
        if not self._closing:
            await self._spawn()

    async def _kill(self) -> None:
        proc = self._proc
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        if self._watch:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._watch, 2)

    async def restart(self) -> None:
        """Drop the Spotify session and free the DAC; the player stays visible in the app."""
        self._restarting = True
        try:
            await self._kill()
            self._session_ended()
            await self._spawn()
        finally:
            self._restarting = False

    async def shutdown(self) -> None:
        self._closing = True
        if self._ticker:
            self._ticker.cancel()
        await self._kill()

    # ------------------------------------------------------------ source interface
    def details(self) -> dict[str, Any]:
        return {
            "device_name": self.device_name,
            "connected": self.connected,
            "client": self.client,
            "hint": f"Open Spotify on any device and choose '{self.device_name}' as the speaker.",
        }

    async def output_changed(self) -> None:
        # librespot opens the device it was started with; restart it on the new DAC.
        if self.available and not self._closing:
            await self.restart()

    async def deactivate(self) -> None:
        if self.connected or self.np.state != PlayState.STOPPED:
            await self.restart()

    # ------------------------------------------------------------ events from the hook
    async def handle_event(self, ev: dict[str, str]) -> None:
        kind = ev.get("PLAYER_EVENT", "")
        log.debug("Spotify event %s", kind)
        if kind in ("session_connected", "start", "change"):
            self.connected = True
            self.client = ev.get("CLIENT_NAME") or ev.get("USER_NAME") or self.client
            await self._claim()
        elif kind == "session_client_changed":
            self.client = ev.get("CLIENT_NAME") or self.client
            self.ctx.manager.notify(True)
        elif kind in ("session_disconnected", "stop"):
            self._session_ended()
            # The phone let go: the player is idle again (librespot keeps running for the next one).
            if self.active:
                await self.ctx.manager.release(self.id)
        elif kind == "track_changed":
            covers = [c for c in (ev.get("COVERS") or "").splitlines() if c.strip()]
            artists = [a for a in (ev.get("ARTISTS") or "").splitlines() if a.strip()]
            is_episode = ev.get("ITEM_TYPE", "").lower() == "episode"
            self._pos_base, self._pos_at = 0.0, time.monotonic()
            self.update(
                title=ev.get("NAME", ""),
                artist=ev.get("SHOW_NAME", "") if is_episode else ", ".join(artists),
                album=ev.get("ALBUM", ""),
                art_url=covers[0] if covers else "",
                duration=_ms(ev.get("DURATION_MS")),
                position=0.0,
                track_number=int(ev["NUMBER"]) if ev.get("NUMBER", "").isdigit() else None,
                extra={"uri": ev.get("URI", ""), "client": self.client},
            )
        elif kind in ("playing", "started"):
            await self._claim()
            self._set_position(ev.get("POSITION_MS"))
            self.update(state=PlayState.PLAYING)
        elif kind == "paused":
            self._set_position(ev.get("POSITION_MS"))
            self._pos_base = self.np.position
            self.update(state=PlayState.PAUSED)
        elif kind == "stopped":
            self.update(state=PlayState.STOPPED)
        elif kind in ("seeked", "position_correction"):
            self._set_position(ev.get("POSITION_MS"))
        elif kind == "loading":
            self.update(state=PlayState.LOADING)

    async def _claim(self) -> None:
        if not self.active:
            # Hand the DAC over: whatever plays now stops before Spotify starts its audio.
            await self.ctx.manager.activate(self.id)
        self.ctx.manager.notify(True)

    def _session_ended(self) -> None:
        self.connected = False
        self.client = ""
        self._pos_base = None
        self.reset_now_playing()

    def _set_position(self, ms: str | None) -> None:
        pos = _ms(ms)
        if pos is not None:
            self._pos_base, self._pos_at = pos, time.monotonic()
            self.update(significant=False, position=round(pos, 1))

    async def _tick(self) -> None:
        """librespot only reports position on events; count along in between."""
        while True:
            await asyncio.sleep(1)
            if self.np.state == PlayState.PLAYING and self._pos_base is not None:
                pos = self._pos_base + (time.monotonic() - self._pos_at)
                if self.np.duration:
                    pos = min(pos, self.np.duration)
                self.update(significant=False, position=round(pos, 1))
