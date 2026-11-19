"""Sender authentication from the receiving MTA's Authentication-Results header (design D9).

``From`` can be forged, so the allowlist is only meaningful for mail whose *topmost*
Authentication-Results header comes from our trusted authserv-id and shows an aligned pass
(dmarc, or dkim/spf aligned with the From domain). Mirrors Hermes' policy (EMAIL_AUTHSERV_ID).
"""

from __future__ import annotations

import re
from email.message import Message

_METHOD_RE = re.compile(r"\s*([a-z0-9_-]+)\s*=\s*([a-z]+)", re.IGNORECASE)
_PROP_RE = re.compile(r"([a-z]+\.[a-z0-9_-]+)\s*=\s*(\"[^\"]*\"|[^\s;]+)", re.IGNORECASE)


def _strip_comments(value: str) -> str:
    """Remove RFC 5322 comments ``( ... )`` (nested), leaving quoted strings intact."""
    out: list[str] = []
    depth = 0
    in_quote = False
    escaped = False
    for ch in value:
        if escaped:
            if depth == 0:
                out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            if depth == 0:
                out.append(ch)
            continue
        if in_quote:
            out.append(ch)
            if ch == '"':
                in_quote = False
            continue
        if ch == '"' and depth == 0:
            in_quote = True
            out.append(ch)
            continue
        if ch == "(":
            depth += 1
            continue
        if ch == ")" and depth > 0:
            depth -= 1
            continue
        if depth == 0:
            out.append(ch)
    return "".join(out)


def _split_semicolons(value: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    in_quote = False
    for ch in value:
        if ch == '"':
            in_quote = not in_quote
        if ch == ";" and not in_quote:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def parse_authentication_results(value: str) -> tuple[str, list[tuple[str, str, dict[str, str]]]]:
    """-> (authserv_id, [(method, result, {property: value})])."""
    parts = _split_semicolons(_strip_comments(value))
    head = parts[0].strip().split() if parts else []
    authserv = head[0].lower() if head else ""
    results: list[tuple[str, str, dict[str, str]]] = []
    for clause in parts[1:]:
        match = _METHOD_RE.match(clause)
        if not match:
            continue
        props = {k.lower(): v.strip('"').lower() for k, v in _PROP_RE.findall(clause)}
        results.append((match.group(1).lower(), match.group(2).lower(), props))
    return authserv, results


def aligned(domain: str, from_domain: str) -> bool:
    a = domain.lower().strip(".")
    b = from_domain.lower().strip(".")
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def verify_sender(msg: Message, authserv_id: str, from_addr: str) -> tuple[bool, str]:
    headers = msg.get_all("Authentication-Results") or []
    if not headers:
        return False, "no Authentication-Results header"
    authserv, results = parse_authentication_results(str(headers[0]))
    if authserv != authserv_id.strip().lower():
        return False, "topmost Authentication-Results is not from the trusted authserv-id"
    from_domain = from_addr.rpartition("@")[2].lower()
    for method, result, props in results:
        if result != "pass":
            continue
        if method == "dmarc":
            header_from = props.get("header.from")
            if header_from is None or aligned(header_from, from_domain):
                return True, "dmarc=pass"
        elif method == "dkim":
            domain = props.get("header.d") or props.get("header.i", "").rpartition("@")[2]
            if domain and aligned(domain, from_domain):
                return True, "dkim=pass (aligned)"
        elif method == "spf":
            mailfrom = props.get("smtp.mailfrom", "")
            domain = mailfrom.rpartition("@")[2] if "@" in mailfrom else mailfrom
            if domain and aligned(domain, from_domain):
                return True, "spf=pass (aligned)"
    return False, "no aligned pass result"
