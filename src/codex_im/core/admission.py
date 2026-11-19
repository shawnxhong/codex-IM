"""Who may talk to the bot (design D9). Default deny; variable names follow Hermes (D11)."""

from __future__ import annotations

import os
from collections.abc import Mapping

from ..config import split_csv, truthy


def _norm(value: str) -> str:
    return value.strip().lower()


class Admission:
    def __init__(self, env: Mapping[str, str] | None = None, admins: set[str] | None = None) -> None:
        self._env = os.environ if env is None else env
        self._admins = {_norm(v) for v in (admins or set())}

    def allow_all(self, platform: str) -> bool:
        return truthy(self._env.get(f"{platform.upper()}_ALLOW_ALL_USERS"))

    def allowed(self, platform: str, user_id: str) -> bool:
        if self.allow_all(platform):
            return True
        allowed = {_norm(v) for v in split_csv(self._env.get(f"{platform.upper()}_ALLOWED_USERS"))}
        return _norm(user_id) in allowed

    def is_admin(self, platform: str, user_id: str) -> bool:
        """Admins may use /provider and /release. Empty list = every allowlisted user."""
        if not self._admins:
            return True
        return f"{_norm(platform)}:{_norm(user_id)}" in self._admins
