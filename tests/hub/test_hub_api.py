"""The hub over HTTP: events from a display, lyrics for the house, corrections and imports."""
import time

import pytest
from fastapi.testclient import TestClient

from jplay.common import lrclib
from jplay.hub import app as hub_app, db, importer, lyrics, spins

SONG = {"found": True, "instrumental": False, "synced": [[1.0, "Hello"], [3.0, "World"]], "plain": ["Hello", "World"],
        "offset": 0, "lrclib_id": 42}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_app, "PROCESS_EVERY", 3600)
    with TestClient(hub_app.create_app(tmp_path, announce=False)) as c:
        yield c


def test_events_become_plays(client):
    r = client.post("/api/events", json={"unit": {"id": "u1", "name": "Lounge display", "room": "Lounge"}, "events": [
        {"id": "e1", "type": "mic", "ts": time.time() - 120, "data": {"title": "Houdini", "artist": "Dua Lipa"}}]})
    assert r.json() == {"accepted": 1, "room": "Lounge"}
    spins.process_events(at=db.now() + spins.SETTLE_MS + 1000)
    plays = client.get("/api/plays").json()
    assert plays["total"] == 1 and plays["plays"][0]["title"] == "Houdini"
    rooms = client.get("/api/rooms").json()
    assert rooms["rooms"][0]["name"] == "Lounge" and rooms["units"][0]["online"] is True
    now = client.get("/api/now").json()["rooms"][0]
    assert now["room"] == "Lounge" and now["play"]["title"] == "Houdini"
    pid = plays["plays"][0]["id"]
    assert client.put(f"/api/plays/{pid}", json={"physical": "vinyl"}).json() == {"ok": True}
    assert client.get("/api/plays", params={"kind": "physical"}).json()["total"] == 1
    assert client.get("/api/spins").json()["spins"] == []           # no album: no spin
    client.delete(f"/api/plays/{pid}")
    assert client.get("/api/plays").json()["total"] == 0


def test_events_need_a_room(client):
    r = client.post("/api/events", json={"unit": {"id": "u1"}, "events": []})
    assert r.status_code == 400


def test_lyrics_are_looked_up_once_and_kept(client, monkeypatch):
    calls = []

    def fake_fetch(artist, title, album="", duration=None):
        calls.append(title)
        return dict(SONG)
    monkeypatch.setattr(lyrics, "fetch", fake_fetch)
    q = {"artist": "Dua Lipa & Elton John", "title": "Cold Heart (PNAU Remix)", "duration": 202}
    a = client.get("/api/lyrics", params=q).json()
    b = client.get("/api/lyrics", params=q).json()
    assert a == b and a["synced"][0] == [1.0, "Hello"] and calls == ["Cold Heart (PNAU Remix)"]
    key = lrclib.cache_key("Dua Lipa", "Cold Heart")
    assert client.put("/api/lyrics/song", params={"key": key}, json={"offset": 1.5}).json()["offset"] == 1.5
    assert client.get("/api/lyrics", params=q).json()["offset"] == 1.5
    songs = client.get("/api/lyrics/songs").json()
    assert songs["stats"] == {"songs": 1, "found": 1, "synced": 1} and songs["songs"][0]["asked_count"] == 3


def test_lyrics_unavailable_is_not_stored(client, monkeypatch):
    def down(*a, **kw):
        raise lrclib.Unavailable("503")
    monkeypatch.setattr(lyrics, "fetch", down)
    assert client.get("/api/lyrics", params={"artist": "A", "title": "B"}).status_code == 502
    assert client.get("/api/lyrics/songs").json()["total"] == 0
    monkeypatch.setattr(lyrics, "_failed", {})
    monkeypatch.setattr(lyrics, "fetch", lambda *a, **kw: dict(lrclib.NOT_FOUND))
    ans = client.get("/api/lyrics", params={"artist": "A", "title": "B"}).json()
    assert ans["found"] is False
    assert client.get("/api/lyrics/songs", params={"only": "missing"}).json()["total"] == 1


def test_lyrics_pasted_by_hand_win(client, monkeypatch):
    monkeypatch.setattr(lyrics, "fetch", lambda *a, **kw: dict(lrclib.NOT_FOUND))
    client.get("/api/lyrics", params={"artist": "Wurts", "title": "分かってないよ"})
    key = lrclib.cache_key("Wurts", "分かってないよ")
    s = client.put("/api/lyrics/song", params={"key": key}, json={"text": "[00:01.00] 一行目\n[00:04.50] 二行目"}).json()
    assert s["found"] and s["synced"] == [[1.0, "一行目"], [4.5, "二行目"]] and s["source"] == "manual"
    assert client.get("/api/lyrics", params={"artist": "Wurts", "title": "分かってないよ"}).json()["found"] is True


def test_import_from_jplay_records(client, monkeypatch):
    pages = {
        "rooms": {"rooms": [{"room": "Lounge", "priority": 10, "formats": ["vinyl"]}]},
        "plays": {"plays": [
            {"id": 1, "room": "Lounge", "kind": "physical", "detail": "vinyl", "station": None, "started_at": 1_700_000_000_000,
             "ended_at": None, "title": "Let's Go Crazy", "artist": "Prince", "album": "Purple Rain", "cover": None,
             "instance_id": 7, "release_id": 70, "confidence": "mic", "heard_by": ["Lounge"], "note": "Matched your vinyl",
             "via": None, "created_at": 1_700_000_000_000, "alternatives": [],
             "record": {"artist": "Prince", "title": "Purple Rain", "year": 1984, "cover": "/covers/7-small.jpg"}},
            {"id": 2, "room": "Kitchen", "kind": "streamed", "detail": "radio", "station": "P3",
             "started_at": 1_700_000_500_000, "ended_at": None, "title": "Houdini", "artist": "Dua Lipa", "album": "",
             "cover": None, "instance_id": None, "release_id": None, "confidence": "mic+metadata", "heard_by": [],
             "note": None, "via": None, "created_at": 1_700_000_500_000, "alternatives": [], "record": None}]},
        "lyrics": {"lyrics": [{"key": "x", "artist": "Prince", "title": "Purple Rain", "album": "", "duration": 520,
                               "found": 1, "instrumental": 0, "synced": None, "plain": '["Line"]', "offset": -1.0,
                               "source": "lrclib", "lrclib_id": 5, "fetched_at": 1, "asked_count": 4, "last_asked_at": 2}]},
    }
    seen = []

    def fake_get(url, params=None, token="", timeout=60):
        seen.append((url, dict(params or {}), token))
        part = params["part"]
        if part == "plays" and params.get("after"):
            return {"plays": []}
        if part == "lyrics" and params.get("after"):
            return {"lyrics": []}
        return pages[part]
    monkeypatch.setattr(importer, "_get", fake_get)
    for _ in range(2):                                     # a second import adds nothing
        assert client.post("/api/import/records", json={"url": "records.example", "token": "tok"}).status_code == 200
        for _ in range(100):
            if not client.get("/api/import").json()["running"]:
                break
            time.sleep(0.05)
        st = client.get("/api/import").json()
        assert st["error"] == ""
    assert seen[0] == ("https://records.example/api/spins/export", {"part": "rooms"}, "tok")
    plays = client.get("/api/plays").json()
    assert plays["total"] == 2
    vinyl = next(p for p in plays["plays"] if p["kind"] == "physical")
    assert vinyl["record"]["cover"] == "https://records.example/covers/7-small.jpg" and vinyl["origin"] == "records"
    assert client.get("/api/spins").json()["spins"][0]["record"]["title"] == "Purple Rain"
    assert {r["name"]: r["priority"] for r in client.get("/api/rooms").json()["rooms"]} == {"Lounge": 10, "Kitchen": 1}
    assert client.get("/api/lyrics", params={"artist": "Prince", "title": "Purple Rain"}).json()["offset"] == -1.0
    assert client.get("/api/settings").json()["records"]["enabled"] is True     # connected for matching from now on
