"""File-backed runtime bus: heartbeats, snapshots, event log, sequence numbers."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def atomic_write_json(path: Path, payload: Any, *, fsync: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, separators=(",", ":"), default=str)
            f.write("\n")
            f.flush()
            # Heartbeats are 1Hz liveness; fsync on a wedged data disk can
            # block every keeper and the supervisor inspect loop together.
            if fsync:
                os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> Optional[Any]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


@dataclass
class Heartbeat:
    component: str
    seq: int
    wall_ts: float
    mono_ts: float
    ok: bool = True
    status: str = "ok"
    details: str = ""
    pid: Optional[int] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def age(self, now: Optional[float] = None) -> float:
        return (now or time.time()) - self.wall_ts


EVENTS_ROTATE_BYTES = 20 * 1024 * 1024


class Bus:
    def __init__(self, root: Path):
        self.root = root
        self.hb_dir = root / "heartbeats"
        self.snap_dir = root / "snapshots"
        self.events_path = root / "events.jsonl"
        self.seq_dir = root / "seq"
        self.hb_dir.mkdir(parents=True, exist_ok=True)
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        self.seq_dir.mkdir(parents=True, exist_ok=True)

    def _next_seq(self, name: str) -> int:
        """Monotonic per-name counter. Locked so a HeartbeatKeeper thread and the
        worker thread cannot truncate-and-read the file as empty (which used to
        reset the counter to 1 and trip state_mismatch: 1836 -> 1 on 2026-11-24).
        """
        p = self.seq_dir / f"{name}.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = os.read(fd, 64).decode().strip()
            try:
                n = int(raw or "0") + 1
            except ValueError:
                n = 1
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, f"{n}\n".encode())
            os.fsync(fd)
            return n
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def beat(self, component: str, *, ok: bool = True, status: str = "ok", details: str = "", pid: Optional[int] = None, **extra: Any) -> Heartbeat:
        seq = self._next_seq(f"hb:{component}")
        if pid is None:
            pid = os.getpid()
        # Don't let a caller accidentally stomp pid via **extra.
        extra = {k: v for k, v in extra.items() if k != "pid"}
        hb = Heartbeat(
            component=component,
            seq=seq,
            wall_ts=time.time(),
            mono_ts=time.monotonic(),
            ok=ok,
            status=status,
            details=details,
            pid=pid,
            extra=extra,
        )
        self._write_heartbeat(hb)
        return hb

    def touch(self, component: str) -> Optional[Heartbeat]:
        """Refresh wall/mono clocks without bumping seq. Used by the heartbeat thread."""
        hb = self.read_heartbeat(component)
        if hb is None:
            return self.beat(component, ok=True, status="starting")
        hb.wall_ts = time.time()
        hb.mono_ts = time.monotonic()
        self._write_heartbeat(hb)
        return hb

    def _write_heartbeat(self, hb: Heartbeat) -> None:
        # Durability is for snapshots and HALT; heartbeats must not share a
        # blocking fsync with the data disk (2:02 ET stall).
        atomic_write_json(self.hb_dir / f"{hb.component}.json", {
            "component": hb.component,
            "seq": hb.seq,
            "wall_ts": hb.wall_ts,
            "mono_ts": hb.mono_ts,
            "ok": hb.ok,
            "status": hb.status,
            "details": hb.details,
            "pid": hb.pid,
            "extra": hb.extra,
            "ts_et": datetime.now(ET).strftime("%Y-%m-%d %I:%M:%S %p %Z"),
        }, fsync=False)

    def read_heartbeat(self, component: str) -> Optional[Heartbeat]:
        data = read_json(self.hb_dir / f"{component}.json")
        if not data:
            return None
        pid_raw = data.get("pid")
        try:
            pid = int(pid_raw) if pid_raw is not None and pid_raw != "" else None
        except (TypeError, ValueError):
            pid = None
        return Heartbeat(
            component=str(data.get("component") or component),
            seq=int(data.get("seq") or 0),
            wall_ts=float(data.get("wall_ts") or 0.0),
            mono_ts=float(data.get("mono_ts") or 0.0),
            ok=bool(data.get("ok", True)),
            status=str(data.get("status") or ""),
            details=str(data.get("details") or ""),
            pid=pid,
            extra=dict(data.get("extra") or {}),
        )

    def all_heartbeats(self) -> dict[str, Heartbeat]:
        out: dict[str, Heartbeat] = {}
        if not self.hb_dir.exists():
            return out
        for p in self.hb_dir.glob("*.json"):
            hb = self.read_heartbeat(p.stem)
            if hb:
                out[p.stem] = hb
        return out

    def publish(self, name: str, payload: dict) -> int:
        seq = self._next_seq(f"snap:{name}")
        envelope = {
            "name": name,
            "seq": seq,
            "ts_unix": time.time(),
            "ts_et": datetime.now(ET).strftime("%Y-%m-%d %I:%M:%S %p %Z"),
            "payload": payload,
        }
        atomic_write_json(self.snap_dir / f"{name}.json", envelope)
        return seq

    def snapshot(self, name: str) -> Optional[dict]:
        return read_json(self.snap_dir / f"{name}.json")

    def event(self, kind: str, component: str, message: str, **extra: Any) -> None:
        rec = {
            "ts_unix": time.time(),
            "ts_et": datetime.now(ET).strftime("%Y-%m-%d %I:%M:%S %p %Z"),
            "kind": kind,
            "component": component,
            "message": message,
            "extra": extra,
        }
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(rec, default=str) + "\n").encode()
        # Single O_APPEND write: concurrent processes cannot interleave a line.
        fd = os.open(self.events_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
            size = os.fstat(fd).st_size
        finally:
            os.close(fd)
        if size > EVENTS_ROTATE_BYTES:
            self._rotate_events()

    def _rotate_events(self) -> None:
        lock = self.events_path.with_suffix(".lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                if self.events_path.stat().st_size > EVENTS_ROTATE_BYTES:
                    # Keep every rotated file (the archiver moves them into data/archive).
                    rot = self.events_path.parent / "rotated"
                    rot.mkdir(exist_ok=True)
                    stamp = time.strftime("%Y%m%dT%H%M%S")
                    os.replace(self.events_path, rot / f"events-{stamp}-{os.getpid()}.jsonl")
            except FileNotFoundError:
                pass
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def recent_events(self, n: int = 200) -> list[dict]:
        """Last n events, oldest first. Reads only the file tail."""
        if not self.events_path.exists():
            return []
        try:
            with self.events_path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                end = f.tell()
                block = max(65536, n * 600)
                start = max(0, end - block)
                f.seek(start)
                data = f.read()
        except OSError:
            return []
        lines = data.splitlines()
        if start > 0 and lines:
            lines = lines[1:]  # first line may be partial
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        return out
