"""Text shaping: chunking that respects code fences, and Markdown -> Telegram HTML."""

from __future__ import annotations

import html
import re

_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
_CLOSER_RESERVE = 8  # room for "\n```" closing a fence that a chunk boundary cut through


def chunk_text(text: str, limit: int) -> list[str]:
    """Split ``text`` into chunks of at most ``limit`` characters.

    Splits on line boundaries; overlong lines are hard-split. A chunk boundary inside a fenced code block
    closes the fence at the end of the chunk and reopens it (same opener) at the start of the next one.
    """
    if limit < 32:
        raise ValueError("limit too small")
    if len(text) <= limit:
        return [text] if text else []
    budget = limit - _CLOSER_RESERVE
    # > budget/2 so two pieces of one hard-split line never share a chunk (that would inject a newline).
    piece_len = max(16, budget // 2 + 1)
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    fence_open: str | None = None  # opener line of the fence we are inside
    fence_marker = ""

    def flush() -> None:
        nonlocal cur, cur_len
        if not cur:
            return
        body = "\n".join(cur)
        if fence_open is not None:
            body += "\n" + fence_marker
        chunks.append(body)
        if fence_open is not None:
            cur = [fence_open]
            cur_len = len(fence_open)
        else:
            cur = []
            cur_len = 0

    for line in text.split("\n"):
        pieces = [line[i : i + piece_len] for i in range(0, len(line), piece_len)] if len(line) > piece_len else [line]
        for index, piece in enumerate(pieces):
            extra = len(piece) + (1 if cur else 0)
            if cur and cur_len + extra > budget:
                flush()
                extra = len(piece) + (1 if cur else 0)
            cur.append(piece)
            cur_len += extra
            if index == 0:
                match = _FENCE_RE.match(piece)
                if match:
                    marker = match.group(1)
                    if fence_open is None:
                        fence_open = piece.strip()
                        fence_marker = marker[:3]
                    elif marker.startswith(fence_marker[0]) and piece.strip() == marker:
                        fence_open = None
                        fence_marker = ""
    if cur:
        body = "\n".join(cur)
        if fence_open is not None:
            body += "\n" + fence_marker
        chunks.append(body)
    return [c for c in chunks if c.strip()]


# --------------------------------------------------------------------------- Telegram HTML

_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_PLACEHOLDER = "\x00{}\x00"
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")


def _inline_to_html(line: str) -> str:
    codes: list[str] = []

    def stash(match: re.Match[str]) -> str:
        codes.append(match.group(1))
        return _PLACEHOLDER.format(len(codes) - 1)

    line = _INLINE_CODE_RE.sub(stash, line)
    heading = _HEADING_RE.match(line)
    out = html.escape(heading.group(1) if heading else line, quote=False)
    out = _LINK_RE.sub(lambda m: f'<a href="{m.group(2).replace(chr(34), "%22")}">{m.group(1)}</a>', out)
    out = _BOLD_RE.sub(r"<b>\1</b>", out)
    out = _STRIKE_RE.sub(r"<s>\1</s>", out)
    out = _ITALIC_RE.sub(r"<i>\1</i>", out)
    if heading:
        out = f"<b>{out}</b>"
    return _PLACEHOLDER_RE.sub(lambda m: f"<code>{html.escape(codes[int(m.group(1))], quote=False)}</code>", out)


def md_to_telegram_html(md: str) -> str:
    """Conservative Markdown -> Telegram HTML. Callers fall back to plain text if Telegram rejects it."""
    out: list[str] = []
    in_code = False
    code_lang = ""
    code_lines: list[str] = []
    for line in md.split("\n"):
        match = _FENCE_RE.match(line)
        if match and not in_code:
            in_code = True
            code_lang = line.strip()[len(match.group(1)) :].strip().split(" ")[0]
            code_lines = []
            continue
        if match and in_code and line.strip() == match.group(1):
            out.append(_code_block(code_lines, code_lang))
            in_code = False
            continue
        if in_code:
            code_lines.append(line)
        else:
            out.append(_inline_to_html(line))
    if in_code:
        out.append(_code_block(code_lines, code_lang))
    return "\n".join(out)


def _code_block(lines: list[str], lang: str) -> str:
    body = html.escape("\n".join(lines), quote=False)
    if lang and re.fullmatch(r"[A-Za-z0-9_+#.-]{1,32}", lang):
        return f'<pre><code class="language-{lang}">{body}</code></pre>'
    return f"<pre>{body}</pre>"
