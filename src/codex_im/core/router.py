"""Inbound entry point: admission -> durable insert (dedup) -> command lane or session worker (D9, D14)."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from ..config import Settings
from ..faults import Faults
from ..logutil import h
from ..platforms.base import PlatformAdapter
from .admission import Admission
from .commands import parse_command
from .sessions import SessionManager
from .store import Store
from .tasks import spawn
from .types import InboundMessage
from .workspace import Workspace

log = logging.getLogger(__name__)


class Router:
    def __init__(
        self,
        *,
        store: Store,
        sessions: SessionManager,
        admission: Admission,
        workspace: Workspace,
        settings: Settings,
        faults: Faults,
        adapters: Mapping[str, PlatformAdapter],
    ) -> None:
        self._store = store
        self._sessions = sessions
        self._admission = admission
        self._workspace = workspace
        self._settings = settings
        self._faults = faults
        self._adapters = adapters

    async def submit(self, msg: InboundMessage) -> bool:
        """Durably record ``msg``. Returning means the adapter may acknowledge it to the platform."""
        if not msg.text.strip():
            return True
        if not self._admission.allowed(msg.platform, msg.user_id):
            log.info("dropped message from non-allowlisted user %s on %s", h(msg.user_id), msg.platform)
            return True
        adapter = self._adapters.get(msg.platform)
        command = parse_command(msg.text, adapter.bot_username if adapter is not None else None)
        if msg.is_group and not msg.mentions_bot and command is None:
            return True
        key = msg.chat.session_key
        key_str = key.encode()
        self._store.upsert_session(
            key_str,
            msg.chat.to_json(),
            provider=self._settings.default_provider,
            model=self._settings.default_model,
            project=self._workspace.project_for(key),
        )
        row_id = self._store.insert_inbound(msg, kind="command" if command else "message")
        self._faults.hit("crash_after_inbound_insert")
        if row_id is None:
            log.info("duplicate inbound %s on %s ignored", h(msg.message_key), msg.platform)
            return True
        log.info(
            "inbound %s on %s (%s, %d chars) for session %s",
            h(msg.message_key),
            msg.platform,
            f"/{command[0]}" if command else "message",
            len(msg.text),
            h(key_str),
        )
        if command is not None:
            self._sessions.submit_command(key_str, row_id, msg, command[0], command[1])
        else:
            spawn(self._sessions.on_message(key_str), name=f"on-message-{h(key_str)}")
        return True
