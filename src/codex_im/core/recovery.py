"""Reconcile turns whose outcome the bridge did not observe (design D14; review R3, R6, R7, R1).

- Never re-executes anything. A completed turn's result is delivered from Codex's thread history; an
  interrupted, failed or unidentifiable turn produces a notice that offers /retry.
- A turn stays *unresolved* (and keeps its workspace hold) until history shows a terminal status, so there
  is no deadline: in-progress turns are polled with backoff; abandoned turns are re-interrupted on each
  attempt. /release is the explicit escape hatch.
- A record without a Codex turn id is matched only to a *unique* history turn with the same input that
  started after the record was created (and before the thread's next record); the match is persisted.
- Async questions found in history are re-enqueued with the same identity the live path uses.
- Steers whose outcome is unknown are resolved against the turn's history: present -> steered, absent ->
  never executed (the user is told), never replayed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..codex.errors import ErrorKind, classify
from ..codex.events import (
    agent_items_of_turn,
    format_questions,
    is_async_message,
    pick_final_text,
    started_at_of_turn,
    user_text_of_turn,
    user_texts_of_turn,
)
from ..logutil import h
from .finalize import Finalizer
from .outbox import Outbox
from .store import UNFINISHED_TURN_STATES, Store
from .tasks import spawn
from .types import ChatRef

log = logging.getLogger(__name__)
CLOCK_SKEW = 5.0  # seconds of tolerance between the bridge clock and Codex's startedAt
MAX_ID_NOT_IN_HISTORY = 5  # a known turn id missing from history: retry a few times, then unknown
MAX_STEER_ATTEMPTS = 8
TEXT_STEER_ABSENT = "⚠️ 有 {n} 条追加的消息没有送达 Codex（任务已结束），没有执行；需要的话请重新发送。"
TEXT_STEER_UNCONFIRMED = "⚠️ 有 {n} 条追加的消息无法确认是否送达 Codex，没有自动重发；请检查结果后决定是否重新发送。"
_AMBIGUOUS = object()


class Recovery:
    def __init__(self, store: Store, runtime: Any, finalizer: Finalizer, outbox: Outbox) -> None:
        self._store = store
        self._runtime = runtime
        self._finalizer = finalizer
        self._outbox = outbox
        self._pending: set[int] = set()
        self._steer_pending: set[int] = set()
        self._missing: dict[int, int] = {}
        finalizer.on_terminal.append(self._on_terminal)

    # ------------------------------------------------------------------ scheduling
    def run_startup(self) -> int:
        stale = self._store.mark_stale_commands()
        if stale:
            log.info("ignored %d command(s) left over from before the restart", stale)
        for steer in self._store.unresolved_steers():
            if steer["state"] == "sending":  # crashed around the RPC: outcome unknown
                self._store.mark_steer_unknown(int(steer["id"]))
        turns = self._store.unfinished_turns()
        for turn in turns:
            abandoned = turn["state"] == "abandoned"
            if not abandoned:
                self._store.set_turn_state(int(turn["id"]), "reconciling")
            self.schedule(int(turn["id"]), delay=0.5, interrupt=abandoned)
        for steer in self._store.unresolved_steers():
            row = self._store.get_turn(int(steer["turn_id"]))
            if row is not None and row["state"] not in UNFINISHED_TURN_STATES:
                self.schedule_steers(int(steer["turn_id"]))
        if turns:
            log.info("reconciling %d unfinished turn(s)", len(turns))
        return len(turns)

    def schedule(self, turn_id: int, delay: float = 3.0, *, interrupt: bool = False) -> None:
        if turn_id in self._pending:
            return
        self._pending.add(turn_id)
        spawn(self._loop(turn_id, delay, interrupt), name=f"reconcile-{turn_id}")

    def schedule_steers(self, turn_id: int) -> None:
        if turn_id in self._steer_pending:
            return
        self._steer_pending.add(turn_id)
        spawn(self._steer_loop(turn_id), name=f"steers-{turn_id}")

    def _on_terminal(self, turn_id: int) -> None:
        if self._store.unresolved_steers(turn_id):
            self.schedule_steers(turn_id)

    async def _loop(self, turn_id: int, delay: float, interrupt: bool) -> None:
        try:
            while True:
                await asyncio.sleep(delay)
                try:
                    if await self.reconcile(turn_id, interrupt=interrupt):
                        return
                except Exception:
                    log.exception("reconcile of turn %d failed", turn_id)
                row = self._store.get_turn(turn_id)
                if row is None or row["state"] not in UNFINISHED_TURN_STATES:
                    return  # e.g. /release
                interrupt = interrupt or row["state"] == "abandoned"
                delay = min(60.0, max(2.0, delay * 2))
        finally:
            self._pending.discard(turn_id)

    # ------------------------------------------------------------------ turns
    def _find(self, turns: list[dict[str, Any]], row: Any) -> Any:
        codex_turn_id = row["codex_turn_id"]
        if codex_turn_id:
            return next((t for t in turns if str(t.get("id")) == str(codex_turn_id)), None)
        created = float(row["created_at"])
        later = self._store.turns_of_thread_after(row["thread_id"], int(row["id"]))
        upper = float(later[0]["created_at"]) + CLOCK_SKEW if later else None
        candidates = []
        for turn in turns:
            started = started_at_of_turn(turn)
            if started is None or started < created - CLOCK_SKEW:
                continue
            if upper is not None and started > upper:
                continue
            if user_text_of_turn(turn) != row["input_text"]:
                continue
            if self._store.has_codex_turn(str(turn.get("id"))):
                continue
            candidates.append(turn)
        if len(candidates) == 1:
            return candidates[0]
        return _AMBIGUOUS if candidates else None

    async def reconcile(self, turn_id: int, *, interrupt: bool = False) -> bool:
        """Returns True when the turn reached a terminal state (or needs no work)."""
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
            if interrupt and row["codex_turn_id"]:
                try:
                    await self._runtime.interrupt_turn(thread_id, str(row["codex_turn_id"]))
                except Exception as exc:  # best effort; history decides
                    log.info("re-interrupt of turn %d failed (%s)", turn_id, type(exc).__name__)
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
        if match is _AMBIGUOUS:
            log.warning("turn %d matches several history turns; marking unknown", turn_id)
            self._finalizer.finalize(turn_id, status="unknown")
            return True
        if match is None:
            if row["codex_turn_id"]:
                misses = self._missing.get(turn_id, 0) + 1
                self._missing[turn_id] = misses
                if misses < MAX_ID_NOT_IN_HISTORY:
                    return False  # may not be persisted yet
            self._missing.pop(turn_id, None)
            self._finalizer.finalize(turn_id, status="unknown")
            return True
        if not row["codex_turn_id"]:
            self._store.bind_codex_turn(turn_id, str(match.get("id")))  # R6: persist the association
        status = str(match.get("status") or "")
        if status == "inProgress":
            return False  # still running remotely: keep the hold. VERIFY(ubuntu): V3b/V5-4
        self._recover_questions(row, session, match)
        self._resolve_steers_with(turn_id, session, match)
        agent_items = agent_items_of_turn(match)
        final = pick_final_text(agent_items)
        has_questions = any(is_async_message(i) for i in agent_items)
        error = (match.get("error") or {}).get("message") if isinstance(match.get("error"), dict) else None
        if status == "completed":
            log.info("turn %d had completed; delivering its result without re-running", turn_id)
            self._finalizer.finalize(turn_id, status="completed", final_text=final, has_questions=has_questions)
        elif status == "failed":
            self._finalizer.finalize(turn_id, status="failed", final_text=final, error=error)
        elif row["state"] == "abandoned":
            self._finalizer.finalize(turn_id, status="interrupted", final_text=final)  # the user asked to stop
        else:
            self._finalizer.finalize(turn_id, status="restart_interrupted", final_text=final)
        return True

    def _recover_questions(self, row: Any, session: Any, history_turn: dict[str, Any]) -> None:
        """R7: async questions persisted by Codex but never enqueued locally. Same identity as the live path."""
        chat = ChatRef.from_json(session["chat_json"])
        index = 0
        for item in agent_items_of_turn(history_turn):
            if not is_async_message(item):
                continue
            index += 1
            self._store.add_open_question(row["thread_id"], int(row["id"]), str(item.get("id") or ""))
            self._outbox.async_question(
                str(row["session_key"]), chat, int(row["id"]), index, "❓ " + format_questions(item)
            )

    # ------------------------------------------------------------------ steers (R1)
    def _resolve_steers_with(self, turn_id: int, session: Any, history_turn: dict[str, Any]) -> None:
        steers = self._store.unresolved_steers(turn_id)
        if not steers:
            return
        delivered = user_texts_of_turn(history_turn)[1:]  # items after the turn's own input
        absent = 0
        for steer in steers:
            if steer["text"] in delivered:
                self._store.accept_steer(int(steer["id"]))
            else:
                self._store.steer_absent(int(steer["id"]))
                absent += 1
        if absent:
            chat = ChatRef.from_json(session["chat_json"])
            self._outbox.notice(str(session["session_key"]), chat, TEXT_STEER_ABSENT.format(n=absent))

    async def _steer_loop(self, turn_id: int) -> None:
        """Resolve unknown steers of a turn that is already terminal locally."""
        delay = 1.0
        try:
            for _ in range(MAX_STEER_ATTEMPTS):
                await asyncio.sleep(delay)
                delay = min(60.0, delay * 2)
                if not self._store.unresolved_steers(turn_id):
                    return
                row = self._store.get_turn(turn_id)
                thread = self._store.get_thread(row["thread_id"]) if row is not None else None
                session = self._store.get_session(row["session_key"]) if row is not None else None
                if row is None or thread is None or session is None or not row["codex_turn_id"]:
                    break
                try:
                    await self._runtime.load_thread(
                        str(thread["thread_id"]), cwd=thread["cwd"], model=session["model"], provider=thread["provider"]
                    )
                    turns = await self._runtime.read_turns(str(thread["thread_id"]))
                except Exception as exc:
                    if classify(exc) is ErrorKind.TRANSIENT:
                        await self._runtime.reset()
                    continue
                match = next((t for t in turns if str(t.get("id")) == str(row["codex_turn_id"])), None)
                if match is None or match.get("status") == "inProgress":
                    continue
                self._resolve_steers_with(turn_id, session, match)
                return
            steers = self._store.unresolved_steers(turn_id)
            if steers:
                for steer in steers:
                    self._store.steer_unconfirmed(int(steer["id"]))
                row = self._store.get_turn(turn_id)
                session = self._store.get_session(row["session_key"]) if row is not None else None
                if session is not None:
                    chat = ChatRef.from_json(session["chat_json"])
                    self._outbox.notice(str(session["session_key"]), chat, TEXT_STEER_UNCONFIRMED.format(n=len(steers)))
        finally:
            self._steer_pending.discard(turn_id)
