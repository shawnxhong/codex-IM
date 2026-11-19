"""Bridge process wiring: ``python -m codex_im``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from dataclasses import dataclass
from typing import Any

from .agentd.client import make_agentd
from .codex.runtime import CodexRuntime
from .config import Settings, SettingsError, load_settings
from .core.admission import Admission
from .core.finalize import Finalizer
from .core.holds import WorkspaceHolds
from .core.outbox import Outbox
from .core.recovery import Recovery
from .core.router import Router
from .core.sessions import SessionManager
from .core.store import Store
from .core.tasks import cancel_all, spawn
from .core.workspace import ConfigError, Workspace
from .faults import Faults
from .logutil import setup_logging
from .platforms import build_adapters
from .platforms.base import PlatformAdapter

log = logging.getLogger("codex_im")


@dataclass
class Bridge:
    settings: Settings
    store: Store
    adapters: dict[str, PlatformAdapter]
    outbox: Outbox
    runtime: Any
    sessions: SessionManager
    router: Router
    recovery: Recovery
    holds: WorkspaceHolds


def build(settings: Settings, *, runtime: Any = None, adapters: dict[str, PlatformAdapter] | None = None) -> Bridge:
    """Assemble the bridge. ``runtime``/``adapters`` can be injected (tests)."""
    store = Store(settings.db_path)
    faults = Faults(settings.faults, settings.data_dir)
    agentd = make_agentd(settings)
    workspace = Workspace.from_settings(settings, agentd)
    adapters = adapters if adapters is not None else build_adapters(settings, store)
    outbox = Outbox(store, adapters, faults)
    runtime = runtime if runtime is not None else CodexRuntime(settings)
    holds = WorkspaceHolds()
    finalizer = Finalizer(store, outbox, adapters, holds)
    recovery = Recovery(store, runtime, finalizer, outbox)
    admission = Admission(admins=settings.admins)
    sessions = SessionManager(
        store=store,
        runtime=runtime,
        workspace=workspace,
        holds=holds,
        adapters=adapters,
        outbox=outbox,
        finalizer=finalizer,
        recovery=recovery,
        settings=settings,
        admission=admission,
        faults=faults,
    )
    router = Router(
        store=store,
        sessions=sessions,
        admission=admission,
        workspace=workspace,
        settings=settings,
        faults=faults,
        adapters=adapters,
    )
    return Bridge(settings, store, adapters, outbox, runtime, sessions, router, recovery, holds)


async def start(bridge: Bridge) -> None:
    spawn(bridge.outbox.run(), name="outbox")
    # R3: restore workspace holds of unresolved turns *before* anything queued may run.
    held = bridge.holds.restore(bridge.store.unfinished_turns())
    if held:
        log.info("restored %d workspace hold(s) of unresolved turns", held)
    bridge.recovery.run_startup()
    bridge.sessions.kick_all_queued()
    for name, adapter in bridge.adapters.items():
        await adapter.start(bridge.router.submit)
        log.info("platform %s started", name)


async def stop(bridge: Bridge) -> None:
    for adapter in bridge.adapters.values():
        with contextlib.suppress(Exception):
            await adapter.stop()
    await bridge.sessions.shutdown()
    bridge.outbox.stop()
    await cancel_all()
    with contextlib.suppress(Exception):
        await bridge.runtime.reset()
    bridge.store.close()


async def run(settings: Settings) -> None:
    bridge = build(settings)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
    await start(bridge)
    log.info("codex-IM bridge running (platforms: %s)", ", ".join(bridge.adapters))
    await stop_event.wait()
    log.info("shutting down")
    await stop(bridge)


def cli() -> int:
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings.log_level)
    try:
        asyncio.run(run(settings))
    except (SettingsError, ConfigError) as exc:
        log.error("configuration error: %s", exc)
        return 2
    return 0
