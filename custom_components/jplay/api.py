"""Small HTTP client shared by the three kinds of JPlay device."""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp


class JPlayError(Exception):
    """The device couldn't be reached, or refused the request (the message says why)."""


class JPlayAuthError(JPlayError):
    """The token was refused."""


class ApiClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, token: str | None = None) -> None:
        self.session = session
        self.base = base_url.rstrip("/")
        self.headers = {"Accept": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return self.base + path

    async def request(self, method: str, path: str, json: Any = None, timeout: float = 10) -> Any:
        try:
            async with self.session.request(
                method, self.url(path), json=json, headers=self.headers, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status in (401, 403):
                    raise JPlayAuthError(await _message(resp))
                if resp.status >= 400:
                    raise JPlayError(await _message(resp))
                if resp.content_type == "application/json":
                    return await resp.json()
                return await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise JPlayError(f"Can't reach {self.base}: {err or type(err).__name__}") from err

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, json: Any = None, **kw: Any) -> Any:
        return await self.request("POST", path, json=json if json is not None else {}, **kw)

    async def put(self, path: str, json: Any, **kw: Any) -> Any:
        return await self.request("PUT", path, json=json, **kw)

    async def get_bytes(self, path: str) -> tuple[bytes, str]:
        try:
            async with self.session.get(self.url(path), headers=self.headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status >= 400:
                    raise JPlayError(await _message(resp))
                return await resp.read(), resp.content_type
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise JPlayError(f"Can't reach {self.base}: {err or type(err).__name__}") from err


async def _message(resp: aiohttp.ClientResponse) -> str:
    try:
        data = await resp.json(content_type=None)
        return str(data.get("detail") or data.get("error") or resp.reason)
    except Exception:
        return f"{resp.status} {resp.reason}"
