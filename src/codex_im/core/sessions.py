"""Per-session state machine and turn execution (design D6, D13, D14, D15).

State machine per session::

    idle --turn()--> running --completed/failed--> idle
                       |--/stop--> stopping --turn ends, or deadline -> abandoned--> idle
                       '--/new --> stopping --> switch current thread --> idle

Invariants (review 2026-11-19-p0a):
- R1  steer intent is persisted before the RPC; only a provable refusal returns input to the queue.
- R2  state-changing commands raise a control barrier *when received*; the worker neither starts turns nor
      steers while a barrier is up, so later messages never reach the old thread.
- R3  "local consumer stopped" is not "remote turn ended": a turn holds its cwd until its remote outcome
      is confirmed (WorkspaceHolds, released only by the Finalizer).
- R4  /stop and /new run under one deadline that includes the interrupt RPC; no network await ever runs
      while holding ``SessionRuntime.lock``; read-only commands bypass the command queue.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..agentd.client import AgentdError, AgentdUnreachable
from ..codex.errors import ErrorKind, classify, short_message
from ..codex.events import format_questions, is_async_message, payload_dict, pick_final_text, turn_id_of
from ..config import Settings
from ..faults import Faults
from ..logutil import h
from ..platforms.base import PlatformAdapter
from .admission import Admission
from .finalize import Finalizer
from .holds import Reservation, WorkspaceHolds
from .outbox import Outbox
from .render import StreamRenderer
from .store import Store
from .tasks import spawn
from .types import ChatRef, InboundMessage, SessionKey
from .workspace import Workspace

log = logging.getLogger(__name__)

IDLE, RUNNING, STOPPING = "idle", "running", "stopping"
STATE_LABELS = {IDLE: "空闲", RUNNING: "运行中", STOPPING: "停止中"}

# Commands that change execution state: they raise the control barrier as soon as they are received.
BARRIER_COMMANDS = frozenset({"stop", "new", "resume", "provider", "retry", "compact", "model", "release"})
# Commands that only read state: executed immediately, never queued behind a slow /stop.
READ_ONLY_COMMANDS = frozenset({"status", "sessions", "help", "start"})

TEXT_RATE_LIMITED = "⚠️ 消息太频繁，这条没有执行，请稍后再发。"
TEXT_CONTEXT_LOST = (
    "⚠️ 之前的上下文已经无法恢复（Codex 中找不到这个会话）。这条消息没有执行。\n"
    "请发 /new 开始新会话，或用 /sessions 和 /resume 切换到其他会话，然后重新发送。"
)
TEXT_UNAVAILABLE = "⚠️ Codex 暂时不可用，消息已保留，恢复后会自动处理。"
TEXT_PROJECT_BUSY = "⏳ 工作目录（{project}）正被另一个任务使用，已排队。"
TEXT_HOLD_SELF = "⏳ 上一个任务还没有确认结束（Codex 端可能仍在运行），这条消息已排队；可以发 /stop 再次尝试停止。"
TEXT_CANCELLED = "已取消，这条消息没有执行。"
TEXT_REJECTED = "❌ Codex 拒绝了请求：{error}\n消息没有执行（可以用 /model 或 /provider 调整后重新发送）。"
TEXT_WORKSPACE_FAILED = "❌ 工作目录准备失败：{error}\n消息没有执行。"
TEXT_ABANDONED = (
    "⚠️ 停止超时：已不再等待这个任务，但 Codex 端可能仍在运行。工作目录会保持占用，直到确认它结束"
    "（会继续尝试中断）。确认 Codex 已不在运行时，管理员可以用 /release 强制释放。"
)


class ContextLost(Exception):
    pass


class CodexUnavailable(Exception):
    pass


class StartRejected(Exception):
    pass


class WorkspaceFailed(Exception):
    pass


class RetryLater(Exception):
    def __init__(self, delay: float) -> None:
        super().__init__(f"retry in {delay:.0f}s")
        self.delay = delay


@dataclass(eq=False)
class SessionRuntime:
    key: SessionKey
    key_str: str
    state: str = IDLE
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # short critical sections only, never across I/O
    idle: asyncio.Event = field(default_factory=asyncio.Event)
    work: asyncio.Event = field(default_factory=asyncio.Event)
    commands: asyncio.Queue[tuple[int, InboundMessage, str, str]] = field(default_factory=asyncio.Queue)
    controls_pending: int = 0  # R2 barrier: received but not yet finished state-changing commands
    handle: Any = None
    turn_id: int | None = None
    thread_id: str | None = None
    current: asyncio.Task[None] | None = None
    stop_requested: bool = False
    abandoned: bool = False
    unavailable_notified: bool = False
    retry_delay: float = 0.0
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)


class SessionManager:
    def __init__(
        self,
        *,
        store: Store,
        runtime: Any,
        workspace: Workspace,
        holds: WorkspaceHolds,
        adapters: Mapping[str, PlatformAdapter],
        outbox: Outbox,
        finalizer: Finalizer,
        recovery: Any,
        settings: Settings,
        admission: Admission,
        faults: Faults,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.workspace = workspace
        self.holds = holds
        self.adapters = adapters
        self.outbox = outbox
        self.finalizer = finalizer
        self.recovery = recovery
        self.settings = settings
        self.admission = admission
        self.faults = faults
        self._rts: dict[str, SessionRuntime] = {}
        self._rate: dict[str, collections.deque[float]] = {}
        self._closing = False

    # ------------------------------------------------------------------ lifecycle
    def runtime_for(self, key_str: str) -> SessionRuntime:
        rt = self._rts.get(key_str)
        if rt is None:
            rt = SessionRuntime(SessionKey.decode(key_str), key_str)
            rt.idle.set()
            self._rts[key_str] = rt
            rt.tasks.append(spawn(self._worker(rt), name=f"session-worker-{h(key_str)}"))
            rt.tasks.append(spawn(self._command_worker(rt), name=f"session-commands-{h(key_str)}"))
        return rt

    def kick(self, key_str: str) -> None:
        self.runtime_for(key_str).work.set()

    def kick_all_queued(self) -> None:
        for key_str in self.store.sessions_with_queued():
            self.kick(key_str)

    async def shutdown(self) -> None:
        self._closing = True
        tasks = [t for rt in self._rts.values() for t in rt.tasks]
        tasks += [rt.current for rt in self._rts.values() if rt.current is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _deadline(self, seconds: float) -> float:
        return asyncio.get_running_loop().time() + seconds

    @staticmethod
    def _left(deadline: float) -> float:
        return deadline - asyncio.get_running_loop().time()

    # ------------------------------------------------------------------ inbound
    async def on_message(self, key_str: str) -> None:
        rt = self.runtime_for(key_str)
        if self.settings.busy_policy == "steer" and await self._try_steer(rt):
            return
        rt.work.set()

    def submit_command(self, key_str: str, inbound_id: int, msg: InboundMessage, name: str, args: str) -> None:
        """Called synchronously by the router, in arrival order, before later messages are recorded."""
        rt = self.runtime_for(key_str)
        if name in READ_ONLY_COMMANDS:
            spawn(self._run_command(rt, inbound_id, msg, name, args, barrier=False), name=f"cmd-{name}")
            return
        if name in BARRIER_COMMANDS:
            rt.controls_pending += 1  # R2: effective immediately, not when the command worker gets to it
        rt.commands.put_nowait((inbound_id, msg, name, args))

    async def _try_steer(self, rt: SessionRuntime) -> bool:
        """Append queued input to the running turn. Returns True when the input was taken care of."""
        async with rt.lock:
            if (
                rt.state != RUNNING
                or rt.handle is None
                or rt.turn_id is None
                or rt.controls_pending
                or rt.stop_requested
            ):
                return False
            rows = self.store.queued_messages(rt.key_str)
            if not rows:
                return True
            text = self.compose_input(rows)
            ids = [int(r["id"]) for r in rows]
            handle, turn_id, thread_id = rt.handle, rt.turn_id, rt.thread_id
            steer_id = self.store.begin_steer(turn_id, text, ids)  # R1: intent first; rows leave the queue
        self.faults.hit("crash_after_steer_persist")
        try:
            await asyncio.wait_for(handle.steer(text), self.settings.rpc_timeout)  # outside the lock (R4)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            kind = classify(exc)
            if kind in (ErrorKind.REJECTED, ErrorKind.NOT_FOUND):
                # A JSON-RPC refusal proves the input was not taken: it may run as the next turn.
                # VERIFY(ubuntu): V1 — error returned when steering a turn that just finished.
                self.store.reject_steer(steer_id)
                log.info("steer refused (%s); input returns to the queue", type(exc).__name__)
                rt.work.set()
                return True
            # Timeout / transport loss: Codex may have taken it. Never replay; resolve from history later.
            self.store.mark_steer_unknown(steer_id)
            log.warning("steer outcome unknown (%s: %s); not replaying", kind.value, type(exc).__name__)
            return True
        self.faults.hit("crash_after_steer_send")
        self.store.accept_steer(steer_id)
        if thread_id:
            self.store.close_open_questions(thread_id)
        log.info("steered %d message(s) into turn %d of %s", len(ids), turn_id, h(rt.key_str))
        return True

    async def _command_worker(self, rt: SessionRuntime) -> None:
        while True:
            inbound_id, msg, name, args = await rt.commands.get()
            await self._run_command(rt, inbound_id, msg, name, args, barrier=name in BARRIER_COMMANDS)

    async def _run_command(
        self, rt: SessionRuntime, inbound_id: int, msg: InboundMessage, name: str, args: str, *, barrier: bool
    ) -> None:
        from .commands import execute

        try:
            reply = await execute(self, rt, msg, name, args)
            if reply:
                self.outbox.notice(rt.key_str, msg.chat, reply, reply_to=msg.reply_to)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("command /%s failed", name)
            self.outbox.notice(rt.key_str, msg.chat, f"❌ 命令 /{name} 执行失败", reply_to=msg.reply_to)
        finally:
            self.store.set_inbound_state([inbound_id], "done")
            if barrier:
                rt.controls_pending = max(0, rt.controls_pending - 1)
                if rt.controls_pending == 0:
                    rt.work.set()

    # ------------------------------------------------------------------ worker
    async def _worker(self, rt: SessionRuntime) -> None:
        while not self._closing:
            await rt.work.wait()
            rt.work.clear()
            while not self._closing:
                async with rt.lock:
                    if rt.controls_pending or rt.state != IDLE:
                        break
                    rows = self.store.queued_messages(rt.key_str)
                    if not rows:
                        break
                    rt.state = RUNNING
                    rt.idle.clear()
                    rt.stop_requested = False
                    rt.abandoned = False
                delay = 0.0
                rt.current = asyncio.ensure_future(self._run_turn(rt, rows))
                try:
                    await rt.current
                except asyncio.CancelledError:
                    if not rt.abandoned or self._closing:
                        raise
                except RetryLater as exc:
                    delay = exc.delay
                except Exception:
                    log.exception("turn execution failed for %s", h(rt.key_str))
                finally:
                    rt.current = None
                    async with rt.lock:
                        rt.state = IDLE
                        rt.handle = None
                        rt.turn_id = None
                        rt.idle.set()
                if delay:
                    await asyncio.sleep(delay)

    def _rate_ok(self, key_str: str) -> bool:
        limit = self.settings.rate_limit_per_min
        if limit <= 0:
            return True
        now = time.monotonic()
        window = self._rate.setdefault(key_str, collections.deque())
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        return True

    @staticmethod
    def compose_input(rows: list[sqlite3.Row]) -> str:
        parts: list[str] = []
        for row in rows:
            text = str(row["text"])
            if int(row["is_group"]) and not int(row["composed"]):
                name = str(row["user_name"] or row["user_id"]).replace("\n", " ").replace("]", ")")[:40]
                text = f"[{name}]: {text}"
            parts.append(text)
        return "\n\n".join(parts)

    @staticmethod
    def _title(rows: list[sqlite3.Row]) -> str:
        first = str(rows[0]["text"]).strip().splitlines()[0] if rows and str(rows[0]["text"]).strip() else ""
        return first[:40]

    async def _run_turn(self, rt: SessionRuntime, rows: list[sqlite3.Row]) -> None:
        key_str = rt.key_str
        ids = [int(r["id"]) for r in rows]
        chat = ChatRef.from_json(rows[-1]["chat_json"])
        reply_to = rows[-1]["reply_to"]
        session = self.store.get_session(key_str)
        adapter = self.adapters.get(rt.key.platform)
        if session is None or adapter is None:
            log.error("no session or adapter for %s; dropping %d message(s)", h(key_str), len(ids))
            self.store.set_inbound_state(ids, "ignored")
            return
        if not self._rate_ok(key_str):
            self.store.set_inbound_state(ids, "ignored")
            self.outbox.notice(key_str, chat, TEXT_RATE_LIMITED, reply_to=reply_to)
            return

        turn_created = False
        token: Reservation | None = None
        cwd = ""
        try:
            try:
                thread = await self._ensure_thread(rt, session, rows)
            except ContextLost:
                self.store.set_inbound_state(ids, "ignored")
                self.outbox.notice(key_str, chat, TEXT_CONTEXT_LOST, reply_to=reply_to)
                return
            except StartRejected as exc:
                self.store.set_inbound_state(ids, "ignored")
                self.outbox.notice(key_str, chat, TEXT_REJECTED.format(error=exc), reply_to=reply_to)
                return
            except WorkspaceFailed as exc:
                self.store.set_inbound_state(ids, "ignored")
                self.outbox.notice(key_str, chat, TEXT_WORKSPACE_FAILED.format(error=exc), reply_to=reply_to)
                return
            except CodexUnavailable as exc:
                if not rt.unavailable_notified:
                    rt.unavailable_notified = True
                    self.outbox.notice(key_str, chat, TEXT_UNAVAILABLE, reply_to=reply_to)
                rt.retry_delay = min(60.0, max(2.0, rt.retry_delay * 2))
                log.warning("codex unavailable for %s (%s); retry in %.0fs", h(key_str), exc, rt.retry_delay)
                raise RetryLater(rt.retry_delay) from exc
            rt.unavailable_notified = False
            rt.retry_delay = 0.0
            rt.thread_id = str(thread["thread_id"])
            cwd = str(thread["cwd"])

            token = await self._wait_hold(rt, session, cwd, chat, reply_to)
            if token is None or rt.stop_requested:
                self.store.set_inbound_state(ids, "ignored")
                self.outbox.notice(key_str, chat, TEXT_CANCELLED, reply_to=reply_to)
                return

            text = self.compose_input(rows)
            turn_id = self.store.create_turn(key_str, rt.thread_id, text, ids, reply_to, cwd=cwd)
            self.holds.bind(cwd, token, turn_id)  # from now on only the Finalizer releases it
            turn_created = True
            rt.turn_id = turn_id
            self.faults.hit("crash_after_turn_pending")
            self.store.close_open_questions(rt.thread_id)
            await self._execute_turn(rt, session, thread, turn_id, text, adapter, chat, reply_to)
        except asyncio.CancelledError:
            if rt.abandoned and not turn_created:
                self.store.set_inbound_state(ids, "ignored")
            raise
        finally:
            if token is not None and not turn_created:
                self.holds.cancel(cwd, token)

    async def _wait_hold(
        self, rt: SessionRuntime, session: sqlite3.Row, cwd: str, chat: ChatRef, reply_to: str | None
    ) -> Reservation | None:
        owner = self.holds.owner(cwd)
        if owner is not None:
            same_session = False
            if isinstance(owner, int):
                held = self.store.get_turn(owner)
                same_session = held is not None and held["session_key"] == rt.key_str
            text = TEXT_HOLD_SELF if same_session else TEXT_PROJECT_BUSY.format(project=session["project"] or cwd)
            self.outbox.notice(rt.key_str, chat, text, reply_to=reply_to)
        return await self.holds.acquire(cwd, lambda: rt.stop_requested or self._closing)

    async def _ensure_thread(self, rt: SessionRuntime, session: sqlite3.Row, rows: list[sqlite3.Row]) -> sqlite3.Row:
        key_str = rt.key_str
        thread_id = session["active_thread_id"]
        if thread_id:
            trow = self.store.get_thread(thread_id)
            if trow is not None:
                if trow["state"] == "lost":
                    raise ContextLost()
                try:
                    await self.runtime.load_thread(
                        thread_id, cwd=trow["cwd"], model=session["model"], provider=trow["provider"]
                    )
                    return trow
                except Exception as exc:
                    kind = classify(exc)
                    if kind is ErrorKind.REJECTED:
                        raise StartRejected(short_message(exc)) from exc
                    if kind is not ErrorKind.NOT_FOUND:
                        if kind is ErrorKind.TRANSIENT:
                            await self.runtime.reset()
                        raise CodexUnavailable(short_message(exc)) from exc
                    if trow["state"] == "created" and int(trow["turn_count"]) == 0:
                        # D14: never materialized, so no work can be lost. Replace silently.
                        log.info("thread %s was never materialized; starting a new one", h(thread_id))
                        self.store.set_thread_state(thread_id, "discarded")
                    else:
                        log.warning("thread %s no longer exists in codex; marking lost", h(thread_id))
                        self.store.set_thread_state(thread_id, "lost")
                        raise ContextLost() from exc

        try:
            cwd = await asyncio.wait_for(
                self.workspace.cwd_for_new_thread(rt.key, session["project"]), self.settings.rpc_timeout
            )
        except (AgentdUnreachable, asyncio.TimeoutError) as exc:
            raise CodexUnavailable(f"agentd: {exc or type(exc).__name__}") from exc
        except AgentdError as exc:
            raise WorkspaceFailed(str(exc)) from exc
        try:
            new_id = await self.runtime.start_thread(cwd=cwd, model=session["model"], provider=session["provider"])
        except Exception as exc:
            kind = classify(exc)
            if kind is ErrorKind.REJECTED:
                raise StartRejected(short_message(exc)) from exc
            if kind is ErrorKind.TRANSIENT:
                await self.runtime.reset()
            raise CodexUnavailable(short_message(exc)) from exc
        self.store.insert_thread(new_id, key_str, cwd=cwd, provider=session["provider"], title=self._title(rows))
        trow = self.store.get_thread(new_id)
        assert trow is not None
        return trow

    def _to_reconcile(self, turn_id: int, error: str | None = None, *, interrupt: bool = False) -> None:
        self.store.set_turn_state(turn_id, "abandoned" if interrupt else "reconciling", error)
        self.recovery.schedule(turn_id, delay=1.0, interrupt=interrupt)

    async def _execute_turn(
        self,
        rt: SessionRuntime,
        session: sqlite3.Row,
        thread: sqlite3.Row,
        turn_id: int,
        text: str,
        adapter: PlatformAdapter,
        chat: ChatRef,
        reply_to: str | None,
    ) -> None:
        renderer = StreamRenderer(adapter, chat, reply_to, self.settings.stream_interval)
        try:
            await renderer.start()
            if renderer.stream_ref is not None:
                self.store.set_turn_stream_ref(turn_id, renderer.stream_ref.to_json())
            try:
                handle = await self.runtime.start_turn(
                    str(thread["thread_id"]), text, model=session["model"], cwd=thread["cwd"]
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                kind = classify(exc)
                if kind is ErrorKind.REJECTED:
                    self.finalizer.finalize(turn_id, status="failed", error=short_message(exc))
                    return
                # Timeout or transport loss: Codex may have started it. Reconcile from history (D14).
                if kind is ErrorKind.TRANSIENT:
                    await self.runtime.reset()
                self._to_reconcile(turn_id, short_message(exc))
                return
            self.store.set_turn_started(turn_id, str(handle.id))
            self.faults.hit("crash_after_turn_start")
            async with rt.lock:
                rt.handle = handle
                stop_now = rt.stop_requested
            if stop_now:
                await self._interrupt_until(handle, self._deadline(self.settings.rpc_timeout))
            await self._consume(rt, handle, renderer, turn_id, str(thread["thread_id"]), chat)
        except asyncio.CancelledError:
            if rt.abandoned:
                row = self.store.get_turn(turn_id)
                if row is not None and row["state"] in ("pending", "started"):
                    # R3: the local consumer is gone, the remote turn may still run: keep the hold.
                    self._to_reconcile(turn_id, "stop timeout", interrupt=True)
                    self.outbox.notice(rt.key_str, chat, TEXT_ABANDONED)
            raise
        except Exception:
            log.exception("turn %d failed unexpectedly; reconciling from history", turn_id)
            row = self.store.get_turn(turn_id)
            if row is not None and row["state"] in ("pending", "started"):
                self._to_reconcile(turn_id, "internal error")
        finally:
            await renderer.close()

    async def _consume(
        self,
        rt: SessionRuntime,
        handle: Any,
        renderer: StreamRenderer,
        turn_id: int,
        thread_id: str,
        chat: ChatRef,
    ) -> None:
        items: list[dict[str, Any]] = []
        status: str | None = None
        error: str | None = None
        questions = 0
        handle_id = str(handle.id)
        stream = handle.stream()
        try:
            async for note in stream:
                method = str(getattr(note, "method", "") or "")
                data = payload_dict(note)
                tid = turn_id_of(method, data)
                if tid is not None and tid != handle_id:
                    continue
                if method == "item/agentMessage/delta":
                    renderer.on_delta(str(data.get("itemId") or ""), str(data.get("delta") or ""))
                elif method == "item/completed":
                    item = data.get("item")
                    if not isinstance(item, dict) or item.get("type") != "agentMessage":
                        continue
                    item_id = str(item.get("id") or "")
                    if is_async_message(item):
                        # D13: non-blocking clarification — delivered on its own, never via final_response.
                        questions += 1
                        renderer.drop(item_id)
                        self.store.add_open_question(thread_id, turn_id, item_id)
                        self.outbox.async_question(
                            rt.key_str, chat, turn_id, questions, "❓ " + format_questions(item)
                        )
                    else:
                        items.append(item)
                        renderer.on_agent_message(item_id, str(item.get("text") or ""))
                elif method == "error":
                    if not data.get("willRetry"):
                        err = data.get("error")
                        if isinstance(err, dict) and err.get("message"):
                            error = str(err["message"])
                elif method == "turn/completed":
                    turn = data.get("turn") or {}
                    status = str(turn.get("status") or "")
                    err = turn.get("error")
                    if isinstance(err, dict) and err.get("message"):
                        error = str(err["message"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            kind = classify(exc)
            log.warning("turn stream for %s broke (%s: %s)", h(rt.key_str), kind.value, type(exc).__name__)
            if kind is ErrorKind.TRANSIENT:
                await self.runtime.reset()
            self._to_reconcile(turn_id, short_message(exc))
            return
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()

        final = pick_final_text(items)
        if status in ("completed", "interrupted", "failed"):
            self.finalizer.finalize(turn_id, status=status, final_text=final, error=error, has_questions=questions > 0)
        else:
            log.warning("turn stream ended without a terminal status (%r); reconciling", status)
            self._to_reconcile(turn_id)

    # ------------------------------------------------------------------ stop / new (D15, R4)
    async def _interrupt_until(self, handle: Any, deadline: float) -> None:
        remaining = self._left(deadline)
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(handle.interrupt(), remaining)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.info("interrupt did not complete (%s)", type(exc).__name__)

    async def _wait_idle_until(self, rt: SessionRuntime, deadline: float) -> bool:
        remaining = self._left(deadline)
        if remaining <= 0:
            return rt.idle.is_set()
        try:
            await asyncio.wait_for(rt.idle.wait(), remaining)
            return True
        except asyncio.TimeoutError:
            return False

    async def _abandon(self, rt: SessionRuntime) -> None:
        """Stop waiting for the local consumer. The remote turn keeps its hold (R3)."""
        log.warning("stop deadline exceeded for %s; abandoning the local consumer", h(rt.key_str))
        rt.abandoned = True
        task = rt.current
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=5)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(rt.idle.wait(), 5)

    def _close_questions(self, rt: SessionRuntime) -> None:
        thread_id = rt.thread_id
        if thread_id is None:
            session = self.store.get_session(rt.key_str)
            thread_id = session["active_thread_id"] if session is not None else None
        if thread_id:
            self.store.close_open_questions(thread_id)

    async def _stop_running(self, rt: SessionRuntime, deadline: float) -> str:
        """Common part of /stop and /new for a running turn: interrupt, wait, abandon at the deadline."""
        async with rt.lock:
            handle = rt.handle
        if handle is not None:
            await self._interrupt_until(handle, deadline)
        if await self._wait_idle_until(rt, deadline):
            return "stopped"
        await self._abandon(rt)
        return "abandoned"

    async def request_stop(self, rt: SessionRuntime) -> str:
        deadline = self._deadline(self.settings.stop_timeout)
        async with rt.lock:
            if rt.state == STOPPING:
                return "already"
            if rt.state == IDLE:
                self._close_questions(rt)
                rows = self.store.queued_messages(rt.key_str)
                if rows:
                    self.store.set_inbound_state([int(r["id"]) for r in rows], "ignored")
                    return "cancelled_queue"
                unresolved = self.store.unfinished_turns(rt.key_str)
                if unresolved:
                    for row in unresolved:
                        self.recovery.schedule(int(row["id"]), delay=0.1, interrupt=True)
                    return "unresolved"
                return "idle"
            rt.stop_requested = True
            rt.state = STOPPING
        self._close_questions(rt)
        return await self._stop_running(rt, deadline)

    async def request_new(self, rt: SessionRuntime) -> str:
        deadline = self._deadline(self.settings.stop_timeout)
        async with rt.lock:
            was_running = rt.state in (RUNNING, STOPPING)
            if rt.state == RUNNING:
                rt.stop_requested = True
                rt.state = STOPPING
        self._close_questions(rt)
        if was_running:
            await self._stop_running(rt, deadline)
        async with rt.lock:
            self.store.set_active_thread(rt.key_str, None)
            rt.thread_id = None
        return "stopped_and_new" if was_running else "new"

    # ------------------------------------------------------------------ other commands (D7)
    def _threads_for(self, rt: SessionRuntime, limit: int = 10) -> list[sqlite3.Row]:
        return self.store.list_threads(rt.key_str, limit=limit)

    async def resume(self, rt: SessionRuntime, ref: str) -> str:
        async with rt.lock:
            if rt.state != IDLE:
                return "当前有任务在运行，请先发 /stop。"
            target = self._resolve_thread(rt, ref)
            if target is None:
                return "不存在。用 /sessions 查看本会话的历史。"
            if target["state"] == "lost":
                return "这个会话已经丢失，无法恢复。"
            self.store.set_active_thread(rt.key_str, target["thread_id"])
            self.store.set_session_provider(rt.key_str, target["provider"])
            rt.thread_id = str(target["thread_id"])
            title = target["title"] or ""
            return f"已切换到会话 {str(target['thread_id'])[:8]}（{title}）。"

    def _resolve_thread(self, rt: SessionRuntime, ref: str) -> sqlite3.Row | None:
        ref = ref.strip()
        if not ref:
            return None
        if ref.isdigit() and len(ref) <= 2:  # list index; longer digit strings are id prefixes
            listed = self._threads_for(rt, limit=10)
            index = int(ref)
            return listed[index - 1] if 1 <= index <= len(listed) else None
        if len(ref) < 6:
            return None
        matches = [t for t in self._threads_for(rt, limit=200) if str(t["thread_id"]).startswith(ref)]
        return matches[0] if len(matches) == 1 else None

    async def retry(self, rt: SessionRuntime) -> str:
        async with rt.lock:
            if rt.state != IDLE:
                return "当前有任务在运行，请先发 /stop。"
            turn = self.store.last_retryable_turn(rt.key_str)
            if turn is None:
                return "没有可以重试的任务。"
            session = self.store.get_session(rt.key_str)
            if session is None:
                return "没有可以重试的任务。"
            chat = ChatRef.from_json(session["chat_json"])
            msg = InboundMessage(
                platform=rt.key.platform,
                message_key=f"retry:{turn['id']}:{time.time_ns()}",
                chat=chat,
                user_id="retry",
                text=str(turn["input_text"]),
                reply_to=turn["reply_to"],
            )
            self.store.insert_inbound(msg, kind="message", composed=True)
            self.store.set_turn_state(int(turn["id"]), "retried")
        return "🔁 正在重新执行上一个任务。"

    async def release(self, rt: SessionRuntime, msg: InboundMessage) -> str:
        """Escape hatch for a turn whose remote end can never be confirmed (R3). Admin only."""
        if not self.admission.is_admin(msg.platform, msg.user_id):
            return "你没有执行 /release 的权限。"
        async with rt.lock:
            if rt.state != IDLE:
                return "当前有任务在运行，请先发 /stop。"
            rows = self.store.unfinished_turns(rt.key_str)
        if not rows:
            return "没有需要释放的任务。"
        for row in rows:
            log.warning("turn %d force-released by %s", int(row["id"]), h(msg.user_id))
            self.finalizer.finalize(int(row["id"]), status="released")
        return f"已强制释放 {len(rows)} 个未确认结束的任务。"

    async def set_model(self, rt: SessionRuntime, slug: str) -> str:
        session = self.store.get_session(rt.key_str)
        if session is None:
            return "会话不存在。"
        if not slug:
            return f"当前 model：{session['model']}"
        if len(slug) > 100 or any(c.isspace() for c in slug):
            return "model 名称无效。"
        self.store.set_session_model(rt.key_str, slug)
        return f"model 已切换为 {slug}，从下一个任务开始生效。"

    async def set_provider(self, rt: SessionRuntime, provider: str, msg: InboundMessage) -> str:
        session = self.store.get_session(rt.key_str)
        if session is None:
            return "会话不存在。"
        if not provider:
            allowed = "、".join(self.settings.allowed_providers)
            return f"当前 provider：{session['provider']}（可选：{allowed}）"
        if provider not in self.settings.allowed_providers:
            return f"不允许的 provider：{provider}"
        if not self.admission.is_admin(msg.platform, msg.user_id):
            return "你没有切换 provider 的权限。"
        async with rt.lock:
            if rt.state != IDLE:
                return "当前有任务在运行，请先发 /stop。"
            if provider == session["provider"]:
                return f"已经在使用 {provider}。"
            self.store.set_session_provider(rt.key_str, provider)
            self.store.set_active_thread(rt.key_str, None)
            rt.thread_id = None
        return f"provider 已切换为 {provider}。上下文已重置（provider 绑定在会话上，旧会话可用 /sessions 找回）。"

    async def compact(self, rt: SessionRuntime) -> str:
        async with rt.lock:  # check only; the RPC below runs outside the lock (R4), guarded by the barrier
            if rt.state != IDLE:
                return "当前有任务在运行，请稍后再试。"
            session = self.store.get_session(rt.key_str)
            thread_id = session["active_thread_id"] if session is not None else None
            trow = self.store.get_thread(thread_id) if thread_id else None
        if session is None or trow is None or not thread_id:
            return "当前没有会话可以压缩。"
        try:
            await self.runtime.load_thread(thread_id, cwd=trow["cwd"], model=session["model"], provider=trow["provider"])
            await self.runtime.compact(thread_id)
        except Exception as exc:
            return f"压缩失败：{short_message(exc)}"
        return "已请求 Codex 压缩上下文。"

    def status_text(self, rt: SessionRuntime) -> str:
        session = self.store.get_session(rt.key_str)
        if session is None:
            return "会话不存在。"
        thread_id = session["active_thread_id"]
        trow = self.store.get_thread(thread_id) if thread_id else None
        queued = len(self.store.queued_messages(rt.key_str))
        open_q = self.store.open_question_count(thread_id) if thread_id else 0
        unresolved = len(self.store.unfinished_turns(rt.key_str)) if rt.state == IDLE else 0
        cwd = trow["cwd"] if trow is not None else "（下一条消息时创建）"
        lines = [
            f"状态：{STATE_LABELS.get(rt.state, rt.state)}",
            f"会话：{str(thread_id)[:8] if thread_id else '（下一条消息时创建）'}",
            f"provider：{session['provider']}",
            f"model：{session['model']}",
            f"工作目录：{cwd}",
            f"项目：{session['project'] or '无'}",
            f"排队消息：{queued}",
            f"待回答的问题：{open_q}",
        ]
        if unresolved:
            lines.append(f"等待 Codex 确认结束的任务：{unresolved}")
        return "\n".join(lines)

    def sessions_text(self, rt: SessionRuntime) -> str:
        session = self.store.get_session(rt.key_str)
        active = session["active_thread_id"] if session is not None else None
        threads = self._threads_for(rt, limit=10)
        if not threads:
            return "本会话还没有历史。"
        lines = ["本会话的历史（/resume <序号> 切换）："]
        for index, t in enumerate(threads, 1):
            when = time.strftime("%m-%d %H:%M", time.localtime(float(t["last_active_at"])))
            marks = ""
            if t["thread_id"] == active:
                marks += "（当前）"
            if t["state"] == "lost":
                marks += "（已丢失）"
            title = (t["title"] or "").replace("\n", " ")
            lines.append(f"{index}. {str(t['thread_id'])[:8]} {title} · {t['turn_count']} 轮 · {when}{marks}")
        return "\n".join(lines)
