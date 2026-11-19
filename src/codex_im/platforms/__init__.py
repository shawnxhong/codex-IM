"""Platform registry. Adapters are imported lazily so unused platform SDKs are never loaded."""

from __future__ import annotations

from typing import Any

from ..config import Settings, SettingsError
from .base import PlatformAdapter

IMPLEMENTED = ("telegram", "email", "fake")


def build_adapters(settings: Settings, store: Any) -> dict[str, PlatformAdapter]:
    adapters: dict[str, PlatformAdapter] = {}
    for name in settings.platforms:
        if name == "telegram":
            from .telegram.adapter import TelegramAdapter

            adapters[name] = TelegramAdapter(store=store, data_dir=settings.data_dir)
        elif name == "email":
            from .mail.adapter import EmailAdapter

            adapters[name] = EmailAdapter(data_dir=settings.data_dir)
        elif name == "fake":
            from .fake.adapter import FakeAdapter

            adapters[name] = FakeAdapter.from_env()
        else:
            raise SettingsError(
                f"platform {name!r} is not implemented yet (implemented: {', '.join(IMPLEMENTED)}; see docs/roadmap.md)"
            )
    return adapters
