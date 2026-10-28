"""The display over HTTP: who it is, its settings (room and hub), and lyrics from the hub."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from jplay.common import hub as hubs
from jplay.display import app as display, lyrics


@pytest.fixture
def client(tmp_path, monkeypatch):
    data = tmp_path / "display"
    data.mkdir()
    monkeypatch.setattr(display, "DATA_DIR", str(data))
    monkeypatch.setattr(display, "CONFIG_FILE", str(data / "config.json"))
    monkeypatch.setattr(display, "HISTORY_DB", str(data / "history.db"))
    monkeypatch.setattr(display, "ANNOUNCE", False)
    monkeypatch.setattr(display, "is_service_active", lambda name: False)
    (tmp_path / "roles").write_text("display")          # no hub on this Pi
    hubs.forget()
    with TestClient(display.app) as c:
        yield c
    hubs.forget()


def test_info_and_a_lasting_unit_id(client):
    a = client.get("/api/info").json()
    assert a["kind"] == "display" and len(a["id"]) == 16
    assert client.get("/api/info").json()["id"] == a["id"]


def test_old_settings_are_moved_over(client, tmp_path):
    cfg_path = display.CONFIG_FILE
    with open(cfg_path, "w") as f:
        json.dump({"spins_enabled": True, "spins_url": "https://records.jplay.cloud", "spins_token": "t",
                   "spins_room_player": "192.168.1.222", "update_channel": "main", "update_auto": "night",
                   "players": [{"id": "player_1", "name": "Lounge", "host": "192.168.1.222", "port": 8080}]}, f)
    cfg = display.load_config()
    assert cfg["room_player"] == "192.168.1.222" and "spins_token" not in cfg and "update_auto" not in cfg
    assert display.UPDATES.settings() == {"channel": "main", "auto": "night"}


def test_saving_the_room_and_hub(client):
    r = client.post("/settings/save", data={"room": "Lounge", "report_plays": "on", "hub": "jplayhub",
                                            "room_player": "", "update_channel": "releases"}, follow_redirects=False)
    assert r.status_code == 303
    cfg = display.load_config()
    assert cfg["room"] == "Lounge" and cfg["hub"] == "jplayhub" and cfg["report_plays"] is True
    assert client.get("/api/info").json()["name"] == "Lounge display"
    page = client.get("/settings").text
    assert "http://jplayhub:8090" in page and 'value="Lounge"' in page
    dash = client.get("/api/dashboard").json()
    assert dash["hub"] == {"url": "http://jplayhub:8090", "here": False}


def test_lyrics_come_from_the_hub_and_stay_for_when_it_is_away(client, monkeypatch):
    calls = []

    class Resp:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    def fake_get(url, params=None, timeout=None, headers=None):
        calls.append(url)
        if len(calls) > 1:
            raise ConnectionError("hub away")
        return Resp({"found": True, "instrumental": False, "synced": [[1, "Hi"]], "plain": ["Hi"], "offset": 2})
    monkeypatch.setattr(lyrics.requests, "get", fake_get)
    monkeypatch.setattr(lyrics, "fetch", lambda *a, **kw: pytest.fail("LRCLIB asked while a copy was there"))
    with open(display.CONFIG_FILE) as f:
        cfg = json.load(f)
    cfg["hub"] = "jplayhub"
    display.save_config(cfg)
    q = {"artist": "Dua Lipa", "title": "Houdini"}
    assert client.get("/api/lyrics", params=q).json()["offset"] == 2
    assert client.get("/api/lyrics", params=q).json()["synced"] == [[1, "Hi"]]   # the hub is away: this Pi's copy
    assert calls == ["http://jplayhub:8090/api/lyrics"] * 2


def test_the_pages_render(client):
    for path in ("/", "/settings", "/history"):
        assert client.get(path).status_code == 200


def test_touch_turns_with_the_screen(client, monkeypatch):
    ran, restarted = [], []
    monkeypatch.setattr(display.subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    monkeypatch.setattr(display, "is_service_active", lambda name: name == display.KIOSK_SERVICE)
    monkeypatch.setattr(display, "set_service_state", lambda *a: None)
    monkeypatch.setattr(display, "restart_kiosk_browser", lambda clear=True: restarted.append(clear))
    client.post("/settings/save", data={"screen_rotation": "inverted", "local_kiosk": "on"}, follow_redirects=False)
    with open(os.path.join(display.DATA_DIR, "touch-rotation.env")) as f:
        assert f.read() == 'LIBINPUT_CALIBRATION_MATRIX="-1 0 1 0 -1 1"\n'
    assert any("udevadm" in c[2] for c in ran if len(c) > 2) and restarted == [False]
    assert client.get("/api/screen").json()["transform"] == "180"
    # Saving again with the same rotation leaves the kiosk alone
    client.post("/settings/save", data={"screen_rotation": "inverted", "local_kiosk": "on"}, follow_redirects=False)
    assert restarted == [False]
