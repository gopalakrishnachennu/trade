"""Independent heartbeat so a blocking HTTP call cannot starve the watchdog."""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from .bus import Bus

log = logging.getLogger(__name__)


class HeartbeatKeeper:
    """Re-issues the last heartbeat on a side thread every `interval` seconds.

    The worker still calls Bus.beat when it makes progress. This thread only
    keeps the liveness clock fresh while the main thread is blocked in I/O.
    Status/ok/details are copied from the last real beat (or "starting").
    """

    def __init__(self, bus: Bus, role: str, interval: float = 1.0):
        self.bus = bus
        self.role = role
        self.interval = max(0.2, float(interval))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last: dict[str, Any] = {"ok": True, "status": "starting", "details": ""}

    def beat(self, ok: bool = True, status: str = "ok", details: str = "", **extra: Any) -> None:
        with self._lock:
            self._last = {"ok": ok, "status": status, "details": details, **extra}
        self.bus.beat(self.role, ok=ok, status=status, details=details, **extra)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # Seed immediately so the watchdog sees the role as soon as we start.
        self.beat(ok=True, status="starting")
        self._thread = threading.Thread(
            target=self._run, name=f"{self.role}-heartbeat", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        # Refresh wall_ts only. Bumping seq here raced the worker thread on an
        # unlocked counter and produced "1836 -> 1" on a still-living process.
        while not self._stop.wait(self.interval):
            try:
                self.bus.touch(self.role)
            except Exception:
                log.exception("heartbeat thread failed for %s", self.role)
