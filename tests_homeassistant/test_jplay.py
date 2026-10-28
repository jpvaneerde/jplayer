"""The JPlay integration against a fake JPlay player and a fake JPlaying Now, in a real Home Assistant."""
import asyncio
import copy
import time

import pytest
from aiohttp import web
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

try:
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
except ImportError:
    from homeassistant.components.zeroconf import ZeroconfServiceInfo

from ipaddress import ip_address

DOMAIN = "jplay"

STATUS = {
    "player": "Lounge",
    "active": "radio",
    "now_playing": {"state": "playing", "title": "Training Season", "artist": "Dua Lipa", "album": "",
                    "art_url": "/logo.png", "duration": None, "position": 12.0, "track_number": None,
                    "track_count": None, "extra": {"station": "P3", "stream_url": "http://p3/stream", "codec": "aac",
                                                   "bitrate": 192, "samplerate": 48000}},
    "capabilities": {"play": True, "pause": True, "stop": True, "next": False, "previous": False, "seek": False},
    "volume": 40, "volume_available": True,
    "output": {"setting": "auto", "device": "hw:CARD=DAC,DEV=0", "name": "Audiolab", "kind": "usb", "present": True},
    "sources": [
        {"id": "radio", "name": "Radio", "available": True, "details": {}, "now_playing": {"position": 12.0}},
        {"id": "plex", "name": "Plex", "available": True, "details": {}, "now_playing": {}},
        {"id": "cd", "name": "CD", "available": True, "now_playing": {},
         "details": {"status": "ready", "disc": {"album": "Blue", "artist": "Joni Mitchell", "art_url": "",
                                                 "tracks": [{"number": 1, "title": "All I Want"}, {"number": 2, "title": "My Old Man"}]}}},
        {"id": "spotify", "name": "Spotify", "available": True, "details": {}, "now_playing": {}},
        {"id": "bluetooth", "name": "Bluetooth", "available": True, "now_playing": {},
         "details": {"discoverable": False, "devices": [{"name": "Pixel", "connected": True}]}},
    ],
}
FAVS = [{"uuid": "u1", "name": "P3", "url": "http://p3/stream", "favicon": "https://x/p3.png"},
        {"uuid": "u2", "name": "Jazz FM", "url": "http://jazz/stream", "favicon": ""}]
OUTPUT = {"current": {"setting": "auto", "name": "Audiolab", "present": True}, "auto_choice": "Audiolab",
          "cards": [{"name": "Audiolab", "kind": "usb", "device": "hw:CARD=DAC,DEV=0"},
                    {"name": "HiFiBerry", "kind": "hat", "device": "hw:CARD=sndrpihifiberry,DEV=0"}]}


def fake_update_status(version="1.0.0", latest="1.1.0"):
    return {"supported": True, "version": version, "latest": latest, "available": latest != version,
            "target": f"v{latest}" if latest != version else None, "notes": f"## {latest}\nLyrics in JPlay Records\nJapanese titles",
            "state": "idle", "step": "checked", "message": "", "in_progress": False, "finished_at": None, "channel": "releases"}


async def simulate_update(st: dict, ok: bool = True):
    """What the Pi's updater does: progress through its steps, then done (or rolled back)."""
    st.update(state="updating", step="starting", in_progress=True)
    for step in ("fetching", "applying", "restarting", "verifying"):
        await asyncio.sleep(0.3)
        st["step"] = step
    await asyncio.sleep(0.3)
    if ok:
        st.update(state="done", step="done", in_progress=False, version=st["latest"], available=False, target=None,
                  message=f"Updated to {st['latest']}", finished_at=time.time())
    else:
        st.update(state="rolled_back", step="rolled_back", in_progress=False, finished_at=time.time(),
                  message="Update failed (JPlay didn't start); back on 1.0.0")


class FakePlayer:
    def __init__(self, with_updates=True):
        self.update = fake_update_status() if with_updates else None
        self.update_ok = True
        self.status = copy.deepcopy(STATUS)
        self.calls = []
        self.sockets = []
        app = web.Application()
        app.router.add_get("/api/v1/info", lambda r: web.json_response(
            {"id": "abc123", "name": "Lounge", "version": "0.1.0", "sources": []}))
        app.router.add_get("/api/v1/status", lambda r: web.json_response(self.status))
        app.router.add_get("/api/v1/radio/favourites", lambda r: web.json_response(FAVS))
        app.router.add_get("/api/v1/output", lambda r: web.json_response(OUTPUT))
        app.router.add_get("/api/v1/plex/playlists", lambda r: web.json_response(
            [{"key": "77", "title": "Friday", "thumb": "/api/v1/plex/art?path=x"}]))
        app.router.add_get("/api/v1/events", self.ws)
        if with_updates:
            app.router.add_get("/api/v1/update", lambda r: web.json_response(self.update))
            app.router.add_post("/api/v1/update/install", self.install)
        app.router.add_route("*", "/api/v1/{tail:.*}", self.record)
        self.app = app

    async def install(self, request):
        body = await request.json()
        self.calls.append(("POST", "/api/v1/update/install", body))
        asyncio.get_running_loop().create_task(simulate_update(self.update, self.update_ok))
        return web.json_response({"ok": True, "target": body.get("target")})

    async def record(self, request):
        if request.method == "GET":
            return web.json_response({"detail": "Not Found"}, status=404)   # like a real player
        body = await request.json() if request.can_read_body else None
        self.calls.append((request.method, request.path, body))
        if request.path == "/api/v1/standby":
            self.status["active"] = None
            await self.push()
        return web.json_response({"ok": True})

    async def ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        await ws.send_json({"type": "state", "data": self.status})
        async for _ in ws:
            pass
        return ws

    async def push(self):
        for ws in self.sockets:
            if not ws.closed:
                await ws.send_json({"type": "state", "data": self.status})


class FakeNow:
    def __init__(self):
        self.screen = True
        self.view = "p1"
        self.calls = []
        app = web.Application()
        app.router.add_get("/api/dashboard", self.dashboard)
        app.router.add_get("/api/screen-status", lambda r: web.json_response(
            {"manager": "running", "off_after_minutes": 10, "switched_off_by_hand": not self.screen,
             "touch": {"devices": 2}}))
        app.router.add_get("/api/screen", lambda r: web.json_response({"on": self.screen}))
        app.router.add_post("/api/screen", self.set_screen)
        app.router.add_post("/api/view", self.set_view)
        self.update = fake_update_status("2.0.0", "2.0.0")
        app.router.add_get("/api/update", lambda r: web.json_response(self.update))
        self.app = app

    async def dashboard(self, request):
        return web.json_response({
            "config": {"secret": "x"},
            "player": {"id": self.view, "name": "Lounge", "online": True, "source": "radio", "show": True,
                       "song_known": True, "title": "Training Season", "artist": "Dua Lipa", "station": "P3",
                       "source_name": "Radio", "recognised": True},
            "players": [{"id": "p1", "name": "Lounge"}, {"id": "p2", "name": "Study"}],
            "view": self.view,
            "ambient": {"identified": True, "title": "Blue", "artist": "Joni Mitchell"},
            "screen": {"on": self.screen, "changed": 1},
        })

    async def set_screen(self, request):
        self.screen = bool((await request.json())["on"])
        self.calls.append(("screen", self.screen))
        return web.json_response({"on": self.screen})

    async def set_view(self, request):
        self.view = (await request.json())["player"]
        return web.json_response({"view": self.view})


async def serve(app):
    runner = web.AppRunner(app, shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


async def wait(cond, timeout=5):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


async def test_player(hass: HomeAssistant, socket_enabled):
    fake = FakePlayer()
    runner, port = await serve(fake.app)
    try:
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        assert r["type"] is FlowResultType.MENU
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "player"})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"host": "127.0.0.1", "port": port})
        assert r["type"] is FlowResultType.CREATE_ENTRY, r
        assert r["title"] == "Lounge"
        await hass.async_block_till_done()
        await wait(lambda: fake.sockets)

        mp = hass.states.get("media_player.lounge")
        assert mp.state == "playing", mp
        a = mp.attributes
        assert a["media_title"] == "Training Season" and a["media_artist"] == "Dua Lipa"
        assert a["source"] == "Radio" and a["media_channel"] == "P3"
        assert a["volume_level"] == 0.4
        assert "CD" in a["source_list"]
        assert hass.states.get("sensor.lounge_source").state == "Radio"
        assert hass.states.get("sensor.lounge_now_playing").state == "Dua Lipa – Training Season"
        assert hass.states.get("sensor.lounge_audio_format").state == "AAC · 48 kHz · 192 kbps"
        assert hass.states.get("sensor.lounge_cd").state == "Ready"
        assert hass.states.get("sensor.lounge_bluetooth_device").state == "Pixel"
        assert hass.states.get("select.lounge_radio_station").state == "P3"
        assert hass.states.get("select.lounge_output").state == "Automatic"
        assert hass.states.get("binary_sensor.lounge_playing").state == "on"
        assert hass.states.get("binary_sensor.lounge_dac").state == "on"
        assert hass.states.get("binary_sensor.lounge_cd_inserted").state == "on"
        assert hass.states.get("switch.lounge_bluetooth_pairing").state == "off"

        # Live: a new song pushed over the WebSocket shows up at once
        fake.status["now_playing"]["title"] = "Houdini"
        await fake.push()
        await wait(lambda: hass.states.get("media_player.lounge").attributes.get("media_title") == "Houdini")

        # Position-only updates don't rewrite the state every second
        changes = []
        hass.bus.async_listen("state_changed", lambda e: changes.append(e.data["entity_id"]))
        fake.status["now_playing"]["position"] = 13.0
        await fake.push()
        await asyncio.sleep(0.3)
        assert changes == [], changes

        # Controls
        await hass.services.async_call("media_player", "media_pause", {"entity_id": "media_player.lounge"}, blocking=True)
        await hass.services.async_call("media_player", "volume_set", {"entity_id": "media_player.lounge", "volume_level": 0.55}, blocking=True)
        await hass.services.async_call("media_player", "select_source", {"entity_id": "media_player.lounge", "source": "CD"}, blocking=True)
        await hass.services.async_call("select", "select_option", {"entity_id": "select.lounge_radio_station", "option": "Jazz FM"}, blocking=True)
        await hass.services.async_call("select", "select_option", {"entity_id": "select.lounge_output", "option": "HiFiBerry (hat)"}, blocking=True)
        await hass.services.async_call("media_player", "play_media", {"entity_id": "media_player.lounge", "media_content_type": "music", "media_content_id": "radio:jazz fm"}, blocking=True)
        await hass.services.async_call("media_player", "play_media", {"entity_id": "media_player.lounge", "media_content_type": "music", "media_content_id": "cd:2"}, blocking=True)
        await hass.services.async_call("button", "press", {"entity_id": "button.lounge_eject_cd"}, blocking=True)
        await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.lounge_bluetooth_pairing"}, blocking=True)
        made = [(m, p, b) for m, p, b in fake.calls]
        assert ("POST", "/api/v1/player/pause", {}) in made
        assert ("PUT", "/api/v1/volume", {"volume": 55}) in made
        assert ("POST", "/api/v1/sources/cd/activate", {}) in made
        assert ("POST", "/api/v1/radio/play", {"uuid": "u2"}) in made
        assert ("PUT", "/api/v1/output", {"device": "hw:CARD=sndrpihifiberry,DEV=0"}) in made
        assert ("POST", "/api/v1/cd/play", {"track": 2}) in made
        assert ("POST", "/api/v1/cd/eject", {}) in made
        assert ("POST", "/api/v1/bluetooth/discoverable", {"on": True}) in made

        # Media browser
        ent = hass.data["media_player"].get_entity("media_player.lounge")
        root = await ent.async_browse_media()
        assert [c.media_content_id for c in root.children][:3] == ["radio", "cd", "plex"]
        radio = await ent.async_browse_media("jplay", "radio")
        assert [c.title for c in radio.children] == ["P3", "Jazz FM"]
        cd = await ent.async_browse_media("jplay", "cd")
        assert cd.children[1].media_content_id == "cd:2"
        plex = await ent.async_browse_media("jplay", "plex")
        assert plex.children[0].media_content_id == "plex:77" and "media_image_id" in plex.children[0].thumbnail

        # Off = standby; the pushed state turns the media player off
        await hass.services.async_call("media_player", "turn_off", {"entity_id": "media_player.lounge"}, blocking=True)
        await wait(lambda: hass.states.get("media_player.lounge").state == "off")
        assert hass.states.get("sensor.lounge_source").state == "Standby"
        assert hass.states.get("sensor.lounge_now_playing").state == "unknown"

        # Player gone: entities go unavailable
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await runner.cleanup()
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get("media_player.lounge").state == "unavailable"
        assert await hass.config_entries.async_unload(entry.entry_id)
    finally:
        await runner.cleanup()


async def test_zeroconf_discovery(hass: HomeAssistant, socket_enabled):
    fake = FakePlayer()
    runner, port = await serve(fake.app)
    try:
        info = ZeroconfServiceInfo(ip_address=ip_address("127.0.0.1"), ip_addresses=[ip_address("127.0.0.1")],
                                   hostname="lounge.local.", name="Lounge._jplay._tcp.local.", port=port,
                                   type="_jplay._tcp.local.", properties={"id": "abc123"})
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info)
        assert r["type"] is FlowResultType.FORM and r["step_id"] == "zeroconf_confirm", r
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
        assert r["type"] is FlowResultType.CREATE_ENTRY and r["data"]["port"] == port
        await hass.async_block_till_done()
        # Found again (e.g. new address): no second entry
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info)
        assert r["type"] is FlowResultType.ABORT and r["reason"] == "already_configured"
        for e in hass.config_entries.async_entries(DOMAIN):
            await hass.config_entries.async_unload(e.entry_id)
    finally:
        await runner.cleanup()


async def test_display_discovery(hass: HomeAssistant, socket_enabled):
    fake = FakeNow()
    fake.app.router.add_get("/api/info", lambda r: web.json_response(
        {"id": "d1sp1ay", "name": "Lounge display", "room": "Lounge", "kind": "display", "version": "2.0.0"}))
    runner, port = await serve(fake.app)
    try:
        info = ZeroconfServiceInfo(ip_address=ip_address("127.0.0.1"), ip_addresses=[ip_address("127.0.0.1")],
                                   hostname="lounge.local.", name="Lounge display._jplay-display._tcp.local.", port=port,
                                   type="_jplay-display._tcp.local.", properties={"id": "d1sp1ay"})
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info)
        assert r["type"] is FlowResultType.FORM and r["step_id"] == "zeroconf_confirm_display", r
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
        assert r["type"] is FlowResultType.CREATE_ENTRY and r["data"]["kind"] == "jplayingnow" and r["title"] == "Lounge display"
        await hass.async_block_till_done()
        assert hass.states.get("switch.lounge_display_screen").state == "on"
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info)
        assert r["type"] is FlowResultType.ABORT and r["reason"] == "already_configured"
        for e in hass.config_entries.async_entries(DOMAIN):
            await hass.config_entries.async_unload(e.entry_id)
    finally:
        await runner.cleanup()


async def test_jplayingnow(hass: HomeAssistant, socket_enabled):
    fake = FakeNow()
    runner, port = await serve(fake.app)
    try:
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "jplayingnow"})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"name": "Lounge display", "host": "127.0.0.1", "port": port})
        assert r["type"] is FlowResultType.CREATE_ENTRY, r
        await hass.async_block_till_done()
        assert hass.states.get("switch.lounge_display_screen").state == "on"
        assert hass.states.get("select.lounge_display_shown_player").state == "Lounge"
        assert hass.states.get("sensor.lounge_display_showing").state == "Dua Lipa – Training Season"
        assert hass.states.get("sensor.lounge_display_heard").state == "Joni Mitchell – Blue"
        await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.lounge_display_screen"}, blocking=True)
        assert fake.calls == [("screen", False)]
        assert hass.states.get("switch.lounge_display_screen").state == "off"
        await hass.services.async_call("select", "select_option", {"entity_id": "select.lounge_display_shown_player", "option": "Study"}, blocking=True)
        assert fake.view == "p2"
        assert hass.states.get("select.lounge_display_shown_player").state == "Study"
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        assert "config" not in entry.runtime_data.coordinator.data["dashboard"]
        u = hass.states.get("update.lounge_display")
        assert u.state == "off" and u.attributes["installed_version"] == "2.0.0" and u.attributes["title"] == "JPlaying Now"
        assert await hass.config_entries.async_unload(entry.entry_id)
    finally:
        await runner.cleanup()



async def test_updates(hass: HomeAssistant, socket_enabled):
    fake = FakePlayer()
    runner, port = await serve(fake.app)
    try:
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "player"})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"host": "127.0.0.1", "port": port})
        await hass.async_block_till_done()
        u = hass.states.get("update.lounge")
        assert u.state == "on", u
        assert u.attributes["installed_version"] == "1.0.0" and u.attributes["latest_version"] == "1.1.0"
        assert u.attributes["title"] == "JPlay"
        assert u.attributes["release_summary"] == "Lyrics in JPlay Records · Japanese titles"
        ent = hass.data["update"].get_entity("update.lounge")
        assert "Japanese titles" in await ent.async_release_notes()

        # Install from Home Assistant: the Pi's updater runs, progress shows, then up to date
        task = hass.async_create_task(hass.services.async_call("update", "install", {"entity_id": "update.lounge"}, blocking=True))
        seen = set()
        for _ in range(60):
            await asyncio.sleep(0.1)
            await ent.coordinator.async_refresh()
            st = hass.states.get("update.lounge")
            if st.attributes.get("in_progress"):
                seen.add(st.attributes.get("update_percentage"))
            if task.done():
                break
        await task
        assert ("POST", "/api/v1/update/install", {"target": "v1.1.0"}) in fake.calls
        assert seen - {None}, seen                          # progress was shown
        u = hass.states.get("update.lounge")
        assert u.state == "off" and u.attributes["installed_version"] == "1.1.0", u

        # A failed update (rolled back by the Pi) is reported as an error
        fake.update.update(fake_update_status("1.1.0", "1.2.0"))
        fake.update_ok = False
        await ent.coordinator.async_refresh(); await hass.async_block_till_done()
        assert hass.states.get("update.lounge").state == "on"
        with pytest.raises(Exception, match="back on 1.0.0"):
            await hass.services.async_call("update", "install", {"entity_id": "update.lounge"}, blocking=True)

        # Without the updater set up on the Pi: shown, but no Install
        fake.update.update(fake_update_status("1.1.0", "1.2.0"), supported=False)
        await ent.coordinator.async_refresh(); await hass.async_block_till_done()
        assert not hass.states.get("update.lounge").attributes["supported_features"] & 1
        for e in hass.config_entries.async_entries(DOMAIN):
            await hass.config_entries.async_unload(e.entry_id)
    finally:
        await runner.cleanup()


async def test_player_without_update_api(hass: HomeAssistant, socket_enabled):
    fake = FakePlayer(with_updates=False)
    runner, port = await serve(fake.app)
    try:
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "player"})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"host": "127.0.0.1", "port": port})
        await hass.async_block_till_done()
        assert hass.states.get("media_player.lounge").state == "playing"     # everything else still works
        assert hass.states.get("update.lounge").state == "unavailable"
        for e in hass.config_entries.async_entries(DOMAIN):
            await hass.config_entries.async_unload(e.entry_id)
    finally:
        await runner.cleanup()


class FakeHub:
    def __init__(self):
        self.rooms = [
            {"room": "Lounge", "online": True, "session": None,
             "play": {"title": "Let's Go Crazy", "artist": "Prince", "album": "Purple Rain", "kind": "physical",
                      "detail": "vinyl", "station": None, "cover": "https://records.example/covers/7-s.webp",
                      "record": {"id": 7, "artist": "Prince", "title": "Purple Rain", "year": 1984}}},
            {"room": "Study", "online": False, "session": {"mode": "radio", "station": "P3", "started_at": 1}, "play": None},
        ]
        self.update = fake_update_status("1.0.3", "1.0.3")
        app = web.Application()
        app.router.add_get("/api/info", lambda r: web.json_response({"id": "h1", "name": "JPlay hub", "kind": "hub", "version": "1.0.3"}))
        app.router.add_get("/api/now", lambda r: web.json_response({"rooms": self.rooms}))
        app.router.add_get("/api/stats", lambda r: web.json_response({
            "physical": {"spins": 2, "tracks": 9, "vinyl_hours": 1.25}, "streamed": {"tracks": 14}, "heard": {"tracks": 3},
            "listening_hours": 2.5}))
        app.router.add_get("/api/spins", lambda r: web.json_response({"spins": [
            {"room": "Lounge", "medium": "vinyl", "tracks": 5, "started_at": 1_760_000_000_000,
             "record": {"artist": "Prince", "title": "Purple Rain", "year": 1984, "cover": "https://records.example/covers/7-s.webp"}}]}))
        app.router.add_get("/api/update", lambda r: web.json_response(self.update))
        self.app = app


async def test_hub(hass: HomeAssistant, socket_enabled):
    fake = FakeHub()
    runner, port = await serve(fake.app)
    try:
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "hub"})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"host": "127.0.0.1", "port": port})
        assert r["type"] is FlowResultType.CREATE_ENTRY and r["title"] == "JPlay hub", r
        await hass.async_block_till_done()
        lounge = hass.states.get("sensor.jplay_hub_lounge_now_playing")
        assert lounge.state == "Prince – Let's Go Crazy"
        assert lounge.attributes["kind"] == "record" and lounge.attributes["source"] == "Record"
        assert lounge.attributes["record"] == "Prince – Purple Rain"
        assert lounge.attributes["entity_picture"].endswith("/api/cover?u=https%3A%2F%2Frecords.example%2Fcovers%2F7-s.webp")
        assert hass.states.get("sensor.jplay_hub_study_now_playing").state == "P3"
        assert hass.states.get("binary_sensor.jplay_hub_lounge_display").state == "on"
        assert hass.states.get("binary_sensor.jplay_hub_study_display").state == "off"
        assert hass.states.get("sensor.jplay_hub_records_put_on_today").state == "2"
        assert hass.states.get("sensor.jplay_hub_songs_played_today").state == "26"
        assert float(hass.states.get("sensor.jplay_hub_radio_and_streams_today").state) == 2.5
        assert hass.states.get("sensor.jplay_hub_last_record_put_on").state == "Prince – Purple Rain"
        assert hass.states.get("update.jplay_hub").attributes["installed_version"] == "1.0.3"
        # A new room (a new display) gets its entities by itself
        fake.rooms.append({"room": "Kitchen", "online": True, "session": None, "play": None})
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get("sensor.jplay_hub_kitchen_now_playing").state == "Quiet"
        # Found on the network as well: the same hub, not a second one
        info = ZeroconfServiceInfo(ip_address=ip_address("127.0.0.1"), ip_addresses=[ip_address("127.0.0.1")],
                                   hostname="hub.local.", name="JPlay hub._jplay-hub._tcp.local.", port=port,
                                   type="_jplay-hub._tcp.local.", properties={"id": "h1"})
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info)
        assert r["type"] is FlowResultType.ABORT and r["reason"] == "already_configured"
        assert await hass.config_entries.async_unload(entry.entry_id)
    finally:
        await runner.cleanup()
