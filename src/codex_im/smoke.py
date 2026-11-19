"""``python -m codex_im.smoke`` — post-upgrade smoke test (design D12, verification V9).

Uses the same settings and connection as the bridge: initialize, start a thread, run a read-only turn,
reconnect, resume the thread and find the turn in its history. Exit code 0 on success.
"""

from __future__ import annotations

import asyncio
import posixpath
import sys

from .agentd.client import make_agentd
from .codex.runtime import CodexRuntime
from .config import SettingsError, load_settings
from .logutil import setup_logging


async def _run() -> int:
    settings = load_settings()
    setup_logging(settings.log_level)
    runtime = CodexRuntime(settings)
    agentd = make_agentd(settings)
    cwd = posixpath.join(settings.workspace_root, "smoke")
    steps: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        steps.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{(': ' + detail) if detail else ''}", flush=True)

    try:
        await agentd.ensure_dir(cwd)
        record("agentd ensure_dir", True, cwd)
        thread_id = await runtime.start_thread(
            cwd=cwd, model=settings.default_model, provider=settings.default_provider
        )
        record("thread_start", True, thread_id[:8])
        handle = await runtime.start_turn(thread_id, "Reply with exactly the word: ok", model=settings.default_model, cwd=cwd)
        result = await handle.run()
        answer = (result.final_response or "").strip()
        record("read-only turn", "ok" in answer.lower(), repr(answer[:80]))
        turn_id = str(handle.id)
        await runtime.reset()
        await runtime.load_thread(thread_id, cwd=cwd, model=settings.default_model, provider=settings.default_provider)
        record("reconnect + thread_resume", True)
        turns = await runtime.read_turns(thread_id)
        found = any(str(t.get("id")) == turn_id for t in turns)
        record("turn present in history", found, f"{len(turns)} turn(s)")
    except Exception as exc:  # report and fail
        record("smoke", False, f"{type(exc).__name__}: {exc}")
    finally:
        await runtime.reset()
    return 0 if steps and all(ok for _, ok, _ in steps) else 1


def main() -> int:
    try:
        return asyncio.run(_run())
    except SettingsError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
