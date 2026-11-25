"""Watchdog: freshness, missing windows, clock skew, sequence checks. Fail closed."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .bus import Bus, Heartbeat
from .interlock import HaltLatch

REQUIRED = ("discovery", "index_feed", "book_feed", "engine", "execution")

DEFAULT_STALE = {
    "index_feed": 5.0,
    "book_feed": 8.0,
    "discovery": 25.0,
    "engine": 8.0,
    "execution": 8.0,
    "watchdog": 8.0,
}

DEFAULT_GRACE = {"5m": 20.0, "15m": 30.0, "1h": 45.0}

# Supervisor loop is time.sleep(1) plus inspect work. A gap at or above this
# means this process was not scheduled (host pause, CPU steal, wedged fsync),
# not that a single worker died. Must stay well under the lowest stale
# threshold so a real hang is still caught on the next timely tick.
HOST_STALL_SECONDS = 5.0


@dataclass
class HealthItem:
    component: str
    present: bool
    age_seconds: Optional[float]
    stale: bool
    ok: bool
    status: str
    threshold: float
    seq: int = 0


def _age(hb: Optional[Heartbeat], now: float) -> Optional[float]:
    if hb is None:
        return None
    return max(0.0, now - hb.wall_ts)


class Watchdog:
    def __init__(
        self,
        bus: Bus,
        latch: HaltLatch,
        *,
        stale: Optional[dict[str, float]] = None,
        grace: Optional[dict[str, float]] = None,
        clock_skew_seconds: float = 3.0,
        wall_mono_drift_seconds: float = 2.0,
        assets: tuple[str, ...] = ("btc", "eth", "sol", "xrp"),
        horizons: tuple[str, ...] = ("5m", "15m", "1h"),
    ):
        self.bus = bus
        self.latch = latch
        self.stale = dict(DEFAULT_STALE if stale is None else stale)
        self.grace = dict(DEFAULT_GRACE if grace is None else grace)
        self.clock_skew_seconds = clock_skew_seconds
        self.wall_mono_drift_seconds = wall_mono_drift_seconds
        self.assets = assets
        self.horizons = horizons
        self._last_seq: dict[str, int] = {}
        self._last_pid: dict[str, int] = {}
        self._mono0 = time.monotonic()
        self._wall0 = time.time()
        self.host_stall_seconds = HOST_STALL_SECONDS
        self._last_loop_mono: Optional[float] = None
        self._last_loop_wall: Optional[float] = None

    def health(self, now: Optional[float] = None) -> list[HealthItem]:
        now = now or time.time()
        beats = self.bus.all_heartbeats()
        items = []
        for name in list(REQUIRED) + ["watchdog"]:
            hb = beats.get(name)
            age = _age(hb, now)
            thresh = float(self.stale.get(name, 8.0))
            stale = hb is None or age is None or age > thresh or not hb.ok
            items.append(HealthItem(
                component=name,
                present=hb is not None,
                age_seconds=None if age is None else round(age, 2),
                stale=stale,
                ok=bool(hb and hb.ok) and not stale,
                status=("missing" if hb is None else hb.status),
                threshold=thresh,
                seq=0 if hb is None else hb.seq,
            ))
        return items

    def reset_generation(self) -> None:
        """Forget last-seen sequences/pids and restart freshness clocks.

        Call this when the supervisor starts a new generation of trading
        processes so leftover last-seen seq numbers cannot re-trip HALT.
        """
        self._last_seq.clear()
        self._last_pid.clear()
        self._wall0 = time.time()
        self._mono0 = time.monotonic()

    def observe_scheduler(
        self,
        now_mono: Optional[float] = None,
        now_wall: Optional[float] = None,
    ) -> Optional[str]:
        """Detect that the supervisor itself just stalled.

        The supervisor must call this once per loop, before inspect. A large
        monotonic gap means the guest did not schedule us; every in-process
        HeartbeatKeeper in this guest also failed to run, so a stale_heartbeat
        trip on this tick would blame the wrong component (2:02 ET, 24 Nov 2026).
        A wall jump with a small mono gap is an NTP/hypervisor clock step.

        On stall: reset the watchdog generation and return a details string.
        The caller should skip inspect on that tick. A worker that is still
        stale on the next timely tick is a real halt.
        """
        now_mono = time.monotonic() if now_mono is None else now_mono
        now_wall = time.time() if now_wall is None else now_wall
        details: Optional[str] = None
        if self._last_loop_mono is not None and self._last_loop_wall is not None:
            mono_gap = now_mono - self._last_loop_mono
            wall_gap = now_wall - self._last_loop_wall
            thresh = float(self.host_stall_seconds)
            if mono_gap >= thresh:
                details = f"scheduler gap {mono_gap:.1f}s (wall {wall_gap:.1f}s)"
            elif (wall_gap - mono_gap) >= thresh:
                details = (
                    f"wall clock jumped {wall_gap - mono_gap:.1f}s "
                    f"(mono gap {mono_gap:.1f}s)"
                )
        self._last_loop_mono = now_mono
        self._last_loop_wall = now_wall
        if details:
            self.reset_generation()
        return details

    def inspect(self, now: Optional[float] = None) -> Optional[tuple[str, str, str]]:
        """Return (reason, component, details) if a halt should trip, else None."""
        now_wall = now or time.time()
        now_mono = time.monotonic()
        beats = self.bus.all_heartbeats()

        # Supervisor's own wall vs monotonic drift.
        wall_elapsed = now_wall - self._wall0
        mono_elapsed = now_mono - self._mono0
        if abs(wall_elapsed - mono_elapsed) > self.wall_mono_drift_seconds + 0.5:
            return (
                "clock_skew",
                "watchdog",
                f"wall elapsed {wall_elapsed:.2f}s vs monotonic {mono_elapsed:.2f}s",
            )

        for name in REQUIRED:
            hb = beats.get(name)
            thresh = float(self.stale.get(name, 8.0))
            if hb is None:
                # Give components a brief moment after process start before "missing" is a halt.
                if wall_elapsed < thresh:
                    continue
                return ("stale_heartbeat", name, "no heartbeat file")
            if not hb.ok:
                return ("component_unhealthy", name, hb.details or hb.status)
            age = now_wall - hb.wall_ts
            if age > thresh:
                return ("stale_heartbeat", name, f"heartbeat age {age:.1f}s > {thresh:.1f}s")
            # Sequence must not go backwards *within the same process*.
            # A new pid (supervised restart / new epoch) may legally start at 1.
            prev = self._last_seq.get(name)
            prev_pid = self._last_pid.get(name)
            new_pid = hb.pid
            if prev is not None and hb.seq < prev:
                restarted = (
                    new_pid is not None
                    and prev_pid is not None
                    and int(new_pid) != int(prev_pid)
                )
                if restarted:
                    # New process, new epoch — reset tracking, do not halt.
                    self._last_seq[name] = hb.seq
                    self._last_pid[name] = int(new_pid)
                else:
                    return (
                        "state_mismatch",
                        name,
                        f"sequence went backwards {prev} -> {hb.seq}",
                    )
            else:
                self._last_seq[name] = hb.seq
                if new_pid is not None:
                    self._last_pid[name] = int(new_pid)
            # Clock skew vs watchdog wall, but only if the beat is recent.
            if age < thresh and abs(hb.wall_ts - now_wall) > self.clock_skew_seconds + age + 0.25:
                return (
                    "clock_skew",
                    name,
                    f"component wall {hb.wall_ts:.3f} vs watchdog {now_wall:.3f}",
                )
            # Drift between that component's wall and mono across its last beat vs now
            # is checked when we have a previous mono; skip if first seen.
            if hb.mono_ts and age < thresh:
                # Compare this beat's claimed (wall, mono) to now.
                wall_d = now_wall - hb.wall_ts
                mono_d = now_mono - hb.mono_ts
                # mono_ts is process-local and not comparable across processes!
                # Only check wall vs watchdog. Cross-process mono is meaningless.
                _ = wall_d, mono_d

        # Missing current window after grace, from discovery snapshot.
        miss = self._missing_window(now_wall)
        if miss:
            return miss
        return None


    def _missing_window(self, now_wall: float) -> Optional[tuple[str, str, str]]:
        """Halt only after grace if a slot has no live (or already-listed next) window.

        A snapshot that still lists a window that *just* ended is not a fault —
        discovery may be mid-HTTP. Trip only if the ended window is older than
        the horizon grace and no live replacement is already in the snapshot.
        """
        snap = self.bus.snapshot("discovery")
        if not snap:
            return None  # heartbeat check already covers a dead discovery
        payload = snap.get("payload") or {}
        windows = [w for w in (payload.get("windows") or []) if isinstance(w, dict)]
        markets = [m for m in (payload.get("markets") or []) if isinstance(m, dict)]
        now_utc = datetime.fromtimestamp(now_wall, timezone.utc)

        def parse(ts):
            if not ts:
                return None
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

        def is_live(start, end) -> bool:
            return bool(start and end and start <= now_utc < end)

        groups: dict[tuple, list] = {}
        for w in windows:
            groups.setdefault((str(w.get("asset") or "").lower(), str(w.get("horizon") or "")), []).append(w)

        live_from_markets: set[tuple] = set()
        for m in markets:
            key = (str(m.get("asset") or "").lower(), str(m.get("horizon") or ""))
            if is_live(parse(m.get("window_start")), parse(m.get("window_end"))):
                live_from_markets.add(key)

        for asset in self.assets:
            for horizon in self.horizons:
                key = (str(asset).lower(), str(horizon))
                grace = float(self.grace.get(horizon, 20.0))
                cands = groups.get(key, [])
                live = [w for w in cands if is_live(parse(w.get("window_start")), parse(w.get("window_end")))]
                if live or key in live_from_markets:
                    continue
                future = []
                ended_ends = []
                for w in cands:
                    start, end = parse(w.get("window_start")), parse(w.get("window_end"))
                    if start and start > now_utc:
                        future.append(w)
                    if end and now_utc >= end:
                        ended_ends.append(end)
                    if w.get("next_slug"):
                        # Successor already known; treat like a future window for grace purposes.
                        future.append(w)
                if future:
                    # A replacement is already in the snapshot (or its slug is). Wait for it
                    # to become live; do not halt at the expiry tick.
                    if ended_ends:
                        age = (now_utc - max(ended_ends)).total_seconds()
                        # Only if the successor never became live well past grace.
                        starts = [parse(w.get("window_start")) for w in cands]
                        starts = [s for s in starts if s and s <= now_utc]
                        # If we only have a next_slug and the old window ended past grace
                        # with no live market, fall through to the ended check below
                        # only when there is no not-yet-started future window.
                        has_unstarted = any(
                            (parse(w.get("window_start")) and parse(w.get("window_start")) > now_utc)
                            for w in cands
                        )
                        if has_unstarted or age <= grace:
                            continue
                    else:
                        continue
                if not cands:
                    if (now_wall - float(snap.get("ts_unix") or now_wall)) < grace:
                        continue
                    return ("missing_window", "discovery", f"no slot for {asset} {horizon}")
                if ended_ends:
                    age = (now_utc - max(ended_ends)).total_seconds()
                    if age <= grace:
                        continue
                    slug = next((w.get("slug") for w in cands if parse(w.get("window_end")) == max(ended_ends)), None)
                    return ("missing_window", "discovery", f"{asset} {horizon} still showing an ended window {slug}")
                # Awaiting / no market well after the expected open.
                for w in cands:
                    start = parse(w.get("window_start"))
                    status = w.get("status")
                    if start and now_utc >= start:
                        into = (now_utc - start).total_seconds()
                        if into > grace and status in ("awaiting_next", "no_market", "ended"):
                            return ("missing_window", "discovery", f"{asset} {horizon} status={status} {into:.0f}s after open")
        return None

    def check_and_trip(self) -> Optional[tuple[str, str, str]]:
        if self.latch.is_halted():
            return None  # already latched; do not overwrite
        fault = self.inspect()
        if fault:
            reason, component, details = fault
            self.latch.trip(reason, component, details)
            self.bus.event("halt", component, f"{reason}: {details}")
            return fault
        return None
