"""Workspace operations executed inside the agent container (D5, D6).

A fixed set of operations, never arbitrary commands. Every path must resolve inside the workspace root.
Runs as the agent user, i.e. with exactly Codex's privileges, so it adds no escalation surface.
"""

from __future__ import annotations

import asyncio
import os
import posixpath
import re
from typing import Any

_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
GIT_TIMEOUT = 120.0


class OpError(Exception):
    pass


class WorkspaceOps:
    def __init__(self, root: str) -> None:
        if not posixpath.isabs(root):
            raise ValueError("workspace root must be absolute")
        self.root = root

    def check_path(self, path: str) -> str:
        if not isinstance(path, str) or not path or not posixpath.isabs(path):
            raise OpError("path must be an absolute path")
        if "\x00" in path:
            raise OpError("invalid path")
        real = os.path.realpath(posixpath.normpath(path))
        root = os.path.realpath(self.root)
        if real != root and not real.startswith(root.rstrip("/") + "/"):
            raise OpError("path is outside the workspace")
        return real

    async def ping(self) -> dict[str, Any]:
        return {"ok": True}

    async def ensure_dir(self, path: str) -> dict[str, Any]:
        real = self.check_path(path)
        os.makedirs(real, exist_ok=True)
        return {"path": real}

    async def worktree_add(self, repo: str, path: str, branch: str) -> dict[str, Any]:
        repo_real = self.check_path(repo)
        path_real = self.check_path(path)
        if not _BRANCH_RE.match(branch or "") or ".." in branch or branch.endswith((".lock", "/")):
            raise OpError("invalid branch name")
        if os.path.exists(os.path.join(path_real, ".git")):
            return {"path": path_real, "created": False}
        if not os.path.isdir(repo_real):
            raise OpError("project path does not exist")
        os.makedirs(os.path.dirname(path_real), exist_ok=True)
        rc, _, _ = await _git(repo_real, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        if rc == 0:
            rc, _, err = await _git(repo_real, "worktree", "add", path_real, branch)
        else:
            rc, _, err = await _git(repo_real, "worktree", "add", "-b", branch, path_real)
        if rc != 0:
            raise OpError(f"git worktree add failed: {err.strip()[-500:]}")
        return {"path": path_real, "created": True}

    async def worktree_remove(self, repo: str, path: str) -> dict[str, Any]:
        repo_real = self.check_path(repo)
        path_real = self.check_path(path)
        rc, _, err = await _git(repo_real, "worktree", "remove", "--force", path_real)
        if rc != 0:
            raise OpError(f"git worktree remove failed: {err.strip()[-500:]}")
        return {"path": path_real, "removed": True}


async def _git(repo: str, *args: str) -> tuple[int, str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        repo,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise OpError("git timed out") from None
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


OPERATIONS = {
    "ping": (),
    "ensure_dir": ("path",),
    "worktree_add": ("repo", "path", "branch"),
    "worktree_remove": ("repo", "path"),
}
