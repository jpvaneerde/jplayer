"""Questions to Shazam: one at a time, spaced out, and a pause after a refusal."""
import asyncio
import time

from jplay.display import recognizer


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


def test_found_refused_then_waits(monkeypatch):
    monkeypatch.setattr(recognizer, "MIN_GAP", 0.05)
    fake = FakeShazam([{"matches": [], "track": {"title": "Song"}}, RuntimeError("429 Too Many Requests"),
                       {"matches": []}])
    r = recognizer.Recognizer(fake)

    async def run():
        assert (await r.recognize(b"x", "mic"))["track"]["title"] == "Song"
        assert await r.recognize(b"x", "radio") == {}            # refused
        assert await r.recognize(b"x", "mic") == {}              # waiting: Shazam isn't asked
        assert len(fake.asked) == 2 and fake.asked[1] - fake.asked[0] >= 0.05
        v = r.view()
        assert v["state"].startswith("waiting") and 55 <= v["waiting_seconds"] <= 60
        assert v["found"] == 1 and v["refused"] == 1 and v["skipped"] == 1 and "429" in v["last_error"]
        assert v["by"] == {"mic": 1, "radio": 1}
        r.paused_until = 0                                         # the wait is over
        assert await r.recognize(b"x") == {"matches": []} and r.backoff == 0
    asyncio.run(run())


def test_backoff_doubles_and_odd_answers_count_as_refusals():
    r = recognizer.Recognizer(FakeShazam([{"error": "blocked"}, asyncio.TimeoutError()]))

    async def run():
        await r.recognize(b"x")
        r.paused_until = r.last_ask = 0
        await r.recognize(b"x")
    asyncio.run(run())
    assert r.backoff == 120 and r.status["refused"] == 2 and r.status["last_error"] == "TimeoutError"
