"""Email parsing helpers: automated-mail detection, threading, body extraction and quote stripping."""

from __future__ import annotations

import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser


def parse_email(raw: bytes) -> EmailMessage:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    assert isinstance(msg, EmailMessage)
    return msg


def is_automated(msg: EmailMessage) -> str | None:
    """Reason string if the message is automated (auto-replies, lists, bulk) — dropped to avoid loops."""
    auto = str(msg.get("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        return "Auto-Submitted"
    precedence = str(msg.get("Precedence") or "").strip().lower()
    if precedence in {"bulk", "list", "junk"}:
        return "Precedence"
    for header in ("X-Auto-Response-Suppress", "List-Unsubscribe", "List-Id"):
        if msg.get(header):
            return header
    return None


_MSGID_RE = re.compile(r"<[^<>\s]+>")


def reference_ids(msg: EmailMessage) -> tuple[list[str], list[str]]:
    """-> (References ids in order, In-Reply-To ids)."""
    references = _MSGID_RE.findall(str(msg.get("References") or ""))
    in_reply_to = _MSGID_RE.findall(str(msg.get("In-Reply-To") or ""))
    return references, in_reply_to


def reply_chain(root: str, references: list[str], in_reply_to: list[str], message_id: str, limit: int = 20) -> list[str]:
    """References for our reply: the session root always first (it must survive truncation, review R8),
    then the most recent ids."""
    chain: list[str] = []
    for mid in [*references, *in_reply_to, message_id]:
        if mid != root and mid not in chain:
            chain.append(mid)
    return [root, *chain[-(limit - 1) :]]


class _HTMLText(HTMLParser):
    _BREAKS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._quote = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"style", "script", "head"}:
            self._skip += 1
        if tag == "blockquote":
            self._quote += 1
        if tag in self._BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"style", "script", "head"} and self._skip:
            self._skip -= 1
        if tag == "blockquote" and self._quote:
            self._quote -= 1
        if tag in self._BREAKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip and not self._quote:
            self.parts.append(data)


def html_to_text(html_text: str) -> str:
    parser = _HTMLText()
    parser.feed(html_text)
    parser.close()
    text = "".join(parser.parts)
    return re.sub(r"\n{3,}", "\n\n", text)


_QUOTE_START = [
    re.compile(r"^On .{1,300} wrote:\s*$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*原始邮件\s*-{2,}\s*$"),
    re.compile(r"^在.{1,300}写道[:：]\s*$"),
]
_OUTLOOK_HEADER = re.compile(r"^(From|发件人)\s*[:：]", re.IGNORECASE)
_OUTLOOK_FOLLOW = re.compile(r"^(Sent|Date|发送时间|时间|To|收件人)\s*[:：]", re.IGNORECASE)


def strip_quoted(text: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if line in ("-- ", "--"):
            break  # signature delimiter
        if any(p.match(stripped) for p in _QUOTE_START):
            break
        if _OUTLOOK_HEADER.match(stripped):
            following = [nxt.strip() for nxt in lines[index + 1 : index + 4]]
            if any(_OUTLOOK_FOLLOW.match(nxt) for nxt in following):
                break
        if stripped.startswith(">"):
            continue
        out.append(line)
    return "\n".join(out).strip()


def extract_body(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, ValueError):
        payload = part.get_payload(decode=True) or b""
        content = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)
    if not isinstance(content, str):
        return ""
    if part.get_content_type() == "text/html":
        content = html_to_text(content)
    return strip_quoted(content)
