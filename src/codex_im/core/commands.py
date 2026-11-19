"""Bridge commands (design D7). They are handled by the bridge and never sent to Codex."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .types import InboundMessage

if TYPE_CHECKING:
    from .sessions import SessionManager, SessionRuntime

COMMANDS = frozenset(
    {"new", "stop", "status", "sessions", "resume", "retry", "model", "provider", "compact", "help", "start"}
)

HELP_TEXT = """可用命令：
/new — 开始新会话（旧会话进入历史）
/stop — 停止当前任务
/status — 查看状态
/sessions — 本会话的历史
/resume <序号> — 切换到历史会话
/retry — 重新执行上一个被中断的任务
/model [名称] — 查看或切换 model
/provider [id] — 查看或切换 provider（会重置上下文）
/compact — 压缩当前上下文
/help — 显示本帮助"""


def parse_command(text: str, bot_username: str | None = None) -> tuple[str, str] | None:
    """``/name[@bot] args`` -> (name, args). Unknown commands are treated as normal text (e.g. paths)."""
    stripped = text.strip()
    if not stripped.startswith("/") or len(stripped) < 2:
        return None
    head, _, rest = stripped.partition(" ")
    if "\n" in head:
        head, _, extra = head.partition("\n")
        rest = (extra + " " + rest).strip()
    name = head[1:]
    if "@" in name:
        name, _, target = name.partition("@")
        if bot_username and target.lower() != bot_username.lower():
            return None
    name = name.lower()
    if name not in COMMANDS:
        return None
    return name, rest.strip()


async def execute(mgr: SessionManager, rt: SessionRuntime, msg: InboundMessage, name: str, args: str) -> str | None:
    if name in ("help", "start"):
        return HELP_TEXT
    if name == "stop":
        result = await mgr.request_stop(rt)
        return {
            "idle": "当前没有运行中的任务。",
            "already": "正在停止中…",
            "cancelled_queue": "已取消排队中的消息。",
        }.get(result)  # "stopped": the turn's own final message says "已停止"
    if name == "new":
        result = await mgr.request_new(rt)
        if result == "stopped_and_new":
            return "🆕 已停止当前任务并开始新会话（旧会话可用 /sessions 找回）。"
        return "🆕 已开始新会话（旧会话可用 /sessions 找回）。"
    if name == "status":
        return mgr.status_text(rt)
    if name == "sessions":
        return mgr.sessions_text(rt)
    if name == "resume":
        return await mgr.resume(rt, args)
    if name == "retry":
        return await mgr.retry(rt)
    if name == "model":
        return await mgr.set_model(rt, args)
    if name == "provider":
        return await mgr.set_provider(rt, args, msg)
    if name == "compact":
        return await mgr.compact(rt)
    return None
