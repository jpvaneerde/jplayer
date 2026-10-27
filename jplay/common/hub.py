"""Finding the hub: the one unit that keeps the house's plays and lyrics.

In order: the address set in a unit's settings, this Pi itself when it is the hub, or whichever hub announces
itself on the network (_jplay-hub._tcp). The answer is kept for a minute; forget() makes the next call look again
(after the hub didn't answer).
"""
from __future__ import annotations

import time

from . import discover, system
from .announce import HUB_TYPE

HUB_PORT = 8090
_found: dict = {"url": None, "at": 0.0, "key": None}


def base_url(address: str) -> str:
    """'jplayhub', 'jplayhub:8090', 'http://192.168.1.5:8090/' -> 'http://jplayhub:8090'."""
    a = address.strip().rstrip("/")
    if not a:
        return ""
    if not a.startswith(("http://", "https://")):
        a = "http://" + a
    host_part = a.split("://", 1)[1]
    if ":" not in host_part.split("/")[0]:
        a += f":{HUB_PORT}"
    return a


def find(configured: str = "") -> str | None:
    """The hub's address (http://host:8090), or None when there's no hub."""
    if configured.strip():
        return base_url(configured)
    if _found["url"] and time.time() - _found["at"] < 60:
        return _found["url"]
    url = None
    if "hub" in system.roles():
        url = f"http://127.0.0.1:{HUB_PORT}"
    else:
        hubs = discover.browse(HUB_TYPE)
        if hubs:
            h = sorted(hubs, key=lambda x: x["name"])[0]       # more than one hub: always the same one
            addr = h["address"]
            url = f"http://{'[' + addr + ']' if ':' in addr else addr}:{h['port'] or HUB_PORT}"
    _found.update(url=url, at=time.time())
    return url


def known(configured: str = "") -> str | None:
    """Like find(), but never waits for the network: what was found last time (find() runs in the background)."""
    if configured.strip():
        return base_url(configured)
    if "hub" in system.roles():
        return f"http://127.0.0.1:{HUB_PORT}"
    return _found["url"]


def forget() -> None:
    _found.update(url=None, at=0.0)
