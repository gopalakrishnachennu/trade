from __future__ import annotations

import math
from typing import Optional, Sequence

from .models import PricePoint

SECONDS_PER_YEAR = 365.25 * 24 * 3600


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def realized_annualized_vol(points: Sequence[PricePoint], lookback_seconds: int) -> Optional[float]:
    if len(points) < 10:
        return None
    cutoff = points[-1].ts - lookback_seconds
    xs = [p for p in points if p.ts >= cutoff and p.close > 0]
    if len(xs) < 10:
        return None
    rets = []
    for a, b in zip(xs, xs[1:]):
        dt = b.ts - a.ts
        if dt <= 0 or a.close <= 0 or b.close <= 0:
            continue
        # scale each log return to a 1-second equivalent, then annualize from 1s
        lr = math.log(b.close / a.close)
        rets.append(lr / math.sqrt(dt) if dt != 1 else lr)
    if len(rets) < 8:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    sigma_1s = math.sqrt(max(var, 0.0))
    return sigma_1s * math.sqrt(SECONDS_PER_YEAR)


def clamp_vol(annualized: Optional[float], floor: float, ceiling: float) -> float:
    if annualized is None or not math.isfinite(annualized):
        return floor
    return min(ceiling, max(floor, annualized))


def prob_settlement_up(
    points: Sequence[PricePoint],
    strike: float,
    window_end_ts: int,
    now_ts: int,
    annualized_vol: float,
) -> Optional[float]:
    """P(close statistic >= strike), matching US settlement rules.

    Settlement is the simple average of the 60 one-second index values in
    [T-60, T), rounded later by the exchange. Mid-window we treat the
    current index as a GBM to the midpoint of that minute. Once inside the
    final 60 seconds, known prints are locked and only the remainder is random.
    """
    if strike <= 0 or annualized_vol < 0:
        return None
    sigma_1s = annualized_vol / math.sqrt(SECONDS_PER_YEAR)
    settlement_start = window_end_ts - 60
    by_ts = {p.ts: p.close for p in points}

    if now_ts >= window_end_ts:
        # Window is over: compute the realized statistic if we have the prints.
        known = [by_ts[t] for t in range(settlement_start, window_end_ts) if t in by_ts]
        if len(known) < 30:
            return None
        avg = sum(known) / len(known)
        return 1.0 if avg >= strike else 0.0

    if now_ts < settlement_start:
        if not points:
            return None
        spot = points[-1].close
        # Time to the midpoint of the settlement minute.
        tau = max((window_end_ts - 30) - now_ts, 1)
        return _lognormal_prob_above(spot, strike, sigma_1s, tau)

    # Inside the settlement minute: lock in known seconds, randomize the rest.
    known = [by_ts[t] for t in range(settlement_start, min(now_ts + 1, window_end_ts)) if t in by_ts]
    n_known = len(known)
    n_left = 60 - n_known
    if n_left <= 0:
        avg = sum(known) / max(n_known, 1)
        return 1.0 if avg >= strike else 0.0
    if not points:
        return None
    spot = points[-1].close
    known_sum = sum(known)
    # Need sum_remaining >= 60*K - known_sum. Model remaining prints as
    # lognormal around current spot; approximate the average with a normal.
    # Horizon for remaining prints: they are only a few seconds away, so
    # drift/vol over n_left seconds is small.
    tau = max(n_left, 1)
    # P(mean(remaining) >= threshold)
    threshold_mean = (60.0 * strike - known_sum) / n_left
    if threshold_mean <= 0:
        return 1.0
    return _lognormal_prob_above(spot, threshold_mean, sigma_1s, tau)


def _lognormal_prob_above(spot: float, barrier: float, sigma_1s: float, tau_seconds: float) -> float:
    if spot <= 0 or barrier <= 0:
        return 0.0 if spot < barrier else 1.0
    vol = sigma_1s * math.sqrt(max(tau_seconds, 1.0))
    if vol <= 1e-12:
        return 1.0 if spot >= barrier else 0.0
    # P(S * exp(-0.5 σ²τ + σ√τ Z) >= K) = Φ( (ln(S/K) - 0.5 σ²τ) / (σ√τ) ) wait
    # actually Φ( (ln(S/K) - 0.5 var) / vol ) is P(S_T >= K) under GBM with zero drift
    z = (math.log(spot / barrier) - 0.5 * vol * vol) / vol
    return min(1.0, max(0.0, norm_cdf(z)))
