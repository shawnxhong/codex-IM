"""Email via IMAP polling + SMTP. P0a final-only platform (cannot edit).

- A session is one email thread (D6). Every received and sent Message-ID is mapped to the session root, and
  the root of a new mail is found through its nearest known parent (review R8).
- Messages are marked \\Seen only after the router has durably recorded them (D14); redelivery is
  deduplicated by Message-ID.
- Sender authentication via Authentication-Results (D9); automated mail is dropped.

Env (Hermes names, D11): EMAIL_ADDRESS, EMAIL_PASSWORD, EMAIL_IMAP_HOST, EMAIL_SMTP_HOST,
EMAIL_ALLOWED_USERS, EMAIL_AUTHSERV_ID; optional EMAIL_IMAP_PORT (993), EMAIL_SMTP_PORT (587),
EMAIL_SMTP_SECURITY (starttls|ssl), EMAIL_MAILBOX (INBOX), EMAIL_POLL_INTERVAL (30),
EMAIL_TRUST_FROM_HEADER (dev only: skip sender authentication).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import imaplib
import logging
import os
import smtplib
import ssl
from collections.abc import Mapping
from typing import Any
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr

from ...config import SettingsError, truthy
from ...core.types import Capabilities, ChatRef, InboundMessage, MessageRef
from ...logutil import h
from ..base import DeliveryError, InboundSink, InstanceLock, PlatformAdapter
from .auth import verify_sender
from .parse import extract_body, is_automated, parse_email, reference_ids, reply_chain

log = logging.getLogger(__name__)
MAX_PER_POLL = 50
ROOT_KEY = "email.root:"
IO_TIMEOUT = 60


class EmailAdapter(PlatformAdapter):
    name = "email"
    capabilities = Capabilities(can_edit=False, max_text_len=50_000, supports_typing=False)

    def __init__(self, *, data_dir: str, store: Any = None, env: Mapping[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        self._store = store

        def need(key: str) -> str:
            value = (env.get(key) or "").strip()
            if not value:
                raise SettingsError(f"{key} is required for the email platform")
            return value

        self._address = need("EMAIL_ADDRESS")
        self._password = env.get("EMAIL_PASSWORD") or ""
        if not self._password:
            raise SettingsError("EMAIL_PASSWORD is required for the email platform")
        self._imap_host = need("EMAIL_IMAP_HOST")
        self._smtp_host = need("EMAIL_SMTP_HOST")
        self._imap_port = int(env.get("EMAIL_IMAP_PORT") or 993)
        self._smtp_port = int(env.get("EMAIL_SMTP_PORT") or 587)
        self._smtp_security = (env.get("EMAIL_SMTP_SECURITY") or ("ssl" if self._smtp_port == 465 else "starttls")).lower()
        if self._smtp_security not in {"ssl", "starttls"}:
            raise SettingsError("EMAIL_SMTP_SECURITY must be 'ssl' or 'starttls'")
        self._mailbox = env.get("EMAIL_MAILBOX") or "INBOX"
        self._poll_interval = max(5.0, float(env.get("EMAIL_POLL_INTERVAL") or 30))
        self._trust_from = truthy(env.get("EMAIL_TRUST_FROM_HEADER"))
        self._authserv_id = (env.get("EMAIL_AUTHSERV_ID") or "").strip()
        if not self._authserv_id and not self._trust_from:
            raise SettingsError("EMAIL_AUTHSERV_ID is required (or EMAIL_TRUST_FROM_HEADER=true, dev only)")
        if self._trust_from:
            log.warning("EMAIL_TRUST_FROM_HEADER=true: sender authentication is DISABLED (dev only)")
        self._lock = InstanceLock(data_dir, "email", f"{self._address}@{self._imap_host}")
        self._task: asyncio.Task[None] | None = None
        self._running = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self, sink: InboundSink) -> None:
        self._lock.acquire()
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(sink), name="email-poll")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._lock.release()

    async def _poll_loop(self, sink: InboundSink) -> None:
        backoff = self._poll_interval
        while self._running:
            try:
                await self.poll_once(sink)
                backoff = self._poll_interval
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("email poll failed (%s); retry in %.0fs", type(exc).__name__, backoff)
                backoff = min(600.0, backoff * 2)
            await asyncio.sleep(backoff)

    async def poll_once(self, sink: InboundSink) -> None:
        fetched = await asyncio.to_thread(self._fetch_unseen)
        done: list[str] = []
        try:
            for uid, raw in fetched:
                try:
                    msg = self.to_inbound(raw)
                except Exception:
                    log.exception("unparsable email uid=%s; marking seen", uid)
                    done.append(uid)
                    continue
                if msg is not None:
                    await sink(msg)  # raises -> stop here; unrecorded mails stay unseen
                done.append(uid)
        finally:
            if done:
                await asyncio.to_thread(self._mark_seen, done)

    # ------------------------------------------------------------------ IMAP (blocking, run in threads)
    def _imap(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(
            self._imap_host, self._imap_port, ssl_context=ssl.create_default_context(), timeout=IO_TIMEOUT
        )
        conn.login(self._address, self._password)
        typ, _ = conn.select(self._mailbox)
        if typ != "OK":
            conn.logout()
            raise RuntimeError(f"cannot select mailbox {self._mailbox!r}")
        return conn

    def _fetch_unseen(self) -> list[tuple[str, bytes]]:
        conn = self._imap()
        try:
            typ, data = conn.uid("SEARCH", None, "UNSEEN")
            if typ != "OK":
                raise RuntimeError("IMAP SEARCH failed")
            uids = (data[0] or b"").split()[:MAX_PER_POLL]
            result: list[tuple[str, bytes]] = []
            for uid in uids:
                typ, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                if typ != "OK" or not parts:
                    continue
                raw = next((p[1] for p in parts if isinstance(p, tuple) and len(p) >= 2), None)
                if isinstance(raw, bytes):
                    result.append((uid.decode("ascii"), raw))
            return result
        finally:
            with contextlib.suppress(Exception):
                conn.logout()

    def _mark_seen(self, uids: list[str]) -> None:
        conn = self._imap()
        try:
            for uid in uids:
                conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
        finally:
            with contextlib.suppress(Exception):
                conn.logout()

    # ------------------------------------------------------------------ parsing
    def to_inbound(self, raw: bytes) -> InboundMessage | None:
        msg = parse_email(raw)
        from_name, from_addr = parseaddr(str(msg.get("From") or ""))
        from_addr = from_addr.strip().lower()
        if not from_addr or "@" not in from_addr:
            return None
        if from_addr == self._address.lower():
            return None
        reason = is_automated(msg)
        if reason:
            log.info("dropped automated email from %s (%s)", h(from_addr), reason)
            return None
        if not self._trust_from:
            ok, why = verify_sender(msg, self._authserv_id, from_addr)
            if not ok:
                log.info("dropped unauthenticated email from %s: %s", h(from_addr), why)
                return None
        message_id = str(msg.get("Message-ID") or "").strip()
        if not message_id:
            message_id = f"<synthetic-{hashlib.sha256(raw).hexdigest()[:24]}@codex-im>"
        references, in_reply_to = reference_ids(msg)
        root = self.resolve_root(message_id, references, in_reply_to)
        subject = str(msg.get("Subject") or "").strip()
        body = extract_body(msg) or subject
        if not body.strip():
            return None
        self._remember(message_id, root)
        chain = reply_chain(root, references, in_reply_to, message_id)
        chat = ChatRef(
            self.name,
            root,
            None,
            {"to": from_addr, "subject": subject, "in_reply_to": message_id, "references": " ".join(chain)},
        )
        return InboundMessage(
            platform=self.name,
            message_key=message_id,
            chat=chat,
            user_id=from_addr,
            user_name=from_name or from_addr,
            text=body,
            is_group=False,
            mentions_bot=True,
            reply_to=message_id,
        )

    def resolve_root(self, message_id: str, references: list[str], in_reply_to: list[str]) -> str:
        """Session root of a mail: the root of its nearest known ancestor, else the oldest reference."""
        if self._store is not None:
            for mid in [*in_reply_to, *reversed(references)]:
                known = self._store.kv_get(ROOT_KEY + mid)
                if known:
                    return known
            own = self._store.kv_get(ROOT_KEY + message_id)
            if own:
                return own
        if references:
            return references[0]
        if in_reply_to:
            return in_reply_to[0]
        return message_id

    def _remember(self, message_id: str, root: str) -> None:
        if self._store is not None:
            self._store.kv_set_if_absent(ROOT_KEY + message_id, root)

    # ------------------------------------------------------------------ outbound
    def build_reply(self, chat: ChatRef, text: str) -> EmailMessage:
        to = str(chat.extra.get("to") or "")
        if not to:
            raise DeliveryError("email chat has no recipient", transient=False)
        msg = EmailMessage()
        msg["From"] = self._address
        msg["To"] = to
        subject = str(chat.extra.get("subject") or "codex-IM")
        msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        if chat.extra.get("in_reply_to"):
            msg["In-Reply-To"] = str(chat.extra["in_reply_to"])
        if chat.extra.get("references"):
            msg["References"] = str(chat.extra["references"])
        msg["Message-ID"] = make_msgid(domain=self._address.rpartition("@")[2] or None)
        msg["Auto-Submitted"] = "auto-replied"  # RFC 3834; other bots will not answer us in a loop
        msg.set_content(text)
        return msg

    def _smtp_send(self, msg: EmailMessage) -> None:
        context = ssl.create_default_context()
        if self._smtp_security == "ssl":
            with smtplib.SMTP_SSL(self._smtp_host, self._smtp_port, context=context, timeout=IO_TIMEOUT) as smtp:
                smtp.login(self._address, self._password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=IO_TIMEOUT) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                smtp.login(self._address, self._password)
                smtp.send_message(msg)

    async def send(self, chat: ChatRef, text: str, *, reply_to: str | None = None, final: bool = True) -> MessageRef:
        msg = self.build_reply(chat, text)
        try:
            await asyncio.to_thread(self._smtp_send, msg)
        except smtplib.SMTPRecipientsRefused as exc:
            raise DeliveryError("recipient refused", transient=False) from exc
        except smtplib.SMTPResponseException as exc:
            raise DeliveryError(f"SMTP {exc.smtp_code}", transient=not 500 <= exc.smtp_code < 600) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise DeliveryError(type(exc).__name__, transient=True) from exc
        self._remember(str(msg["Message-ID"]), chat.chat_id)  # replies to our reply stay in this session
        return MessageRef(self.name, chat.chat_id, str(msg["Message-ID"]))
