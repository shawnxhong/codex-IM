"""Plain data types shared by the core and the platform adapters."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

NO_TOPIC = "_"


@dataclass(frozen=True, slots=True)
class SessionKey:
    """Identity of one conversation: (platform, chat, topic). See design D6."""

    platform: str
    chat_id: str
    topic: str = NO_TOPIC

    def encode(self) -> str:
        return json.dumps([self.platform, self.chat_id, self.topic], ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def decode(cls, raw: str) -> SessionKey:
        platform, chat_id, topic = json.loads(raw)
        return cls(str(platform), str(chat_id), str(topic))


@dataclass(slots=True)
class ChatRef:
    """Where to deliver messages. ``extra`` carries platform specifics (e.g. email threading headers)."""

    platform: str
    chat_id: str
    topic: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def session_key(self) -> SessionKey:
        return SessionKey(self.platform, self.chat_id, self.topic or NO_TOPIC)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> ChatRef:
        data = json.loads(raw)
        return cls(
            platform=data["platform"],
            chat_id=data["chat_id"],
            topic=data.get("topic"),
            extra=dict(data.get("extra") or {}),
        )


@dataclass(slots=True)
class MessageRef:
    """A message the bridge sent, so it can be edited later."""

    platform: str
    chat_id: str
    message_id: str
    topic: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> MessageRef:
        data = json.loads(raw)
        return cls(
            platform=data["platform"],
            chat_id=data["chat_id"],
            message_id=data["message_id"],
            topic=data.get("topic"),
        )


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a platform can render. Declared by each adapter (design D8)."""

    can_edit: bool
    max_text_len: int
    supports_typing: bool = False
    typing_interval: float = 4.0

    @property
    def chunk_len(self) -> int:
        """Budget for one chunk of source text, leaving room for markup conversion."""
        return max(64, int(self.max_text_len * 0.9))


DEFAULT_CAPABILITIES = Capabilities(can_edit=False, max_text_len=4000)


@dataclass(slots=True)
class InboundMessage:
    platform: str
    message_key: str
    chat: ChatRef
    user_id: str
    text: str
    user_name: str | None = None
    is_group: bool = False
    mentions_bot: bool = False
    reply_to: str | None = None
    received_at: float = field(default_factory=time.time)
