from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

from .config import Config
from .fees import edge_after_fees, fee_per_contract
from .models import Book, Market, Signal
from .probability import clamp_vol, prob_settlement_up, realized_annualized_vol


class Rule(Protocol):
    name: str

    def check(self, ctx: "RuleContext") -> Optional[str]:
        """Return a failure reason, or None if the rule passes."""


@dataclass
class RuleContext:
    market: Market
    book: Book
    side: str
    ask: float
    model_prob: float
    edge: float
    spread: Optional[float]
    liquidity_contracts: float
    liquidity_usd: float
    now: datetime
    cfg: Config


class PriceToBeatKnown:
    name = "price_to_beat_known"

    def check(self, ctx: RuleContext) -> Optional[str]:
        if ctx.cfg.require_price_to_beat and ctx.market.price_to_beat is None:
            return "price_to_beat not yet stamped"
        return None


class WindowTiming:
    name = "window_timing"

    def check(self, ctx: RuleContext) -> Optional[str]:
        rem = ctx.market.seconds_remaining(ctx.now)
        into = ctx.market.seconds_into_window(ctx.now)
        if rem <= 0:
            return "window already ended"
        if rem < ctx.cfg.no_trade_final_seconds:
            return f"inside final {ctx.cfg.no_trade_final_seconds:.0f}s ({rem:.1f}s left)"
        if into < ctx.cfg.min_seconds_into_window:
            return f"only {into:.1f}s into window"
        return None


class MaxSpread:
    name = "max_spread"

    def check(self, ctx: RuleContext) -> Optional[str]:
        if ctx.spread is None:
            return "spread unknown"
        if ctx.spread > ctx.cfg.max_spread:
            return f"spread {ctx.spread:.3f} > max {ctx.cfg.max_spread:.3f}"
        return None


class MinLiquidity:
    name = "min_liquidity"

    def check(self, ctx: RuleContext) -> Optional[str]:
        if ctx.liquidity_contracts < ctx.cfg.min_liquidity_contracts:
            return f"liq {ctx.liquidity_contracts:.2f} contracts < {ctx.cfg.min_liquidity_contracts}"
        if ctx.liquidity_usd < ctx.cfg.min_liquidity_usd:
            return f"liq ${ctx.liquidity_usd:.2f} < ${ctx.cfg.min_liquidity_usd:.2f}"
        return None


class AskBounds:
    name = "ask_bounds"

    def check(self, ctx: RuleContext) -> Optional[str]:
        if ctx.ask < ctx.cfg.min_ask or ctx.ask > ctx.cfg.max_ask:
            return f"ask {ctx.ask:.3f} outside [{ctx.cfg.min_ask:.2f}, {ctx.cfg.max_ask:.2f}]"
        return None


class MinEdgeAfterFees:
    name = "min_edge_after_fees"

    def check(self, ctx: RuleContext) -> Optional[str]:
        if ctx.edge < ctx.cfg.min_edge:
            return f"edge {ctx.edge:.4f} < min {ctx.cfg.min_edge:.4f}"
        return None


DEFAULT_RULES: list[Rule] = [
    PriceToBeatKnown(),
    WindowTiming(),
    MaxSpread(),
    MinLiquidity(),
    AskBounds(),
    MinEdgeAfterFees(),
]


def measure(name: str, ctx: RuleContext) -> dict:
    """Numbers behind each rule so the dashboard can show value vs threshold."""
    c = ctx.cfg
    if name == "price_to_beat_known":
        return {"value": ctx.market.price_to_beat, "threshold": "required" if c.require_price_to_beat else "optional"}
    if name == "window_timing":
        return {
            "seconds_left": ctx.market.seconds_remaining(ctx.now),
            "seconds_into": ctx.market.seconds_into_window(ctx.now),
            "final_cutoff": c.no_trade_final_seconds,
            "min_into": c.min_seconds_into_window,
        }
    if name == "max_spread":
        return {"value": ctx.spread, "threshold": c.max_spread}
    if name == "min_liquidity":
        return {"value": ctx.liquidity_contracts, "threshold": c.min_liquidity_contracts,
                "value_usd": ctx.liquidity_usd, "threshold_usd": c.min_liquidity_usd}
    if name == "ask_bounds":
        return {"value": ctx.ask, "lo": c.min_ask, "hi": c.max_ask}
    if name == "min_edge_after_fees":
        return {"value": ctx.edge, "threshold": c.min_edge}
    return {}


class RulesEngine:
    def __init__(self, cfg: Config, rules: Optional[list[Rule]] = None):
        self.cfg = cfg
        self.rules = list(rules if rules is not None else DEFAULT_RULES)

    def evaluate(
        self,
        market: Market,
        book: Book,
        price_points,
        now: Optional[datetime] = None,
    ) -> list[Signal]:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        raw_vol = realized_annualized_vol(price_points, self.cfg.vol_lookback_seconds)
        vol = clamp_vol(raw_vol, self.cfg.vol_floor(market.asset), self.cfg.vol_ceiling(market.asset))
        spot = price_points[-1].close if price_points else None
        p_up: Optional[float] = None
        if market.price_to_beat and price_points:
            p_up = prob_settlement_up(
                price_points,
                market.price_to_beat,
                int(market.window_end.timestamp()),
                int(now.timestamp()),
                vol,
            )
        signals: list[Signal] = []
        for side in ("up", "down"):
            ask = book.up_ask if side == "up" else book.down_ask
            if p_up is None or ask is None:
                reason = []
                if p_up is None:
                    reason.append("model probability unavailable")
                if ask is None:
                    reason.append(f"{side} ask missing")
                signals.append(
                    Signal(
                        market_slug=market.slug,
                        asset=market.asset,
                        horizon=market.horizon,
                        side=side,
                        model_prob=float("nan") if p_up is None else (p_up if side == "up" else 1.0 - p_up),
                        ask=ask or 0.0,
                        fee_per_contract=fee_per_contract(self.cfg.fee_theta, ask or 0.0) if ask else 0.0,
                        edge_after_fees=float("nan"),
                        spread=book.spread,
                        liquidity_contracts=book.liquidity_contracts(side),
                        spot=spot,
                        price_to_beat=market.price_to_beat,
                        seconds_remaining=market.seconds_remaining(now),
                        annualized_vol=vol,
                        reasons=reason,
                        passed=False,
                    )
                )
                continue
            q = p_up if side == "up" else (1.0 - p_up)
            edge = edge_after_fees(q, ask, self.cfg.fee_theta)
            liq_c = book.liquidity_contracts(side)
            ctx = RuleContext(
                market=market,
                book=book,
                side=side,
                ask=ask,
                model_prob=q,
                edge=edge,
                spread=book.spread,
                liquidity_contracts=liq_c,
                liquidity_usd=liq_c * ask,
                now=now,
                cfg=self.cfg,
            )
            reasons = []
            checks = []
            for rule in self.rules:
                fail = rule.check(ctx)
                checks.append({"name": rule.name, "passed": fail is None, "detail": fail, **measure(rule.name, ctx)})
                if fail:
                    reasons.append(f"{rule.name}: {fail}")
            signals.append(
                Signal(
                    market_slug=market.slug,
                    asset=market.asset,
                    horizon=market.horizon,
                    side=side,
                    model_prob=q,
                    ask=ask,
                    fee_per_contract=fee_per_contract(self.cfg.fee_theta, ask),
                    edge_after_fees=edge,
                    spread=book.spread,
                    liquidity_contracts=liq_c,
                    spot=spot,
                    price_to_beat=market.price_to_beat,
                    seconds_remaining=market.seconds_remaining(now),
                    annualized_vol=vol,
                    reasons=reasons,
                    passed=not reasons,
                    rule_checks=checks,
                )
            )
        return signals
