"""Where things are on a JPlay Pi, and which roles it has.

  /opt/jplayer                 the repo (root-owned; updated by deploy/update.sh)
  /etc/jplay/roles             the roles this Pi has, e.g. "player display" (written by deploy/install.sh)
  /etc/jplay/config.toml       the player's settings
  /var/lib/jplay               state (owned by the jplay user):
      state.json               the player's favourites and settings
      display/                 the display's settings and history
      hub/                     the hub's database (plays, lyrics)
      update-*.json            the updater's status and settings (one per Pi)

On a PC (development) nothing there is writable, and ~/.local/state/jplay is used instead.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from .updates import Updates

REPO_DIR = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_DIR / "web"
ROLES_FILE = Path(os.environ.get("JPLAY_ROLES_FILE", "/etc/jplay/roles"))
ROLES = ("player", "display", "hub")
UPDATER_NAME = "jplayer"        # jplayer-update.service, jplayer-update-check.service


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("x")
        probe.unlink()
        return True
    except OSError:
        return False


@lru_cache(maxsize=None)
def state_root() -> Path:
    """/var/lib/jplay, or ~/.local/state/jplay when that isn't writable (a PC). JPLAY_STATE overrides both."""
    if os.environ.get("JPLAY_STATE"):
        p = Path(os.environ["JPLAY_STATE"])
        p.mkdir(parents=True, exist_ok=True)
        return p
    for p in (Path("/var/lib/jplay"), Path.home() / ".local" / "state" / "jplay"):
        if _writable(p):
            return p
    raise RuntimeError("No writable state directory")


def state_dir(sub: str) -> Path:
    p = state_root() / sub
    p.mkdir(parents=True, exist_ok=True)
    return p


def roles() -> list[str]:
    """This Pi's roles, in a fixed order. Without the file (a PC): every role."""
    try:
        words = ROLES_FILE.read_text().split()
    except OSError:
        return list(ROLES)
    return [r for r in ROLES if r in words]


def primary_role() -> str:
    """The one app on this Pi that installs updates at night (the player when there is one: it knows when music plays)."""
    r = roles()
    return r[0] if r else "player"


@lru_cache(maxsize=None)
def updates() -> Updates:
    return Updates(UPDATER_NAME, str(REPO_DIR), str(state_root()))


@lru_cache(maxsize=None)
def sysupdates():
    from .sysupdates import SystemUpdates
    return SystemUpdates(str(state_root()))
