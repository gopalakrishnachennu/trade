from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .config import Config
from .models import PaperOrder
from .store import Store


@dataclass
class RiskState:
    day: date
    starting_day_equity: float
    realized_pnl: float = 0.0
    open_orders: dict[str, PaperOrder] = field(default_factory=dict)  # order id
    by_market: dict[str, str] = field(default_factory=dict)  # slug -> order id
    kill_reason: Optional[str] = None
    settles: list = field(default_factory=list)      # [(settle_ts, pnl)] today, for profit lock
    history: list = field(default_factory=list)      # [DayStat] prior days, for the adaptive target

    @property
    def open_count(self) -> int:
        return len(self.open_orders)

    @property
    def open_exposure(self) -> float:
        return sum(o.cost_usd for o in self.open_orders.values())


class RiskManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def lock_state(self, state: RiskState):
        from . import profit_lock as pl
        params = pl.params_from_cfg(self.cfg)
        tpct, info = pl.adaptive_target(params, state.history)
        settles = list(state.settles)
        gap = state.realized_pnl - sum(x[1] for x in settles)
        if abs(gap) > 1e-6:
            # realized_pnl is the truth; attribute any unexplained part to "now".
            import time as _t
            settles.append((max([_t.time()] + [x[0] for x in settles]), gap))
        return pl.evaluate(params, state.starting_day_equity, settles, tpct, info)

    def profit_target_usd(self, state: RiskState) -> float:
        return self.lock_state(state).target_usd

    def loss_limit_usd(self, state: RiskState) -> float:
        return state.starting_day_equity * self.cfg.daily_loss_limit_pct

    def size_multiplier(self, state: RiskState) -> float:
        return self.lock_state(state).size_mult

    def daily_halt_reason(self, state: RiskState) -> Optional[str]:
        """Why new entries are stopped for the day (fixed target / profit-lock floor / loss limit), or None.

        Only blocks *new* entries. Open positions are left to settle.
        """
        return self.lock_state(state).reason

    def apply_daily_halts(self, state: RiskState) -> Optional[str]:
        reason = self.daily_halt_reason(state)
        if reason:
            state.kill_reason = reason
        return reason

    def kill_switch_on(self) -> Optional[str]:
        if self.cfg.kill_switch_file.exists():
            return f"kill switch file present: {self.cfg.kill_switch_file}"
        return None

    def allow(self, state: RiskState, market_slug: str, cost: float, cash: float) -> Optional[str]:
        reason = self.kill_switch_on() or state.kill_reason or self.daily_halt_reason(state)
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

    def register_settle(self, state: RiskState, order: PaperOrder) -> None:
        state.open_orders.pop(order.id, None)
        if state.by_market.get(order.market_slug) == order.id:
            state.by_market.pop(order.market_slug, None)
        if order.pnl_usd is not None:
            state.realized_pnl += order.pnl_usd
            state.settles.append((settle_time(order, self.cfg.timezone), order.pnl_usd))
        self.apply_daily_halts(state)


def today_in_tz(tz_name: str) -> date:
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
        import time as _t
        t = _t.time()
    return t


def day_history(orders, tz_name: str, today: date, start_equity: float) -> list:
    """Per-day realized stats for prior days (adaptive target input)."""
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
    for d in sorted(days):
        r = days[d]
        out.append(DayStat(day=d.isoformat(), pnl_pct=r["pnl"] / start_equity if start_equity else 0.0,
                           wins=r["w"], losses=r["l"],
                           avg_edge=(sum(r["edges"]) / len(r["edges"])) if r["edges"] else None))
    return out


def restore_state(cfg: Config, store: Store, risk: "RiskManager") -> tuple[RiskState, float]:
    """Rebuild cash, open positions and today's settlement path from the ledger (crash restart)."""
    day = today_in_tz(cfg.timezone)
    state = RiskState(day=day, starting_day_equity=cfg.starting_day_equity)
    orders = store.active_orders() if hasattr(store, "active_orders") else store.all_orders()
    state.history = day_history(orders, cfg.timezone, day, cfg.starting_day_equity)
    for order in orders:
        d = order_date_in_tz(order.ts, cfg.timezone)
        if d != day:
            continue
        if order.status == "open":
            risk.register_open(state, order)
        elif order.status == "settled" and order.pnl_usd is not None:
            state.realized_pnl += order.pnl_usd
            state.settles.append((settle_time(order, cfg.timezone), order.pnl_usd))
    risk.apply_daily_halts(state)
    cash = state.starting_day_equity + state.realized_pnl - state.open_exposure
    return state, cash
