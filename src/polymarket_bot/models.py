from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    s = ts.replace("Z", "+00:00")
    # allow extra fractional digits
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        if "." in s:
            head, rest = s.split(".", 1)
            frac, _, tz = rest.partition("+")
            if not tz and "-" in rest[1:] if rest.startswith("-") else False:
                pass
            # trim frac to 6 digits
            sign_idx = max(rest.find("+"), rest.find("-", 1))
            if sign_idx == -1:
                return datetime.fromisoformat(s)
            frac = rest[:sign_idx][:6]
            tz = rest[sign_idx:]
            return datetime.fromisoformat(f"{head}.{frac}{tz}")
        raise


def money(obj) -> Optional[float]:
    if obj is None:
        return None
    if isinstance(obj, (int, float)):
        return float(obj)
    if isinstance(obj, str):
        return float(obj)
    if isinstance(obj, dict) and "value" in obj:
        return float(obj["value"])
    return None


@dataclass
class BookLevel:
    price: float
    qty: float


@dataclass
class Book:
    slug: str
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    long_quote: Optional[float] = None   # ask to buy Up / Yes
    short_quote: Optional[float] = None  # ask to buy Down / No
    bid_shares: float = 0.0
    ask_shares: float = 0.0
    last_trade: Optional[float] = None
    bids: list[BookLevel] = field(default_factory=list)
    offers: list[BookLevel] = field(default_factory=list)
    raw_ts: Optional[str] = None

    @property
    def up_ask(self) -> Optional[float]:
        return self.long_quote if self.long_quote is not None else self.best_ask

    @property
    def down_ask(self) -> Optional[float]:
        if self.short_quote is not None:
            return self.short_quote
        if self.best_bid is not None:
            return max(0.01, min(0.99, round(1.0 - self.best_bid, 4)))
        return None

    @property
    def spread(self) -> Optional[float]:
        up, down = self.up_ask, self.down_ask
        if up is None or down is None:
            return None
        return up + down - 1.0

    def liquidity_contracts(self, side: str) -> float:
        if side == "up":
            if self.ask_shares:
                return self.ask_shares
            return sum(l.qty for l in self.offers)
        if self.bid_shares:
            return self.bid_shares
        return sum(l.qty for l in self.bids)

    @property
    def down_bid(self) -> Optional[float]:
        """Approximate bid for Down / No, from the long ask."""
        if self.best_ask is not None:
            return max(0.01, min(0.99, round(1.0 - self.best_ask, 4)))
        if self.up_ask is not None:
            return max(0.01, min(0.99, round(1.0 - self.up_ask, 4)))
        return None

    def mark_price(self, side: str) -> Optional[float]:
        """Exit mark: long bid for Up, implied Down bid for Down."""
        if side == "up":
            return self.best_bid
        return self.down_bid


@dataclass
class Market:
    id: str
    slug: str
    question: str
    asset: str
    horizon: str
    index_symbol: str
    window_start: datetime
    window_end: datetime
    price_to_beat: Optional[float]
    price_decimals: int
    fee_coefficient: Optional[float]
    min_trade_qty: float
    tick_size: float
    status: str
    active: bool
    closed: bool
    description: str = ""

    def seconds_remaining(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return (self.window_end - now).total_seconds()

    def seconds_into_window(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return (now - self.window_start).total_seconds()


@dataclass
class PricePoint:
    ts: int  # unix seconds
    close: float
    tick_count: int


@dataclass
class Signal:
    market_slug: str
    asset: str
    horizon: str
    side: str  # "up" or "down"
    model_prob: float
    ask: float
    fee_per_contract: float
    edge_after_fees: float
    spread: Optional[float]
    liquidity_contracts: float
    spot: Optional[float]
    price_to_beat: Optional[float]
    seconds_remaining: float
    annualized_vol: Optional[float]
    reasons: list[str] = field(default_factory=list)
    passed: bool = False
    rule_checks: list[dict] = field(default_factory=list)


@dataclass
class PaperOrder:
    id: str
    ts: str
    market_slug: str
    asset: str
    horizon: str
    side: str
    contracts: float
    price: float
    fee_usd: float
    cost_usd: float
    model_prob: float
    edge_after_fees: float
    price_to_beat: Optional[float]
    window_end: str
    status: str = "open"  # open, settled, rejected
    settlement_price: Optional[float] = None
    result: Optional[str] = None  # win, loss
    payout_usd: Optional[float] = None
    pnl_usd: Optional[float] = None
    reject_reason: Optional[str] = None
