"""End to end: the Plex source against the fake Plex server, playing through mpv (silent)."""
import asyncio
import shutil
import socket
import struct
import threading
import time
import wave
from pathlib import Path

import httpx
import pytest
import uvicorn

from jplay.player.config import load_config
from jplay.player.core.context import AppContext
from jplay.player.core.events import EventHub
from jplay.player.core.manager import SourceManager
from jplay.player.core.models import PlayState
from jplay.player.core.mpv import MpvEngine
from jplay.player.core.store import StateStore
from jplay.player.core.volume import Volume
from jplay.player.sources.base import SourceError
from jplay.player.sources.plex import PlexSource
from tests.player.fake_plex import create_app

pytestmark = pytest.mark.skipif(shutil.which("mpv") is None, reason="mpv not installed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def fake_plex(tmp_path_factory):
    audio = tmp_path_factory.mktemp("audio")
    with wave.open(str(audio / "tone.wav"), "wb") as w:
        w.setnchannels(2), w.setsampwidth(2), w.setframerate(44100)
        w.writeframes(struct.pack("<hh", 0, 0) * 44100 * 5)  # 5 s: past the 50% scrobble point of the 8 s fake duration
    app = create_app(audio)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", app
    server.should_exit = True
    thread.join(5)


@pytest.fixture
async def plex(fake_plex, tmp_path: Path):
    url, app = fake_plex
    cfg = load_config(None)
    cfg["plex"].update(url=url, token="t")
    ctx = AppContext(
        config=cfg, store=StateStore(tmp_path / "s.json"), hub=EventHub(),
        engine=MpvEngine("mpv", "null", [], tmp_path), volume=Volume("", ""),
        http=httpx.AsyncClient(), state_dir=tmp_path, runtime_dir=tmp_path,
    )
    source = PlexSource(ctx)
    ctx.manager = SourceManager(ctx, [source])
    await ctx.manager.start()
    yield source, app
    await ctx.manager.shutdown()
    await ctx.http.aclose()


async def wait_for(cond, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


async def test_browse(plex):
    source, _ = plex
    assert (await source.libraries()) == [{"key": "1", "title": "Music"}]
    page = await source.browse_section("1", "album", 0, 2, "title")
    assert page["total"] == 4 and [i["title"] for i in page["items"]] == ["Kind of Blue", "Pastel Blues"]
    kids = await source.children("100")
    assert [a["title"] for a in kids["items"]] == ["Kind of Blue", "Sketches of Spain"]
    found = await source.search("blue")
    assert {a["title"] for a in found["album"]} == {"Kind of Blue", "Pastel Blues"}
    assert "token" not in kids["items"][0]["thumb"]  # the token never reaches the browser


async def test_play_album_next_and_scrobble(plex):
    source, app = plex
    await source.play_item("101", index=1)
    await wait_for(lambda: source.np.state == PlayState.PLAYING)
    assert source.np.title == "Freddie Freeloader" and source.np.queue_index == 1
    await source.next()
    await wait_for(lambda: source.np.title == "Blue in Green")
    # let the last track play out: it is scrobbled and the source stops
    await wait_for(lambda: source.np.state == PlayState.STOPPED, timeout=12)
    await wait_for(lambda: "1013" in app.state.scrobbles)


async def test_connecting_from_the_web_page(fake_plex, tmp_path: Path):
    """Plex → Connect: a wrong address is refused and nothing is saved; a working one is saved and kept."""
    url, _ = fake_plex
    cfg = load_config(None)
    store = StateStore(tmp_path / "s.json")

    async def make():
        ctx = AppContext(config=cfg, store=store, hub=EventHub(), engine=MpvEngine("mpv", "null", [], tmp_path),
                         volume=Volume("", ""), http=httpx.AsyncClient(), state_dir=tmp_path, runtime_dir=tmp_path)
        src = PlexSource(ctx)
        ctx.manager = SourceManager(ctx, [src])
        await ctx.manager.start()
        return ctx, src

    ctx, src = await make()
    assert src.connection()["token_set"] is False and not src.available
    with pytest.raises(SourceError) as err:
        await src.configure("http://127.0.0.1:9", "tok")
    assert err.value.status == 400 and "Can't reach" in str(err.value)
    assert store.get("plex.connection") is None
    host = url.split("://")[1]                       # "127.0.0.1:port", typed without http://
    c = await src.configure(host, "tok")
    assert c["connected"] and c["url"] == url and c["from"] == "web" and "token" not in c
    assert src.available and (await src.libraries())
    await ctx.manager.shutdown()
    await ctx.http.aclose()

    ctx, src = await make()                          # after a restart: still connected
    assert src.connection()["url"] == url and src.available
    c = await src.configure(url, None)               # same token, kept
    assert c["connected"]
    c = await src.configure("", None)                # disconnect
    assert not c["token_set"] and not src.available and store.get("plex.connection") is None
    await ctx.manager.shutdown()
    await ctx.http.aclose()


async def test_signing_in_with_plex(fake_plex, tmp_path: Path, monkeypatch):
    """Sign in with Plex (plex.tv faked): approve, pick the server, and the player connects to its local address."""
    import plexapi.myplex as myplex

    url, _ = fake_plex
    port = int(url.rsplit(":", 1)[1])
    state = {"approved": False}

    class Pin:
        def __init__(self, oauth=False, headers=None):
            assert oauth and headers["X-Plex-Product"] == "JPlay"
            self.token = None

        def oauthUrl(self):
            return "https://app.plex.tv/auth#?code=abc"

        def checkLogin(self):
            if state["approved"]:
                self.token = "account-token"
            return state["approved"]

    class Conn:
        def __init__(self, address, port, local, relay=False):
            self.address, self.port, self.local, self.relay = address, port, local, relay
            self.uri = f"https://{address.replace('.', '-')}.abc.plex.direct:{port}"

    class Res:
        name, clientIdentifier, owned, provides, accessToken = "Music NAS", "srv1", True, "server", "server-token"
        connections = [Conn("203.0.113.5", 32400, False), Conn("127.0.0.1", port, True), Conn("10.0.0.9", 1, False, True)]

    class Account:
        def __init__(self, token):
            assert token == "account-token"

        def resources(self):
            return [Res()]

    monkeypatch.setattr(myplex, "MyPlexPinLogin", Pin)
    monkeypatch.setattr(myplex, "MyPlexAccount", Account)
    ctx = AppContext(config=load_config(None), store=StateStore(tmp_path / "s.json"), hub=EventHub(),
                     engine=MpvEngine("mpv", "null", [], tmp_path), volume=Volume("", ""), http=httpx.AsyncClient(),
                     state_dir=tmp_path, runtime_dir=tmp_path)
    src = PlexSource(ctx)
    ctx.manager = SourceManager(ctx, [src])
    await ctx.manager.start()
    assert (await src.sign_in_start())["url"].startswith("https://app.plex.tv/auth")
    assert await src.sign_in_check() == {"done": False}
    state["approved"] = True
    found = await src.sign_in_check()
    assert found["done"] and found["servers"][0]["name"] == "Music NAS" and "token" not in found["servers"][0]
    assert found["servers"][0]["urls"][0] == url                      # the local address first, no relay
    c = await src.sign_in_choose("srv1")
    assert c["connected"] and c["url"] == url
    assert ctx.store.get("plex.connection") == {"url": url, "token": "server-token"}
    await ctx.manager.shutdown()
    await ctx.http.aclose()
