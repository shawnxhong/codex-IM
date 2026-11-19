"""Unit tests for pure helpers (markup, commands, store, workspace, config, admission, events, errors)."""

from __future__ import annotations

import pytest
from openai_codex import InvalidParamsError, InvalidRequestError, ServerBusyError, TransportClosedError

from codex_im.codex.errors import ErrorKind, classify
from codex_im.codex.events import format_questions, payload_dict, pick_final_text, turn_id_of, user_text_of_turn
from codex_im.config import SettingsError, load_settings
from codex_im.core.admission import Admission
from codex_im.core.commands import Command, parse_command
from codex_im.core.markup import chunk_text, md_to_telegram_html
from codex_im.core.types import ChatRef, InboundMessage, SessionKey
from codex_im.core.workspace import ConfigError, parse_projects, safe_segment
from codex_im.faults import Faults, InjectedDeliveryFailure

# --------------------------------------------------------------------------- markup


def test_chunk_text_short_text_unchanged():
    assert chunk_text("hello", 100) == ["hello"]


def test_chunk_text_respects_limit_and_reopens_fences():
    body = "intro\n```python\n" + "\n".join(f"line {i}" for i in range(200)) + "\n```\noutro"
    chunks = chunk_text(body, 300)
    assert len(chunks) > 1
    assert all(len(c) <= 300 for c in chunks)
    inside = [c for c in chunks[1:-1]]
    assert all(c.startswith("```python") and c.rstrip().endswith("```") for c in inside)
    assert "".join(chunks).count("line 199") == 1


def test_chunk_text_hard_splits_long_lines():
    chunks = chunk_text("x" * 1000, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert sum(len(c) for c in chunks) == 1000


def test_telegram_html_escapes_and_formats():
    html = md_to_telegram_html("# Title\nuse **bold** and `a<b>` and [link](https://e.com/?a=1&b=2)\n```py\nx < 1\n```")
    assert "<b>Title</b>" in html
    assert "<b>bold</b>" in html
    assert "<code>a&lt;b&gt;</code>" in html
    assert '<a href="https://e.com/?a=1&amp;b=2">link</a>' in html
    assert '<pre><code class="language-py">x &lt; 1</code></pre>' in html


def test_telegram_html_leaves_snake_case_alone():
    assert md_to_telegram_html("my_var_name * 2") == "my_var_name * 2"


# --------------------------------------------------------------------------- commands


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/stop", Command("stop", "", False)),
        ("/resume 3", Command("resume", "3", False)),
        ("/Stop@MyBot", Command("stop", "", True)),
        ("/release", Command("release", "", False)),
        ("/stop@otherbot", None),
        ("/etc/hosts 是什么", None),
        ("hello", None),
    ],
)
def test_parse_command(text, expected):
    assert parse_command(text, "mybot") == expected


# --------------------------------------------------------------------------- store


def _msg(key: str, text: str = "t") -> InboundMessage:
    return InboundMessage(platform="fake", message_key=key, chat=ChatRef("fake", "c"), user_id="u", text=text)


def test_inbound_dedup(store):
    assert store.insert_inbound(_msg("a"), kind="message") is not None
    assert store.insert_inbound(_msg("a"), kind="message") is None
    assert len(store.queued_messages(ChatRef("fake", "c").session_key.encode())) == 1


def test_finish_turn_writes_outbox_atomically(store):
    from codex_im.core.store import OutboxRow

    chat = ChatRef("fake", "c")
    key = chat.session_key.encode()
    store.upsert_session(key, chat.to_json(), provider="openai", model="m", project=None)
    store.insert_thread("t1", key, cwd="/w", provider="openai", title=None)
    row_id = store.insert_inbound(_msg("a"), kind="message")
    turn_id = store.create_turn(key, "t1", "t", [row_id], None)
    store.set_turn_started(turn_id, "ct1")
    assert store.get_thread("t1")["state"] == "materialized"
    store.finish_turn(
        turn_id, state="completed", final_text="x", error=None, session_key=key, chat_json=chat.to_json(),
        rows=[OutboxRow(kind="final", seq=0, text="x")],
    )
    assert store.get_turn(turn_id)["state"] == "completed"
    assert [r["text"] for r in store.due_outbox(1e18)] == ["x"]


def test_session_key_roundtrip():
    key = SessionKey("telegram", "-100|1", "5")
    assert SessionKey.decode(key.encode()) == key


# --------------------------------------------------------------------------- workspace


def test_safe_segment_is_collision_free():
    assert safe_segment("-100123") == "-100123"
    assert safe_segment("a/b") != safe_segment("a_b")
    assert "/" not in safe_segment("<abc@example.com>")
    assert safe_segment("..") not in {"..", "."}


def test_parse_projects_rejects_paths_outside_workspace():
    with pytest.raises(ConfigError):
        parse_projects({"projects": {"p": {"path": "/etc"}}}, "/workspace")
    projects, bindings = parse_projects(
        {"projects": {"p": {"path": "/workspace/p"}}, "bindings": [{"session": {"platform": "telegram", "chat_id": 1}, "project": "p"}]},
        "/workspace",
    )
    assert projects["p"].concurrency == "serial"
    assert bindings[0].matches(SessionKey("telegram", "1", "_"))
    assert bindings[0].matches(SessionKey("telegram", "1", "42"))


# --------------------------------------------------------------------------- config / admission / faults


def test_settings_require_model_and_platforms():
    with pytest.raises(SettingsError):
        load_settings({"CODEX_IM_PLATFORMS": "telegram"})
    with pytest.raises(SettingsError):
        load_settings({"CODEX_IM_DEFAULT_MODEL": "m"})
    with pytest.raises(SettingsError):
        load_settings({"CODEX_IM_DEFAULT_MODEL": "m", "CODEX_IM_PLATFORMS": "x", "CODEX_IM_AGENTD": "tcp://agent:4501"})
    s = load_settings({"CODEX_IM_DEFAULT_MODEL": "m", "CODEX_IM_PLATFORMS": "Telegram, email"})
    assert s.platforms == ["telegram", "email"]
    assert s.allowed_providers == ["openai"]


def test_admission_default_deny():
    adm = Admission(env={"TELEGRAM_ALLOWED_USERS": "123, 456", "EMAIL_ALLOWED_USERS": "Me@Example.com"})
    assert adm.allowed("telegram", "123")
    assert not adm.allowed("telegram", "789")
    assert adm.allowed("email", "me@example.com")
    assert not adm.allowed("fake", "anyone")
    assert Admission(env={"FAKE_ALLOW_ALL_USERS": "true"}).allowed("fake", "anyone")


def test_admins():
    adm = Admission(env={}, admins={"telegram:1"})
    assert adm.is_admin("telegram", "1")
    assert not adm.is_admin("telegram", "2")
    assert Admission(env={}).is_admin("telegram", "2")


def test_fault_delivery_failures():
    faults = Faults("fail_delivery:1")
    with pytest.raises(InjectedDeliveryFailure):
        faults.delivery_check()
    faults.delivery_check()
    with pytest.raises(ValueError):
        Faults("nonsense")


# --------------------------------------------------------------------------- events / errors


class _P:
    def __init__(self, params):
        self.params = params


class _N:
    def __init__(self, method, params):
        self.method = method
        self.payload = _P(params)


def test_events_helpers():
    data = payload_dict(_N("turn/completed", {"turn": {"id": "t1", "status": "completed"}}))
    assert turn_id_of("turn/completed", data) == "t1"
    assert turn_id_of("item/completed", {"turnId": "t2"}) == "t2"
    items = [
        {"type": "agentMessage", "text": "q?", "phase": "final_answer", "delivery": "async"},
        {"type": "agentMessage", "text": "commentary", "phase": "commentary"},
        {"type": "agentMessage", "text": "answer", "phase": "final_answer"},
    ]
    assert pick_final_text(items) == "answer"
    assert pick_final_text(items[:2]) is None
    assert format_questions({"text": "", "questions": [{"title": "Pick", "options": ["a", "b"]}]}) == "Pick\n- a\n- b"
    turn = {"items": [{"type": "userMessage", "content": [{"type": "text", "text": "hi"}]}]}
    assert user_text_of_turn(turn) == "hi"


def test_classify_errors():
    assert classify(TransportClosedError("x")) is ErrorKind.TRANSIENT
    assert classify(ServerBusyError(-32001, "busy", "server_overloaded")) is ErrorKind.TRANSIENT
    assert classify(InvalidRequestError(-32600, "thread not found: abc")) is ErrorKind.NOT_FOUND
    assert classify(InvalidRequestError(-32600, "x", {"reason": "serverShuttingDown"})) is ErrorKind.TRANSIENT
    assert classify(InvalidRequestError(-32600, "bad model")) is ErrorKind.REJECTED
    assert classify(InvalidParamsError(-32602, "bad")) is ErrorKind.REJECTED
    assert classify(ConnectionResetError()) is ErrorKind.TRANSIENT
    assert classify(ValueError("?")) is ErrorKind.UNKNOWN
