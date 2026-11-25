"""Global HALT latch. First reason wins. Manual reset only."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, default=str)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class HaltState:
    halted: bool
    reason: str = ""
    component: str = ""
    details: str = ""
    ts_unix: float = 0.0
    ts_et: str = ""
    seq: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class HaltLatch:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> HaltState:
        if not self.path.exists():
            return HaltState(halted=False)
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            # A unreadable latch is itself a halt — fail closed.
            return HaltState(
                halted=True,
                reason="latch_unreadable",
                component="interlock",
                details=f"could not read {self.path}",
                ts_unix=time.time(),
                ts_et=_now_et(),
            )
        return HaltState(
            halted=True,
            reason=str(data.get("reason") or "unspecified"),
            component=str(data.get("component") or ""),
            details=str(data.get("details") or ""),
            ts_unix=float(data.get("ts_unix") or 0.0),
            ts_et=str(data.get("ts_et") or ""),
            seq=int(data.get("seq") or 0),
        )

    def is_halted(self) -> bool:
        return self.read().halted

    def trip(self, reason: str, component: str, details: str = "") -> HaltState:
        """Latch the halt. If already halted, keep the original reason and return it."""
        current = self.read()
        if current.halted:
            return current
        state = HaltState(
            halted=True,
            reason=reason,
            component=component,
            details=details or "",
            ts_unix=time.time(),
            ts_et=_now_et(),
            seq=1,
        )
        _atomic_write(self.path, state.as_dict())
        # Re-read in case of a race: the first writer on disk wins.
        return self.read()

    def reset(self, *, yes: bool = False) -> None:
        if not yes:
            raise PermissionError("refusing to clear halt latch without yes=True")
        if self.path.exists():
            self.path.unlink()


def _now_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d %I:%M:%S %p %Z")
