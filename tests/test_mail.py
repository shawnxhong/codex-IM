"""Email platform: sender authentication, automated-mail filtering, threading, quoting (D6, D9)."""

from __future__ import annotations

import pytest

from codex_im.config import SettingsError
from codex_im.platforms.mail.adapter import EmailAdapter
from codex_im.platforms.mail.auth import parse_authentication_results, verify_sender
from codex_im.platforms.mail.parse import parse_email, strip_quoted

ENV = {
    "EMAIL_ADDRESS": "bot@example.org",
    "EMAIL_PASSWORD": "pw",
    "EMAIL_IMAP_HOST": "imap.example.org",
    "EMAIL_SMTP_HOST": "smtp.example.org",
    "EMAIL_AUTHSERV_ID": "mx.example.org",
}


def raw_mail(
    *,
    sender: str = "Alice <alice@corp.com>",
    auth: str | None = "mx.example.org; dkim=pass header.d=corp.com; spf=pass smtp.mailfrom=alice@corp.com; dmarc=pass header.from=corp.com",
    extra: str = "",
    body: str = "please run the tests",
    message_id: str = "<m1@corp.com>",
) -> bytes:
    lines = []
    if auth is not None:
        lines.append(f"Authentication-Results: {auth}")
    lines += [
        f"From: {sender}",
        "To: bot@example.org",
        "Subject: Build",
        f"Message-ID: {message_id}",
    ]
    if extra:
        lines.append(extra)
    lines += ["Content-Type: text/plain; charset=utf-8", "", body, ""]
    return "\r\n".join(lines).encode("utf-8")


@pytest.fixture
def adapter(tmp_path):
    return EmailAdapter(data_dir=str(tmp_path), env=ENV)


def test_requires_authserv_id(tmp_path):
    env = dict(ENV)
    env.pop("EMAIL_AUTHSERV_ID")
    with pytest.raises(SettingsError):
        EmailAdapter(data_dir=str(tmp_path), env=env)


def test_parse_authentication_results_strips_comments():
    authserv, results = parse_authentication_results(
        "mx.example.org (postfix; v1); dkim=pass (2048-bit key) header.d=corp.com; spf=fail smtp.mailfrom=x@y.z"
    )
    assert authserv == "mx.example.org"
    assert results[0] == ("dkim", "pass", {"header.d": "corp.com"})
    assert results[1][:2] == ("spf", "fail")


def test_authenticated_mail_is_accepted(adapter):
    msg = adapter.to_inbound(raw_mail())
    assert msg is not None
    assert msg.user_id == "alice@corp.com"
    assert msg.text == "please run the tests"
    assert msg.chat.chat_id == "<m1@corp.com>"
    assert msg.message_key == "<m1@corp.com>"


def test_forged_from_without_trusted_auth_is_dropped(adapter):
    assert adapter.to_inbound(raw_mail(auth=None)) is None
    assert adapter.to_inbound(raw_mail(auth="evil.example; dmarc=pass header.from=corp.com")) is None
    assert adapter.to_inbound(raw_mail(auth="mx.example.org; dkim=pass header.d=attacker.net; spf=fail")) is None


def test_only_topmost_header_counts():
    raw = raw_mail(extra="Authentication-Results: mx.example.org; dmarc=pass header.from=corp.com",
                   auth="mx.example.org; dmarc=fail header.from=corp.com")
    msg = parse_email(raw)
    ok, _ = verify_sender(msg, "mx.example.org", "alice@corp.com")
    assert not ok


def test_automated_mail_is_dropped(adapter):
    assert adapter.to_inbound(raw_mail(extra="Auto-Submitted: auto-replied")) is None
    assert adapter.to_inbound(raw_mail(extra="Precedence: bulk")) is None


def test_reply_stays_in_thread_and_builds_headers(adapter):
    raw = raw_mail(
        message_id="<m2@corp.com>",
        extra="In-Reply-To: <bot-1@example.org>\r\nReferences: <m1@corp.com> <bot-1@example.org>",
        body="thanks\n\nOn Mon, Alice wrote:\n> old text",
    )
    msg = adapter.to_inbound(raw)
    assert msg is not None
    assert msg.chat.chat_id == "<m1@corp.com>"
    assert msg.text == "thanks"
    reply = adapter.build_reply(msg.chat, "done")
    assert reply["In-Reply-To"] == "<m2@corp.com>"
    assert "<m1@corp.com>" in reply["References"]
    assert reply["Subject"] == "Re: Build"
    assert reply["Auto-Submitted"] == "auto-replied"


def test_strip_quoted_variants():
    assert strip_quoted("hi\n\n-----Original Message-----\nFrom: x") == "hi"
    assert strip_quoted("你好\n在 2026年11月19日 写道：\n> 旧") == "你好"
    assert strip_quoted("a\n> quoted\nb") == "a\nb"
    assert strip_quoted("body\n-- \nsig") == "body"
