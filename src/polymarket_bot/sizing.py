from __future__ import annotations

from .config import Config
from .fees import taker_fee_usd
from .models import Market, Signal


def kelly_fraction(prob: float, price: float, fee_per_contract: float) -> float:
    """Kelly fraction of bankroll for a binary contract paying $1.

    Net win W = 1 - price - fee, net loss L = price + fee.
    f* = (q*W - (1-q)*L) / (W*L) * L wait...

    Standard: bet fraction f of wealth, odds b = W/L (net odds).
    f* = (q*(b+1) - 1) / b = (q*W - (1-q)*L) / W
    """
    cost = price + fee_per_contract
    win = 1.0 - cost
    if cost <= 0 or win <= 0:
        return 0.0
    edge = prob * win - (1.0 - prob) * cost
    if edge <= 0:
        return 0.0
    return edge / win


def size_order(signal: Signal, market: Market, cash: float, cfg: Config,
               kelly_mult: float = 1.0, pattern_mult: float = 1.0) -> tuple[float, float, float]:
    """Return (contracts, cost_usd, fee_usd). Contracts may be 0 if too small.

    kelly_mult <= 1 shrinks Kelly & max fraction (profit-lock mode).
    pattern_mult in [0.25, 1.50] scales Kelly fraction inside existing hard caps.
    """
    fee_pc = signal.fee_per_contract
    f_full = kelly_fraction(signal.model_prob, signal.ask, fee_pc)
    km = max(0.0, min(1.0, float(kelly_mult)))
    pm = max(0.25, min(1.50, float(pattern_mult)))
    f = min(f_full * cfg.kelly_fraction * km * pm, cfg.max_fraction_of_bankroll * km)
    if f <= 0 or cash <= 0:
        return 0.0, 0.0, 0.0
    budget = min(cash * f, cfg.max_per_bet_usd, cash)
    # iterate once so fee is included in the budget
    # cost = contracts * price + fee(contracts, price)
    # fee ≈ theta * C * p * (1-p), so cost ≈ C * (p + theta*p*(1-p))
    unit = signal.ask + fee_pc
    if unit <= 0:
        return 0.0, 0.0, 0.0
    raw = budget / unit
    raw = min(raw, cfg.max_contracts, signal.liquidity_contracts)
    step = market.min_trade_qty or 0.01
    contracts = (int(raw / step)) * step
    if contracts < step:
        return 0.0, 0.0, 0.0
    fee = taker_fee_usd(cfg.fee_theta, contracts, signal.ask)
    cost = contracts * signal.ask + fee
    # shrink if cost slipped over cash/budget due to rounding
    if cost > min(budget, cash) + 1e-9:
        contracts = (int((min(budget, cash) - 0.01) / unit / step)) * step
        if contracts < step:
            return 0.0, 0.0, 0.0
        fee = taker_fee_usd(cfg.fee_theta, contracts, signal.ask)
        cost = contracts * signal.ask + fee
    return contracts, cost, fee
