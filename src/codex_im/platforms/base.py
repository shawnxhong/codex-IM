"""Platform adapter contract (design §3). Adapters only translate protocols; the core owns admission,
commands, sessions, rendering and delivery."""

from __future__ import annotations

import hashlib
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from ..core.types import Capabilities, ChatRef, InboundMessage, MessageRef

log = logging.getLogger(__name__)

# The sink durably records the message (dedup included) and returns once it is safe to ack the platform.
InboundSink = Callable[[InboundMessage], Awaitable[bool]]


class DeliveryError(Exception):
    """Raised by send/edit. ``transient`` failures are retried by the outbox; permanent ones are not."""

    def __init__(self, message: str, *, transient: bool, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.transient = transient
        self.retry_after = retry_after


class EditNotPossible(DeliveryError):
    """The message cannot be edited (too old, deleted...). The outbox falls back to a new message."""

    def __init__(self, message: str) -> None:
        super().__init__(message, transient=False)


class PlatformAdapter(ABC):
    name: str = ""
    capabilities: Capabilities
    bot_username: str | None = None

    @abstractmethod
    async def start(self, sink: InboundSink) -> None:
        """Connect and start receiving. Must return promptly (run loops in background tasks)."""

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, chat: ChatRef, text: str, *, reply_to: str | None = None, final: bool = True) -> MessageRef:
        """Send a message. ``final=False`` means a streaming draft: send plain text, no markup conversion."""

    async def edit(self, ref: MessageRef, text: str, *, final: bool = True) -> None:
        raise EditNotPossible(f"{self.name} does not support editing")

    async def typing(self, chat: ChatRef) -> None:  # noqa: B027 - optional hook
        return None


class InstanceLock:
    """One running instance per credential (D9). Uses flock on Linux; no-op elsewhere."""

    def __init__(self, data_dir: str, platform: str, credential: str) -> None:
        digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()[:16]
        self._path = os.path.join(data_dir, "locks", f"{platform}-{digest}.lock")
        self._fh: Any = None

    def acquire(self) -> None:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - not Linux
            return
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        fh = open(self._path, "w", encoding="utf-8")  # noqa: SIM115 - held for the process lifetime
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise RuntimeError(f"another codex-IM instance already uses this credential ({self._path})") from exc
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
