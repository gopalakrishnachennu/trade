"""Serialize markets, books, and signals for the runtime bus."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from ..models import Book, BookLevel, Market, PricePoint, Signal, money, parse_iso


def dt(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.isoformat()
    return str(v)


def market_to_dict(m: Market) -> dict[str, Any]:
    return {
        "id": m.id,
        "slug": m.slug,
        "question": m.question,
        "asset": m.asset,
        "horizon": m.horizon,
        "index_symbol": m.index_symbol,
        "window_start": dt(m.window_start),
        "window_end": dt(m.window_end),
        "price_to_beat": m.price_to_beat,
        "price_decimals": m.price_decimals,
        "fee_coefficient": m.fee_coefficient,
        "min_trade_qty": m.min_trade_qty,
        "tick_size": m.tick_size,
        "status": m.status,
        "active": m.active,
        "closed": m.closed,
    }


def market_from_dict(d: dict) -> Market:
    return Market(
        id=str(d.get("id") or ""),
        slug=str(d.get("slug") or ""),
        question=str(d.get("question") or ""),
        asset=str(d.get("asset") or ""),
        horizon=str(d.get("horizon") or ""),
        index_symbol=str(d.get("index_symbol") or ""),
        window_start=parse_iso(d.get("window_start")) or datetime.now(timezone.utc),
        window_end=parse_iso(d.get("window_end")) or datetime.now(timezone.utc),
        price_to_beat=money(d.get("price_to_beat")) if d.get("price_to_beat") is not None else None,
        price_decimals=int(d.get("price_decimals") or 2),
        fee_coefficient=d.get("fee_coefficient"),
        min_trade_qty=float(d.get("min_trade_qty") or 0.01),
        tick_size=float(d.get("tick_size") or 0.01),
        status=str(d.get("status") or ""),
        active=bool(d.get("active")),
        closed=bool(d.get("closed")),
    )


def book_to_dict(b: Book) -> dict[str, Any]:
    return {
        "slug": b.slug,
        "best_bid": b.best_bid,
        "best_ask": b.best_ask,
        "long_quote": b.long_quote,
        "short_quote": b.short_quote,
        "bid_shares": b.bid_shares,
        "ask_shares": b.ask_shares,
        "up_ask": b.up_ask,
        "down_ask": b.down_ask,
        "spread": b.spread,
        "last_trade": b.last_trade,
    }


def signal_to_dict(s: Signal) -> dict[str, Any]:
    def f(x):
        try:
            if x is None:
                return None
            v = float(x)
            if v != v:  # nan
                return None
            return v
        except (TypeError, ValueError):
            return None
    return {
        "market_slug": s.market_slug,
        "asset": s.asset,
        "horizon": s.horizon,
        "side": s.side,
        "model_prob": f(s.model_prob),
        "ask": f(s.ask),
        "edge_after_fees": f(s.edge_after_fees),
        "spread": f(s.spread),
        "liquidity_contracts": f(s.liquidity_contracts),
        "spot": f(s.spot),
        "price_to_beat": f(s.price_to_beat),
        "seconds_remaining": f(s.seconds_remaining),
        "annualized_vol": f(s.annualized_vol),
        "reasons": list(s.reasons or []),
        "passed": bool(s.passed),
        "rule_checks": list(s.rule_checks or []),
    }


def points_to_list(points: list[PricePoint], n: int = 180) -> list[dict]:
    return [{"ts": p.ts, "close": p.close} for p in points[-n:]]
