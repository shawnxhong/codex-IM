"""Bridge settings, read from ``CODEX_IM_*`` environment variables (design D11).

Platform credentials keep Hermes' variable names and are read by each adapter.
"""

from __future__ import annotations

import os
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass, field

TRUTHY = {"1", "true", "yes", "on"}


class SettingsError(ValueError):
    pass


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUTHY


def split_csv(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def parse_tcp_url(value: str) -> tuple[str, int]:
    """``tcp://host:port`` -> (host, port)."""
    if not value.startswith("tcp://"):
        raise SettingsError(f"expected tcp://host:port, got {value!r}")
    hostport = value[len("tcp://") :]
    host, sep, port = hostport.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise SettingsError(f"expected tcp://host:port, got {value!r}")
    return host, int(port)


@dataclass(slots=True)
class Settings:
    default_model: str
    data_dir: str = "/data"
    platforms: list[str] = field(default_factory=list)
    default_provider: str = "openai"
    allowed_providers: list[str] = field(default_factory=list)
    provider_admins: set[str] = field(default_factory=set)
    developer_instructions: str | None = None
    app_server: str = "local"  # "local" (spawn bundled codex) or tcp://host:port (stdio-over-TCP relay, D5)
    agentd: str = "local"  # "local" (in-process) or tcp://host:port (agent container, D5)
    agentd_token: str | None = None
    workspace_root: str = "/workspace"
    config_file: str | None = None
    stream_interval: float = 1.5
    busy_policy: str = "steer"  # steer | queue (D6)
    stop_timeout: float = 30.0
    rate_limit_per_min: int = 10
    faults: str = ""
    log_level: str = "INFO"

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "codex-im.sqlite3")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    model = (env.get("CODEX_IM_DEFAULT_MODEL") or "").strip()
    if not model:
        raise SettingsError("CODEX_IM_DEFAULT_MODEL is required (the bridge always passes the model explicitly)")
    default_provider = (env.get("CODEX_IM_DEFAULT_PROVIDER") or "openai").strip()
    allowed = split_csv(env.get("CODEX_IM_ALLOWED_PROVIDERS")) or [default_provider]
    if default_provider not in allowed:
        allowed.insert(0, default_provider)
    busy = (env.get("CODEX_IM_BUSY_POLICY") or "steer").strip().lower()
    if busy not in {"steer", "queue"}:
        raise SettingsError("CODEX_IM_BUSY_POLICY must be 'steer' or 'queue'")
    app_server = (env.get("CODEX_IM_APP_SERVER") or "local").strip()
    if app_server != "local":
        parse_tcp_url(app_server)
    agentd = (env.get("CODEX_IM_AGENTD") or "local").strip()
    agentd_token = env.get("CODEX_IM_AGENTD_TOKEN") or None
    if agentd != "local":
        parse_tcp_url(agentd)
        if not agentd_token:
            raise SettingsError("CODEX_IM_AGENTD_TOKEN is required when CODEX_IM_AGENTD is a tcp:// address")
    workspace_root = (env.get("CODEX_IM_WORKSPACE_ROOT") or "/workspace").strip()
    if not posixpath.isabs(workspace_root):
        raise SettingsError("CODEX_IM_WORKSPACE_ROOT must be an absolute path")
    platforms = [p.lower() for p in split_csv(env.get("CODEX_IM_PLATFORMS"))]
    if not platforms:
        raise SettingsError("CODEX_IM_PLATFORMS is empty (e.g. 'telegram,email')")
    try:
        stream_interval = float(env.get("CODEX_IM_STREAM_INTERVAL") or 1.5)
        stop_timeout = float(env.get("CODEX_IM_STOP_TIMEOUT") or 30)
        rate = int(env.get("CODEX_IM_RATE_LIMIT_PER_MIN") or 10)
    except ValueError as exc:
        raise SettingsError(f"invalid numeric setting: {exc}") from exc
    return Settings(
        default_model=model,
        data_dir=(env.get("CODEX_IM_DATA_DIR") or "/data").strip(),
        platforms=platforms,
        default_provider=default_provider,
        allowed_providers=allowed,
        provider_admins={p.lower() for p in split_csv(env.get("CODEX_IM_PROVIDER_ADMINS"))},
        developer_instructions=(env.get("CODEX_IM_DEVELOPER_INSTRUCTIONS") or "").strip() or None,
        app_server=app_server,
        agentd=agentd,
        agentd_token=agentd_token,
        workspace_root=workspace_root.rstrip("/") or "/",
        config_file=(env.get("CODEX_IM_CONFIG_FILE") or "").strip() or None,
        stream_interval=max(0.05, stream_interval),
        busy_policy=busy,
        stop_timeout=max(1.0, stop_timeout),
        rate_limit_per_min=max(0, rate),
        faults=env.get("CODEX_IM_FAULTS") or "",
        log_level=(env.get("CODEX_IM_LOG_LEVEL") or "INFO").upper(),
    )
