"""Questions to Shazam: one at a time, spaced out, a pause and a slow hour after a refusal; AudD as the backup."""
import asyncio
import time

import httpx

from jplay.display import audd, recognizer


class FakeShazam:
    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    async def recognize(self, audio):
        self.asked.append(time.time())
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


SONG = {"matches": [{"offset": 12.5}], "track": {"title": "Song"}}


def test_refused_then_paused_then_slow(monkeypatch):
    monkeypatch.setattr(recognizer, "MIN_GAP", 0.05)
    fake = FakeShazam([SONG, RuntimeError("RateLimited: rate limited the request"), {"matches": []}, SONG])
    r = recognizer.Recognizer(fake)

    async def run():
        assert (await r.recognize(b"x", "mic"))["track"]["title"] == "Song"
        assert await r.recognize(b"x", "radio") == {}            # refused
        assert await r.recognize(b"x", "mic") == {}              # paused: Shazam isn't asked
        assert len(fake.asked) == 2 and fake.asked[1] - fake.asked[0] >= 0.05
        v = r.view()
        assert v["state"].startswith("waiting") and 55 <= v["waiting_seconds"] <= 60 and v["slow_minutes"] >= 60
        assert v["refused"] == 1 and v["skipped"] == 1 and "RateLimited" in v["last_error"]
        r.paused_until = r.last_ask = 0                            # the pause is over: slow now
        assert await r.recognize(b"x") == {"matches": []}           # an empty answer doesn't end the slow hour
        assert await r.recognize(b"x") == {}                        # too soon in the slow hour: skipped
        assert r.view()["state"].startswith("slow") and len(fake.asked) == 3
        r.last_ask = 0
        assert (await r.recognize(b"x"))["track"]["title"] == "Song"   # a song ends it
        assert r.view()["state"] == "ok" and r.backoff == 0
    asyncio.run(run())


def test_backoff_doubles_and_odd_answers_count_as_refusals():
    r = recognizer.Recognizer(FakeShazam([{"error": "blocked"}, asyncio.TimeoutError()]))

    async def run():
        await r.recognize(b"x")
        r.paused_until = r.slow_until = r.last_ask = 0
        await r.recognize(b"x")
    asyncio.run(run())
    assert r.backoff == 120 and r.status["refused"] == 2 and r.status["last_error"] == "TimeoutError"


def test_backup_only_while_shazam_refuses():
    asked = []

    async def backup(audio):
        asked.append(audio)
        return SONG
    r = recognizer.Recognizer(FakeShazam([{"matches": []}, RuntimeError("RateLimited")]))

    async def run():
        assert await r.recognize(b"a", fallback=backup) == {"matches": []}   # Shazam answered: no backup
        r.last_ask = 0
        assert (await r.recognize(b"b", fallback=backup))["track"]["title"] == "Song"   # refused: backup
        assert (await r.recognize(b"c", fallback=backup))["track"]["title"] == "Song"   # paused: backup
    asyncio.run(run())
    assert asked == [b"b", b"c"] and r.status["backup_used"] == 2


AUDD_SONG = {"status": "success", "result": {
    "artist": "Interpol", "title": "If You Really Love Nothing", "album": "Marauder", "release_date": "2018-08-24",
    "timecode": "01:23", "apple_music": {"artwork": {"url": "https://img/{w}x{h}bb.jpg"}, "isrc": "GBCEL1800361",
                                         "genreNames": ["Alternative"]}}}


def _client(answer, seen):
    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=answer)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_audd_answer_in_shazam_shape():
    a, seen = audd.AudD(), []

    async def run():
        async with _client(AUDD_SONG, seen) as c:
            return await a.recognize(b"wav", "tok", c)
    out = asyncio.run(run())
    t = out["track"]
    assert t["title"] == "If You Really Love Nothing" and t["subtitle"] == "Interpol" and t["isrc"] == "GBCEL1800361"
    assert t["images"]["coverart"] == "https://img/600x600bb.jpg" and t["genres"]["primary"] == "Alternative"
    assert {"title": "Album", "text": "Marauder"} in t["sections"][0]["metadata"]
    assert out["matches"][0]["offset"] == 83.0
    assert b"tok" in seen[0].content and a.view()["found"] == 1


def test_audd_spaced_out_capped_and_errors_kept(monkeypatch):
    a, seen = audd.AudD(), []

    async def ask(answer):
        async with _client(answer, seen) as c:
            return await a.recognize(b"wav", "tok", c)
    assert asyncio.run(ask({"status": "success", "result": None})) == {"matches": []}
    assert asyncio.run(ask(AUDD_SONG)) == {} and len(seen) == 1         # within MIN_GAP: not asked
    a.last_ask = 0
    assert asyncio.run(ask({"status": "error", "error": {"error_code": 901, "error_message": "no api_token"}})) == {}
    assert "901" in a.view()["last_error"]
    a.last_ask, a.today = 0, audd.DAILY_MAX
    assert asyncio.run(ask(AUDD_SONG)) == {} and len(seen) == 2          # the day's limit reached
    assert asyncio.run(audd.AudD().recognize(b"wav", "")) == {}           # no key: never asked
