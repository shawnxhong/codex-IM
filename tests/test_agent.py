"""Agent-side pieces: config.toml rendering and agentd (D4, D5, D6, D13)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib

import pytest

from codex_im.agent_config import AgentConfigError, render
from codex_im.agentd.ops import OpError, WorkspaceOps
from codex_im.agentd.server import AgentdServer


def test_render_defaults_are_full_access_and_disable_blocking_questions():
    config = tomllib.loads(render({}))
    assert config["approval_policy"] == "never"
    assert config["sandbox_mode"] == "danger-full-access"
    assert config["cli_auth_credentials_store"] == "file"
    assert config["features"]["default_mode_request_user_input"] is False


def test_render_custom_providers():
    config = tomllib.loads(
        render(
            {
                "CODEX_IM_DEFAULT_MODEL": "m",
                "CODEX_IM_FORCED_LOGIN_METHOD": "chatgpt",
                "CODEX_IM_PROVIDER_IDS": "custom,openai-apikey",
                "CODEX_IM_PROVIDER_CUSTOM_BASE_URL": "https://gw.example.com/v1",
                "CODEX_IM_PROVIDER_OPENAI_APIKEY_BASE_URL": "https://api.openai.com/v1",
                "CODEX_IM_PROVIDER_OPENAI_APIKEY_ENV_KEY": "OPENAI_API_KEY",
            }
        )
    )
    assert config["model"] == "m"
    assert config["forced_login_method"] == "chatgpt"
    custom = config["model_providers"]["custom"]
    assert custom == {
        "name": "custom",
        "base_url": "https://gw.example.com/v1",
        "env_key": "CODEX_IM_PROVIDER_CUSTOM_API_KEY",
        "wire_api": "responses",
    }
    assert config["model_providers"]["openai-apikey"]["env_key"] == "OPENAI_API_KEY"


@pytest.mark.parametrize("pid", ["openai", "ollama", "Bad Id"])
def test_render_rejects_reserved_or_invalid_ids(pid):
    with pytest.raises(AgentConfigError):
        render({"CODEX_IM_PROVIDER_IDS": pid, f"CODEX_IM_PROVIDER_{pid.upper()}_BASE_URL": "https://x"})


def test_render_requires_base_url():
    with pytest.raises(AgentConfigError):
        render({"CODEX_IM_PROVIDER_IDS": "custom"})


async def test_ops_confine_paths(tmp_path):
    ops = WorkspaceOps(str(tmp_path / "ws"))
    created = await ops.ensure_dir(str(tmp_path / "ws" / "a" / "b"))
    assert os.path.isdir(created["path"])
    for bad in ("relative/path", str(tmp_path / "outside"), str(tmp_path / "ws" / ".." / "outside")):
        with pytest.raises(OpError):
            await ops.ensure_dir(bad)
    os.symlink(str(tmp_path), str(tmp_path / "ws" / "escape"))
    with pytest.raises(OpError):
        await ops.ensure_dir(str(tmp_path / "ws" / "escape" / "x"))


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_worktree_add_is_idempotent(tmp_path):
    root = tmp_path / "ws"
    repo = root / "projects" / "p"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
    )
    ops = WorkspaceOps(str(root))
    target = str(root / "worktrees" / "p" / "s1")
    first = await ops.worktree_add(str(repo), target, "codex-im/abc12345")
    second = await ops.worktree_add(str(repo), target, "codex-im/abc12345")
    assert first["created"] is True and second["created"] is False
    with pytest.raises(OpError):
        await ops.worktree_add(str(repo), target + "2", "-evil")


async def test_agentd_dispatch_requires_token_and_known_ops(tmp_path):
    server = AgentdServer(WorkspaceOps(str(tmp_path)), "secret")
    assert (await server.dispatch({"token": "nope", "op": "ping", "args": {}}))["error"] == "unauthorized"
    assert (await server.dispatch({"token": "secret", "op": "rm_rf", "args": {}}))["error"] == "unknown operation"
    assert (await server.dispatch({"token": "secret", "op": "ensure_dir", "args": {"path": "/etc", "x": 1}}))[
        "error"
    ] == "invalid arguments"
    ok = await server.dispatch({"token": "secret", "op": "ensure_dir", "args": {"path": str(tmp_path / "d")}})
    assert ok["ok"] is True
