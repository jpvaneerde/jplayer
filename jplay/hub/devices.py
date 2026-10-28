"""Every JPlay device in the house, for the hub's Home page (and its HTTPS addresses, see https.py).

Players are found on the network (_jplay._tcp), displays too (_jplay-display._tcp) and through their reports to
the hub (which also gives their room). The hub asks the players what they play and passes on controls, so the
Home page only ever talks to the hub (one address, one certificate).
"""
from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
import unicodedata
from typing import Any
from urllib.parse import quote

import httpx

from ..common import discover, system
from ..common.announce import DISPLAY_TYPE, SERVICE_TYPE
from . import spins

log = logging.getLogger(__name__)
PLAYER_PORT, DISPLAY_PORT, HUB_PORT = 8080, 5000, 8090
_known: dict[str, dict] = {}        # device id -> device, kept while it is away for a while
FORGET_AFTER = 7 * 86400


_LETTERS = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "ß": "ss", "đ": "d", "ł": "l", "Ł": "L", "þ": "th"})


def slug(name: str) -> str:
    """'Lounge' -> 'lounge', 'Lounge display' -> 'lounge-display', 'Café Øst' -> 'cafe-ost' (a DNS label)."""
    s = (name or "").translate(_LETTERS)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:40] or "device"


def own_address() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))          # no packet is sent; picks the LAN interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _browse() -> list[dict]:
    found = []
    for svc in discover.browse(SERVICE_TYPE):
        found.append({"id": "player-" + (svc["txt"].get("id") or slug(svc["name"])), "kind": "player",
                      "name": svc["name"], "address": svc["address"], "port": svc["port"] or PLAYER_PORT,
                      "room": ""})
    for svc in discover.browse(DISPLAY_TYPE):
        found.append({"id": "display-" + (svc["txt"].get("id") or slug(svc["name"])), "kind": "display",
                      "name": svc["name"], "address": svc["address"], "port": svc["port"] or DISPLAY_PORT,
                      "room": svc["txt"].get("room") or ""})
    return found


def devices() -> list[dict]:
    """Players, displays and this hub, each with a stable id and a subdomain name (lounge, lounge-display, ...)."""
    now = time.time()
    seen = {}
    for d in _browse():
        seen[d["id"]] = {**d, "seen": now}
    # Displays that report to the hub: their room, and their address when mDNS doesn't find them
    for u in spins.units():
        did = f"display-{u['id']}"
        if did in seen:
            seen[did]["room"] = seen[did]["room"] or u["room"]
        elif u.get("address") and u["online"]:
            seen[did] = {"id": did, "kind": "display", "name": u["name"], "address": u["address"],
                         "port": DISPLAY_PORT, "room": u["room"], "seen": now}
    for did, d in seen.items():
        _known[did] = d
    for did in [k for k, d in _known.items() if now - d["seen"] > FORGET_AFTER]:
        _known.pop(did, None)
    out = [{**d, "online": now - d["seen"] < 600} for d in _known.values()]
    # A player's room: the room of the display that names it as its room's player, else its own name
    out.append({"id": "hub", "kind": "hub", "name": "JPlay hub", "address": own_address(), "port": HUB_PORT,
                "room": "", "online": True})
    names: set[str] = set()
    for d in sorted(out, key=lambda x: ({"hub": 0, "player": 1, "display": 2}[x["kind"]], x["name"].lower())):
        base = "home" if d["kind"] == "hub" else slug(d["name"])
        if d["kind"] == "display" and not base.endswith("display"):
            base += "-display"
        name, n = base, 2
        while name in names:
            name, n = f"{base}-{n}", n + 1
        names.add(name)
        d["subdomain"] = name
    return sorted(out, key=lambda x: ({"hub": 0, "player": 1, "display": 2}[x["kind"]], x["name"].lower()))


def find(device_id: str) -> dict | None:
    return next((d for d in devices() if d["id"] == device_id), None)


async def overview(client: httpx.AsyncClient) -> list[dict]:
    """Every device with what it does right now (players: what plays; displays: screen on/off)."""
    devs = await asyncio.to_thread(devices)

    async def state(d: dict) -> dict:
        base = f"http://{d['address']}:{d['port']}"
        try:
            if d["kind"] == "player":
                r = await client.get(base + "/api/v1/status", timeout=2.5)
                st = r.json()
                np = st.get("now_playing") or {}
                art = np.get("art_url") or ""
                if art.startswith("/"):
                    art = f"/api/devices/{d['id']}/art?path={quote(art, safe='')}"
                vol = await client.get(base + "/api/v1/volume", timeout=2.5)
                v = vol.json() if vol.status_code == 200 else {}
                return {**d, "online": True, "state": {
                    "source": st.get("active"), "playing": np.get("state") in ("playing", "loading"),
                    "status": np.get("state"), "title": np.get("title"), "artist": np.get("artist"),
                    "album": np.get("album"), "art": art, "station": (np.get("extra") or {}).get("station"),
                    "volume": v.get("volume"), "volume_available": bool(v.get("available"))}}
            if d["kind"] == "display":
                r = await client.get(base + "/api/screen", timeout=2.5)
                return {**d, "online": True, "state": {"screen_on": bool(r.json().get("on"))}}
            return {**d, "state": {}}
        except Exception:
            return {**d, "online": False, "state": None}

    return list(await asyncio.gather(*(state(d) for d in devs)))


PLAYER_ACTIONS = {"toggle": "/api/v1/player/toggle", "next": "/api/v1/player/next",
                  "previous": "/api/v1/player/previous", "stop": "/api/v1/player/stop", "standby": "/api/v1/standby"}


async def control(client: httpx.AsyncClient, device_id: str, action: str, value: Any = None) -> dict:
    d = await asyncio.to_thread(find, device_id)
    if not d:
        raise LookupError("Unknown device")
    base = f"http://{d['address']}:{d['port']}"
    if d["kind"] == "player" and action in PLAYER_ACTIONS:
        r = await client.post(base + PLAYER_ACTIONS[action], json={}, timeout=10)
    elif d["kind"] == "player" and action == "volume":
        r = await client.put(base + "/api/v1/volume", json={"volume": max(0, min(100, int(value)))}, timeout=10)
    elif d["kind"] == "display" and action == "screen":
        r = await client.post(base + "/api/screen", json={"on": bool(value)}, timeout=10)
    else:
        raise ValueError(f"{action} isn't something a {d['kind']} does")
    if r.status_code >= 400:
        try:
            msg = r.json().get("detail") or r.json().get("error")
        except Exception:
            msg = r.text[:200]
        raise RuntimeError(msg or f"HTTP {r.status_code}")
    return {"ok": True}


async def art(client: httpx.AsyncClient, device_id: str, path: str) -> tuple[bytes, str]:
    """Cover art that lives on a player (Plex art, CD covers): fetched through the hub."""
    d = await asyncio.to_thread(find, device_id)
    if not d or d["kind"] != "player" or not path.startswith("/api/v1/"):
        raise LookupError("No such art")
    r = await client.get(f"http://{d['address']}:{d['port']}{path}", timeout=10)
    r.raise_for_status()
    return r.content, r.headers.get("content-type", "image/jpeg")


def open_url(d: dict, https_domain: str | None) -> str:
    """Where a device's own page is: its HTTPS address when that is set up, else its home address."""
    if https_domain:
        return f"https://{d['subdomain']}.{https_domain}/" if d["kind"] != "hub" else f"https://{https_domain}/"
    return f"http://{d['address']}:{d['port']}/"


def roles_here() -> list[str]:
    return system.roles()
