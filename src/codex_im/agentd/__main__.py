"""``python -m codex_im.agentd --listen 0.0.0.0:4501`` (agent container). Stdlib only."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from .ops import WorkspaceOps
from .server import AgentdServer


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex-im-agentd")
    parser.add_argument("--listen", default=os.environ.get("AGENTD_LISTEN", "0.0.0.0:4501"))
    parser.add_argument("--root", default=os.environ.get("CODEX_IM_WORKSPACE_ROOT", "/workspace"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s agentd: %(message)s")
    token = os.environ.get("CODEX_IM_AGENTD_TOKEN", "")
    if not token:
        print("CODEX_IM_AGENTD_TOKEN is required", file=sys.stderr)
        return 2
    host, _, port = args.listen.rpartition(":")
    server = AgentdServer(WorkspaceOps(args.root), token)
    try:
        asyncio.run(server.serve(host or "0.0.0.0", int(port)))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(cli())
