from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from fakes import FakeRuntime  # noqa: E402

from codex_im import app  # noqa: E402
from codex_im.config import Settings  # noqa: E402
from codex_im.core import outbox as outbox_module  # noqa: E402
from codex_im.core.store import Store  # noqa: E402
from codex_im.platforms.fake.adapter import FakeAdapter  # noqa: E402


async def wait_for(predicate: Callable[[], Any], timeout: float = 5.0, interval: float = 0.02) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(str(tmp_path / "db.sqlite3"))
    yield s
    s.close()


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "default_model": "test-model",
            "data_dir": str(tmp_path / "data"),
            "platforms": ["fake"],
            "default_provider": "openai",
            "allowed_providers": ["openai", "custom"],
            "agentd": "local",
            "workspace_root": str(tmp_path / "ws"),
            "stream_interval": 0.05,
            "stop_timeout": 2.0,
            "rpc_timeout": 2.0,
            "rate_limit_per_min": 0,
        }
        values.update(overrides)
        return Settings(**values)

    return factory


class Harness:
    def __init__(self, bridge: app.Bridge, adapter: FakeAdapter, runtime: FakeRuntime) -> None:
        self.bridge = bridge
        self.adapter = adapter
        self.runtime = runtime
        self.store = bridge.store

    def finals(self) -> list[str]:
        return [e["text"] for e in self.adapter.events if e["op"] in ("send", "edit") and e.get("final")]

    async def say(self, text: str, **kwargs: Any) -> bool:
        kwargs.setdefault("chat_id", "chat-1")
        kwargs.setdefault("user_id", "u1")
        return await self.adapter.inject(text=text, **kwargs)


@pytest.fixture
async def harness_factory(monkeypatch: pytest.MonkeyPatch, make_settings: Callable[..., Settings]):
    monkeypatch.setenv("FAKE_ALLOW_ALL_USERS", "true")
    monkeypatch.setattr(outbox_module, "backoff", lambda attempts: 0.05)
    created: list[Harness] = []

    async def factory(*, can_edit: bool = True, runtime: FakeRuntime | None = None, **overrides: Any) -> Harness:
        settings = make_settings(**overrides)
        adapter = FakeAdapter(can_edit=can_edit, serve=False)
        runtime = runtime or FakeRuntime()
        bridge = app.build(settings, runtime=runtime, adapters={"fake": adapter})
        await app.start(bridge)
        harness = Harness(bridge, adapter, runtime)
        created.append(harness)
        return harness

    yield factory
    for harness in created:
        await app.stop(harness.bridge)
