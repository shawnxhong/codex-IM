"""End-to-end behaviour of router + sessions + outbox with a scripted Codex (design D6–D8, D13–D15)."""

from __future__ import annotations

from conftest import wait_for
from fakes import agent_item, answer, completed, hold
from openai_codex import TransportClosedError

from codex_im.core.finalize import TEXT_STOPPED
from codex_im.core.render import PLACEHOLDER
from codex_im.core.sessions import TEXT_CONTEXT_LOST, TEXT_PROJECT_BUSY, TEXT_UNAVAILABLE
from codex_im.core.types import ChatRef


async def test_turn_streams_then_finalizes_by_editing_placeholder(harness_factory):
    hx = await harness_factory()
    hx.runtime.scripts.append(answer("hello world"))
    await hx.say("hi")
    await wait_for(lambda: "hello world" in hx.finals())
    first = hx.adapter.events[0]
    assert first["op"] == "send" and first["text"] == PLACEHOLDER and first["final"] is False
    final = [e for e in hx.adapter.events if e.get("final")][-1]
    assert final["op"] == "edit" and final["message_id"] == first["message_id"]
    turn = hx.store.get_turn(1)
    assert turn["state"] == "completed"
    assert len(hx.runtime.started_turns) == 1


async def test_duplicate_inbound_executes_once(harness_factory):
    hx = await harness_factory()
    await hx.say("same", message_key="k-1")
    await hx.say("same", message_key="k-1")
    await wait_for(lambda: "ok" in hx.finals())
    assert len(hx.runtime.started_turns) == 1


async def test_final_only_platform_sends_result_without_placeholder(harness_factory):
    hx = await harness_factory(can_edit=False)
    hx.runtime.scripts.append(answer("result!"))
    await hx.say("hi")
    await wait_for(lambda: "result!" in hx.finals())
    assert not any(e["op"] == "send" and e["text"] == PLACEHOLDER for e in hx.adapter.events)
    assert not any(e["op"] == "edit" for e in hx.adapter.events)


async def test_message_during_running_turn_is_steered(harness_factory):
    hx = await harness_factory()
    hx.runtime.scripts.append(hold())
    await hx.say("first")
    handle = await wait_for(lambda: hx.runtime.handles and hx.runtime.handles[0])
    await wait_for(lambda: hx.bridge.sessions._rts and next(iter(hx.bridge.sessions._rts.values())).handle)
    await hx.say("second")
    await wait_for(lambda: handle.steered)
    assert handle.steered == ["second"]
    handle.finish("both handled")
    await wait_for(lambda: "both handled" in hx.finals())
    assert len(hx.runtime.started_turns) == 1


async def test_async_question_is_delivered_on_its_own(harness_factory):
    hx = await harness_factory()
    hx.runtime.scripts.append(
        lambda tid: [
            agent_item(tid, "q1", "Which language?\n- Python\n- Go", delivery="async"),
            agent_item(tid, "m1", "Starting with defaults."),
            completed(tid),
        ]
    )
    await hx.say("build it")
    await wait_for(lambda: "Starting with defaults." in hx.finals())
    question = [t for t in hx.finals() if t.startswith("❓")]
    assert question and "Which language?" in question[0]
    thread_id = hx.runtime.started_turns[0][0]
    assert hx.store.open_question_count(thread_id) == 1


async def test_stop_interrupts_running_turn(harness_factory):
    hx = await harness_factory()
    hx.runtime.scripts.append(hold())
    await hx.say("long task")
    handle = await wait_for(lambda: hx.runtime.handles and hx.runtime.handles[0])
    await wait_for(lambda: next(iter(hx.bridge.sessions._rts.values())).handle)
    await hx.say("/stop")
    await wait_for(lambda: any(TEXT_STOPPED in t for t in hx.finals()))
    assert handle.interrupted
    assert hx.store.get_turn(1)["state"] == "interrupted"


async def test_new_during_turn_switches_thread_and_routes_followup(harness_factory):
    hx = await harness_factory()
    hx.runtime.scripts.append(hold())
    await hx.say("old context")
    await wait_for(lambda: hx.runtime.handles)
    await wait_for(lambda: next(iter(hx.bridge.sessions._rts.values())).handle)
    hx.runtime.scripts.append(answer("fresh answer"))
    await hx.say("/new")
    await hx.say("follow-up")
    await wait_for(lambda: "fresh answer" in hx.finals())
    (old_thread, _), (new_thread, text) = hx.runtime.started_turns
    assert old_thread != new_thread
    assert text == "follow-up"


async def test_materialized_thread_missing_is_reported_not_replaced(harness_factory):
    hx = await harness_factory()
    await hx.say("first")
    await wait_for(lambda: "ok" in hx.finals())
    thread_id = hx.runtime.started_turns[0][0]
    hx.runtime.forget_thread(thread_id)  # rollout gone
    await hx.say("second")
    await wait_for(lambda: TEXT_CONTEXT_LOST in hx.finals())
    assert hx.store.get_thread(thread_id)["state"] == "lost"
    assert len(hx.runtime.started_turns) == 1  # nothing executed silently on a new thread
    await hx.say("/new")
    await hx.say("third")
    await wait_for(lambda: hx.finals().count("ok") == 2)


async def test_never_materialized_thread_is_replaced_silently(harness_factory):
    hx = await harness_factory()
    chat = ChatRef("fake", "chat-1")
    key = chat.session_key.encode()
    hx.store.upsert_session(key, chat.to_json(), provider="openai", model="test-model", project=None)
    hx.store.insert_thread("ghost-thread", key, cwd="/tmp", provider="openai", title="ghost")
    await hx.say("hi")
    await wait_for(lambda: "ok" in hx.finals())
    assert hx.store.get_thread("ghost-thread")["state"] == "discarded"
    assert TEXT_CONTEXT_LOST not in hx.finals()


async def test_transient_codex_failure_keeps_message_and_retries(harness_factory):
    hx = await harness_factory()
    hx.runtime.fail_start_thread.append(TransportClosedError("relay down"))
    await hx.say("hello")
    await wait_for(lambda: TEXT_UNAVAILABLE in hx.finals())
    await wait_for(lambda: "ok" in hx.finals(), timeout=10)
    assert len(hx.runtime.started_turns) == 1


async def test_delivery_failure_is_retried_without_reexecution(harness_factory):
    hx = await harness_factory(can_edit=False)
    hx.runtime.scripts.append(answer("deliver me"))
    hx.adapter.fail_next(2)
    await hx.say("go")
    await wait_for(lambda: "deliver me" in hx.finals())
    assert len(hx.runtime.started_turns) == 1


async def test_retry_reexecutes_only_on_request(harness_factory):
    hx = await harness_factory()
    hx.runtime.scripts.append(lambda tid: [completed(tid, "failed", error="boom")])
    await hx.say("flaky task")
    await wait_for(lambda: any("boom" in t for t in hx.finals()))
    assert len(hx.runtime.started_turns) == 1
    hx.runtime.scripts.append(answer("second try"))
    await hx.say("/retry")
    await wait_for(lambda: "second try" in hx.finals())
    assert [t for _, t in hx.runtime.started_turns] == ["flaky task", "flaky task"]


async def test_group_requires_mention_and_tags_sender(harness_factory):
    hx = await harness_factory()
    await hx.say("ignored", is_group=True, mentions_bot=False, chat_id="g1", user_name="Alice")
    await hx.say("please help", is_group=True, mentions_bot=True, chat_id="g1", user_name="Alice")
    await wait_for(lambda: hx.runtime.started_turns)
    assert hx.runtime.started_turns == [(hx.runtime.started_turns[0][0], "[Alice]: please help")]


async def test_topics_of_one_group_get_separate_directories(harness_factory, tmp_path):
    hx = await harness_factory()
    await hx.say("a", chat_id="g1", topic="10")
    await hx.say("b", chat_id="g1", topic="20")
    await wait_for(lambda: len(hx.runtime.started_turns) == 2)
    cwds = {t["cwd"] for t in hx.runtime.threads.values()}
    assert len(cwds) == 2
    assert all(c.startswith(str(tmp_path / "ws" / "sessions" / "fake" / "g1")) for c in cwds)


async def test_serial_project_lock_queues_second_session(harness_factory, tmp_path):
    root = tmp_path / "ws"
    (root / "projects" / "p").mkdir(parents=True)
    config = tmp_path / "codex-im.yaml"
    config.write_text(
        "projects:\n"
        f"  p: {{path: {root / 'projects' / 'p'}, concurrency: serial}}\n"
        "bindings:\n"
        "  - {session: {platform: fake, chat_id: c1}, project: p}\n"
        "  - {session: {platform: fake, chat_id: c2}, project: p}\n",
        encoding="utf-8",
    )
    hx = await harness_factory(config_file=str(config))
    hx.runtime.scripts.append(hold())
    hx.runtime.scripts.append(answer("second done"))
    await hx.say("one", chat_id="c1")
    first = await wait_for(lambda: hx.runtime.handles and hx.runtime.handles[0])
    await hx.say("two", chat_id="c2")
    await wait_for(lambda: TEXT_PROJECT_BUSY.format(project="p") in hx.finals())
    assert len(hx.runtime.started_turns) == 1
    first.finish("first done")
    await wait_for(lambda: "second done" in hx.finals())
    assert [t for _, t in hx.runtime.started_turns] == ["one", "two"]


async def test_sessions_and_resume_are_scoped_to_the_session(harness_factory):
    hx = await harness_factory()
    await hx.say("topic A", chat_id="a")
    await wait_for(lambda: hx.finals().count("ok") == 1)
    thread_a = hx.runtime.started_turns[0][0]
    await hx.say(f"/resume {thread_a[:8]}", chat_id="b")
    await wait_for(lambda: any(t.startswith("不存在") for t in hx.finals()))
    await hx.say("/sessions", chat_id="a")
    await wait_for(lambda: any(thread_a[:8] in t and "（当前）" in t for t in hx.finals()))
