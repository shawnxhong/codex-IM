"""Bridge-side access to agentd. ``LocalAgentd`` runs the same operations in-process (single-machine dev)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from .ops import OpError, WorkspaceOps


class AgentdError(Exception):
    """agentd refused the operation (bad path, git failure...)."""


class AgentdUnreachable(Exception):
    """agentd could not be reached (treated as transient by the session layer)."""


class AgentdClient:
    def __init__(self, host: str, port: int, token: str, timeout: float = 150.0) -> None:
        self._host, self._port, self._token, self._timeout = host, port, token, timeout

    async def call(self, op: str, **args: Any) -> Any:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(self._host, self._port), 10)
        except (OSError, asyncio.TimeoutError) as exc:
            raise AgentdUnreachable(str(exc) or type(exc).__name__) from exc
        try:
            writer.write((json.dumps({"op": op, "args": args, "token": self._token}) + "\n").encode("utf-8"))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), self._timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise AgentdUnreachable(str(exc) or type(exc).__name__) from exc
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if not line:
            raise AgentdUnreachable("agentd closed the connection")
        response = json.loads(line)
        if not response.get("ok"):
            raise AgentdError(str(response.get("error") or "agentd error"))
        return response.get("result")

    async def ensure_dir(self, path: str) -> Any:
        return await self.call("ensure_dir", path=path)

    async def worktree_add(self, repo: str, path: str, branch: str) -> Any:
        return await self.call("worktree_add", repo=repo, path=path, branch=branch)


class LocalAgentd:
    def __init__(self, root: str) -> None:
        self._ops = WorkspaceOps(root)

    async def ensure_dir(self, path: str) -> Any:
        try:
            return await self._ops.ensure_dir(path)
        except OpError as exc:
            raise AgentdError(str(exc)) from exc

    async def worktree_add(self, repo: str, path: str, branch: str) -> Any:
        try:
            return await self._ops.worktree_add(repo, path, branch)
        except OpError as exc:
            raise AgentdError(str(exc)) from exc


def make_agentd(settings: Any) -> AgentdClient | LocalAgentd:
    from ..config import parse_tcp_url

    if settings.agentd == "local":
        return LocalAgentd(settings.workspace_root)
    host, port = parse_tcp_url(settings.agentd)
    return AgentdClient(host, port, settings.agentd_token or "")
