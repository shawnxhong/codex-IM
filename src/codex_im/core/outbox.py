"""Durable delivery (design D14): persist first, deliver later, retry transient failures, never re-execute.

Per-session order is kept across scans: only the earliest pending row of a session is eligible, so a row in
backoff blocks later rows of the same session (review R5). Every text is chunked to the platform limit
before it is stored (review R10).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Mapping

from ..faults import Faults, InjectedDeliveryFailure
from ..logutil import h
from ..platforms.base import DeliveryError, EditNotPossible, PlatformAdapter
from .markup import chunk_text
from .store import Store
from .types import DEFAULT_CAPABILITIES, ChatRef, MessageRef

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 30
MAX_BACKOFF = 300.0
QUESTION_SEQ_STRIDE = 1000  # async question n, chunk k -> seq n*1000 + k (stable across live and recovery)


def backoff(attempts: int) -> float:
    return min(MAX_BACKOFF, 2.0 ** min(attempts, 9))


class Outbox:
    def __init__(self, store: Store, adapters: Mapping[str, PlatformAdapter], faults: Faults) -> None:
        self._store = store
        self._adapters = adapters
        self._faults = faults
        self._wake = asyncio.Event()
        self._stopped = False
        self._lock = asyncio.Lock()

    def notify(self) -> None:
        self._wake.set()

    def chunks(self, chat: ChatRef, text: str) -> list[str]:
        adapter = self._adapters.get(chat.platform)
        caps = adapter.capabilities if adapter is not None else DEFAULT_CAPABILITIES
        return chunk_text(text, caps.chunk_len) or [text]

    def notice(self, session_key: str, chat: ChatRef, text: str, *, reply_to: str | None = None) -> None:
        for index, chunk in enumerate(self.chunks(chat, text)):
            self._store.add_outbox(
                session_key=session_key,
                chat_json=chat.to_json(),
                kind="notice",
                text=chunk,
                seq=index,
                reply_to=reply_to if index == 0 else None,
            )
        self.notify()

    def async_question(self, session_key: str, chat: ChatRef, turn_id: int, question_index: int, text: str) -> None:
        """Idempotent per (turn, question, chunk): the live path and recovery produce the same rows."""
        for index, chunk in enumerate(self.chunks(chat, text)):
            self._store.add_outbox(
                session_key=session_key,
                chat_json=chat.to_json(),
                kind="async_question",
                text=chunk,
                turn_id=turn_id,
                seq=question_index * QUESTION_SEQ_STRIDE + index,
            )
        self.notify()

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    async def run(self) -> None:
        while not self._stopped:
            try:
                await self.deliver_due()
            except Exception:
                log.exception("outbox delivery pass failed")
            next_due = self._store.next_outbox_due()
            timeout = 30.0 if next_due is None else max(0.2, min(30.0, next_due - time.time()))
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout)
            self._wake.clear()

    async def deliver_due(self) -> None:
        async with self._lock:
            for _ in range(1000):  # bounded: each pass delivers at most one row per session
                heads = self._store.due_outbox_heads(time.time())
                if not heads:
                    return
                progressed = False
                for row in heads:
                    if await self._deliver(row):
                        progressed = True
                if not progressed:
                    return

    async def _deliver(self, row) -> bool:  # noqa: ANN001 - sqlite3.Row
        """True when the row left 'pending' (sent or dead); False while it waits for a retry."""
        outbox_id = int(row["id"])
        chat = ChatRef.from_json(row["chat_json"])
        adapter = self._adapters.get(chat.platform)
        if adapter is None:
            self._store.mark_outbox_dead(outbox_id, f"platform {chat.platform} is not configured")
            return True
        self._faults.hit("crash_before_outbox_send")
        try:
            self._faults.delivery_check()
            message_id = await self._send_or_edit(adapter, chat, row)
        except InjectedDeliveryFailure as exc:
            return self._failed(row, str(exc), transient=exc.transient, retry_after=None)
        except DeliveryError as exc:
            return self._failed(row, str(exc), transient=exc.transient, retry_after=exc.retry_after)
        except Exception as exc:
            log.exception("unexpected delivery error for outbox %d", outbox_id)
            return self._failed(row, f"{type(exc).__name__}: {exc}", transient=True, retry_after=None)
        self._store.mark_outbox_sent(outbox_id, message_id)
        log.info("delivered outbox %d (%s, %d chars) to %s", outbox_id, row["kind"], len(row["text"]), h(chat.chat_id))
        return True

    async def _send_or_edit(self, adapter: PlatformAdapter, chat: ChatRef, row) -> str | None:  # noqa: ANN001
        edit_ref = row["edit_ref_json"]
        if edit_ref and adapter.capabilities.can_edit:
            ref = MessageRef.from_json(edit_ref)
            try:
                await adapter.edit(ref, row["text"], final=True)
                return ref.message_id
            except EditNotPossible as exc:
                log.info("edit not possible for outbox %d (%s); sending a new message", int(row["id"]), exc)
            except DeliveryError as exc:
                if exc.transient:
                    raise
                log.info("edit failed permanently for outbox %d; sending a new message", int(row["id"]))
        sent = await adapter.send(chat, row["text"], reply_to=row["reply_to"], final=True)
        return sent.message_id

    def _failed(self, row, error: str, *, transient: bool, retry_after: float | None) -> bool:  # noqa: ANN001
        outbox_id = int(row["id"])
        attempts = int(row["attempts"]) + 1
        if not transient or attempts >= MAX_ATTEMPTS:
            self._store.mark_outbox_dead(outbox_id, error)
            log.warning("outbox %d is dead after %d attempt(s): %s", outbox_id, attempts, error[:200])
            return True
        delay = retry_after if retry_after and retry_after > 0 else backoff(attempts)
        self._store.mark_outbox_retry(outbox_id, attempts, time.time() + delay, error)
        log.info("outbox %d delivery failed (attempt %d), retry in %.0fs", outbox_id, attempts, delay)
        return False
