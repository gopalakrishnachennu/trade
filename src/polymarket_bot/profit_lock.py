"""Dynamic daily profit target ("profit lock").

Fixed mode (legacy): stop new entries once realized P&L >= target or <= -loss limit.

Profit-lock mode (default):
  1. TRADING: normal size until realized P&L reaches the initial target.
  2. LOCKED:  keep trading at reduced size (locked_size_mult x Kelly). A floor
              of lock_ratio x peak daily gain is locked in and ratchets up with
              every new peak.
  3. STOPPED: if realized P&L falls to the locked floor (STOPPED_FLOOR) or the
              hard loss limit (STOPPED_LOSS), new entries stop for the rest of
              the day. Stops are sticky for the day.

The state is a pure function of today's settlements (ledger = source of truth),
so a restart reproduces it exactly. Settlements that share a timestamp are
applied together (the order inside a batch is arbitrary).

Adaptive target: the initial target can scale with the last N days' realized
win rate and average edge, clamped to [target_min_pct, target_max_pct]. With
fewer than min_days of history it uses the base target.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional

FIXED = "fixed"
PROFIT_LOCK = "profit_lock"


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


def params_from_cfg(cfg) -> LockParams:
    return LockParams(
        mode=cfg.target_mode, target_pct=cfg.daily_profit_target_pct, loss_limit_pct=cfg.daily_loss_limit_pct,
        lock_ratio=cfg.lock_ratio, locked_size_mult=cfg.locked_size_mult, adaptive=cfg.adaptive_target,
        target_min_pct=cfg.target_min_pct, target_max_pct=cfg.target_max_pct,
        lookback_days=cfg.adaptive_lookback_days, min_days=cfg.adaptive_min_days,
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


@dataclass
class LockState:
    mode: str
    phase: str                 # TRADING | LOCKED | STOPPED_FLOOR | STOPPED_LOSS | STOPPED_TARGET
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

    def as_dict(self) -> dict:
        d = asdict(self)
        d["target_equity"] = round(self.start_equity + self.target_usd, 2)
        d["floor_equity"] = None if self.floor_usd is None else round(self.start_equity + self.floor_usd, 2)
        d["peak_equity"] = round(self.start_equity + self.peak_usd, 2)
        d["loss_equity"] = round(self.start_equity - self.loss_limit_usd, 2)
        return d


def evaluate(p: LockParams, start_equity: float, settles: Iterable[tuple[float, float]],
             target_pct: Optional[float] = None, target_info: str = "") -> LockState:
    tpct = p.target_pct if target_pct is None else target_pct
    target = start_equity * tpct
    loss = start_equity * p.loss_limit_pct
    st = LockState(mode=p.mode, phase="TRADING", start_equity=start_equity, target_pct=tpct,
                   target_usd=round(target, 2), loss_limit_usd=round(loss, 2), target_info=target_info)
    batches: dict[float, float] = {}
    for ts, pnl in settles:
        batches[float(ts or 0)] = batches.get(float(ts or 0), 0.0) + float(pnl or 0.0)
    cum = 0.0
    for ts in sorted(batches):
        cum += batches[ts]
        st.pnl = cum
        if p.mode == FIXED:
            continue  # fixed mode judges only the current total (legacy behavior)
        if st.phase.startswith("STOPPED"):
            continue
        st.peak_usd = max(st.peak_usd, cum)
        if cum - 1e-9 <= -loss:
            st.phase, st.stopped_at = "STOPPED_LOSS", ts
            continue
        if st.phase == "TRADING" and cum + 1e-9 >= target:
            st.phase, st.locked_at = "LOCKED", ts
        if st.phase == "LOCKED":
            floor = round(p.lock_ratio * st.peak_usd, 2)
            if st.floor_usd is None or floor > st.floor_usd:
                st.floor_usd = floor
                st.floor_history.append((ts, floor))
            if cum + 1e-9 <= st.floor_usd and ts != st.locked_at:
                st.phase, st.stopped_at = "STOPPED_FLOOR", ts
    st.pnl = round(cum, 2)
    st.peak_usd = round(max(st.peak_usd, 0.0 if p.mode == FIXED else st.peak_usd), 2)
    pct = 0.0 if start_equity <= 0 else cum / start_equity
    if p.mode == FIXED:
        st.peak_usd = round(max(cum, 0.0), 2)
        if cum + 1e-9 >= target:
            st.phase = "STOPPED_TARGET"
            st.reason = (f"daily profit target hit (+{tpct:.0%} = ${target:.2f}, pnl=${cum:.2f} / {pct:+.2%})")
        elif cum - 1e-9 <= -loss:
            st.phase = "STOPPED_LOSS"
            st.reason = (f"daily loss limit hit (-{p.loss_limit_pct:.0%} = ${loss:.2f}, pnl=${cum:.2f} / {pct:+.2%})")
        return st
    if st.phase == "LOCKED":
        st.size_mult = p.locked_size_mult
    elif st.phase == "STOPPED_LOSS":
        st.reason = (f"daily loss limit hit (-{p.loss_limit_pct:.0%} = ${loss:.2f}, pnl=${cum:.2f} / {pct:+.2%})")
    elif st.phase == "STOPPED_FLOOR":
        st.reason = (f"profit lock floor hit (floor +${st.floor_usd:.2f} = {p.lock_ratio:.0%} of peak "
                     f"+${st.peak_usd:.2f}; pnl=${cum:.2f} / {pct:+.2%})")
    return st


def describe(st: LockState) -> str:
    if st.phase == "TRADING":
        return f"trading: target +${st.target_usd:.2f} ({st.target_pct:.1%}), pnl ${st.pnl:+.2f}"
    if st.phase == "LOCKED":
        return (f"profit lock: target hit, floor +${st.floor_usd:.2f} locked (peak +${st.peak_usd:.2f}), "
                f"trading at {st.size_mult:.2g}x size")
    return st.reason or st.phase
