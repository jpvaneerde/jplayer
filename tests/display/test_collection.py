"""Your collection (the fingerprint service) as the first place the microphone looks."""
import asyncio
import io
import json
import time
import wave

import httpx

from jplay.display import collection


def wav(seconds, rate=16000, value=1000):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(value.to_bytes(2, "little", signed=True) * int(seconds * rate))
    return out.getvalue()


def seconds(b):
    with wave.open(io.BytesIO(b)) as w:
        return w.getnframes() / w.getframerate()


def test_clips_are_joined_and_capped():
    assert seconds(collection.join_wavs(wav(8), wav(8))) == 16
    assert seconds(collection.join_wavs(wav(20), wav(10))) == collection.MAX_SECONDS
    assert collection.join_wavs(wav(8, rate=44100), wav(8)) == wav(8)        # different formats: just the new one
    assert collection.join_wavs(b"junk", wav(8)) == wav(8)


FOUND = {"found": True, "offset": 61.3, "score": 40, "time_factor": 1.004, "elapsed_ms": 700,
         "song": {"id": 12, "title": "Tongue", "artist": "Fontaines D.C.", "album": "Dopamine Chamber",
                  "albumartist": "Fontaines D.C.", "year": "2026", "track": "3", "duration": 214.0,
                  "path": "Fontaines D.C./Dopamine Chamber/03 Tongue.flac", "cover": "/api/cover/12"}}


def client(answer, seen, status=200):
    def handler(req):
        seen.append(req)
        return httpx.Response(status, json=answer)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ask(c, answer, seen, status=200, token="tok"):
    async def go():
        async with client(answer, seen, status) as h:
            return await c.recognize(wav(16), "http://192.168.1.240/", token, h)
    return asyncio.run(go())


def test_a_song_from_the_collection_in_shazams_shape():
    c, seen = collection.Collection(), []
    out = ask(c, FOUND, seen)
    t = out["track"]
    assert t["title"] == "Tongue" and t["subtitle"] == "Fontaines D.C." and t["duration"] == 214.0
    assert t["images"]["coverart"] == "http://192.168.1.240/api/cover/12" and t["source"] == "collection"
    assert {"title": "Album", "text": "Dopamine Chamber"} in t["sections"][0]["metadata"]
    assert out["matches"][0]["offset"] == 61.3
    req = seen[0]
    assert str(req.url) == "http://192.168.1.240/api/recognize" and req.headers["authorization"] == "Bearer tok"
    assert req.headers["content-type"] == "audio/wav" and c.view()["found"] == 1


def test_not_in_the_collection_and_errors():
    c, seen = collection.Collection(), []
    assert ask(c, {"found": False}, seen) == {"matches": []}
    assert ask(c, {"detail": "Wrong or missing token"}, seen, status=401) == {}
    assert "token is wrong" in c.view()["last_error"] and c.view()["waiting_seconds"] > 50
    assert ask(c, FOUND, seen) == {} and len(seen) == 2                  # waiting after the error: not asked
    assert asyncio.run(collection.Collection().recognize(wav(8), "")) == {}   # not set up


def test_settings_keep_the_token_on_the_pi(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from jplay.display import app as display
    data = tmp_path / "display"
    data.mkdir()
    monkeypatch.setattr(display, "DATA_DIR", str(data))
    monkeypatch.setattr(display, "CONFIG_FILE", str(data / "config.json"))
    monkeypatch.setattr(display, "HISTORY_DB", str(data / "history.db"))
    monkeypatch.setattr(display, "ANNOUNCE", False)
    monkeypatch.setattr(display, "is_service_active", lambda name: False)
    with TestClient(display.app) as c:
        c.post("/settings/save", data={"collection_url": "192.168.1.240/", "collection_token": "s3cret"},
               follow_redirects=False)
        cfg = display.load_config()
        assert cfg["collection_url"] == "http://192.168.1.240" and cfg["collection_token"] == "s3cret"
        assert "s3cret" not in c.get("/settings").text and "collection_token" not in c.get("/api/dashboard").json()["config"]
        assert c.get("/api/recognizer").json()["collection"]["on"] is True
        c.post("/settings/save", data={"collection_url": "http://192.168.1.240", "collection_token_remove": "on"},
               follow_redirects=False)
        assert display.load_config()["collection_token"] == ""


def test_the_microphone_asks_the_collection_first(tmp_path, monkeypatch):
    """Unknown music: the collection for every clip, Shazam at most every 30 s; a match: nobody until it ends."""
    from jplay.display import app as display, history
    data = tmp_path / "display"
    data.mkdir()
    monkeypatch.setattr(display, "DATA_DIR", str(data))
    monkeypatch.setattr(display, "CONFIG_FILE", str(data / "config.json"))
    history.init_db(str(data / "h.db"))
    display.report.init(str(data / "h.db"))
    with open(data / "config.json", "w") as f:
        json.dump({"mic_enabled": True, "mic_interval_seconds": 0.05, "collection_url": "http://fp"}, f)
    for k in ("mic_next_ask", "mic_song_until"):
        monkeypatch.setattr(display, k, [0.0])
    monkeypatch.setattr(display, "mic_prev", {})
    monkeypatch.setattr(display, "MIN_PLAY_SECONDS", 0)
    monkeypatch.setattr(display, "fetch_online_info", lambda a, t: {"year": "", "album": "", "genre": "", "duration": None})
    clips = iter([-30] * 6 + [-30] * 4)

    def record(d):
        try:
            display.LAST_LEVEL_DB = next(clips)
        except StopIteration:
            time.sleep(5)
        time.sleep(0.01)
        return wav(0.2)
    monkeypatch.setattr(display, "record_audio", record)
    asked = {"collection": [], "shazam": 0}

    class FakeCollection:
        async def recognize(self, audio, url, token=""):
            asked["collection"].append(seconds(audio))
            if len(asked["collection"]) < 4:
                return {"matches": []}
            return collection.to_shazam(FOUND, url)

    class FakeShazam:
        async def recognize(self, audio, by="mic", fallback=None):
            asked["shazam"] += 1
            return {"matches": []}
    monkeypatch.setattr(display, "collection_client", FakeCollection())
    monkeypatch.setattr(display, "shazam", FakeShazam())

    async def run():
        t = asyncio.create_task(display.background_mic_listener())
        await asyncio.sleep(1.5)
        t.cancel()
    asyncio.run(run())
    assert asked["shazam"] == 1                       # once, then 30 s rest; the collection kept being asked
    assert len(asked["collection"]) == 4              # until it knew the song; then nobody (the song plays on)
    assert asked["collection"][0] == 0.2 and asked["collection"][1] == 0.4   # the clip before joined on
    s = display.latest_ambient_state
    assert s["title"] == "Tongue" and s["source"] == "collection" and s["duration"] == 214.0
    assert display.mic_song_until[0] - time.time() > 100
