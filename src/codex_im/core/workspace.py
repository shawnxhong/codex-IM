"""Working directories and project concurrency (design D6).

- Default cwd includes the *full* session identity: <root>/sessions/<platform>/<chat>/<topic>.
- A session bound to a project uses either the shared project path (``serial``, default) or its own git
  worktree (``worktree``). Mutual exclusion is a hold on the cwd (core/holds.py), so ``serial`` sessions
  queue behind each other and ``worktree`` sessions run in parallel. Directories and worktrees are created
  by agentd, never by the bridge (D5).
"""

from __future__ import annotations

import hashlib
import logging
import posixpath
import re
from dataclasses import dataclass
from typing import Any

import yaml

from .types import NO_TOPIC, SessionKey

log = logging.getLogger(__name__)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def safe_segment(raw: str) -> str:
    """Filesystem-safe, collision-free path segment for an arbitrary id."""
    cleaned = _UNSAFE.sub("_", raw)
    if cleaned != raw or cleaned in {"", ".", ".."} or len(cleaned) > 64 or cleaned.startswith("."):
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
        cleaned = (cleaned.strip("._")[:40] or "x") + "-" + digest
    return cleaned


def session_short_id(key: SessionKey) -> str:
    return hashlib.sha256(key.encode().encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True, slots=True)
class Project:
    name: str
    path: str
    concurrency: str = "serial"  # serial | worktree


@dataclass(frozen=True, slots=True)
class Binding:
    platform: str
    chat_id: str
    topic: str  # exact topic, or "*" for any
    project: str

    def matches(self, key: SessionKey) -> bool:
        return (
            key.platform == self.platform
            and key.chat_id == self.chat_id
            and (self.topic == "*" or self.topic == key.topic)
        )


class ConfigError(ValueError):
    pass


def parse_projects(data: dict[str, Any] | None, workspace_root: str) -> tuple[dict[str, Project], list[Binding]]:
    data = data or {}
    projects: dict[str, Project] = {}
    for name, spec in (data.get("projects") or {}).items():
        if not isinstance(spec, dict) or not spec.get("path"):
            raise ConfigError(f"project {name!r} needs a path")
        path = str(spec["path"])
        root = workspace_root.rstrip("/")
        if not posixpath.isabs(path) or not (path == root or path.startswith(root + "/")):
            raise ConfigError(f"project {name!r} path must be inside {workspace_root}")
        concurrency = str(spec.get("concurrency") or "serial")
        if concurrency not in {"serial", "worktree"}:
            raise ConfigError(f"project {name!r}: concurrency must be 'serial' or 'worktree'")
        projects[str(name)] = Project(str(name), posixpath.normpath(path), concurrency)
    bindings: list[Binding] = []
    for entry in data.get("bindings") or []:
        session = (entry or {}).get("session") or {}
        project = str((entry or {}).get("project") or "")
        if project not in projects:
            raise ConfigError(f"binding refers to unknown project {project!r}")
        if not session.get("platform") or session.get("chat_id") is None:
            raise ConfigError("binding needs session.platform and session.chat_id")
        topic = session.get("topic")
        bindings.append(
            Binding(
                platform=str(session["platform"]).lower(),
                chat_id=str(session["chat_id"]),
                topic="*" if topic in (None, "*") else str(topic),
                project=project,
            )
        )
    return projects, bindings


class Workspace:
    def __init__(self, root: str, agentd: Any, projects: dict[str, Project], bindings: list[Binding]) -> None:
        self.root = root.rstrip("/") or "/"
        self._agentd = agentd
        self.projects = projects
        self._bindings = bindings

    @classmethod
    def from_settings(cls, settings: Any, agentd: Any) -> Workspace:
        data: dict[str, Any] = {}
        if settings.config_file:
            with open(settings.config_file, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        projects, bindings = parse_projects(data, settings.workspace_root)
        return cls(settings.workspace_root, agentd, projects, bindings)

    def project_for(self, key: SessionKey) -> str | None:
        for binding in self._bindings:
            if binding.matches(key):
                return binding.project
        return None

    def session_dir(self, key: SessionKey) -> str:
        topic = key.topic if key.topic else NO_TOPIC
        return posixpath.join(
            self.root, "sessions", safe_segment(key.platform), safe_segment(key.chat_id), safe_segment(topic)
        )

    def worktree_dir(self, project: Project, key: SessionKey) -> str:
        return posixpath.join(
            self.root,
            "worktrees",
            safe_segment(project.name),
            safe_segment(key.platform),
            safe_segment(key.chat_id),
            safe_segment(key.topic or NO_TOPIC),
        )

    async def cwd_for_new_thread(self, key: SessionKey, project_name: str | None) -> str:
        if not project_name or project_name not in self.projects:
            path = self.session_dir(key)
            await self._agentd.ensure_dir(path)
            return path
        project = self.projects[project_name]
        if project.concurrency == "serial":
            await self._agentd.ensure_dir(project.path)
            return project.path
        path = self.worktree_dir(project, key)
        branch = f"codex-im/{session_short_id(key)}"
        await self._agentd.worktree_add(project.path, path, branch)
        return path

