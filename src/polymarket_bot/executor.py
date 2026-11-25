from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .config import Config
from .models import Market, PaperOrder, PricePoint, Signal
from .risk import RiskManager, RiskState
from .sizing import size_order
from .store import Store

log = logging.getLogger(__name__)


def _ts(tz: str, when: Optional[datetime] = None) -> str:
    now = when or datetime.now(timezone.utc)
    return now.astimezone(ZoneInfo(tz)).isoformat()


def settlement_average(points: list[PricePoint], window_end_ts: int) -> Optional[float]:
    """Simple average of 1s closes in [T-60, T). Official rule on docs.polymarket.us."""
    by_ts = {p.ts: p.close for p in points}
    vals = [by_ts[t] for t in range(window_end_ts - 60, window_end_ts) if t in by_ts]
    if len(vals) < 30:
        return None
    return sum(vals) / len(vals)


def round_settlement(avg: float, decimals: int) -> float:
    return round(avg, decimals)


class PaperExecutor:
    """Simulated taker fills. Never sends orders to Polymarket US."""

    def __init__(self, cfg: Config, store: Store, risk: RiskManager, state: RiskState):
        self.cfg = cfg
        self.store = store
        self.risk = risk
        self.state = state
        self.cash = cfg.starting_cash

    def try_fill(self, signal: Signal, market: Market) -> Optional[PaperOrder]:
        tz = self.cfg.timezone
        now = datetime.now(timezone.utc)
        contracts, cost, fee = size_order(signal, market, self.cash, self.cfg, self.risk.size_multiplier(self.state))
        if contracts <= 0:
            log.info(
                "skip size=0 %s %s %s edge=%.4f ask=%.3f cash=%.2f",
                market.slug, signal.side, market.horizon, signal.edge_after_fees, signal.ask, self.cash,
            )
            return None
        reason = self.risk.allow(self.state, market.slug, cost, self.cash)
        order = PaperOrder(
            id=str(uuid.uuid4()),
            ts=_ts(tz, now),
            market_slug=market.slug,
            asset=market.asset,
            horizon=market.horizon,
            side=signal.side,
            contracts=contracts,
            price=signal.ask,
            fee_usd=fee,
            cost_usd=cost,
            model_prob=signal.model_prob,
            edge_after_fees=signal.edge_after_fees,
            price_to_beat=market.price_to_beat,
            window_end=market.window_end.isoformat(),
        )
        if reason:
            # Do not persist recurring risk blocks (already in the market / at
            # the concurrent cap) — they fire every poll and drown the trade log.
            recurring = reason.startswith("already have an open position") or reason.startswith("max concurrent")
            if recurring:
                log.debug("paper skip %s: %s", market.slug, reason)
                return None
            order.status = "rejected"
            order.reject_reason = reason
            self.store.write_order(order)
            log.info("paper reject %s: %s", market.slug, reason)
            return order
        self.cash -= cost
        order.status = "open"
        self.risk.register_open(self.state, order)
        self.store.write_order(order)
        log.info(
            "PAPER FILL %s %s %.2f ct @ %.3f fee=$%.2f cost=$%.2f edge=%.4f p=%.3f cash=$%.2f",
            market.slug, signal.side, contracts, signal.ask, fee, cost,
            signal.edge_after_fees, signal.model_prob, self.cash,
        )
        return order

    def settle_due(self, markets_by_slug: dict[str, Market], prices: dict[str, list[PricePoint]]) -> list[PaperOrder]:
        settled: list[PaperOrder] = []
        now_ts = int(datetime.now(timezone.utc).timestamp())
        for order in list(self.state.open_orders.values()):
            market = markets_by_slug.get(order.market_slug)
            end_ts = int(datetime.fromisoformat(order.window_end.replace("Z", "+00:00")).timestamp()) if order.window_end else None
            if end_ts is None and market is not None:
                end_ts = int(market.window_end.timestamp())
            if end_ts is None or now_ts < end_ts + 1:
                continue
            asset = order.asset or (market.asset if market else "")
            pts = prices.get(asset, [])
            avg = settlement_average(pts, end_ts)
            if avg is None:
                log.info("waiting for settlement prints %s (end=%s)", order.market_slug, end_ts)
                continue
            decimals = market.price_decimals if market else 2
            settle_px = round_settlement(avg, decimals)
            strike = order.price_to_beat
            if strike is None and market is not None:
                strike = market.price_to_beat
            if strike is None:
                log.warning("cannot settle %s: no price_to_beat", order.market_slug)
                continue
            went_up = settle_px >= strike
            won = (order.side == "up" and went_up) or (order.side == "down" and not went_up)
            payout = order.contracts * (1.0 if won else 0.0)
            pnl = payout - order.cost_usd
            order.status = "settled"
            order.settlement_price = settle_px
            order.result = "win" if won else "loss"
            order.payout_usd = payout
            order.pnl_usd = pnl
            self.cash += payout
            self.risk.register_settle(self.state, order)
            self.store.update_order(order)
            settled.append(order)
            log.info(
                "SETTLED %s %s settle=%.4f strike=%.4f %s pnl=$%.2f cash=$%.2f",
                order.market_slug, order.side, settle_px, strike, order.result, pnl, self.cash,
            )
        return settled
