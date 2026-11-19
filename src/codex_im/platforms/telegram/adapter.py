"""Telegram via python-telegram-bot (Bot API long polling). P0a editable platform.

Polling is done by hand (not ``Application``) so the update offset is advanced only *after* the router
has durably recorded the update (D14): a crash before that makes Telegram redeliver it, and the router
deduplicates by ``update_id``.

Env (Hermes names, D11): TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USERS, TELEGRAM_ALLOW_ALL_USERS;
optional TELEGRAM_PROXY (http(s)/socks proxy URL for the Bot API).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Mapping
from typing import Any

from telegram import Bot, MessageEntity, ReplyParameters, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Conflict, Forbidden, InvalidToken, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.request import HTTPXRequest

from ...config import SettingsError
from ...core.markup import md_to_telegram_html
from ...core.store import Store
from ...core.types import Capabilities, ChatRef, InboundMessage, MessageRef
from ...logutil import h
from ..base import DeliveryError, EditNotPossible, InboundSink, InstanceLock, PlatformAdapter

log = logging.getLogger(__name__)
OFFSET_KEY = "telegram.offset"
POLL_TIMEOUT = 30


def _seconds(value: Any) -> float:
    if hasattr(value, "total_seconds"):
        return float(value.total_seconds())
    try:
        return float(value)
    except (TypeError, ValueError):
        return 5.0


def map_error(exc: TelegramError) -> DeliveryError:
    """Telegram error -> DeliveryError. Order matters: BadRequest subclasses NetworkError in PTB."""
    if isinstance(exc, RetryAfter):
        return DeliveryError(str(exc), transient=True, retry_after=_seconds(exc.retry_after))
    if isinstance(exc, (Forbidden, InvalidToken, BadRequest)):
        return DeliveryError(str(exc), transient=False)
    if isinstance(exc, (TimedOut, NetworkError)):
        return DeliveryError(str(exc), transient=True)
    return DeliveryError(str(exc), transient=True)


def _is_parse_error(exc: BadRequest) -> bool:
    text = str(exc).lower()
    return "parse" in text or "can't find end" in text or "unsupported start tag" in text


class TelegramAdapter(PlatformAdapter):
    name = "telegram"
    capabilities = Capabilities(can_edit=True, max_text_len=4096, supports_typing=True)

    def __init__(self, *, store: Store, data_dir: str, env: Mapping[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        self._token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not self._token:
            raise SettingsError("TELEGRAM_BOT_TOKEN is required for the telegram platform")
        self._proxy = (env.get("TELEGRAM_PROXY") or "").strip() or None
        self._store = store
        self._lock = InstanceLock(data_dir, "telegram", self._token)
        self._bot: Bot | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._bot_id: int | None = None

    def _require_bot(self) -> Bot:
        if self._bot is None:
            raise DeliveryError("telegram adapter is not started", transient=True)
        return self._bot

    async def start(self, sink: InboundSink) -> None:
        self._lock.acquire()
        request = HTTPXRequest(connect_timeout=10, read_timeout=20, write_timeout=20, proxy=self._proxy)
        # get_updates adds the long-poll timeout to this read timeout. VERIFY(ubuntu): V0/V2.
        updates_request = HTTPXRequest(connect_timeout=10, read_timeout=10, proxy=self._proxy)
        self._bot = Bot(self._token, request=request, get_updates_request=updates_request)
        await self._bot.initialize()
        me = self._bot.bot
        self.bot_username = me.username
        self._bot_id = me.id
        self._running = True
        self._task = asyncio.create_task(self._poll(sink), name="telegram-poll")
        log.info("telegram connected as bot %s", h(me.id))

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        if self._bot is not None:
            with contextlib.suppress(Exception):
                await self._bot.shutdown()
        self._lock.release()

    async def _poll(self, sink: InboundSink) -> None:
        raw = self._store.kv_get(OFFSET_KEY)
        offset: int | None = int(raw) if raw else None
        backoff = 1.0
        bot = self._require_bot()
        while self._running:
            try:
                updates = await bot.get_updates(offset=offset, timeout=POLL_TIMEOUT, allowed_updates=["message"])
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except InvalidToken:
                log.error("telegram rejected the bot token; polling stopped")
                return
            except Conflict:
                log.error("another process is polling this bot token (a host-side Hermes using the same bot?)")
                await asyncio.sleep(30)
                continue
            except RetryAfter as exc:
                await asyncio.sleep(_seconds(exc.retry_after))
                continue
            except TelegramError as exc:
                log.warning("telegram polling error (%s); retry in %.0fs", type(exc).__name__, backoff)
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)
                continue
            for update in updates:
                msg = self._to_inbound(update)
                if msg is not None:
                    try:
                        await sink(msg)
                    except Exception:
                        log.exception("failed to record telegram update; it will be fetched again")
                        await asyncio.sleep(5)
                        break
                offset = update.update_id + 1
                self._store.kv_set(OFFSET_KEY, str(offset))

    def _to_inbound(self, update: Update) -> InboundMessage | None:
        message = update.message
        if message is None or message.from_user is None or message.from_user.is_bot:
            return None
        text = message.text or message.caption or ""
        if not text.strip():
            return None  # P0a: text only (images are P1)
        chat = message.chat
        is_group = chat.type in ("group", "supergroup")
        topic = str(message.message_thread_id) if message.is_topic_message and message.message_thread_id else None
        mentions = not is_group
        if is_group:
            kinds = [MessageEntity.MENTION, MessageEntity.TEXT_MENTION]
            entities = message.parse_entities(kinds) if message.text else message.parse_caption_entities(kinds)
            for entity, value in entities.items():
                if (
                    entity.type == MessageEntity.MENTION
                    and self.bot_username
                    and value.lower() == "@" + self.bot_username.lower()
                ):
                    mentions = True
                    text = text.replace(value, " ").strip()
                elif entity.type == MessageEntity.TEXT_MENTION and entity.user and entity.user.id == self._bot_id:
                    mentions = True
            replied = message.reply_to_message
            if replied is not None and replied.from_user is not None and replied.from_user.id == self._bot_id:
                mentions = True
        user = message.from_user
        return InboundMessage(
            platform=self.name,
            message_key=str(update.update_id),
            chat=ChatRef(self.name, str(chat.id), topic),
            user_id=str(user.id),
            user_name=user.full_name or user.username,
            text=text,
            is_group=is_group,
            mentions_bot=mentions,
            reply_to=str(message.message_id),
        )

    # ------------------------------------------------------------------ outbound
    def _target(self, chat_id: str, topic: str | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"chat_id": int(chat_id)}
        if topic:
            kwargs["message_thread_id"] = int(topic)
        return kwargs

    async def send(self, chat: ChatRef, text: str, *, reply_to: str | None = None, final: bool = True) -> MessageRef:
        bot = self._require_bot()
        kwargs = self._target(chat.chat_id, chat.topic)
        if reply_to:
            kwargs["reply_parameters"] = ReplyParameters(message_id=int(reply_to), allow_sending_without_reply=True)
        try:
            if final:
                try:
                    sent = await bot.send_message(text=md_to_telegram_html(text), parse_mode=ParseMode.HTML, **kwargs)
                except BadRequest as exc:
                    if not _is_parse_error(exc):
                        raise
                    sent = await bot.send_message(text=text, **kwargs)
            else:
                sent = await bot.send_message(text=text, **kwargs)
        except TelegramError as exc:
            raise map_error(exc) from exc
        return MessageRef(self.name, chat.chat_id, str(sent.message_id), chat.topic)

    async def edit(self, ref: MessageRef, text: str, *, final: bool = True) -> None:
        bot = self._require_bot()
        target = {"chat_id": int(ref.chat_id), "message_id": int(ref.message_id)}
        try:
            if final:
                try:
                    await bot.edit_message_text(text=md_to_telegram_html(text), parse_mode=ParseMode.HTML, **target)
                except BadRequest as exc:
                    if not _is_parse_error(exc):
                        raise
                    await bot.edit_message_text(text=text, **target)
            else:
                await bot.edit_message_text(text=text, **target)
        except BadRequest as exc:
            lowered = str(exc).lower()
            if "message is not modified" in lowered:
                return
            if "message to edit not found" in lowered or "can't be edited" in lowered:
                raise EditNotPossible(str(exc)) from exc
            raise map_error(exc) from exc
        except TelegramError as exc:
            raise map_error(exc) from exc

    async def typing(self, chat: ChatRef) -> None:
        bot = self._require_bot()
        kwargs = self._target(chat.chat_id, chat.topic)
        with contextlib.suppress(TelegramError):
            await bot.send_chat_action(action=ChatAction.TYPING, **kwargs)
