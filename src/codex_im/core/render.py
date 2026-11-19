"""Live rendering of a running turn (design D8).

Drafts are best-effort and never go through the outbox: on editable platforms a placeholder message is
edited at most every ``interval`` seconds with the accumulated agent text (append-only, plain text). The
final text is delivered through the outbox by editing that placeholder (D14). Non-editable platforms get
only a typing indicator while the turn runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from ..platforms.base import DeliveryError, PlatformAdapter
from .types import ChatRef, MessageRef

log = logging.getLogger(__name__)
PLACEHOLDER = "⏳ 处理中…"
_ELLIPSIS = "…"


class StreamRenderer:
    def __init__(self, adapter: PlatformAdapter, chat: ChatRef, reply_to: str | None, interval: float) -> None:
        self.adapter = adapter
        self.chat = chat
        self.reply_to = reply_to
        self.interval = interval
        self.caps = adapter.capabilities
        self.stream_ref: MessageRef | None = None
        self._items: dict[str, str] = {}
        self._dirty = False
        self._frozen = False
        self._last_sent = ""
        self._backoff_until = 0.0
        self._last_typing = 0.0
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        if self.caps.can_edit:
            try:
                self.stream_ref = await self.adapter.send(self.chat, PLACEHOLDER, reply_to=self.reply_to, final=False)
                self._last_sent = PLACEHOLDER
            except DeliveryError as exc:
                log.info("placeholder send failed (%s); final will be sent as a new message", type(exc).__name__)
        elif self.caps.supports_typing:
            await self._typing()
        self._task = asyncio.create_task(self._loop(), name="stream-renderer")

    def on_delta(self, item_id: str, delta: str) -> None:
        if not delta:
            return
        self._items[item_id] = self._items.get(item_id, "") + delta
        self._dirty = True

    def on_agent_message(self, item_id: str, text: str) -> None:
        self._items[item_id] = text
        self._dirty = True

    def drop(self, item_id: str) -> None:
        """Remove an item delivered separately (async questions)."""
        if self._items.pop(item_id, None) is not None:
            self._dirty = True

    def draft(self) -> str:
        return "\n\n".join(t.strip() for t in self._items.values() if t.strip())

    async def _typing(self) -> None:
        self._last_typing = time.monotonic()
        with contextlib.suppress(Exception):
            await self.adapter.typing(self.chat)

    async def _loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self.interval)
                now = time.monotonic()
                if self.caps.can_edit and self.stream_ref is not None:
                    if self._dirty and not self._frozen and now >= self._backoff_until:
                        await self._push_draft()
                elif self.caps.supports_typing and now - self._last_typing >= self.caps.typing_interval:
                    await self._typing()
        except asyncio.CancelledError:
            pass

    async def _push_draft(self) -> None:
        self._dirty = False
        text = self.draft()
        if not text:
            return
        limit = self.caps.max_text_len - 8
        if len(text) > limit:
            # Freeze instead of rewriting earlier content: the final edit delivers the full text in chunks.
            text = text[:limit] + _ELLIPSIS
            self._frozen = True
        if text == self._last_sent or self.stream_ref is None:
            return
        try:
            await self.adapter.edit(self.stream_ref, text, final=False)
            self._last_sent = text
        except DeliveryError as exc:
            if exc.retry_after:
                self._backoff_until = time.monotonic() + exc.retry_after
            log.debug("draft edit failed: %s", type(exc).__name__)

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
