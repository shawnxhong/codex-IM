"""Background task helper: keeps strong references and logs failures instead of losing them."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger(__name__)
_tasks: set[asyncio.Task[Any]] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task[Any]:
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_done)
    return task


def _done(task: asyncio.Task[Any]) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("background task %s failed", task.get_name(), exc_info=exc)


async def cancel_all() -> None:
    pending = [t for t in _tasks if not t.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
