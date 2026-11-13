"""Background tasks that are started and not awaited ("fire and forget").

asyncio keeps only a weak reference to a task, so one nobody holds can be garbage-collected halfway, and its error
only shows up later as "Task exception was never retrieved". spawn() holds every task until it is done and logs
a failure straight away.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine

log = logging.getLogger(__name__)
_running: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], name: str | None = None) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _running.add(task)
    task.add_done_callback(_done)
    return task


def _done(task: asyncio.Task) -> None:
    _running.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("Background task %s failed: %s", task.get_name(), exc, exc_info=exc)
