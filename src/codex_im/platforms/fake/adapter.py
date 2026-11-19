"""In-memory platform for automated acceptance tests (docs/verification.md).

HTTP control API (local only, default 127.0.0.1:8787):
  POST /inbound   {"chat_id", "user_id", "text", "message_key"?, "topic"?, "is_group"?, "mentions_bot"?, "user_name"?}
  GET  /outbound  -> {"events": [...]}  (send / edit / typing, in order)
  POST /control   {"can_edit"?: bool, "fail_next"?: int, "fail_permanent"?: bool}
  POST /reset     clears the outbound log
"""

from __future__ import annotations

import itertools
import logging
import os
import time
from collections.abc import Mapping
from typing import Any

from ...config import truthy
from ...core.types import Capabilities, ChatRef, InboundMessage, MessageRef
from ..base import DeliveryError, EditNotPossible, InboundSink, PlatformAdapter

log = logging.getLogger(__name__)


class FakeAdapter(PlatformAdapter):
    name = "fake"

    def __init__(self, *, host: str = "127.0.0.1", port: int = 8787, can_edit: bool = True, serve: bool = True) -> None:
        self.capabilities = Capabilities(can_edit=can_edit, max_text_len=4000, supports_typing=not can_edit)
        self.bot_username = "fakebot"
        self.events: list[dict[str, Any]] = []
        self._ids = itertools.count(1)
        self._keys = itertools.count(1)
        self._fail_transient = 0
        self._fail_permanent = False
        self._sink: InboundSink | None = None
        self._host, self._port, self._serve = host, port, serve
        self._runner: Any = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FakeAdapter:
        env = os.environ if env is None else env
        return cls(
            host=env.get("FAKE_HOST", "127.0.0.1"),
            port=int(env.get("FAKE_PORT", "8787")),
            can_edit=truthy(env.get("FAKE_CAN_EDIT", "true")),
        )

    def set_can_edit(self, can_edit: bool) -> None:
        self.capabilities = Capabilities(can_edit=can_edit, max_text_len=4000, supports_typing=not can_edit)

    def fail_next(self, count: int = 1, *, permanent: bool = False) -> None:
        self._fail_transient = count
        self._fail_permanent = permanent

    # ------------------------------------------------------------------ adapter API
    async def start(self, sink: InboundSink) -> None:
        self._sink = sink
        if self._serve:
            await self._start_http()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def inject(
        self,
        *,
        chat_id: str,
        user_id: str,
        text: str,
        message_key: str | None = None,
        topic: str | None = None,
        is_group: bool = False,
        mentions_bot: bool = True,
        user_name: str | None = None,
    ) -> bool:
        if self._sink is None:
            raise RuntimeError("adapter not started")
        msg = InboundMessage(
            platform=self.name,
            message_key=message_key or f"fake-{next(self._keys)}-{time.time_ns()}",
            chat=ChatRef(self.name, str(chat_id), topic),
            user_id=str(user_id),
            text=text,
            user_name=user_name,
            is_group=is_group,
            mentions_bot=mentions_bot,
            reply_to=None,
        )
        return await self._sink(msg)

    def _maybe_fail(self) -> None:
        if self._fail_permanent:
            raise DeliveryError("fake permanent failure", transient=False)
        if self._fail_transient > 0:
            self._fail_transient -= 1
            raise DeliveryError("fake transient failure", transient=True)

    async def send(self, chat: ChatRef, text: str, *, reply_to: str | None = None, final: bool = True) -> MessageRef:
        self._maybe_fail()
        message_id = str(next(self._ids))
        self.events.append(
            {
                "op": "send",
                "chat_id": chat.chat_id,
                "topic": chat.topic,
                "message_id": message_id,
                "text": text,
                "final": final,
                "reply_to": reply_to,
            }
        )
        return MessageRef(self.name, chat.chat_id, message_id, chat.topic)

    async def edit(self, ref: MessageRef, text: str, *, final: bool = True) -> None:
        if not self.capabilities.can_edit:
            raise EditNotPossible("editing disabled")
        self._maybe_fail()
        self.events.append(
            {"op": "edit", "chat_id": ref.chat_id, "topic": ref.topic, "message_id": ref.message_id, "text": text, "final": final}
        )

    async def typing(self, chat: ChatRef) -> None:
        self.events.append({"op": "typing", "chat_id": chat.chat_id, "topic": chat.topic})

    # ------------------------------------------------------------------ HTTP control API
    async def _start_http(self) -> None:
        from aiohttp import web

        async def inbound(request: web.Request) -> web.Response:
            body = await request.json()
            accepted = await self.inject(
                chat_id=str(body["chat_id"]),
                user_id=str(body["user_id"]),
                text=str(body["text"]),
                message_key=body.get("message_key"),
                topic=body.get("topic"),
                is_group=bool(body.get("is_group", False)),
                mentions_bot=bool(body.get("mentions_bot", True)),
                user_name=body.get("user_name"),
            )
            return web.json_response({"accepted": accepted})

        async def outbound(_request: web.Request) -> web.Response:
            return web.json_response({"events": self.events})

        async def control(request: web.Request) -> web.Response:
            body = await request.json()
            if "can_edit" in body:
                self.set_can_edit(bool(body["can_edit"]))
            if "fail_next" in body or "fail_permanent" in body:
                self.fail_next(int(body.get("fail_next", 0)), permanent=bool(body.get("fail_permanent", False)))
            return web.json_response({"ok": True, "can_edit": self.capabilities.can_edit})

        async def reset(_request: web.Request) -> web.Response:
            self.events.clear()
            return web.json_response({"ok": True})

        app = web.Application()
        app.add_routes(
            [
                web.post("/inbound", inbound),
                web.get("/outbound", outbound),
                web.post("/control", control),
                web.post("/reset", reset),
            ]
        )
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self._host, self._port).start()
        log.info("fake platform listening on %s:%d", self._host, self._port)
