"""Session profit lock with escalating cooldowns.

Fixed mode (legacy): stop new entries once realized session P&L >= target or <= -loss limit.

Profit-lock mode (default):
  1. TRADING: normal size until realized session P&L reaches the initial target.
  2. LOCKED (dashboard: LOCKED-HALF): keep trading at locked_size_mult x Kelly.
     A floor of lock_ratio x peak session gain (relative to the current segment
     baseline) is locked in and ratchets up only.
  3. COOLDOWN: a floor breach pauses new entries for cooldown_minutes[n-1]
     minutes (default 60, then 120). After the cooldown, the bot resumes at
     locked_size_mult x Kelly with the floor re-armed from current session
     P&L (peak tracking restarts from that baseline).
  4. SESSION STOP: the max_breaches-th floor breach (default 3) or the session
     -loss-limit stops new entries for the rest of the session. Open positions
     still settle. Everything resets at the session boundary (default: midnight ET).

The state is a pure function of the session's settlements plus `now`, so a
restart reproduces it exactly and it cannot go stale. Settlements that share
a timestamp are applied together. Cooldown expiry is determined from
breach timestamps + configured durations (no side file required).

Adaptive target: the initial target can scale with the last N days' realized
win rate and average edge, clamped to [target_min_pct, target_max_pct]. With
fewer than min_days of history it uses the base target.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Iterable, Optional, Sequence
from zoneinfo import ZoneInfo

FIXED = "fixed"
PROFIT_LOCK = "profit_lock"

ET = ZoneInfo("America/New_York")
DEFAULT_COOLDOWNS = (60, 120, 240)


@dataclass(frozen=True)
class LockParams:
    mode: str = PROFIT_LOCK
    target_pct: float = 0.05
    loss_limit_pct: float = 0.10
    lock_ratio: float = 0.70
    locked_size_mult: float = 0.5
    adaptive: bool = True
    target_min_pct: float = 0.03
    target_max_pct: float = 0.10
    lookback_days: int = 7
    min_days: int = 3
    max_breaches: int = 3
    cooldown_minutes: tuple[int, ...] = DEFAULT_COOLDOWNS


def _cooldowns(raw) -> tuple[int, ...]:
    if isinstance(raw, (list, tuple)) and raw:
        out = tuple(max(1, int(x)) for x in raw)
        return out or DEFAULT_COOLDOWNS
    return DEFAULT_COOLDOWNS


def params_from_cfg(cfg) -> LockParams:
    return LockParams(
        mode=cfg.target_mode, target_pct=cfg.daily_profit_target_pct, loss_limit_pct=cfg.daily_loss_limit_pct,
        lock_ratio=cfg.lock_ratio, locked_size_mult=cfg.locked_size_mult, adaptive=cfg.adaptive_target,
        target_min_pct=cfg.target_min_pct, target_max_pct=cfg.target_max_pct,
        lookback_days=cfg.adaptive_lookback_days, min_days=cfg.adaptive_min_days,
        max_breaches=int(getattr(cfg, "max_breaches", 3) or 3),
        cooldown_minutes=_cooldowns(getattr(cfg, "cooldown_minutes", DEFAULT_COOLDOWNS)),
    )


@dataclass(frozen=True)
class DayStat:
    day: str
    pnl_pct: float
    wins: int
    losses: int
    avg_edge: Optional[float] = None


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def adaptive_target(p: LockParams, history: Iterable[DayStat]) -> tuple[float, str]:
    """Initial target pct for today and a one-line explanation."""
    hist = sorted(history, key=lambda d: d.day)[-max(1, p.lookback_days):]
    if not p.adaptive:
        return p.target_pct, f"base {p.target_pct:.1%} (adaptive off)"
    if len(hist) < p.min_days:
        return p.target_pct, f"base {p.target_pct:.1%} ({len(hist)} prior day(s) < {p.min_days} needed to adapt)"
    w = sum(d.wins for d in hist)
    lo = sum(d.losses for d in hist)
    if w + lo == 0:
        return p.target_pct, f"base {p.target_pct:.1%} (no settled bets in lookback)"
    wr = w / (w + lo)
    edges = [d.avg_edge for d in hist if d.avg_edge is not None and math.isfinite(d.avg_edge)]
    edge = statistics.fmean(edges) if edges else 0.06
    quality = _clamp((wr - 0.5) / 0.2, -1.0, 1.0)          # +1 at 70% win rate, -1 at 30%
    edge_f = _clamp(edge / 0.06, 0.75, 1.25)
    t = p.target_pct * (1.0 + 0.5 * quality) * edge_f
    vol = statistics.pstdev([d.pnl_pct for d in hist]) if len(hist) >= 2 else 0.0
    if vol > 0:
        # Don't set the bar inside one day's normal noise band, nor far beyond it.
        t = _clamp(t, 0.5 * vol, 3.0 * vol) if 0.5 * vol <= 3.0 * vol else t
    t = _clamp(t, p.target_min_pct, p.target_max_pct)
    return t, (f"adaptive {t:.2%}: {len(hist)}d win rate {wr:.0%}, avg edge {edge:+.1%}, "
               f"daily P&L vol {vol:.1%}, bounds {p.target_min_pct:.1%}–{p.target_max_pct:.1%}")


def fmt_et(ts: Optional[float]) -> str:
    if ts is None:
        return "—"
    dt = datetime.fromtimestamp(float(ts), ET)
    h = dt.strftime("%I").lstrip("0") or "12"
    return f"{h}:{dt.strftime('%M %p')} ET"


def _cooldown_seconds(p: LockParams, breach_n: int) -> float:
    """Duration of the cooldown that follows breach_n (1-based), if it is recoverable."""
    mins = p.cooldown_minutes or DEFAULT_COOLDOWNS
    idx = max(0, min(len(mins) - 1, breach_n - 1))
    return float(mins[idx]) * 60.0


@dataclass
class LockState:
    mode: str
    phase: str                 # TRADING | LOCKED | COOLDOWN | STOPPED_LOSS | STOPPED_SESSION | STOPPED_TARGET
    start_equity: float
    target_pct: float
    target_usd: float
    loss_limit_usd: float
    pnl: float = 0.0
    peak_usd: float = 0.0
    floor_usd: Optional[float] = None
    locked_at: Optional[float] = None
    stopped_at: Optional[float] = None
    size_mult: float = 1.0
    reason: Optional[str] = None          # halt reason for new entries, None if allowed
    target_info: str = ""
    floor_history: list = field(default_factory=list)   # [(ts, floor_usd)] for the chart
    breach_count: int = 0
    max_breaches: int = 3
    cooldown_until: Optional[float] = None
    cooldown_seconds: Optional[float] = None
    segment_base_usd: float = 0.0
    last_resume_at: Optional[float] = None
    breaches: list = field(default_factory=list)

    @property
    def display_phase(self) -> str:
        return {
            "TRADING": "TRADING",
            "LOCKED": "LOCKED-HALF",
            "COOLDOWN": "COOLDOWN",
            "STOPPED_LOSS": "SESSION STOP",
            "STOPPED_SESSION": "SESSION STOP",
            "STOPPED_TARGET": "SESSION STOP",
            "STOPPED_FLOOR": "SESSION STOP",
        }.get(self.phase, self.phase)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["display_phase"] = self.display_phase
        d["target_equity"] = round(self.start_equity + self.target_usd, 2)
        d["floor_equity"] = None if self.floor_usd is None else round(self.start_equity + self.floor_usd, 2)
        d["peak_equity"] = round(self.start_equity + self.peak_usd, 2)
        d["loss_equity"] = round(self.start_equity - self.loss_limit_usd, 2)
        d["resume_et"] = fmt_et(self.cooldown_until) if self.phase == "COOLDOWN" else None
        return d


def evaluate(p: LockParams, start_equity: float, settles: Iterable[tuple[float, float]],
             target_pct: Optional[float] = None, target_info: str = "",
             now: Optional[float] = None) -> LockState:
    """Replay the session. `now` defaults to the last settlement time (as-of that tick)."""
    tpct = p.target_pct if target_pct is None else target_pct
    target = start_equity * tpct
    loss = start_equity * p.loss_limit_pct
    max_b = max(1, int(p.max_breaches or 3))
    st = LockState(
        mode=p.mode, phase="TRADING", start_equity=start_equity, target_pct=tpct,
        target_usd=round(target, 2), loss_limit_usd=round(loss, 2), target_info=target_info,
        max_breaches=max_b,
    )
    batches: dict[float, float] = {}
    for ts, pnl in settles:
        batches[float(ts or 0)] = batches.get(float(ts or 0), 0.0) + float(pnl or 0.0)
    times = sorted(batches)
    now_ts = float(now) if now is not None else (times[-1] if times else 0.0)

    cum = 0.0

    def _ratchet(ts: float) -> None:
        floor = round(st.segment_base_usd + p.lock_ratio * max(0.0, st.peak_usd - st.segment_base_usd), 2)
        if st.floor_usd is None or floor > st.floor_usd + 1e-9:
            st.floor_usd = floor
            st.floor_history.append((ts, floor))
        else:
            st.floor_usd = max(st.floor_usd, floor)

    def _resume(t: float) -> None:
        if st.phase != "COOLDOWN" or st.cooldown_until is None:
            return
        if t + 1e-9 < st.cooldown_until:
            return
        st.phase = "LOCKED"
        st.size_mult = p.locked_size_mult
        st.segment_base_usd = cum
        st.peak_usd = cum
        st.last_resume_at = st.cooldown_until
        st.floor_usd = None
        _ratchet(st.cooldown_until)
        st.cooldown_until = None
        st.reason = None

    def _breach(ts: float) -> None:
        st.breach_count += 1
        n = st.breach_count
        rec = {"ts": ts, "pnl": round(cum, 2), "n": n, "floor": st.floor_usd, "peak": st.peak_usd}
        if n >= max_b:
            st.phase, st.stopped_at = "STOPPED_SESSION", ts
            rec["action"] = "session_stop"
            st.breaches.append(rec)
            return
        dur = _cooldown_seconds(p, n)
        st.phase = "COOLDOWN"
        st.cooldown_seconds = dur
        st.cooldown_until = ts + dur
        rec["action"] = "cooldown"
        rec["cooldown_until"] = st.cooldown_until
        rec["cooldown_seconds"] = dur
        st.breaches.append(rec)

    def _apply(ts: float) -> None:
        if st.phase.startswith("STOPPED"):
            return
        if st.phase == "COOLDOWN":
            return
        if cum - 1e-9 <= -loss:
            st.phase, st.stopped_at = "STOPPED_LOSS", ts
            return
        if p.mode == FIXED:
            return
        if st.phase == "TRADING" and cum + 1e-9 >= target:
            st.phase, st.locked_at = "LOCKED", ts
            st.size_mult = p.locked_size_mult
            st.segment_base_usd = 0.0
            st.peak_usd = max(st.peak_usd, cum)
            _ratchet(ts)
            return
        if st.phase == "LOCKED":
            st.peak_usd = max(st.peak_usd, cum)
            _ratchet(ts)
            if st.floor_usd is not None and cum + 1e-9 <= st.floor_usd and ts != st.locked_at and ts != st.last_resume_at:
                _breach(ts)

    for ts in times:
        _resume(ts)
        cum += batches[ts]
        st.pnl = cum
        if p.mode != FIXED and not st.phase.startswith("STOPPED") and st.phase != "COOLDOWN":
            st.peak_usd = max(st.peak_usd, cum) if st.phase != "TRADING" or cum + 1e-9 >= target else st.peak_usd
        _apply(ts)

    if now_ts + 1e-9 >= (times[-1] if times else now_ts):
        _resume(now_ts)

    st.pnl = round(cum, 2)
    if p.mode == FIXED:
        st.peak_usd = round(max(cum, 0.0), 2)
    else:
        st.peak_usd = round(max(st.peak_usd, 0.0), 2)
    st.segment_base_usd = round(st.segment_base_usd, 2)
    pct = 0.0 if start_equity <= 0 else cum / start_equity

    if p.mode == FIXED:
        if cum + 1e-9 >= target:
            st.phase = "STOPPED_TARGET"
            st.reason = (f"daily profit target hit (+{tpct:.0%} = ${target:.2f}, pnl=${cum:.2f} / {pct:+.2%})")
        elif cum - 1e-9 <= -loss:
            st.phase = "STOPPED_LOSS"
            st.reason = (f"session loss limit hit (-{p.loss_limit_pct:.0%} = ${loss:.2f}, pnl=${cum:.2f} / {pct:+.2%})")
        return st

    if st.phase == "LOCKED":
        st.size_mult = p.locked_size_mult
        st.reason = None
    elif st.phase == "COOLDOWN":
        st.size_mult = p.locked_size_mult
        st.reason = (
            f"cooldown after floor breach {st.breach_count}/{max_b}, resumes {fmt_et(st.cooldown_until)}"
            f" (floor +${st.floor_usd:.2f} = {p.lock_ratio:.0%} of peak +${st.peak_usd:.2f}; "
            f"pnl=${cum:.2f} / {pct:+.2%})"
        )
    elif st.phase == "STOPPED_LOSS":
        st.reason = (f"session loss limit hit (-{p.loss_limit_pct:.0%} = ${loss:.2f}, pnl=${cum:.2f} / {pct:+.2%})")
    elif st.phase == "STOPPED_SESSION":
        st.reason = (
            f"session stop after floor breach {st.breach_count}/{max_b} "
            f"(floor +${st.floor_usd:.2f} = {p.lock_ratio:.0%} of peak +${st.peak_usd:.2f}; "
            f"pnl=${cum:.2f} / {pct:+.2%})"
        )
    return st


def describe(st: LockState) -> str:
    if st.phase == "TRADING":
        return f"trading: target +${st.target_usd:.2f} ({st.target_pct:.1%}), pnl ${st.pnl:+.2f}"
    if st.phase == "LOCKED":
        return (f"profit lock: target hit, floor +${st.floor_usd:.2f} locked (peak +${st.peak_usd:.2f}), "
                f"trading at {st.size_mult:.2g}x size, breaches {st.breach_count}/{st.max_breaches}")
    if st.phase == "COOLDOWN":
        return st.reason or f"cooldown until {fmt_et(st.cooldown_until)}"
    return st.reason or st.phase
