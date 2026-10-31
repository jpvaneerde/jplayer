"""Your collection as a Shazam (jplayazam, the fingerprint service in Kubernetes), seen from the hub's Settings:
how far its index is, and buttons to look for new music now or to start the index over.

The hub calls the service itself, so its token stays on the hub (STATE/hub/collection.token, readable by JPlay
alone) and never reaches a browser.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from . import db


class CollectionError(Exception):
    pass


def _token_file() -> Path:
    from ..common import system
    p = system.state_dir("hub")
    p.mkdir(parents=True, exist_ok=True)
    return p / "collection.token"


def _token() -> str:
    try:
        return _token_file().read_text().strip()
    except OSError:
        return ""


def settings() -> dict:
    s = db.setting("collection", {}) or {}
    return {"url": s.get("url") or "", "token_set": bool(_token())}


def save(url: str, token: str | None) -> dict:
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "http://" + url
    db.set_setting("collection", {"url": url})
    if token is not None and token.strip():
        fd = os.open(_token_file(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token.strip())
    return settings()


async def view(client: httpx.AsyncClient) -> dict:
    """The settings and the service's own status (or why it can't be reached)."""
    s = settings()
    out: dict[str, Any] = {**s, "status": None, "error": ""}
    if not s["url"]:
        return out
    try:
        r = await client.get(s["url"] + "/api/status", timeout=5)
        r.raise_for_status()
        out["status"] = r.json()
    except Exception as e:
        out["error"] = f"Can't reach it: {type(e).__name__}: {e}"[:200]
    return out


async def action(client: httpx.AsyncClient, what: str) -> dict:
    """"index": look for new, changed and removed music now; "rebuild": start the index over (from the cache)."""
    if what not in ("index", "rebuild"):
        raise CollectionError("Unknown action")
    s = settings()
    if not s["url"]:
        raise CollectionError("Fill in the address first")
    token = _token()
    try:
        r = await client.post(s["url"] + f"/api/{what}", timeout=30,
                              headers={"Authorization": f"Bearer {token}"} if token else {})
    except Exception as e:
        raise CollectionError(f"Can't reach it: {type(e).__name__}: {e}"[:200])
    if r.status_code == 401:
        raise CollectionError("The token is wrong")
    if r.status_code >= 400:
        try:
            msg = r.json().get("detail")
        except Exception:
            msg = r.text[:200]
        raise CollectionError(msg or f"HTTP {r.status_code}")
    return r.json()
