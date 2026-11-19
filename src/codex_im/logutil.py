"""Logging helpers. Rule (D9): no user content at INFO and above — log lengths and hashed ids only."""

from __future__ import annotations

import hashlib
import logging
import sys


def h(value: object) -> str:
    """Short stable hash for identifiers (chat ids, user ids, message keys) in logs."""
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:10]


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Third-party libraries can log request bodies at DEBUG; keep them quieter.
    for noisy in ("httpx", "telegram", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
