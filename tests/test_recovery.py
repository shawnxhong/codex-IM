"""Restart reconciliation never re-executes (design D14, verification V3)."""

from __future__ import annotations

import time

from conftest import wait_for
from fakes import FakeRuntime

from codex_im.core.finalize import TEXT_RESTART_INTERRUPTED, TEXT_UNKNOWN
from codex_im.core.store import Store
from codex_im.core.types import ChatRef


def _seed(settings, *, codex_turn_id: str | None, input_text: str = "do it") -> tuple[str, int]:
    """DB state as left by a bridge that crashed mid-turn."""
    store = Store(settings.db_path)
    chat = ChatRef("fake", "chat-1")
    key = chat.session_key.encode()
    store.upsert_session(key, chat.to_json(), provider="openai", model="test-model", project=None)
    store.insert_thread("thread-x", key, cwd="/tmp", provider="openai", title="t")
    turn_id = store.create_turn(key, "thread-x", input_text, [], None)
    if codex_turn_id:
        store.set_turn_started(turn_id, codex_turn_id)
    store.close()
    return key, turn_id


def _history(
    status: str, final: str | None, text: str = "do it", turn_id: str = "turn-x", started: float | None = None
) -> dict:
    items = [{"type": "userMessage", "content": [{"type": "text", "text": text}]}]
    if final:
        items.append({"type": "agentMessage", "id": "m", "text": final, "phase": "final_answer"})
    return {"id": turn_id, "status": status, "startedAt": int(started or time.time()), "items": items}


async def test_completed_turn_is_delivered_from_history(harness_factory, make_settings):
    settings = make_settings()
    _, turn_id = _seed(settings, codex_turn_id="turn-x")
    runtime = FakeRuntime()
    runtime.add_thread("thread-x", [_history("completed", "the result")])
    hx = await harness_factory(runtime=runtime)
    await wait_for(lambda: "the result" in hx.finals())
    assert runtime.started_turns == []
    assert hx.store.get_turn(turn_id)["state"] == "completed"


async def test_interrupted_turn_offers_retry(harness_factory, make_settings):
    settings = make_settings()
    _, turn_id = _seed(settings, codex_turn_id="turn-x")
    runtime = FakeRuntime()
    runtime.add_thread("thread-x", [_history("interrupted", None)])
    hx = await harness_factory(runtime=runtime)
    await wait_for(lambda: TEXT_RESTART_INTERRUPTED in hx.finals())
    assert runtime.started_turns == []
    assert hx.store.get_turn(turn_id)["state"] == "interrupted"


async def test_pending_turn_matched_by_input_text(harness_factory, make_settings):
    settings = make_settings()
    _seed(settings, codex_turn_id=None, input_text="unique input")
    runtime = FakeRuntime()
    runtime.add_thread("thread-x", [_history("completed", "found it", text="unique input")])
    hx = await harness_factory(runtime=runtime)
    await wait_for(lambda: "found it" in hx.finals())
    assert runtime.started_turns == []


async def test_pending_turn_never_received_is_unknown(harness_factory, make_settings):
    settings = make_settings()
    _, turn_id = _seed(settings, codex_turn_id=None)
    runtime = FakeRuntime()
    runtime.add_thread("thread-x", [])
    hx = await harness_factory(runtime=runtime)
    await wait_for(lambda: TEXT_UNKNOWN in hx.finals())
    assert runtime.started_turns == []
    assert hx.store.get_turn(turn_id)["state"] == "unknown"


async def test_retry_after_unknown_reexecutes_once(harness_factory, make_settings):
    settings = make_settings()
    _seed(settings, codex_turn_id=None, input_text="redo me")
    runtime = FakeRuntime()
    runtime.add_thread("thread-x", [])
    hx = await harness_factory(runtime=runtime)
    await wait_for(lambda: TEXT_UNKNOWN in hx.finals())
    await hx.say("/retry")
    await wait_for(lambda: "ok" in hx.finals())
    assert [t for _, t in runtime.started_turns] == ["redo me"]
