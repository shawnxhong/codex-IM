"""agentd: one JSON request per TCP connection, newline-delimited, shared-token authenticated."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any

from .ops import OPERATIONS, OpError, WorkspaceOps

log = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 64 * 1024


class AgentdServer:
    def __init__(self, ops: WorkspaceOps, token: str) -> None:
        if not token:
            raise ValueError("agentd token is required")
        self._ops = ops
        self._token = token

    async def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        if not hmac.compare_digest(str(request.get("token") or ""), self._token):
            return {"ok": False, "error": "unauthorized"}
        op = request.get("op")
        if op not in OPERATIONS:
            return {"ok": False, "error": "unknown operation"}
        args = request.get("args") or {}
        if not isinstance(args, dict) or set(args) != set(OPERATIONS[op]):
            return {"ok": False, "error": "invalid arguments"}
        try:
            result = await getattr(self._ops, op)(**args)
        except OpError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            log.exception("agentd operation %s failed", op)
            return {"ok": False, "error": "internal error"}
        return {"ok": True, "result": result}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 30)
            if len(line) > MAX_REQUEST_BYTES:
                response: dict[str, Any] = {"ok": False, "error": "request too large"}
            else:
                try:
                    request = json.loads(line)
                except ValueError:
                    response = {"ok": False, "error": "invalid json"}
                else:
                    response = (
                        await self.dispatch(request)
                        if isinstance(request, dict)
                        else {"ok": False, "error": "invalid request"}
                    )
            writer.write((json.dumps(response) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception:
            log.debug("agentd connection error", exc_info=True)
        finally:
            writer.close()

    async def serve(self, host: str, port: int) -> None:
        server = await asyncio.start_server(self.handle, host, port, limit=MAX_REQUEST_BYTES + 1)
        log.info("agentd listening on %s:%d", host, port)
        async with server:
            await server.serve_forever()
