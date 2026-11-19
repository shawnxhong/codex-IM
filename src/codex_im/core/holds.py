"""Workspace holds (design D6, D14; review R3).

A turn holds its cwd from before ``turn/start`` until its *remote* outcome is confirmed terminal — not merely
until the local consumer stops. Holding the cwd (instead of a per-project lock) covers every case at once:

- sessions bound to a ``serial`` project share the project path -> one turn at a time across sessions;
- a session's own directory -> a new turn (or a /new thread) waits while an earlier turn may still run there;
- ``worktree`` projects -> each session has its own path, so sessions run in parallel.

Holds of unresolved turns are restored from the database at startup, before any queued work runs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from typing import Any

log = logging.getLogger(__name__)


class Reservation:
    """Placeholder owner between acquiring a hold and creating the turn record."""

    __slots__ = ()


class WorkspaceHolds:
    def __init__(self) -> None:
        self._owners: dict[str, Any] = {}  # cwd -> turn id (int) or Reservation
        self._changed = asyncio.Event()

    def restore(self, rows: Iterable[Any]) -> int:
        count = 0
        for row in rows:
            cwd = row["hold_cwd"]
            if cwd:
                self._owners[str(cwd)] = int(row["id"])
                count += 1
        return count

    def owner(self, cwd: str) -> Any:
        return self._owners.get(cwd)

    def is_held(self, cwd: str) -> bool:
        return cwd in self._owners

    async def acquire(self, cwd: str, cancelled: Callable[[], bool], poll: float = 0.5) -> Reservation | None:
        """Wait until ``cwd`` is free and reserve it. Returns None if ``cancelled()`` became true first."""
        while cwd in self._owners:
            if cancelled():
                return None
            changed = self._changed
            try:
                await asyncio.wait_for(changed.wait(), poll)
            except asyncio.TimeoutError:
                pass
        if cancelled():
            return None
        token = Reservation()
        self._owners[cwd] = token
        return token

    def bind(self, cwd: str, token: Reservation, turn_id: int) -> None:
        if self._owners.get(cwd) is token:
            self._owners[cwd] = turn_id

    def cancel(self, cwd: str, token: Reservation) -> None:
        if self._owners.get(cwd) is token:
            del self._owners[cwd]
            self._notify()

    def release_turn(self, turn_id: int) -> None:
        released = [cwd for cwd, owner in self._owners.items() if owner == turn_id and not isinstance(owner, Reservation)]
        for cwd in released:
            del self._owners[cwd]
        if released:
            log.info("released workspace hold of turn %d", turn_id)
            self._notify()

    def _notify(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()
