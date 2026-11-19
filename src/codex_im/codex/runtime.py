"""The only place that talks to Codex, and only through the public ``openai_codex`` API (D2, AGENTS.md).

Every thread and turn explicitly sets sandbox=full_access and approval_mode=deny_all (D4) and passes the
model (D6). Nothing else is injected (D3).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox

from .. import __version__
from ..config import Settings, parse_tcp_url
from ..logutil import h

log = logging.getLogger(__name__)


def build_codex_config(settings: Settings) -> CodexConfig:
    common: dict[str, Any] = {
        "client_name": "codex_im",
        "client_title": "codex-IM",
        "client_version": __version__,
    }
    if settings.app_server == "local":
        return CodexConfig(**common)
    host, port = parse_tcp_url(settings.app_server)
    # D5: the SDK's "subprocess" is a stdio <-> TCP relay to `codex app-server --listen stdio://` in the
    # agent container. VERIFY(ubuntu): V0 — launch_args_override exists in the pinned SDK release and the
    # relay supports initialize, concurrent turns and reconnects.
    return CodexConfig(launch_args_override=("socat", "-", f"TCP:{host}:{port}"), **common)


class CodexRuntime:
    """Owns one AsyncCodex connection; recreated after transport loss (call :meth:`reset`)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._codex: AsyncCodex | None = None
        self._lock = asyncio.Lock()
        self._threads: dict[str, Any] = {}
        self.generation = 0

    @property
    def connected(self) -> bool:
        return self._codex is not None

    async def _client(self) -> AsyncCodex:
        async with self._lock:
            if self._codex is None:
                codex = AsyncCodex(build_codex_config(self._settings))
                try:
                    await codex.__aenter__()
                except BaseException:
                    with contextlib.suppress(Exception):
                        await codex.close()
                    raise
                self._codex = codex
                self._threads.clear()
                self.generation += 1
                log.info("connected to codex app-server (generation %d)", self.generation)
            return self._codex

    async def reset(self) -> None:
        async with self._lock:
            codex, self._codex = self._codex, None
            self._threads.clear()
        if codex is not None:
            log.warning("resetting codex connection (generation %d)", self.generation)
            with contextlib.suppress(Exception):
                await codex.close()

    @staticmethod
    def _policy() -> dict[str, Any]:
        return {"approval_mode": ApprovalMode.deny_all, "sandbox": Sandbox.full_access}

    async def start_thread(self, *, cwd: str, model: str, provider: str) -> str:
        codex = await self._client()
        thread = await codex.thread_start(
            cwd=cwd,
            model=model,
            model_provider=provider,
            developer_instructions=self._settings.developer_instructions,
            **self._policy(),
        )
        self._threads[thread.id] = thread
        log.info("thread started %s", h(thread.id))
        return str(thread.id)

    async def load_thread(self, thread_id: str, *, cwd: str, model: str, provider: str) -> Any:
        """Return a loaded thread handle, resuming it on this connection if needed."""
        codex = await self._client()
        thread = self._threads.get(thread_id)
        if thread is None:
            thread = await codex.thread_resume(
                thread_id,
                cwd=cwd,
                model=model,
                model_provider=provider,
                **self._policy(),
            )
            if str(thread.id) != thread_id:
                raise RuntimeError(f"resume returned a different thread ({h(thread.id)} != {h(thread_id)})")
            self._threads[thread_id] = thread
            log.info("thread resumed %s", h(thread_id))
        return thread

    def is_loaded(self, thread_id: str) -> bool:
        return thread_id in self._threads

    async def start_turn(self, thread_id: str, text: str, *, model: str, cwd: str) -> Any:
        """Start a turn on a loaded thread and return the AsyncTurnHandle."""
        await self._client()
        thread = self._threads.get(thread_id)
        if thread is None:
            raise RuntimeError("thread is not loaded on the current connection")
        return await thread.turn(text, model=model, cwd=cwd, **self._policy())

    async def read_turns(self, thread_id: str) -> list[dict[str, Any]]:
        await self._client()
        thread = self._threads.get(thread_id)
        if thread is None:
            raise RuntimeError("thread is not loaded on the current connection")
        response = await thread.read(include_turns=True)
        data = response.model_dump(mode="json", by_alias=True)
        turns = (data.get("thread") or {}).get("turns") or []
        return [t for t in turns if isinstance(t, dict)]

    async def compact(self, thread_id: str) -> None:
        await self._client()
        thread = self._threads.get(thread_id)
        if thread is None:
            raise RuntimeError("thread is not loaded on the current connection")
        await thread.compact()
