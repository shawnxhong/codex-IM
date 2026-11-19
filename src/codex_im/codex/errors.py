"""Classify Codex failures (design D14).

TRANSIENT  -> retry with backoff, keep messages queued, never start a replacement thread.
NOT_FOUND  -> the thread really does not exist (only meaningful for resume/read).
REJECTED   -> the server understood and refused the request; retrying the same request will not help.
UNKNOWN    -> anything else; callers treat it like TRANSIENT with a bounded number of attempts.

VERIFY(ubuntu): V3f — exact error classes / codes / messages for each resume and turn failure must be
recorded on Ubuntu and the marker lists below adjusted, with tests.
"""

from __future__ import annotations

import asyncio
from enum import Enum

from openai_codex import (
    InternalRpcError,
    InvalidParamsError,
    InvalidRequestError,
    JsonRpcError,
    MethodNotFoundError,
    ParseError,
    ServerBusyError,
    TransportClosedError,
    is_retryable_error,
)

from ..agentd.client import AgentdUnreachable


class ErrorKind(str, Enum):
    TRANSIENT = "transient"
    NOT_FOUND = "not_found"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


_NOT_FOUND_MARKERS = (
    "not found",
    "no rollout",
    "does not exist",
    "unknown thread",
    "no such thread",
    "missing rollout",
)
_TRANSIENT_MARKERS = (
    "closing",
    "shutting down",
    "servershuttingdown",
    "server_overloaded",
    "busy",
    "temporarily",
    "timed out",
    "timeout",
)


def _text(exc: BaseException) -> str:
    message = getattr(exc, "message", None) or str(exc)
    data = getattr(exc, "data", None)
    return f"{message} {data!s}".lower()


def classify(exc: BaseException) -> ErrorKind:
    if isinstance(exc, (TransportClosedError, ServerBusyError, AgentdUnreachable)):
        return ErrorKind.TRANSIENT
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, BrokenPipeError, EOFError)):
        return ErrorKind.TRANSIENT
    try:
        if is_retryable_error(exc):
            return ErrorKind.TRANSIENT
    except Exception:  # pragma: no cover - defensive against SDK helper changes
        pass
    if isinstance(exc, InvalidRequestError):
        text = _text(exc)
        if any(marker in text for marker in _TRANSIENT_MARKERS):
            return ErrorKind.TRANSIENT
        if any(marker in text for marker in _NOT_FOUND_MARKERS):
            return ErrorKind.NOT_FOUND
        return ErrorKind.REJECTED
    if isinstance(exc, (InvalidParamsError, MethodNotFoundError, ParseError)):
        return ErrorKind.REJECTED
    if isinstance(exc, InternalRpcError):
        return ErrorKind.TRANSIENT
    if isinstance(exc, JsonRpcError):
        return ErrorKind.UNKNOWN
    if isinstance(exc, OSError):
        return ErrorKind.TRANSIENT
    if isinstance(exc, RuntimeError) and "not initialized" in str(exc).lower():
        return ErrorKind.TRANSIENT
    return ErrorKind.UNKNOWN


def short_message(exc: BaseException, limit: int = 300) -> str:
    message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return message if len(message) <= limit else message[: limit - 1] + "…"
