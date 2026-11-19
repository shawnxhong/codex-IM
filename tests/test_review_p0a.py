"""Regression tests for docs/reviews/2026-11-19-p0a-5263355.md (R1–R12). Written on Windows, run on Ubuntu."""

from __future__ import annotations

import time

import yaml
from conftest import wait_for
from fakes import FakeRuntime, agent_item, answer, completed, hold, user_item

from codex_im.core.finalize import TEXT_AWAITING_ANSWER, TEXT_STOPPED, TEXT_UNKNOWN
from codex_im.core.outbox import Outbox
from codex_im.core.recovery import TEXT_STEER_ABSENT
from codex_im.core.sessions import TEXT_ABANDONED, TEXT_HOLD_SELF, TEXT_PROJECT_BUSY
from codex_im.core.store import Store
from codex_im.core.types import ChatRef
from codex_im.faults import Faults
from codex_im.platforms.fake.adapter import FakeAdapter


def _rt(hx):
    return next(iter(hx.bridge.sessions._rts.values()))


async def _running(hx, index: int = 0):
    handle = await wait_for(lambda: len(hx.runtime.handles) > index and hx.runtime.handles[index])
    await wait_for(lambda: any(rt.handle is handle for rt in hx.bridge.sessions._rts.values()))
    return handle


def _inbound_state(hx, text: str) -> str:
    row = hx.store._one("SELECT state FROM inbound WHERE text = ?", (text,))  # noqa: SLF001 - test inspection
    return row["state"]


# --------------------------------------------------------------------------- R1


async def test_r1_steer_with_lost_ack_is_never_replayed(harness_factory):
    runtime = FakeRuntime()
    runtime.steer_mode = "lost_ack"  # Codex records the input, then the response is lost
    hx = await harness_factory(runtime=runtime)
    runtime.scripts.append(hold())
    await hx.say("first")
    handle = await _running(hx)
    await hx.say("second")
    await wait_for(lambda: handle.steer_calls == 1)
    await wait_for(lambda: _inbound_state(hx, "second") == "steer_unknown")
    handle.finish("first done")
    await wait_for(lambda: "first done" in hx.finals())
    await wait_for(lambda: _inbound_state(hx, "second") == "steered", timeout=8)  # resolved from history
    assert [t for _, t in runtime.started_turns] == ["first"]  # never re-run as a new turn


async def test_r1_refused_steer_runs_as_next_turn(harness_factory):
    runtime = FakeRuntime()
    runtime.steer_mode = "reject"
    hx = await harness_factory(runtime=runtime)
    runtime.scripts.append(hold())
    await hx.say("first")
    handle = await _running(hx)
    await hx.say("second")
    await wait_for(lambda: handle.steer_calls == 1)
    handle.finish("first done")
    await wait_for(lambda: [t for _, t in runtime.started_turns] == ["first", "second"])


async def test_r1_unknown_steer_absent_from_history_is_reported_not_replayed(harness_factory):
    runtime = FakeRuntime()
    runtime.steer_mode = "hang"  # times out after rpc_timeout; Codex never recorded it
    hx = await harness_factory(runtime=runtime, rpc_timeout=0.3)
    runtime.scripts.append(hold())
    await hx.say("first")
    handle = await _running(hx)
    await hx.say("second")
    await wait_for(lambda: _inbound_state(hx, "second") == "steer_unknown")
    handle.finish("first done")
    await wait_for(lambda: TEXT_STEER_ABSENT.format(n=1) in hx.finals(), timeout=8)
    assert _inbound_state(hx, "second") == "ignored"
    assert [t for _, t in runtime.started_turns] == ["first"]


# --------------------------------------------------------------------------- R2


async def test_r2_stop_then_new_then_message_goes_to_new_thread(harness_factory):
    runtime = FakeRuntime()
    runtime.interrupt_mode = "ignore"  # the old turn ends only when the test says so
    hx = await harness_factory(runtime=runtime, stop_timeout=5.0)
    runtime.scripts.append(hold())
    await hx.say("old work")
    handle = await _running(hx)
    runtime.scripts.append(answer("fresh"))
    await hx.say("/stop")
    await hx.say("/new")
    await hx.say("follow-up")
    await wait_for(lambda: handle.interrupt_calls == 1)
    assert len(runtime.started_turns) == 1
    handle.push(completed(handle.id, "interrupted"))  # the stop completes now
    await wait_for(lambda: "fresh" in hx.finals())
    (old_thread, _), (new_thread, text) = runtime.started_turns
    assert new_thread != old_thread and text == "follow-up"
    assert handle.steer_calls == 0


# --------------------------------------------------------------------------- R3 + R4


async def test_r3_r4_hanging_interrupt_hits_deadline_and_keeps_the_hold(harness_factory):
    runtime = FakeRuntime()
    runtime.interrupt_mode = "hang"
    hx = await harness_factory(runtime=runtime, stop_timeout=0.5)
    runtime.scripts.append(hold())
    await hx.say("runaway")
    handle = await _running(hx)
    await hx.say("/stop")
    await wait_for(lambda: TEXT_ABANDONED in hx.finals(), timeout=5)
    assert hx.store.get_turn(1)["state"] == "abandoned"
    await hx.say("/status")  # read-only commands still answer
    await wait_for(lambda: any(t.startswith("状态：") for t in hx.finals()))
    await hx.say("next task")
    await wait_for(lambda: TEXT_HOLD_SELF in hx.finals())
    assert len(runtime.started_turns) == 1  # the workspace is still held by the unconfirmed turn
    handle.remote_finish("interrupted")  # history now shows the remote end
    await wait_for(lambda: TEXT_STOPPED in hx.finals(), timeout=8)
    await wait_for(lambda: len(runtime.started_turns) == 2, timeout=8)
    assert runtime.remote_interrupts  # reconciliation re-sent the interrupt by turn id


async def test_r3_abandoned_turn_blocks_other_session_on_serial_project(harness_factory, tmp_path):
    root = tmp_path / "ws"
    (root / "projects" / "p").mkdir(parents=True)
    config = tmp_path / "codex-im.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "projects": {"p": {"path": str(root / "projects" / "p"), "concurrency": "serial"}},
                "bindings": [
                    {"session": {"platform": "fake", "chat_id": "c1"}, "project": "p"},
                    {"session": {"platform": "fake", "chat_id": "c2"}, "project": "p"},
                ],
            }
        ),
        encoding="utf-8",
    )
    runtime = FakeRuntime()
    runtime.interrupt_mode = "hang"
    hx = await harness_factory(runtime=runtime, config_file=str(config), stop_timeout=0.5)
    runtime.scripts.append(hold())
    await hx.say("one", chat_id="c1")
    handle = await _running(hx)
    await hx.say("/stop", chat_id="c1")
    await wait_for(lambda: TEXT_ABANDONED in hx.finals(), timeout=5)
    await hx.say("two", chat_id="c2")
    await wait_for(lambda: TEXT_PROJECT_BUSY.format(project="p") in hx.finals())
    assert len(runtime.started_turns) == 1
    handle.remote_finish("interrupted")
    await wait_for(lambda: [t for _, t in runtime.started_turns] == ["one", "two"], timeout=8)


async def test_r3_restart_restores_hold_before_queued_work(harness_factory, make_settings):
    settings = make_settings()
    store = Store(settings.db_path)
    chat = ChatRef("fake", "chat-1")
    key = chat.session_key.encode()
    store.upsert_session(key, chat.to_json(), provider="openai", model="test-model", project=None)
    store.insert_thread("thread-x", key, cwd="/held", provider="openai", title="t")
    turn_id = store.create_turn(key, "thread-x", "long job", [], None, cwd="/held")
    store.set_turn_started(turn_id, "turn-x")
    store.close()
    runtime = FakeRuntime()
    record = {"id": "turn-x", "status": "inProgress", "startedAt": int(time.time()), "items": [user_item("long job")]}
    runtime.add_thread("thread-x", [record])
    hx = await harness_factory(runtime=runtime)
    await hx.say("queued after restart")
    await wait_for(lambda: TEXT_HOLD_SELF in hx.finals())
    assert runtime.started_turns == []
    record["status"] = "completed"
    record["items"].append({"type": "agentMessage", "id": "m", "text": "job result", "phase": "final_answer"})
    await wait_for(lambda: "job result" in hx.finals(), timeout=10)
    await wait_for(lambda: [t for _, t in runtime.started_turns] == ["queued after restart"], timeout=8)


async def test_r3_release_is_admin_only_and_frees_the_hold(harness_factory):
    runtime = FakeRuntime()
    runtime.interrupt_mode = "hang"
    hx = await harness_factory(runtime=runtime, stop_timeout=0.5, admins={"fake:boss"})
    runtime.scripts.append(hold())
    await hx.say("runaway")
    await _running(hx)
    await hx.say("/stop")
    await wait_for(lambda: TEXT_ABANDONED in hx.finals(), timeout=5)
    await hx.say("/release")
    await wait_for(lambda: any("没有执行 /release 的权限" in t for t in hx.finals()))
    await hx.say("/release", user_id="boss")
    await wait_for(lambda: any("已强制释放 1 个" in t for t in hx.finals()))
    assert hx.store.get_turn(1)["state"] == "released"
    await hx.say("after release")
    await wait_for(lambda: len(runtime.started_turns) == 2)


# --------------------------------------------------------------------------- R5


async def test_r5_outbox_keeps_session_order_across_backoff(tmp_path):
    store = Store(str(tmp_path / "db.sqlite3"))
    adapter = FakeAdapter(serve=False)
    outbox = Outbox(store, {"fake": adapter}, Faults())
    a = ChatRef("fake", "a")
    b = ChatRef("fake", "b")
    outbox.notice(a.session_key.encode(), a, "a-1")
    outbox.notice(a.session_key.encode(), a, "a-2")
    outbox.notice(b.session_key.encode(), b, "b-1")
    adapter.fail_next(1)
    await outbox.deliver_due()  # a-1 fails and backs off; a-2 must wait; b-1 is independent
    assert [e["text"] for e in adapter.events] == ["b-1"]
    await outbox.deliver_due()
    assert [e["text"] for e in adapter.events] == ["b-1"]
    store._exec("UPDATE outbox SET next_attempt_at = 0 WHERE state = 'pending'")  # noqa: SLF001
    await outbox.deliver_due()
    assert [e["text"] for e in adapter.events] == ["b-1", "a-1", "a-2"]
    store.close()


# --------------------------------------------------------------------------- R6


async def test_r6_recovered_match_is_bound_and_not_reused(harness_factory, make_settings):
    settings = make_settings()
    store = Store(settings.db_path)
    chat = ChatRef("fake", "chat-1")
    key = chat.session_key.encode()
    store.upsert_session(key, chat.to_json(), provider="openai", model="test-model", project=None)
    store.insert_thread("thread-x", key, cwd="/tmp", provider="openai", title="t")
    turn_a = store.create_turn(key, "thread-x", "same input", [], None)
    store.close()
    runtime = FakeRuntime()
    history = {
        "id": "codex-a",
        "status": "completed",
        "startedAt": int(time.time()),
        "items": [user_item("same input"), {"type": "agentMessage", "id": "m", "text": "A result", "phase": "final_answer"}],
    }
    runtime.add_thread("thread-x", [history])
    hx = await harness_factory(runtime=runtime)
    await wait_for(lambda: "A result" in hx.finals())
    assert hx.store.get_turn(turn_a)["codex_turn_id"] == "codex-a"
    assert hx.store.get_thread("thread-x")["state"] == "materialized"
    assert hx.store.get_thread("thread-x")["turn_count"] == 1
    # Same input again, crashed before submission: must not inherit A's result.
    turn_b = hx.store.create_turn(key, "thread-x", "same input", [], None)
    hx.store.set_turn_state(turn_b, "reconciling")
    hx.bridge.recovery.schedule(turn_b, delay=0.1)
    await wait_for(lambda: hx.store.get_turn(turn_b)["state"] == "unknown", timeout=5)
    assert hx.finals().count("A result") == 1


async def test_r6_ambiguous_history_match_is_unknown(harness_factory, make_settings):
    settings = make_settings()
    store = Store(settings.db_path)
    chat = ChatRef("fake", "chat-1")
    key = chat.session_key.encode()
    store.upsert_session(key, chat.to_json(), provider="openai", model="test-model", project=None)
    store.insert_thread("thread-x", key, cwd="/tmp", provider="openai", title="t")
    turn = store.create_turn(key, "thread-x", "dup", [], None)
    store.close()
    runtime = FakeRuntime()
    now = int(time.time())
    runtime.add_thread(
        "thread-x",
        [
            {"id": "c1", "status": "completed", "startedAt": now, "items": [user_item("dup")]},
            {"id": "c2", "status": "completed", "startedAt": now, "items": [user_item("dup")]},
        ],
    )
    hx = await harness_factory(runtime=runtime)
    await wait_for(lambda: TEXT_UNKNOWN in hx.finals())
    assert hx.store.get_turn(turn)["codex_turn_id"] is None


# --------------------------------------------------------------------------- R7


async def test_r7_recovery_delivers_async_questions_once(harness_factory, make_settings):
    settings = make_settings()
    store = Store(settings.db_path)
    chat = ChatRef("fake", "chat-1")
    key = chat.session_key.encode()
    store.upsert_session(key, chat.to_json(), provider="openai", model="test-model", project=None)
    store.insert_thread("thread-x", key, cwd="/tmp", provider="openai", title="t")
    turn = store.create_turn(key, "thread-x", "ask me", [], None)
    store.set_turn_started(turn, "turn-x")
    store.close()
    runtime = FakeRuntime()
    question = {"type": "agentMessage", "id": "q", "text": "Which DB?", "phase": "final_answer", "delivery": "async"}
    runtime.add_thread(
        "thread-x",
        [{"id": "turn-x", "status": "completed", "startedAt": int(time.time()), "items": [user_item("ask me"), question]}],
    )
    hx = await harness_factory(runtime=runtime)
    await wait_for(lambda: TEXT_AWAITING_ANSWER in hx.finals())
    assert hx.finals().count("❓ Which DB?") == 1
    assert hx.store.open_question_count("thread-x") == 1
    session = hx.store.get_session(key)
    hx.bridge.recovery._recover_questions(hx.store.get_turn(turn), session, runtime.threads["thread-x"]["turns"][0])
    rows = hx.store._all("SELECT * FROM outbox WHERE kind = 'async_question'")  # noqa: SLF001
    assert len(rows) == 1


# --------------------------------------------------------------------------- R8


def test_r8_email_reply_with_only_in_reply_to_stays_in_session(tmp_path):
    from test_mail import ENV, raw_mail

    from codex_im.platforms.mail.adapter import EmailAdapter

    store = Store(str(tmp_path / "db.sqlite3"))
    adapter = EmailAdapter(data_dir=str(tmp_path), env=ENV, store=store)
    first = adapter.to_inbound(raw_mail(message_id="<root@corp.com>"))
    reply = adapter.build_reply(first.chat, "bot answer")
    adapter._remember(str(reply["Message-ID"]), first.chat.chat_id)  # what send() records  # noqa: SLF001
    second = adapter.to_inbound(
        raw_mail(message_id="<m2@corp.com>", extra=f"In-Reply-To: {reply['Message-ID']}", body="next")
    )
    assert second.chat.chat_id == "<root@corp.com>"
    store.close()


def test_r8_long_reference_chain_keeps_the_root(tmp_path):
    from test_mail import ENV, raw_mail

    from codex_im.platforms.mail.adapter import EmailAdapter

    store = Store(str(tmp_path / "db.sqlite3"))
    adapter = EmailAdapter(data_dir=str(tmp_path), env=ENV, store=store)
    refs = " ".join(["<root@corp.com>"] + [f"<r{i}@corp.com>" for i in range(30)])
    msg = adapter.to_inbound(raw_mail(message_id="<m31@corp.com>", extra=f"References: {refs}", body="deep"))
    chain = msg.chat.extra["references"].split()
    assert chain[0] == "<root@corp.com>" and len(chain) == 20
    fresh = EmailAdapter(data_dir=str(tmp_path / "other"), env=ENV, store=None)
    follow = fresh.to_inbound(
        raw_mail(message_id="<m32@corp.com>", extra=f"References: {msg.chat.extra['references']}", body="more")
    )
    assert follow.chat.chat_id == "<root@corp.com>"
    store.close()


# --------------------------------------------------------------------------- R10


async def test_r10_long_async_question_is_chunked(harness_factory):
    hx = await harness_factory()
    long_question = "Q" * 9000
    hx.runtime.scripts.append(
        lambda tid: [agent_item(tid, "q", long_question, delivery="async"), agent_item(tid, "m", "ok"), completed(tid)]
    )
    await hx.say("go")
    await wait_for(lambda: "ok" in hx.finals())
    pieces = [e["text"] for e in hx.adapter.events if e.get("final") and e["text"].lstrip("❓ ").startswith("Q")]
    assert len(pieces) >= 3
    assert all(len(p) <= hx.adapter.capabilities.max_text_len for p in pieces)
    dead = hx.store._all("SELECT * FROM outbox WHERE state = 'dead'")  # noqa: SLF001
    assert dead == []


# --------------------------------------------------------------------------- R11


async def test_r11_stop_closes_open_questions(harness_factory):
    hx = await harness_factory()
    hx.runtime.scripts.append(lambda tid: [agent_item(tid, "q", "Which one?", delivery="async")])
    await hx.say("start")
    await _running(hx)
    thread_id = hx.runtime.started_turns[0][0]
    await wait_for(lambda: hx.store.open_question_count(thread_id) == 1)
    await hx.say("/stop")
    await wait_for(lambda: any(TEXT_STOPPED in t for t in hx.finals()))
    assert hx.store.open_question_count(thread_id) == 0


# --------------------------------------------------------------------------- R12


async def test_r12_group_commands_must_be_addressed(harness_factory):
    hx = await harness_factory()
    hx.runtime.scripts.append(hold())
    await hx.say("work", chat_id="g", is_group=True, mentions_bot=True)
    handle = await _running(hx)
    await hx.say("/stop", chat_id="g", is_group=True, mentions_bot=False)
    await hx.say("/new", chat_id="g", is_group=True, mentions_bot=False)
    assert handle.interrupt_calls == 0
    assert hx.store._one("SELECT COUNT(*) AS n FROM inbound WHERE kind = 'command'")["n"] == 0  # noqa: SLF001
    await hx.say("/stop@fakebot", chat_id="g", is_group=True, mentions_bot=False)
    await wait_for(lambda: handle.interrupt_calls == 1)


# --------------------------------------------------------------------------- R9


def test_r9_agent_depends_on_healthy_egress_proxy():
    import pathlib

    compose = yaml.safe_load((pathlib.Path(__file__).parent.parent / "deploy" / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert services["agent"]["depends_on"]["egress-proxy"]["condition"] == "service_healthy"
    assert services["bridge"]["depends_on"]["agent"]["condition"] == "service_healthy"
    assert "healthcheck" in services["egress-proxy"] and "healthcheck" in services["agent"]
