"""Fault injection for the Ubuntu acceptance scenarios (docs/verification.md).

Enabled only through ``CODEX_IM_FAULTS``. Crash points fire at most once per data directory (a marker
file is written before exiting) so a restarted bridge does not crash again in a loop.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

CRASH_POINTS = frozenset(
    {
        "crash_after_inbound_insert",
        "crash_after_turn_pending",
        "crash_after_turn_start",
        "crash_before_outbox_send",
    }
)
FAULT_EXIT_CODE = 77


class InjectedDeliveryFailure(Exception):
    def __init__(self, transient: bool) -> None:
        super().__init__("injected transient delivery failure" if transient else "injected permanent delivery failure")
        self.transient = transient


class Faults:
    def __init__(self, spec: str = "", state_dir: str | None = None) -> None:
        self._crash: set[str] = set()
        self._fail_remaining = 0
        self._fail_permanent = False
        self._state_dir = os.path.join(state_dir, "faults-fired") if state_dir else None
        for raw in (spec or "").split(","):
            item = raw.strip()
            if not item:
                continue
            if item in CRASH_POINTS:
                self._crash.add(item)
            elif item.startswith("fail_delivery:"):
                self._fail_remaining = int(item.split(":", 1)[1])
            elif item == "fail_delivery_permanent":
                self._fail_permanent = True
            else:
                raise ValueError(f"unknown fault point: {item!r}")
        if self.enabled:
            log.warning("fault injection enabled: %s", sorted(self._crash) or spec)

    @property
    def enabled(self) -> bool:
        return bool(self._crash or self._fail_remaining or self._fail_permanent)

    def _marker(self, point: str) -> str | None:
        return os.path.join(self._state_dir, point) if self._state_dir else None

    def hit(self, point: str) -> None:
        if point not in self._crash:
            return
        marker = self._marker(point)
        if marker and os.path.exists(marker):
            return
        if marker:
            os.makedirs(os.path.dirname(marker), exist_ok=True)
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write("fired\n")
        log.critical("fault injection: crashing at %s", point)
        logging.shutdown()
        os._exit(FAULT_EXIT_CODE)

    def delivery_check(self) -> None:
        if self._fail_permanent:
            raise InjectedDeliveryFailure(transient=False)
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise InjectedDeliveryFailure(transient=True)
