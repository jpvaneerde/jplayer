"""The source manager: one active source, clean hand-over of the DAC."""
from pathlib import Path

import httpx
import pytest

from jplay.player.config import load_config
from jplay.player.core.context import AppContext
from jplay.player.core.events import EventHub
from jplay.player.core.manager import SourceManager
from jplay.player.core.mpv import MpvEngine
from jplay.player.core.store import StateStore
from jplay.player.core.volume import Volume
from jplay.player.sources.base import Source, SourceError


class FakeSource(Source):
    def __init__(self, ctx, sid, log):
        self.id = sid
        self.name = sid.title()
        super().__init__(ctx)
        self.log = log

    async def start(self):
        self.set_available(True)

    async def activate(self):
        self.log.append(f"activate {self.id}")

    async def deactivate(self):
        self.log.append(f"deactivate {self.id}")

    async def play(self):
        self.log.append(f"play {self.id}")


@pytest.fixture
async def setup(tmp_path: Path):
    cfg = load_config(None)
    ctx = AppContext(
        config=cfg, store=StateStore(tmp_path / "state.json"), hub=EventHub(),
        engine=MpvEngine("mpv", "null", [], tmp_path), volume=Volume("", ""),
        http=httpx.AsyncClient(), state_dir=tmp_path, runtime_dir=tmp_path,
    )
    log: list[str] = []
    sources = [FakeSource(ctx, "a", log), FakeSource(ctx, "b", log)]
    ctx.manager = SourceManager(ctx, sources)
    await ctx.manager.start()
    yield ctx, log
    await ctx.manager.shutdown()
    await ctx.http.aclose()


async def test_switch_deactivates_old_before_activating_new(setup):
    ctx, log = setup
    await ctx.manager.activate("a")
    await ctx.manager.activate("b")
    assert log == ["activate a", "deactivate a", "activate b"]
    assert ctx.manager.active_id == "b"
    assert ctx.store.get("last_source") == "b"


async def test_activating_active_source_is_a_no_op(setup):
    ctx, log = setup
    await ctx.manager.activate("a")
    await ctx.manager.activate("a")
    assert log == ["activate a"]


async def test_transport_goes_to_active_source(setup):
    ctx, log = setup
    with pytest.raises(SourceError):
        await ctx.manager.transport("play")
    await ctx.manager.activate("b")
    await ctx.manager.transport("play")
    assert log[-1] == "play b"
    with pytest.raises(SourceError):
        await ctx.manager.transport("next")  # FakeSource has no next


async def test_unavailable_source_cannot_be_activated(setup):
    ctx, _ = setup
    ctx.manager.get("a").set_available(False, "unplugged")
    with pytest.raises(SourceError) as err:
        await ctx.manager.activate("a")
    assert err.value.status == 409


async def test_snapshot_lists_sources(setup):
    ctx, _ = setup
    await ctx.manager.activate("a")
    snap = ctx.manager.snapshot()
    assert snap["active"] == "a"
    assert [s["id"] for s in snap["sources"]] == ["a", "b"]
    assert snap["sources"][0]["active"] is True


async def test_spotify_events_claim_the_dac(setup):
    from jplay.player.sources.spotify import SpotifySource

    ctx, log = setup
    spotify = SpotifySource(ctx)
    ctx.manager.sources["spotify"] = spotify
    spotify.set_available(True)
    await ctx.manager.activate("a")
    await spotify.handle_event({"PLAYER_EVENT": "session_connected", "CLIENT_NAME": "JP's phone"})
    assert ctx.manager.active_id == "spotify" and log[-1] == "deactivate a"
    await spotify.handle_event({
        "PLAYER_EVENT": "track_changed", "NAME": "So What", "ARTISTS": "Miles Davis\nJohn Coltrane",
        "ALBUM": "Kind of Blue", "COVERS": "https://i.scdn.co/image/a\nhttps://i.scdn.co/image/b",
        "DURATION_MS": "545000", "ITEM_TYPE": "Track",
    })
    await spotify.handle_event({"PLAYER_EVENT": "playing", "POSITION_MS": "12000"})
    np = spotify.np
    assert (np.title, np.artist, np.duration, np.position) == ("So What", "Miles Davis, John Coltrane", 545.0, 12.0)
    assert np.state.value == "playing" and np.art_url.endswith("/a")
    assert spotify.details()["client"] == "JP's phone"


async def test_release_leaves_the_player_idle(setup):
    # A phone that disconnects (Spotify, Bluetooth): the source is no longer active, without being stopped again.
    ctx, log = setup
    await ctx.manager.activate("a")
    await ctx.manager.release("b")          # not the active one: nothing happens
    assert ctx.manager.active_id == "a"
    await ctx.manager.release("a")
    assert ctx.manager.active_id is None
    assert ctx.manager.snapshot()["active"] is None
    assert log == ["activate a"]
