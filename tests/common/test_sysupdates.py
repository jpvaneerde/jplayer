"""Raspberry Pi OS updates: what the apps show and when they install by themselves; and the root script."""
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from jplay.common import system, sysupdates
from jplay.player.app import create_app
from jplay.player.config import load_config

REPO = Path(__file__).resolve().parents[2]


def status_file(state, **kw):
    (state / "system-status.json").write_text(json.dumps({"state": "idle", "updates": 0, **kw}))


def test_status_and_night_installs(tmp_path, monkeypatch):
    su = sysupdates.SystemUpdates(str(tmp_path))
    monkeypatch.setattr(sysupdates, "reboot_needed_now", lambda: False)
    status_file(tmp_path, updates=12, packages=["chromium", "linux-image-rpi-v8"], os="Debian GNU/Linux 13 (trixie)",
                checked_at=1.0)
    st = su.status()
    assert st["updates"] == 12 and st["auto"] == "night" and not st["supported"] and not st["reboot_needed"]
    monkeypatch.setattr(su, "supported", lambda: True)
    started = []
    monkeypatch.setattr(su, "_start", lambda what: started.append(what))
    night, day = datetime(2026, 10, 28, 3, 30), datetime(2026, 10, 28, 14, 0)
    assert not su.auto_due(busy=False, now=day)
    assert not su.auto_due(busy=True, now=night)               # music plays: not now
    assert su.auto_due(busy=False, now=night)
    su.run_auto()
    assert started == ["install"] and json.loads((tmp_path / "system-request.json").read_text())["reboot"] is True
    assert not su.auto_due(busy=False, now=night)               # once a night
    su.set_auto("off")
    assert su.status()["auto"] == "off"
    # only a restart waiting: restart
    (tmp_path / "system-auto.json").unlink()
    su.set_auto("night")
    status_file(tmp_path, updates=0, reboot_needed=True)
    assert su.auto_due(busy=False, now=night)
    su.run_auto()
    assert started[-1] == "reboot"


def test_player_api(tmp_path, monkeypatch):
    cfg = load_config(None)
    cfg["paths"].update(state_dir=str(tmp_path / "state"), runtime_dir=str(tmp_path / "run"))
    cfg["audio"]["device"] = "null"
    for s in ("radio", "plex", "cd", "spotify", "bluetooth"):
        cfg[s]["enabled"] = False
    with TestClient(create_app(cfg)) as c:
        st = c.get("/api/v1/system").json()
        assert st["supported"] is False and st["auto"] == "night"
        r = c.post("/api/v1/system/install", json={"reboot": True})
        assert r.status_code == 409 and "install.sh" in r.json()["detail"]
        assert c.put("/api/v1/system/settings", json={"auto": "off"}).json()["auto"] == "off"
        assert c.put("/api/v1/system/settings", json={"auto": "always"}).status_code == 422


def _stubs(tmp_path, upgradable):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    calls = tmp_path / "calls"
    def stub(name, body):
        (bin_ / name).write_text(f"#!/bin/bash\necho \"{name} $*\" >> {calls}\n{body}\n")
        os.chmod(bin_ / name, 0o755)
    sim = "\\n".join(f"Inst {p} [1] (2 Debian:13)" for p in upgradable)
    stub("apt-get", f'if [[ "$*" == *" -s "* || "$1" == "-s" ]]; then printf "{sim}\\n"; fi')
    stub("dpkg", "")
    stub("systemctl", "")
    return bin_, calls


def run_script(tmp_path, mode, upgradable, request=None):
    bin_, calls = _stubs(tmp_path, upgradable)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if request is not None:
        (state / "system-request.json").write_text(json.dumps(request))
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "STATE_DIR": str(state), "APP_USER": os.environ.get("USER", "root")}
    subprocess.run(["bash", str(REPO / "deploy/system-update.sh"), mode], env=env, check=True, capture_output=True,
                   timeout=60)
    return json.loads((state / "system-status.json").read_text()), calls.read_text()


def test_script_check_and_install(tmp_path):
    st, calls = run_script(tmp_path, "check", ["chromium", "libc6"])
    assert st["state"] == "idle" and st["updates"] == 2 and st["packages"] == ["chromium", "libc6"] and st["checked_at"]
    assert "apt-get update" in calls
    (tmp_path / "x").mkdir()
    st, calls = run_script(tmp_path / "x", "install", ["chromium"], {"reboot": True})
    assert "full-upgrade" in calls and st["state"] == "done"
    assert "systemctl reboot" not in calls        # nothing on this machine says a restart is needed
