"""Fire-and-forget tasks: held until done, and a failure is logged at once."""
import asyncio
import logging

from jplay.common import tasks


async def test_spawn_holds_the_task_and_logs_a_failure(caplog):
    async def fine():
        await asyncio.sleep(0)
        return 1

    async def broken():
        raise ValueError("boom")

    t = tasks.spawn(fine(), "fine")
    assert t in tasks._running
    assert await t == 1
    await asyncio.sleep(0)
    assert t not in tasks._running

    with caplog.at_level(logging.ERROR):
        b = tasks.spawn(broken(), "broken")
        await asyncio.gather(b, return_exceptions=True)
        await asyncio.sleep(0)
    assert "broken" in caplog.text and "boom" in caplog.text
    assert b not in tasks._running
