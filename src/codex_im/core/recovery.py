"""Reconcile turns whose outcome the bridge did not observe (design D14).

Never re-executes anything: a completed turn's result is delivered from Codex's thread history; an
interrupted, failed or unidentifiable turn produces a notice that offers /retry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..codex.errors import ErrorKind, classify
from ..codex.events import agent_items_of_turn, pick_final_text, user_text_of_turn
from ..logutil import h
from .finalize import Finalizer
from .store import UNFINISHED_TURN_STATES, Store
from .tasks import spawn

log = logging.getLogger(__name__)
RECONCILE_DEADLINE = 600.0  # seconds before an in-progress / unreachable turn is declared unknown


class Recovery:
    def __init__(self, store: Store, runtime: Any, finalizer: Finalizer) -> None:
        self._store = store
        self._runtime = runtime
        self._finalizer = finalizer
        self._pending: set[int] = set()

    def run_startup(self) -> int:
        stale = self._store.mark_stale_commands()
        if stale:
            log.info("ignored %d command(s) left over from before the restart", stale)
        turns = self._store.unfinished_turns()
        for turn in turns:
            self._store.set_turn_state(int(turn["id"]), "reconciling")
            self.schedule(int(turn["id"]), delay=0.5)
        if turns:
            log.info("reconciling %d unfinished turn(s)", len(turns))
        return len(turns)

    def schedule(self, turn_id: int, delay: float = 3.0) -> None:
        if turn_id in self._pending:
            return
        self._pending.add(turn_id)
        spawn(self._loop(turn_id, delay), name=f"reconcile-{turn_id}")

    async def _loop(self, turn_id: int, delay: float) -> None:
        deadline = time.monotonic() + RECONCILE_DEADLINE
        try:
            while True:
                await asyncio.sleep(delay)
                try:
                    if await self.reconcile(turn_id):
                        return
                except Exception:
                    log.exception("reconcile of turn %d failed", turn_id)
                if time.monotonic() > deadline:
                    row = self._store.get_turn(turn_id)
                    if row is not None and row["state"] in UNFINISHED_TURN_STATES:
                        log.warning("turn %d could not be reconciled in time; marking unknown", turn_id)
                        self._finalizer.finalize(turn_id, status="unknown")
                    return
                delay = min(60.0, max(2.0, delay * 2))
        finally:
            self._pending.discard(turn_id)

    def _find(self, turns: list[dict[str, Any]], row: Any) -> dict[str, Any] | None:
        codex_turn_id = row["codex_turn_id"]
        if codex_turn_id:
            return next((t for t in turns if str(t.get("id")) == str(codex_turn_id)), None)
        # Crash between turn/start and its response: match by exact input text, newest first, skipping
        # Codex turns already bound to another local turn record.
        for turn in reversed(turns):
            if user_text_of_turn(turn) != row["input_text"]:
                continue
            if self._store.has_codex_turn(str(turn.get("id"))):
                continue
            return turn
        return None

    async def reconcile(self, turn_id: int) -> bool:
        """Returns True when the turn reached a final state (or needs no work)."""
        row = self._store.get_turn(turn_id)
        if row is None or row["state"] not in UNFINISHED_TURN_STATES:
            return True
        thread = self._store.get_thread(row["thread_id"])
        session = self._store.get_session(row["session_key"])
        if thread is None or session is None:
            self._finalizer.finalize(turn_id, status="unknown")
            return True
        thread_id = str(thread["thread_id"])
        try:
            await self._runtime.load_thread(thread_id, cwd=thread["cwd"], model=session["model"], provider=thread["provider"])
            turns = await self._runtime.read_turns(thread_id)
        except Exception as exc:
            kind = classify(exc)
            if kind is ErrorKind.NOT_FOUND:
                log.warning("thread %s of turn %d no longer exists", h(thread_id), turn_id)
                if thread["state"] != "created" or int(thread["turn_count"]) > 0:
                    self._store.set_thread_state(thread_id, "lost")
                self._finalizer.finalize(turn_id, status="unknown")
                return True
            if kind is ErrorKind.TRANSIENT:
                await self._runtime.reset()
            log.info("reconcile of turn %d deferred (%s)", turn_id, kind.value)
            return False
        match = self._find(turns, row)
        if match is None:
            self._finalizer.finalize(turn_id, status="unknown")
            return True
        status = str(match.get("status") or "")
        if status == "inProgress":
            return False  # VERIFY(ubuntu): V3b — can an old connection's turn still be running after reconnect?
        final = pick_final_text(agent_items_of_turn(match))
        if status == "completed":
            log.info("turn %d had completed; delivering its result without re-running", turn_id)
            self._finalizer.finalize(turn_id, status="completed", final_text=final)
        else:
            self._finalizer.finalize(turn_id, status="restart_interrupted", final_text=final)
        return True
