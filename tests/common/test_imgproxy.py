"""Pictures from http:// addresses passed on for the secure pages: only pictures, not too large, kept a while."""
import asyncio

import httpx
import pytest
from fastapi import HTTPException

from jplay.common import imgproxy


def client(seen, kind="image/png", body=b"\x89PNG..."):
    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, headers={"content-type": kind}, content=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


def test_a_picture_is_passed_on_and_kept():
    imgproxy._cache.clear()
    seen = []

    async def go():
        async with client(seen) as c:
            a = await imgproxy.fetch("http://radio.example/logo.png", c)
            b = await imgproxy.fetch("http://radio.example/logo.png", c)
        return a, b
    a, b = run(go())
    assert a == b == (b"\x89PNG...", "image/png") and len(seen) == 1


def test_only_pictures_and_not_too_large(monkeypatch):
    imgproxy._cache.clear()

    async def go(kind, body):
        async with client([], kind, body) as c:
            return await imgproxy.fetch("http://radio.example/x", c)
    with pytest.raises(HTTPException) as e:
        run(go("text/html", b"<html>"))
    assert e.value.status_code == 404
    monkeypatch.setattr(imgproxy, "MAX_BYTES", 10)
    with pytest.raises(HTTPException) as e:
        run(go("image/jpeg", b"x" * 100))
    assert e.value.status_code == 413
    with pytest.raises(HTTPException):
        run(imgproxy.fetch("file:///etc/passwd"))
