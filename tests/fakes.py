"""Test doubles: a scripted stand-in for CodexRuntime, and notification builders.

Notifications mimic the SDK's ``Notification(method, payload)``; payloads expose ``params`` like the SDK's
``UnknownNotification`` so ``codex.events.payload_dict`` reads them as plain dicts.
"""

from __future__ import annotations

import asyncio
import itertools
from collections import deque
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any


class Note:
    def __init__(self, method: str, params: dict[str, Any]) -> None:
        self.method = method
        self.payload = SimpleNamespace(params=params)


def delta(turn_id: str, item_id: str, text: str) -> Note:
    return Note("item/agentMessage/delta", {"turnId": turn_id, "itemId": item_id, "delta": text})


def agent_item(
    turn_id: str, item_id: str, text: str, *, phase: str | None = "final_answer", delivery: str | None = None
) -> Note:
    item = {"type": "agentMessage", "id": item_id, "text": text, "phase": phase, "delivery": delivery}
    return Note("item/completed", {"turnId": turn_id, "item": item})


def completed(turn_id: str, status: str = "completed", error: str | None = None) -> Note:
    turn = {"id": turn_id, "status": status, "error": {"message": error} if error else None}
    return Note("turn/completed", {"turn": turn})


Script = Callable[[str], list[Any]]


def answer(text: str) -> Script:
    return lambda tid: [delta(tid, "m1", text[:3]), delta(tid, "m1", text[3:]), agent_item(tid, "m1", text), completed(tid)]


def hold() -> Script:
    """A turn that keeps running until the test pushes more notifications."""
    return lambda tid: []


class FakeTurnHandle:
    def __init__(self, thread_id: str, turn_id: str, notes: list[Any], record: dict[str, Any]) -> None:
        self.thread_id = thread_id
        self.id = turn_id
        self.steered: list[str] = []
        self.interrupted = False
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._record = record
        for note in notes:
            self.push(note)

    def push(self, note: Any) -> None:
        self._queue.put_nowait(note)
        if isinstance(note, Note) and note.method == "turn/completed":
            self._record["status"] = note.payload.params["turn"]["status"]
        if isinstance(note, Note) and note.method == "item/completed":
            self._record["items"].append(note.payload.params["item"])

    def finish(self, text: str = "done", status: str = "completed") -> None:
        self.push(agent_item(self.id, "fin", text))
        self.push(completed(self.id, status))

    async def stream(self):  # noqa: ANN201 - async generator
        while True:
            note = await self._queue.get()
            if isinstance(note, BaseException):
                raise note
            yield note
            if note.method == "turn/completed":
                return

    async def steer(self, text: str) -> None:
        self.steered.append(text)

    async def interrupt(self) -> None:
        if not self.interrupted:
            self.interrupted = True
            self.push(completed(self.id, "interrupted"))


class FakeRuntime:
    def __init__(self) -> None:
        self.threads: dict[str, dict[str, Any]] = {}
        self.loaded: set[str] = set()
        self.scripts: deque[Script] = deque()
        self.handles: list[FakeTurnHandle] = []
        self.started_turns: list[tuple[str, str]] = []  # (thread_id, text)
        self.fail_start_thread: deque[BaseException] = deque()
        self.fail_load_thread: deque[BaseException] = deque()
        self.fail_start_turn: deque[BaseException] = deque()
        self.resets = 0
        self.compacted: list[str] = []
        self._ids = itertools.count(1)

    # -- helpers for tests
    def add_thread(self, thread_id: str, turns: list[dict[str, Any]] | None = None) -> None:
        self.threads[thread_id] = {"turns": list(turns or [])}

    def forget_thread(self, thread_id: str) -> None:
        self.threads.pop(thread_id, None)
        self.loaded.discard(thread_id)

    # -- CodexRuntime interface
    async def start_thread(self, *, cwd: str, model: str, provider: str) -> str:
        if self.fail_start_thread:
            raise self.fail_start_thread.popleft()
        thread_id = f"thread-{next(self._ids):04d}-0000"
        self.threads[thread_id] = {"turns": [], "cwd": cwd, "provider": provider}
        self.loaded.add(thread_id)
        return thread_id

    async def load_thread(self, thread_id: str, *, cwd: str, model: str, provider: str) -> Any:
        if self.fail_load_thread:
            raise self.fail_load_thread.popleft()
        if thread_id not in self.threads:
            from openai_codex import InvalidRequestError

            raise InvalidRequestError(-32600, f"thread not found: {thread_id}")
        self.loaded.add(thread_id)
        return object()

    def is_loaded(self, thread_id: str) -> bool:
        return thread_id in self.loaded

    async def start_turn(self, thread_id: str, text: str, *, model: str, cwd: str) -> FakeTurnHandle:
        if self.fail_start_turn:
            raise self.fail_start_turn.popleft()
        if thread_id not in self.loaded:
            raise RuntimeError("thread is not loaded on the current connection")
        turn_id = f"turn-{next(self._ids):04d}"
        script = self.scripts.popleft() if self.scripts else answer("ok")
        record = {
            "id": turn_id,
            "status": "inProgress",
            "items": [{"type": "userMessage", "content": [{"type": "text", "text": text}]}],
        }
        self.threads[thread_id]["turns"].append(record)
        handle = FakeTurnHandle(thread_id, turn_id, script(turn_id), record)
        self.handles.append(handle)
        self.started_turns.append((thread_id, text))
        return handle

    async def read_turns(self, thread_id: str) -> list[dict[str, Any]]:
        if thread_id not in self.loaded:
            raise RuntimeError("thread is not loaded on the current connection")
        return list(self.threads[thread_id]["turns"])

    async def compact(self, thread_id: str) -> None:
        self.compacted.append(thread_id)

    async def reset(self) -> None:
        self.resets += 1
        self.loaded.clear()
