"""Turn outcome -> user-visible text -> outbox rows, persisted in one transaction (D14).

The *only* place where a turn becomes terminal, so it is also where the turn's workspace hold is released
(review R3). Shared by the live path (sessions) and recovery.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from ..platforms.base import PlatformAdapter
from .holds import WorkspaceHolds
from .markup import chunk_text
from .outbox import Outbox
from .store import OutboxRow, Store
from .types import DEFAULT_CAPABILITIES, ChatRef

log = logging.getLogger(__name__)

TEXT_EMPTY = "（已完成，没有文字回复）"
TEXT_AWAITING_ANSWER = "（问题见上一条消息，等待你的回答）"
TEXT_STOPPED = "⏹ 已停止"
TEXT_FAILED = "❌ 执行失败"
TEXT_RESTART_INTERRUPTED = "⚠️ 上一个任务因为重启或断线被中断，没有自动重跑；需要的话请发 /retry。"
TEXT_UNKNOWN = "⚠️ 上一个任务的执行状态未知（Codex 中找不到它），没有自动重跑；需要的话请发 /retry。"
TEXT_RELEASED = "⚠️ 上一个任务已被强制释放（Codex 端是否仍在运行未知）。需要的话请发 /retry。"

# status -> stored terminal turn state
_STATE = {
    "completed": "completed",
    "interrupted": "interrupted",
    "failed": "failed",
    "restart_interrupted": "interrupted",
    "unknown": "unknown",
    "released": "released",
}


def compose(status: str, final_text: str | None, error: str | None, *, has_questions: bool = False) -> str:
    final = (final_text or "").strip()
    if status == "completed":
        return final or (TEXT_AWAITING_ANSWER if has_questions else TEXT_EMPTY)
    if status == "interrupted":
        return f"{final}\n\n{TEXT_STOPPED}" if final else TEXT_STOPPED
    if status == "failed":
        return f"{TEXT_FAILED}：{error}" if error else TEXT_FAILED
    if status == "restart_interrupted":
        return f"{final}\n\n{TEXT_RESTART_INTERRUPTED}" if final else TEXT_RESTART_INTERRUPTED
    if status == "released":
        return TEXT_RELEASED
    return TEXT_UNKNOWN


class Finalizer:
    def __init__(
        self, store: Store, outbox: Outbox, adapters: Mapping[str, PlatformAdapter], holds: WorkspaceHolds
    ) -> None:
        self._store = store
        self._outbox = outbox
        self._adapters = adapters
        self._holds = holds
        self.on_terminal: list = []  # callbacks(turn_id) run after a turn became terminal

    def finalize(
        self,
        turn_id: int,
        *,
        status: str,
        final_text: str | None = None,
        error: str | None = None,
        has_questions: bool = False,
    ) -> None:
        turn = self._store.get_turn(turn_id)
        if turn is None:
            log.error("finalize: unknown turn %d", turn_id)
            return
        session = self._store.get_session(turn["session_key"])
        if session is None:
            log.error("finalize: turn %d has no session", turn_id)
            self._store.set_turn_state(turn_id, _STATE.get(status, "unknown"))
            self._holds.release_turn(turn_id)
            return
        chat = ChatRef.from_json(session["chat_json"])
        adapter = self._adapters.get(chat.platform)
        caps = adapter.capabilities if adapter is not None else DEFAULT_CAPABILITIES
        text = compose(status, final_text, error, has_questions=has_questions)
        stream_ref = turn["stream_ref_json"] if caps.can_edit else None
        rows: list[OutboxRow] = []
        for seq, chunk in enumerate(chunk_text(text, caps.chunk_len) or [text]):
            rows.append(
                OutboxRow(
                    kind="final",
                    seq=seq,
                    text=chunk,
                    edit_ref_json=stream_ref if seq == 0 else None,
                    reply_to=turn["reply_to"] if seq == 0 and not stream_ref else None,
                )
            )
        self._store.finish_turn(
            turn_id,
            state=_STATE.get(status, "unknown"),
            final_text=text,
            error=error,
            session_key=str(turn["session_key"]),
            chat_json=chat.to_json(),
            rows=rows,
        )
        self._holds.release_turn(turn_id)
        self._outbox.notify()
        for callback in self.on_terminal:
            try:
                callback(turn_id)
            except Exception:
                log.exception("on_terminal callback failed for turn %d", turn_id)
