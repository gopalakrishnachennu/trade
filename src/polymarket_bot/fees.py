from __future__ import annotations

import decimal
from decimal import Decimal, ROUND_HALF_EVEN


def taker_fee_usd(theta: float, contracts: float, price: float) -> float:
    """Official standard fee: Θ × C × p × (1 − p), banker's rounded to cents.

    https://docs.polymarket.us/fees
    """
    raw = Decimal(str(theta)) * Decimal(str(contracts)) * Decimal(str(price)) * (
        Decimal("1") - Decimal(str(price))
    )
    cents = raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return float(cents)


def fee_per_contract(theta: float, price: float) -> float:
    # per-contract fee before rounding (used for edge calc)
    return float(theta) * float(price) * (1.0 - float(price))


def edge_after_fees(model_prob: float, ask: float, theta: float) -> float:
    """Expected value per $1 payout contract, net of taker fee.

    EV = q * 1 - (ask + fee_per_contract). Positive means a profitable take
    if the model is calibrated.
    """
    return float(model_prob) - float(ask) - fee_per_contract(theta, ask)
