from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .config import Config
from .models import PaperOrder
from .store import Store



def trailing_hwm_state(
    all_settles: list,
    starting_cash: float,
    now: float,
    lookback_hours: float = 24.0,
    drawdown_pct: float = 0.10,
) -> dict:
    """Current equity vs the high-water mark inside the trailing lookback.

    The HWM is the max of (equity at the start of the lookback, equity after
    every settlement in (now-lookback, now]). A recovered historical dip does
    not trip; only current equity vs that HWM does. Trip is fail-closed
    (current + 1e-9 <= HWM * (1 - drawdown_pct)).
    """
    now_ts = float(now)
    cutoff = now_ts - max(1.0, float(lookback_hours)) * 3600.0
    ordered = sorted(((float(ts), float(pnl)) for ts, pnl in (all_settles or [])), key=lambda x: x[0])
    eq = float(starting_cash)
    for ts, pnl in ordered:
        if ts < cutoff:
            eq += pnl
        else:
            break
    hwm = eq
    hwm_ts = cutoff
    for ts, pnl in ordered:
        if ts < cutoff or ts > now_ts:
            continue
        eq += pnl
        if eq > hwm + 1e-12:
            hwm, hwm_ts = eq, ts
    current = float(starting_cash) + sum(pnl for ts, pnl in ordered if ts <= now_ts)
    trigger = hwm * (1.0 - float(drawdown_pct))
    triggered = hwm > 0 and current + 1e-9 <= trigger
    return {
        "enabled": True,
        "lookback_hours": float(lookback_hours),
        "drawdown_pct": float(drawdown_pct),
        "hwm_equity": round(hwm, 2),
        "hwm_ts": hwm_ts,
        "equity": round(current, 2),
        "drawdown_usd": round(current - hwm, 2),
        "drawdown_pct_actual": round((current - hwm) / hwm, 6) if hwm else 0.0,
        "trigger_equity": round(trigger, 2),
        "to_trigger_usd": round(current - trigger, 2),
        "triggered": triggered,
    }


def window_start_ts(now: float, tz_name: str, window_mode: str = "calendar_day", window_hours: float = 24.0) -> float:
    """Start timestamp (unix seconds) for the active risk window."""
    if window_mode == "calendar_day":
        tz = ZoneInfo(tz_name)
        dt = datetime.fromtimestamp(float(now), tz)
        return datetime(dt.year, dt.month, dt.day, tzinfo=tz).timestamp()
    return float(now) - max(1.0, float(window_hours)) * 3600.0


@dataclass
class RiskState:
    day: date
    starting_day_equity: float
    realized_pnl: float = 0.0
    open_orders: dict[str, PaperOrder] = field(default_factory=dict)  # order id
    by_market: dict[str, str] = field(default_factory=dict)          # slug -> order id
    kill_reason: Optional[str] = None
    settles: list = field(default_factory=list)      # [(settle_ts, pnl)] in active window
    history: list = field(default_factory=list)      # [DayStat] prior days, for adaptive target
    starting_cash: Optional[float] = None            # lifetime bankroll start
    all_settles: Optional[list] = None               # [(settle_ts, pnl)] lifetime; None in legacy unit tests

    @property
    def open_count(self) -> int:
        return len(self.open_orders)

    @property
    def open_exposure(self) -> float:
        return sum(o.cost_usd for o in self.open_orders.values())


class RiskManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def refresh_window(self, state: RiskState, now: Optional[float] = None) -> None:
        """Recompute active risk window from lifetime settlements so lock state never goes stale."""
        if state.all_settles is None:
            return
        now_ts = time.time() if now is None else float(now)
        tz_name = self.cfg.timezone
        ws = window_start_ts(
            now_ts,
            tz_name,
            getattr(self.cfg, "window_mode", "calendar_day"),
            getattr(self.cfg, "window_hours", 24.0),
        )
        base = state.starting_cash if state.starting_cash is not None else self.cfg.starting_cash
        prior_pnl = sum(float(pnl) for ts, pnl in state.all_settles if ts < ws)
        state.starting_day_equity = round(max(1.0, base + prior_pnl), 6)
        state.settles = [(ts, pnl) for ts, pnl in state.all_settles if ts >= ws]
        state.day = datetime.fromtimestamp(now_ts, ZoneInfo(tz_name)).date()

    def lock_state(self, state: RiskState, now: Optional[float] = None):
        from . import profit_lock as pl
        self.refresh_window(state, now=now)
        params = pl.params_from_cfg(self.cfg)
        tpct, info = pl.adaptive_target(params, state.history)
        settles = list(state.settles)
        if state.all_settles is None:
            gap = state.realized_pnl - sum(x[1] for x in settles)
            if abs(gap) > 1e-6:
                settles.append((max([time.time()] + [x[0] for x in settles]), gap))
        if now is not None:
            now_ts = float(now)
        elif state.all_settles is not None:
            now_ts = time.time()
        else:
            now_ts = None  # legacy unit tests: evaluate as-of the last settlement
        st = pl.evaluate(params, state.starting_day_equity, settles, tpct, info, now=now_ts)
        if state.all_settles is not None:
            wmode = getattr(self.cfg, "window_mode", "calendar_day")
            whrs = getattr(self.cfg, "window_hours", 24.0)
            wlab = f"rolling {whrs:g}h" if wmode == "rolling_24h" else "calendar day ET"
            st.target_info = f"{st.target_info} · {wlab}" if st.target_info else wlab
        return st

    def profit_target_usd(self, state: RiskState, now: Optional[float] = None) -> float:
        return self.lock_state(state, now=now).target_usd

    def loss_limit_usd(self, state: RiskState, now: Optional[float] = None) -> float:
        self.refresh_window(state, now=now)
        return state.starting_day_equity * self.cfg.daily_loss_limit_pct

    def size_multiplier(self, state: RiskState, now: Optional[float] = None) -> float:
        return self.lock_state(state, now=now).size_mult

    def daily_halt_reason(self, state: RiskState, now: Optional[float] = None) -> Optional[str]:
        """Why new entries are stopped for the active window (floor / target / loss limit), or None.

        Only blocks *new* entries. Open positions are left to settle.
        """
        return self.lock_state(state, now=now).reason

    def apply_daily_halts(self, state: RiskState, now: Optional[float] = None) -> Optional[str]:
        reason = self.daily_halt_reason(state, now=now)
        state.kill_reason = reason
        return reason

    def kill_switch_on(self) -> Optional[str]:
        if self.cfg.kill_switch_file.exists():
            return f"kill switch file present: {self.cfg.kill_switch_file}"
        return None

    def allow(self, state: RiskState, market_slug: str, cost: float, cash: float, now: Optional[float] = None) -> Optional[str]:
        if state.all_settles is not None:
            lock_reason = self.apply_daily_halts(state, now=now)
            reason = self.kill_switch_on() or lock_reason
        else:
            reason = self.kill_switch_on() or state.kill_reason or self.daily_halt_reason(state, now=now)
        if reason:
            return reason
        if cost > cash + 1e-9:
            return f"insufficient cash ${cash:.2f} for cost ${cost:.2f}"
        if cost > self.cfg.max_per_bet_usd + 1e-9:
            return f"cost ${cost:.2f} exceeds max_per_bet ${self.cfg.max_per_bet_usd:.2f}"
        if state.open_count >= self.cfg.max_concurrent_positions:
            return f"max concurrent positions {self.cfg.max_concurrent_positions}"
        if state.open_exposure + cost > self.cfg.max_open_exposure_usd + 1e-9:
            return f"exposure ${state.open_exposure + cost:.2f} exceeds max ${self.cfg.max_open_exposure_usd:.2f}"
        if self.cfg.one_position_per_market and market_slug in state.by_market:
            return "already have an open position in this market"
        return None

    def register_open(self, state: RiskState, order: PaperOrder) -> None:
        state.open_orders[order.id] = order
        state.by_market[order.market_slug] = order.id

    def register_settle(self, state: RiskState, order: PaperOrder, now: Optional[float] = None) -> None:
        state.open_orders.pop(order.id, None)
        if state.by_market.get(order.market_slug) == order.id:
            state.by_market.pop(order.market_slug, None)
        if order.pnl_usd is not None:
            st = settle_time(order, self.cfg.timezone)
            state.realized_pnl += order.pnl_usd
            if state.all_settles is not None:
                state.all_settles.append((st, order.pnl_usd))
            else:
                state.settles.append((st, order.pnl_usd))
        self.apply_daily_halts(state, now=now)


def today_in_tz(tz_name: str, now: Optional[float] = None) -> date:
    if now is not None:
        return datetime.fromtimestamp(float(now), ZoneInfo(tz_name)).date()
    return datetime.now(ZoneInfo(tz_name)).date()


def order_date_in_tz(ts: str, tz_name: str) -> Optional[date]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.astimezone(ZoneInfo(tz_name)).date()


def settle_time(order, tz_name: str) -> float:
    from .analytics import _settle_ts
    t = _settle_ts({"status": order.status, "reject_reason": getattr(order, "reject_reason", None),
                    "window_end": order.window_end}, tz_name)
    if t is None:
        t = time.time()
    return t


def day_history(orders, tz_name: str, today: date, start_equity: float) -> list:
    """Per-day realized stats for prior days (adaptive target input), using each day's starting equity."""
    from .profit_lock import DayStat
    days: dict = {}
    for o in orders:
        if o.status != "settled" or o.pnl_usd is None:
            continue
        d = order_date_in_tz(o.ts, tz_name)
        if d is None or d >= today:
            continue
        r = days.setdefault(d, {"pnl": 0.0, "w": 0, "l": 0, "edges": []})
        r["pnl"] += o.pnl_usd
        if o.pnl_usd > 0:
            r["w"] += 1
        else:
            r["l"] += 1
        if o.edge_after_fees is not None:
            r["edges"].append(float(o.edge_after_fees))
    out = []
    running_eq = float(start_equity)
    for d in sorted(days):
        r = days[d]
        denom = max(1.0, running_eq)
        out.append(DayStat(day=d.isoformat(), pnl_pct=r["pnl"] / denom,
                           wins=r["w"], losses=r["l"],
                           avg_edge=(sum(r["edges"]) / len(r["edges"])) if r["edges"] else None))
        running_eq += r["pnl"]
    return out


def restore_state(cfg: Config, store: Store, risk: "RiskManager", now: Optional[float] = None) -> tuple[RiskState, float]:
    """Rebuild continuous cash, open positions, and lifetime settlements from the ledger."""
    now_ts = time.time() if now is None else float(now)
    day = today_in_tz(cfg.timezone, now=now_ts)
    base_cash = cfg.starting_cash
    state = RiskState(
        day=day,
        starting_day_equity=base_cash,
        starting_cash=base_cash,
        all_settles=[],
    )
    orders = store.active_orders() if hasattr(store, "active_orders") else store.all_orders()
    state.history = day_history(orders, cfg.timezone, day, base_cash)
    for order in orders:
        if order.status == "open":
            risk.register_open(state, order)
        elif order.status == "settled" and order.pnl_usd is not None:
            st = settle_time(order, cfg.timezone)
            state.realized_pnl += order.pnl_usd
            state.all_settles.append((st, order.pnl_usd))
    risk.apply_daily_halts(state, now=now_ts)
    cash = base_cash + state.realized_pnl - state.open_exposure
    return state, cash
