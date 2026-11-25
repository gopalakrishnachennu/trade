"""Online Bayesian pattern learning & adaptive sizing/filtering layer.

Features extracted for every evaluation and every settled trade:
  asset, timeframe (horizon), side, hour_et, hour_bucket (3h ET blocks),
  weekday, minutes_left, distance_sigma (distance from strike in sigma),
  vol_regime (low/mid/high), annualized_vol, model_prob, ask, edge,
  spread, depth_contracts, depth_usd, strategy, outcome, pnl_usd.

Modes:
  - off    : kill switch; no filtering or sizing adjustment
  - shadow : log what it would do (would skip / would mult) without altering trades
  - active : skip contexts with confidently negative EV (after min_samples)
             and scale Kelly fraction inside [min_mult, max_mult] (0.25x-1.50x)

Zero lookahead: the store updates online only after a trade settles.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SECONDS_PER_YEAR = 365.25 * 24 * 3600
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DEFAULT_VOL_BANDS = {
    "btc": (0.40, 1.50),
    "eth": (0.50, 1.80),
    "sol": (0.70, 2.50),
    "xrp": (0.70, 2.50),
}


def _f(x: Any) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _parse_dt(ts: Any, tz_name: str = "America/New_York") -> datetime:
    from .analytics import parse_ts
    dt = parse_ts(ts, tz_name)
    if dt is None:
        return datetime.now(ZoneInfo(tz_name))
    return dt.astimezone(ZoneInfo(tz_name))


def hour_bucket_for(hour_et: int) -> str:
    lo = (int(hour_et) // 3) * 3
    hi = lo + 3
    return f"{lo:02d}-{hi:02d}h"


def hour_bucket_label(hb: str) -> str:
    return f"{hb.replace('-', '–')} ET"


def distance_in_sigma(
    spot: Optional[float],
    strike: Optional[float],
    annualized_vol: Optional[float],
    seconds_remaining: Optional[float],
) -> Optional[float]:
    if spot is None or strike is None or annualized_vol is None or seconds_remaining is None:
        return None
    if spot <= 0 or strike <= 0 or annualized_vol <= 0:
        return None
    tau = max(float(seconds_remaining), 1.0)
    sigma_tau = float(annualized_vol) * math.sqrt(tau / SECONDS_PER_YEAR)
    if sigma_tau <= 1e-12:
        return None
    return round(math.log(float(spot) / float(strike)) / sigma_tau, 4)


def distance_bucket_for(dist_sigma: Optional[float]) -> str:
    if dist_sigma is None:
        return "unknown"
    a = abs(float(dist_sigma))
    if a < 0.25:
        return "<0.25σ"
    if a < 0.75:
        return "0.25-0.75σ"
    if a < 1.50:
        return "0.75-1.5σ"
    return ">=1.5σ"


def vol_regime_for(asset: str, annualized_vol: Optional[float], cfg=None) -> str:
    if annualized_vol is None:
        return "unknown"
    a = (asset or "").lower()
    if cfg is not None and hasattr(cfg, "vol_floor") and hasattr(cfg, "vol_ceiling"):
        lo, hi = cfg.vol_floor(a), cfg.vol_ceiling(a)
    else:
        lo, hi = DEFAULT_VOL_BANDS.get(a, (0.50, 2.00))
    span = max(hi - lo, 1e-6)
    ratio = (float(annualized_vol) - lo) / span
    if ratio < 0.25:
        return "low"
    if ratio <= 0.65:
        return "mid"
    return "high"


def minutes_bucket_for(minutes_left: Optional[float]) -> str:
    if minutes_left is None:
        return "unknown"
    m = float(minutes_left)
    if m < 2:
        return "<2m"
    if m < 5:
        return "2-5m"
    if m < 15:
        return "5-15m"
    return ">=15m"


def edge_bucket_for(edge: Optional[float]) -> str:
    if edge is None:
        return "unknown"
    e = float(edge)
    if e < 0.02:
        return "<2%"
    if e < 0.04:
        return "2-4%"
    if e < 0.08:
        return "4-8%"
    return ">=8%"


def horizon_seconds(horizon: str) -> float:
    return {"5m": 300.0, "15m": 900.0, "1h": 3600.0}.get(str(horizon or ""), 900.0)


def infer_window_start(now_ts: Optional[float], seconds_remaining: Optional[float], horizon: str) -> Optional[float]:
    if now_ts is None:
        return None
    return float(now_ts) + float(seconds_remaining if seconds_remaining is not None else 0.0) - horizon_seconds(horizon)


def side_sign(side: str) -> float:
    s = (side or "").lower()
    if s == "up":
        return 1.0
    if s == "down":
        return -1.0
    return 0.0


def normalize_ticks(ticks: Any) -> list[tuple[float, float]]:
    """Deduped, time-sorted (ts, spot) from hist [{ts, close|spot}] or path points."""
    raw: dict[float, float] = {}
    for pt in ticks or []:
        if isinstance(pt, (tuple, list)) and len(pt) >= 2:
            t, px = _f(pt[0]), _f(pt[1])
        elif isinstance(pt, dict):
            t = _f(pt.get("ts") if pt.get("ts") is not None else pt.get("ts_unix"))
            px = _f(pt.get("spot") if pt.get("spot") is not None else pt.get("close") or pt.get("price"))
        else:
            continue
        if t is None or px is None or px <= 0:
            continue
        raw[float(t)] = float(px)
    return sorted(raw.items())


def _price_asof(ticks: list[tuple[float, float]], t: float) -> Optional[tuple[float, float]]:
    """Last tick at or before t, or None."""
    chosen = None
    for ts, px in ticks:
        if ts <= t + 1e-9:
            chosen = (ts, px)
        else:
            break
    return chosen


def _price_near(ticks: list[tuple[float, float]], t_target: float, t_now: float, max_lag: float = 20.0) -> Optional[float]:
    """Price at t_target using only ticks with ts <= t_now (no lookahead).

    Accepts the last tick in [t_target - max_lag, min(t_target, t_now)].
    """
    hi = min(float(t_target), float(t_now)) + 1e-9
    lo = float(t_target) - float(max_lag)
    chosen = None
    for ts, px in ticks:
        if ts > hi:
            break
        if ts + 1e-12 >= lo:
            chosen = px
    return chosen


def signed_move_sigma(p_now: Optional[float], p_then: Optional[float], vol: Optional[float],
                      dt_seconds: Optional[float], side: str) -> Optional[float]:
    """Log return over dt, in σ units, signed so + means the move agrees with `side`."""
    if p_now is None or p_then is None or vol is None or dt_seconds is None:
        return None
    if p_now <= 0 or p_then <= 0 or vol <= 0 or dt_seconds <= 1e-6:
        return None
    sig = float(vol) * math.sqrt(float(dt_seconds) / SECONDS_PER_YEAR)
    if sig <= 1e-12:
        return None
    raw = math.log(float(p_now) / float(p_then)) / sig
    ss = side_sign(side)
    if ss == 0.0:
        return None
    return round(raw * ss, 4)


def count_strike_crosses(ticks: list[tuple[float, float]], strike: float, t_start: float, t_now: float) -> int:
    """Crossings of `strike` on ticks with t_start <= ts <= t_now. No lookahead."""
    signs: list[int] = []
    n = 0
    eps = 1e-12 * max(1.0, abs(float(strike)))
    for ts, px in ticks:
        if ts < t_start - 1e-9:
            continue
        if ts > t_now + 1e-9:
            break
        d = float(px) - float(strike)
        if abs(d) <= eps:
            continue
        s = 1 if d > 0 else -1
        if signs and s != signs[-1]:
            n += 1
        if not signs or s != signs[-1]:
            signs.append(s)
    return n


def drift_direction(spot: Optional[float], strike: Optional[float], side: str) -> str:
    if spot is None or strike is None or side_sign(side) == 0.0:
        return "unknown"
    if abs(float(spot) - float(strike)) <= 1e-12 * max(1.0, abs(float(strike))):
        return "flat"
    implied = "up" if float(spot) >= float(strike) else "down"
    return "with" if implied == (side or "").lower() else "against"


def signed_drift(spot: Optional[float], strike: Optional[float], side: str) -> Optional[float]:
    """+1 if spot is on the bet's side of the strike, -1 if against, 0 if flat."""
    d = drift_direction(spot, strike, side)
    if d == "with":
        return 1.0
    if d == "against":
        return -1.0
    if d == "flat":
        return 0.0
    return None


def mom_bucket_for(mom: Optional[float]) -> str:
    if mom is None:
        return "unknown"
    x = float(mom)
    if x <= -1.0:
        return "<=-1σ"
    if x <= -0.25:
        return "-1--0.25σ"
    if x < 0.25:
        return "-0.25-+0.25σ"
    if x < 1.0:
        return "0.25-1σ"
    return ">=1σ"


def crosses_bucket_for(n: Optional[int]) -> str:
    if n is None:
        return "unknown"
    k = int(n)
    if k <= 0:
        return "0"
    if k == 1:
        return "1"
    if k <= 3:
        return "2-3"
    return "4+"


ENTRY_FEATURE_KEYS = (
    "mom_1m", "mom_3m", "mom_window", "strike_cross_count",
    "drift_dir", "signed_drift", "mom_1m_bucket", "mom_3m_bucket",
    "mom_window_bucket", "crosses_bucket",
)


def entry_features(
    ticks: Any,
    *,
    now_ts: float,
    side: str,
    strike: Optional[float],
    vol: Optional[float],
    horizon: str,
    seconds_remaining: Optional[float],
    spot: Optional[float] = None,
    max_lag: float = 20.0,
) -> dict[str, Any]:
    """Entry-time path features. Ticks with ts > now_ts are ignored (zero lookahead)."""
    pts = normalize_ticks(ticks)
    # Hard no-lookahead clip
    pts = [(t, p) for t, p in pts if t <= float(now_ts) + 1e-9]
    out: dict[str, Any] = {k: None for k in ENTRY_FEATURE_KEYS}
    out["drift_dir"] = "unknown"
    out["crosses_bucket"] = "unknown"
    out["mom_1m_bucket"] = "unknown"
    out["mom_3m_bucket"] = "unknown"
    out["mom_window_bucket"] = "unknown"

    cur = _price_asof(pts, now_ts)
    p_now = spot if (spot is not None and spot > 0) else (cur[1] if cur else None)
    if p_now is None:
        return out

    out["drift_dir"] = drift_direction(p_now, strike, side)
    out["signed_drift"] = signed_drift(p_now, strike, side)

    p_1 = _price_near(pts, float(now_ts) - 60.0, now_ts, max_lag=max_lag)
    p_3 = _price_near(pts, float(now_ts) - 180.0, now_ts, max_lag=max_lag)
    out["mom_1m"] = signed_move_sigma(p_now, p_1, vol, 60.0, side) if p_1 is not None else None
    out["mom_3m"] = signed_move_sigma(p_now, p_3, vol, 180.0, side) if p_3 is not None else None
    out["mom_1m_bucket"] = mom_bucket_for(out["mom_1m"])
    out["mom_3m_bucket"] = mom_bucket_for(out["mom_3m"])

    ws = infer_window_start(now_ts, seconds_remaining, horizon)
    if ws is not None and strike is not None:
        out["strike_cross_count"] = count_strike_crosses(pts, float(strike), ws, now_ts)
        out["crosses_bucket"] = crosses_bucket_for(out["strike_cross_count"])
        # mom_window only if we actually observed the start of the window (no silent lookahead, no silent gap).
        cover = max(20.0, 0.05 * horizon_seconds(horizon))
        p_open = _price_near(pts, ws, now_ts, max_lag=cover)
        # _price_near looks in [ws - cover, ws]; we also accept the first tick shortly *after* open, still <= now.
        if p_open is None:
            first = None
            for t, p in pts:
                if t < ws - 1e-9:
                    continue
                if t > now_ts + 1e-9:
                    break
                first = (t, p)
                break
            if first is not None and first[0] - ws <= cover:
                p_open = first[1]
        dt_w = max(1.0, float(now_ts) - float(ws))
        if p_open is not None:
            out["mom_window"] = signed_move_sigma(p_now, p_open, vol, dt_w, side)
        out["mom_window_bucket"] = mom_bucket_for(out["mom_window"])
    return out


def paths_from_eval_rows(rows: Any, tz_name: str = "America/New_York") -> dict[str, list[tuple[float, float]]]:
    """market_slug -> unique (ts, spot) ticks. Caller must still clip to decision time."""
    from .analytics import ts_unix
    acc: dict[str, dict[float, float]] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        slug = str(r.get("market_slug") or "")
        spot = _f(r.get("spot"))
        t = ts_unix(r.get("ts"), tz_name)
        if not slug or spot is None or spot <= 0 or t is None:
            continue
        acc.setdefault(slug, {})[float(t)] = float(spot)
    return {k: sorted(v.items()) for k, v in acc.items()}


def extract_features(
    row: Any,
    *,
    ts: Any = None,
    tz_name: str = "America/New_York",
    cfg=None,
    strategy: Optional[str] = None,
    ticks: Any = None,
) -> dict[str, Any]:
    """Extract normalized pattern features from a Signal, signal dict, PaperOrder, or eval row."""
    g = (lambda k, d=None: row.get(k, d)) if isinstance(row, dict) else (lambda k, d=None: getattr(row, k, d))
    dt = _parse_dt(ts if ts is not None else (g("ts") or g("ts_unix")), tz_name)
    asset = str(g("asset") or "").lower()
    horizon = str(g("horizon") or g("timeframe") or "")
    side = str(g("side") or "").lower()
    hour_et = int(dt.hour)
    hb = hour_bucket_for(hour_et)
    weekday = WEEKDAYS[dt.weekday()]
    sr = _f(g("seconds_remaining"))
    minutes_left = round(max(0.0, sr) / 60.0, 3) if sr is not None else _f(g("minutes_left"))
    vol = _f(g("annualized_vol"))
    spot = _f(g("spot"))
    strike = _f(g("price_to_beat"))
    dsigma = _f(g("distance_sigma"))
    if dsigma is None:
        dsigma = distance_in_sigma(spot, strike, vol, sr)
    ask = _f(g("ask"))
    if ask is None:
        ask = _f(g("price"))
    liq_c = _f(g("liquidity_contracts"))
    if liq_c is None:
        liq_c = _f(g("depth_contracts"))
    depth_usd = _f(g("depth_usd"))
    if depth_usd is None and liq_c is not None and ask is not None:
        depth_usd = round(liq_c * ask, 2)
    edge = _f(g("edge_after_fees"))
    if edge is None:
        edge = _f(g("edge"))
    feat = {
        "ts": dt.isoformat(),
        "ts_unix": round(dt.timestamp(), 3),
        "market_slug": str(g("market_slug") or ""),
        "asset": asset,
        "horizon": horizon,
        "side": side,
        "hour_et": hour_et,
        "hour_bucket": hb,
        "weekday": weekday,
        "minutes_left": minutes_left,
        "distance_sigma": dsigma,
        "distance_bucket": distance_bucket_for(dsigma),
        "vol_regime": str(g("vol_regime") or vol_regime_for(asset, vol, cfg)),
        "annualized_vol": round(vol, 4) if vol is not None else None,
        "model_prob": _f(g("model_prob")),
        "ask": ask,
        "edge": edge,
        "spread": _f(g("spread")),
        "depth_contracts": liq_c,
        "depth_usd": depth_usd,
        "strategy": str(strategy or g("strategy") or "baseline"),
        "passed": g("passed"),
        "minutes_bucket": minutes_bucket_for(minutes_left),
        "edge_bucket": edge_bucket_for(edge),
    }
    # Pass through any already-captured entry features (e.g. from a stored pattern row).
    for k in ENTRY_FEATURE_KEYS:
        if feat.get(k) is None:
            v = g(k)
            if v is not None and v != "":
                feat[k] = v
    if ticks:
        feat.update(entry_features(
            ticks,
            now_ts=float(feat["ts_unix"]),
            side=side,
            strike=strike,
            vol=vol,
            horizon=horizon,
            seconds_remaining=sr,
            spot=spot,
        ))
    else:
        feat.setdefault("drift_dir", drift_direction(spot, strike, side) if strike is not None else feat.get("drift_dir") or "unknown")
        if feat.get("signed_drift") is None:
            feat["signed_drift"] = signed_drift(spot, strike, side)
        feat.setdefault("mom_1m_bucket", mom_bucket_for(feat.get("mom_1m")))
        feat.setdefault("mom_3m_bucket", mom_bucket_for(feat.get("mom_3m")))
        feat.setdefault("mom_window_bucket", mom_bucket_for(feat.get("mom_window")))
        feat.setdefault("crosses_bucket", crosses_bucket_for(feat.get("strike_cross_count")))
    return feat


@dataclass
class BucketStat:
    n: float = 0.0
    wins: float = 0.0
    sum_roi: float = 0.0
    sum_roi_sq: float = 0.0
    sum_pnl: float = 0.0
    sum_cost: float = 0.0
    sum_p: float = 0.0

    def add(self, won: bool, roi: float, pnl: float, cost: float, model_prob: Optional[float]) -> None:
        self.n += 1
        if won:
            self.wins += 1
        self.sum_roi += roi
        self.sum_roi_sq += roi * roi
        self.sum_pnl += pnl
        self.sum_cost += cost
        if model_prob is not None and math.isfinite(model_prob):
            self.sum_p += model_prob

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "wins": self.wins,
            "sum_roi": round(self.sum_roi, 6),
            "sum_roi_sq": round(self.sum_roi_sq, 6),
            "sum_pnl": round(self.sum_pnl, 4),
            "sum_cost": round(self.sum_cost, 4),
            "sum_p": round(self.sum_p, 6),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BucketStat":
        return cls(
            n=float(d.get("n") or 0),
            wins=float(d.get("wins") or 0),
            sum_roi=float(d.get("sum_roi") or 0.0),
            sum_roi_sq=float(d.get("sum_roi_sq") or 0.0),
            sum_pnl=float(d.get("sum_pnl") or 0.0),
            sum_cost=float(d.get("sum_cost") or 0.0),
            sum_p=float(d.get("sum_p") or 0.0),
        )


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class PatternStore:
    """Online hierarchical Bayesian performance store across trade contexts."""

    def __init__(self, prior_strength: float = 10.0):
        self.prior_strength = max(1.0, float(prior_strength))
        self.global_stat = BucketStat()
        self.dims: dict[str, dict[str, BucketStat]] = {
            "context": {},         # asset|horizon|hour_bucket
            "asset_horizon": {},   # asset|horizon
            "hour_cell": {},       # asset|horizon|HH
            "hour_bucket": {},     # 00-03h .. 21-24h
            "vol_regime": {},      # low/mid/high/unknown
            "distance_bucket": {}, # <0.25σ .. >=1.5σ
            "weekday": {},         # Mon..Sun
            "strategy": {},        # baseline..
            "edge_bucket": {},
            "minutes_bucket": {},
        }
        self.cf_global = BucketStat()
        self.cf_dims: dict[str, dict[str, BucketStat]] = {k: {} for k in self.dims}
        self.cf_seen: set[str] = set()
        self.cf_cursor_ts: float = 0.0
        self.seen_ids: set[str] = set()
        self.recent_trades: list[dict[str, Any]] = []
        self.shadow_stats: dict[str, Any] = {
            "evaluated": 0,
            "would_skip": 0,
            "would_reduce": 0,
            "would_boost": 0,
            "settled_would_skip": 0,
            "settled_would_skip_wins": 0,
            "settled_would_skip_pnl": 0.0,
        }
        self.updated_at: float = 0.0

    def _bucket(self, dim: str, key: str) -> BucketStat:
        d = self.dims.setdefault(dim, {})
        if key not in d:
            d[key] = BucketStat()
        return d[key]

    def update_trade(self, feat: dict[str, Any], *, order_id: str, pnl_usd: float, cost_usd: float, outcome: str) -> bool:
        """Update posteriors online after one trade settles. Idempotent on order_id."""
        if order_id and order_id in self.seen_ids:
            return False
        if order_id:
            self.seen_ids.add(order_id)
        cost = max(0.01, float(cost_usd or 0.0))
        pnl = float(pnl_usd or 0.0)
        won = (outcome == "win") or (pnl > 0)
        roi = max(-1.0, min(20.0, pnl / cost))
        mp = _f(feat.get("model_prob"))

        asset = str(feat.get("asset") or "").lower()
        horizon = str(feat.get("horizon") or "")
        hb = str(feat.get("hour_bucket") or "00-03h")
        hr = int(feat.get("hour_et") or 0)
        ah_key = f"{asset}|{horizon}"
        ctx_key = f"{asset}|{horizon}|{hb}"
        cell_key = f"{asset}|{horizon}|{hr:02d}"

        self.global_stat.add(won, roi, pnl, cost, mp)
        self._bucket("context", ctx_key).add(won, roi, pnl, cost, mp)
        self._bucket("asset_horizon", ah_key).add(won, roi, pnl, cost, mp)
        self._bucket("hour_cell", cell_key).add(won, roi, pnl, cost, mp)
        self._bucket("hour_bucket", hb).add(won, roi, pnl, cost, mp)
        self._bucket("vol_regime", str(feat.get("vol_regime") or "unknown")).add(won, roi, pnl, cost, mp)
        self._bucket("distance_bucket", str(feat.get("distance_bucket") or "unknown")).add(won, roi, pnl, cost, mp)
        self._bucket("weekday", str(feat.get("weekday") or "Mon")).add(won, roi, pnl, cost, mp)
        self._bucket("strategy", str(feat.get("strategy") or "baseline")).add(won, roi, pnl, cost, mp)
        self._bucket("edge_bucket", str(feat.get("edge_bucket") or edge_bucket_for(_f(feat.get("edge"))))).add(won, roi, pnl, cost, mp)
        self._bucket("minutes_bucket", str(feat.get("minutes_bucket") or minutes_bucket_for(_f(feat.get("minutes_left"))))).add(won, roi, pnl, cost, mp)

        rec = {
            **feat,
            "order_id": order_id,
            "outcome": "win" if won else "loss",
            "pnl_usd": round(pnl, 2),
            "cost_usd": round(cost, 2),
            "roi": round(roi, 4),
            "context": ctx_key,
        }
        self.recent_trades.append(rec)
        if len(self.recent_trades) > 120:
            self.recent_trades = self.recent_trades[-120:]
        # Track counterfactual if shadow mode flagged this trade's context
        if feat.get("pattern_shadow_action") == "skip":
            self.shadow_stats["settled_would_skip"] += 1
            if won:
                self.shadow_stats["settled_would_skip_wins"] += 1
            self.shadow_stats["settled_would_skip_pnl"] = round(
                float(self.shadow_stats["settled_would_skip_pnl"]) + pnl, 2
            )
        self.updated_at = time.time()
        return True

    def _cf_bucket(self, dim: str, key: str) -> BucketStat:
        d = self.cf_dims.setdefault(dim, {})
        if key not in d:
            d[key] = BucketStat()
        return d[key]

    def update_counterfactual(self, feat: dict[str, Any], *, source_id: str, pnl_usd: float,
                              cost_usd: float, outcome: str, weight: float = 0.25) -> bool:
        """Weighted evidence from an evaluation that was not (or might not have been) filled.

        Separate stream: does not change real-fill posteriors. Idempotent on source_id.
        """
        sid = str(source_id or "")
        if not sid or sid in self.cf_seen or sid in self.seen_ids:
            return False
        self.cf_seen.add(sid)
        w = max(0.0, float(weight))
        if w <= 0:
            return False
        cost = max(0.01, float(cost_usd or 0.0))
        pnl = float(pnl_usd or 0.0)
        won = (outcome == "win") or (pnl > 0)
        roi = max(-1.0, min(20.0, pnl / cost))
        mp = _f(feat.get("model_prob"))
        asset = str(feat.get("asset") or "").lower()
        horizon = str(feat.get("horizon") or "")
        hb = str(feat.get("hour_bucket") or "00-03h")
        keys = {
            "context": f"{asset}|{horizon}|{hb}",
            "asset_horizon": f"{asset}|{horizon}",
            "hour_cell": f"{asset}|{horizon}|{int(feat.get('hour_et') or 0):02d}",
            "hour_bucket": hb,
            "vol_regime": str(feat.get("vol_regime") or "unknown"),
            "distance_bucket": str(feat.get("distance_bucket") or "unknown"),
            "weekday": str(feat.get("weekday") or "Mon"),
            "strategy": str(feat.get("strategy") or "baseline"),
            "edge_bucket": str(feat.get("edge_bucket") or edge_bucket_for(_f(feat.get("edge")))),
            "minutes_bucket": str(feat.get("minutes_bucket") or minutes_bucket_for(_f(feat.get("minutes_left")))),
        }
        def _add(st: BucketStat) -> None:
            st.n += w
            if won:
                st.wins += w
            st.sum_roi += roi * w
            st.sum_roi_sq += (roi * roi) * w
            st.sum_pnl += pnl * w
            st.sum_cost += cost * w
            if mp is not None and math.isfinite(mp):
                st.sum_p += mp * w
        _add(self.cf_global)
        for dim, key in keys.items():
            _add(self._cf_bucket(dim, key))
        self.updated_at = time.time()
        return True

    def blended_stat(self, dim: Optional[str], key: Optional[str], cf_weight: float = 0.0) -> BucketStat:
        """Real fills plus cf_weight × the counterfactual stream (cf_weight already applied at ingest if desired).

        `cf_weight` here is an extra scale on the stored CF stream (which was already weighted
        at ingest). Pass 1.0 to use the ingested weights as-is when mode is active; 0.0 to
        ignore CF (shadow/off).
        """
        real = self.global_stat if not dim else (self.dims.get(dim, {}).get(key) or BucketStat())
        if cf_weight <= 0:
            return real
        cf = self.cf_global if not dim else (self.cf_dims.get(dim, {}).get(key) or BucketStat())
        w = float(cf_weight)
        return BucketStat(
            n=real.n + w * cf.n,
            wins=real.wins + w * cf.wins,
            sum_roi=real.sum_roi + w * cf.sum_roi,
            sum_roi_sq=real.sum_roi_sq + w * cf.sum_roi_sq,
            sum_pnl=real.sum_pnl + w * cf.sum_pnl,
            sum_cost=real.sum_cost + w * cf.sum_cost,
            sum_p=real.sum_p + w * cf.sum_p,
        )

    def _global_priors(self) -> tuple[float, float, float]:
        g = self.global_stat
        w0 = (2.0 + g.wins) / (4.0 + g.n)
        mu0 = g.sum_roi / (5.0 + g.n)
        if g.n > 1:
            ss = max(0.0, g.sum_roi_sq - (g.sum_roi * g.sum_roi) / g.n)
            s0_sq = max(0.25, ss / (g.n - 1))
        else:
            s0_sq = 1.0
        return w0, mu0, s0_sq

    def posterior(self, dim: str, key: str, *, parent_wr: Optional[float] = None, parent_ev: Optional[float] = None,
                  cf_weight: float = 0.0) -> dict[str, Any]:
        st = self.blended_stat(dim, key, cf_weight)
        w0, mu0, s0_sq = self._global_priors()
        pw = w0 if parent_wr is None else parent_wr
        pe = mu0 if parent_ev is None else parent_ev
        m = self.prior_strength
        alpha = m * pw + st.wins
        beta = m * (1.0 - pw) + (st.n - st.wins)
        post_wr = alpha / (alpha + beta)
        se_wr = math.sqrt((post_wr * (1.0 - post_wr)) / (alpha + beta + 1.0))

        ev_hat = (m * pe + st.sum_roi) / (m + st.n)
        ss = max(0.0, st.sum_roi_sq - (st.sum_roi * st.sum_roi) / st.n) if st.n > 1 else 0.0
        var_hat = (m * s0_sq + ss) / (m + max(0, st.n - 1))
        se_ev = math.sqrt(var_hat / (m + st.n))
        ev_ucb = ev_hat + 1.645 * se_ev
        ev_lcb = ev_hat - 1.645 * se_ev
        return {
            "key": key,
            "n": st.n,
            "wins": st.wins,
            "losses": st.n - st.wins,
            "raw_wr": round(st.wins / st.n, 4) if st.n else None,
            "post_wr": round(post_wr, 4),
            "se_wr": round(se_wr, 4),
            "alpha": round(alpha, 2),
            "beta": round(beta, 2),
            "raw_ev": round(st.sum_roi / st.n, 4) if st.n else None,
            "post_ev": round(ev_hat, 4),
            "se_ev": round(se_ev, 4),
            "ev_lcb": round(ev_lcb, 4),
            "ev_ucb": round(ev_ucb, 4),
            "pnl_usd": round(st.sum_pnl, 2),
            "cost_usd": round(st.sum_cost, 2),
        }

    def context_posterior(self, asset: str, horizon: str, hour_bucket: str,
                          cf_weight: float = 0.0) -> tuple[dict[str, Any], dict[str, Any]]:
        a, h = asset.lower(), horizon
        parent = self.posterior("asset_horizon", f"{a}|{h}", cf_weight=cf_weight)
        child = self.posterior("context", f"{a}|{h}|{hour_bucket}",
                               parent_wr=parent["post_wr"], parent_ev=parent["post_ev"],
                               cf_weight=cf_weight)
        return child, parent

    def evaluate(
        self,
        feat: dict[str, Any],
        *,
        mode: str = "shadow",
        min_samples: int = 20,
        min_mult: float = 0.25,
        max_mult: float = 1.50,
        skip_ev_threshold: float = -0.02,
        record_shadow: bool = False,
        cf_weight: float = 0.0,
    ) -> dict[str, Any]:
        """Evaluate adaptive pattern adjustment / skip for one candidate signal."""
        if mode == "off":
            return {
                "mode": "off", "passed": True, "mult": 1.0, "would_mult": 1.0,
                "action": "off", "would_action": "off", "context": None,
                "n": 0, "post_ev": None, "post_wr": None, "detail": None,
            }
        asset = str(feat.get("asset") or "").lower()
        horizon = str(feat.get("horizon") or "")
        hb = str(feat.get("hour_bucket") or hour_bucket_for(int(feat.get("hour_et") or 0)))
        child, parent = self.context_posterior(asset, horizon, hb, cf_weight=cf_weight)

        if child["n"] >= min_samples:
            chosen = child
            label = f"{asset.upper()} {horizon} {hour_bucket_label(hb)}"
            level = "context"
        elif parent["n"] >= min_samples:
            chosen = parent
            label = f"{asset.upper()} {horizon}"
            level = "asset_horizon"
        else:
            chosen = child
            label = f"{asset.upper()} {horizon} {hour_bucket_label(hb)}"
            level = "collecting"

        n = chosen["n"]
        ev_hat = chosen["post_ev"]
        ev_ucb = chosen["ev_ucb"]
        post_wr = chosen["post_wr"]
        tag = " (shadow)" if mode == "shadow" else ""

        if n < min_samples:
            would_action = "collect"
            would_mult = 1.0
            detail = f"pattern{tag}: {label}, n={n} < {min_samples} → 1.00× (collecting)"
        elif ev_hat <= skip_ev_threshold and ev_ucb < 0.0:
            would_action = "skip"
            would_mult = min_mult
            detail = f"pattern{tag}: {label}, n={n}, EV {ev_hat:+.1%} → {'would skip' if mode == 'shadow' else 'skip'}"
        else:
            would_mult = round(_clamp(1.0 + 2.5 * ev_hat, min_mult, max_mult), 2)
            would_action = "boost" if would_mult > 1.05 else ("reduce" if would_mult < 0.95 else "neutral")
            detail = f"pattern{tag}: {label}, n={n}, EV {ev_hat:+.1%} → {would_mult:.2f}×"

        if record_shadow:
            self.shadow_stats["evaluated"] = int(self.shadow_stats.get("evaluated") or 0) + 1
            if would_action == "skip":
                self.shadow_stats["would_skip"] = int(self.shadow_stats.get("would_skip") or 0) + 1
            elif would_action == "reduce":
                self.shadow_stats["would_reduce"] = int(self.shadow_stats.get("would_reduce") or 0) + 1
            elif would_action == "boost":
                self.shadow_stats["would_boost"] = int(self.shadow_stats.get("would_boost") or 0) + 1

        if mode == "shadow":
            passed = True
            mult = 1.0
        else:
            passed = would_action != "skip"
            mult = would_mult if passed else 0.0

        return {
            "mode": mode,
            "passed": passed,
            "mult": mult,
            "would_mult": would_mult,
            "action": would_action if mode == "active" else f"shadow:{would_action}",
            "would_action": would_action,
            "level": level,
            "context": chosen["key"],
            "label": label,
            "n": n,
            "post_ev": ev_hat,
            "ev_ucb": ev_ucb,
            "post_wr": post_wr,
            "detail": detail,
        }

    def sync_from_orders(self, orders: list, tz_name: str = "America/New_York", cfg=None,
                         trade_patterns: Optional[dict[str, dict]] = None) -> int:
        """Backfill settled orders not yet in seen_ids (preserves chronological order)."""
        added = 0
        settled = [o for o in orders if (o.get("status") if isinstance(o, dict) else o.status) == "settled"]
        settled.sort(key=lambda o: str(o.get("window_end") if isinstance(o, dict) else o.window_end or ""))
        for o in settled:
            oid = str(o.get("id") if isinstance(o, dict) else o.id)
            if oid in self.seen_ids:
                continue
            pnl = _f(o.get("pnl_usd") if isinstance(o, dict) else o.pnl_usd)
            cost = _f(o.get("cost_usd") if isinstance(o, dict) else o.cost_usd)
            if pnl is None or cost is None:
                continue
            if trade_patterns and oid in trade_patterns:
                feat = trade_patterns[oid]
            else:
                feat = extract_features(o, tz_name=tz_name, cfg=cfg)
            outcome = str((o.get("result") if isinstance(o, dict) else o.result) or ("win" if pnl > 0 else "loss"))
            if self.update_trade(feat, order_id=oid, pnl_usd=pnl, cost_usd=cost, outcome=outcome):
                added += 1
        return added

    def summary(self, assets=("btc", "eth", "sol", "xrp"), horizons=("5m", "15m", "1h"),
                min_samples: int = 20, skip_ev_threshold: float = -0.02) -> dict[str, Any]:
        w0, mu0, _ = self._global_priors()
        g = self.global_stat
        global_view = {
            "n": g.n, "wins": g.wins, "losses": g.n - g.wins,
            "win_rate": round(g.wins / g.n, 4) if g.n else None,
            "post_wr": round(w0, 4),
            "post_ev": round(mu0, 4),
            "pnl_usd": round(g.sum_pnl, 2),
            "cost_usd": round(g.sum_cost, 2),
        }

        def _table(dim: str) -> list[dict]:
            rows = []
            for k in sorted(self.dims.get(dim, {})):
                p = self.posterior(dim, k)
                if dim == "context":
                    parts = k.split("|")
                    if len(parts) == 3:
                        p, _ = self.context_posterior(parts[0], parts[1], parts[2])
                        p["label"] = f"{parts[0].upper()} {parts[1]} {hour_bucket_label(parts[2])}"
                elif dim == "asset_horizon":
                    parts = k.split("|")
                    if len(parts) == 2:
                        p["label"] = f"{parts[0].upper()} {parts[1]}"
                else:
                    p["label"] = hour_bucket_label(k) if dim == "hour_bucket" else k
                p["skip"] = bool(p["n"] >= min_samples and p["post_ev"] <= skip_ev_threshold and p["ev_ucb"] < 0.0)
                rows.append(p)
            rows.sort(key=lambda r: (r["n"], r["post_ev"]), reverse=True)
            return rows

        # 24h x (asset x horizon) heatmap
        heatmap_rows = []
        for a in assets:
            for h in horizons:
                ah = f"{a}|{h}"
                parent = self.posterior("asset_horizon", ah)
                cells = []
                for hr in range(24):
                    hb = hour_bucket_for(hr)
                    ctx, _ = self.context_posterior(a, h, hb)
                    raw_cell = self.dims.get("hour_cell", {}).get(f"{a}|{h}|{hr:02d}")
                    cell_post = self.posterior(
                        "hour_cell", f"{a}|{h}|{hr:02d}",
                        parent_wr=ctx["post_wr"], parent_ev=ctx["post_ev"],
                    )
                    cells.append({
                        "hour": hr,
                        "n": raw_cell.n if raw_cell else 0,
                        "wins": raw_cell.wins if raw_cell else 0,
                        "pnl": round(raw_cell.sum_pnl, 2) if raw_cell else 0.0,
                        "post_wr": cell_post["post_wr"],
                        "post_ev": cell_post["post_ev"],
                        "ctx_n": ctx["n"],
                        "ctx_ev": ctx["post_ev"],
                    })
                heatmap_rows.append({
                    "asset": a, "horizon": h, "label": f"{a.upper()} {h}",
                    "n": parent["n"], "post_wr": parent["post_wr"], "post_ev": parent["post_ev"],
                    "pnl": parent["pnl_usd"], "cells": cells,
                })

        return {
            "updated_at": self.updated_at,
            "prior_strength": self.prior_strength,
            "global": global_view,
            "heatmap": heatmap_rows,
            "tables": {
                "context": _table("context"),
                "asset_horizon": _table("asset_horizon"),
                "hour_bucket": _table("hour_bucket"),
                "vol_regime": _table("vol_regime"),
                "distance_bucket": _table("distance_bucket"),
                "weekday": _table("weekday"),
                "strategy": _table("strategy"),
                "edge_bucket": _table("edge_bucket"),
                "minutes_bucket": _table("minutes_bucket"),
            },
            "cf": {
                "n": round(self.cf_global.n, 2),
                "wins": round(self.cf_global.wins, 2),
                "pnl_usd": round(self.cf_global.sum_pnl, 2),
                "cost_usd": round(self.cf_global.sum_cost, 2),
                "cursor_ts": self.cf_cursor_ts,
            },
            "shadow_stats": dict(self.shadow_stats),
            "recent_trades": list(reversed(self.recent_trades[-50:])),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 2,
            "updated_at": self.updated_at,
            "prior_strength": self.prior_strength,
            "global": self.global_stat.to_dict(),
            "dims": {dim: {k: v.to_dict() for k, v in mp.items()} for dim, mp in self.dims.items()},
            "seen_ids": sorted(self.seen_ids)[-5000:],
            "recent_trades": self.recent_trades[-120:],
            "shadow_stats": self.shadow_stats,
            "cf_global": self.cf_global.to_dict(),
            "cf_dims": {dim: {k: v.to_dict() for k, v in mp.items()} for dim, mp in self.cf_dims.items()},
            "cf_seen": sorted(self.cf_seen)[-20000:],
            "cf_cursor_ts": self.cf_cursor_ts,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any], prior_strength: float = 10.0) -> "PatternStore":
        ps = cls(prior_strength=float(raw.get("prior_strength") or prior_strength))
        ps.updated_at = float(raw.get("updated_at") or 0.0)
        if isinstance(raw.get("global"), dict):
            ps.global_stat = BucketStat.from_dict(raw["global"])
        for dim, mp in (raw.get("dims") or {}).items():
            if dim in ps.dims and isinstance(mp, dict):
                ps.dims[dim] = {k: BucketStat.from_dict(v) for k, v in mp.items() if isinstance(v, dict)}
        ps.seen_ids = set(raw.get("seen_ids") or [])
        ps.recent_trades = list(raw.get("recent_trades") or [])[-120:]
        if isinstance(raw.get("shadow_stats"), dict):
            ps.shadow_stats.update(raw["shadow_stats"])
        if isinstance(raw.get("cf_global"), dict):
            ps.cf_global = BucketStat.from_dict(raw["cf_global"])
        for dim, mp in (raw.get("cf_dims") or {}).items():
            if isinstance(mp, dict):
                ps.cf_dims.setdefault(dim, {})
                ps.cf_dims[dim] = {k: BucketStat.from_dict(v) for k, v in mp.items() if isinstance(v, dict)}
        ps.cf_seen = set(raw.get("cf_seen") or [])
        ps.cf_cursor_ts = float(raw.get("cf_cursor_ts") or 0.0)
        return ps


def store_path(data_dir: Path) -> Path:
    d = Path(data_dir) / "patterns"
    d.mkdir(parents=True, exist_ok=True)
    return d / "store.json"


def evals_path(data_dir: Path) -> Path:
    d = Path(data_dir) / "patterns"
    d.mkdir(parents=True, exist_ok=True)
    return d / "evaluations.jsonl"


def load_pattern_store(data_dir: Path, prior_strength: float = 10.0) -> PatternStore:
    p = store_path(data_dir)
    try:
        raw = json.loads(p.read_text())
        if isinstance(raw, dict):
            return PatternStore.from_dict(raw, prior_strength=prior_strength)
    except Exception:
        pass
    return PatternStore(prior_strength=prior_strength)


def save_pattern_store(data_dir: Path, store: PatternStore) -> None:
    p = store_path(data_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(store.to_dict(), separators=(",", ":")))
    os.replace(tmp, p)


def append_pattern_eval(data_dir: Path, feat: dict[str, Any], decision: dict[str, Any]) -> None:
    p = evals_path(data_dir)
    # Keep file bounded (< 25 MB) by rotating .1 when large
    try:
        if p.exists() and p.stat().st_size > 25 * 1024 * 1024:
            os.replace(p, p.with_suffix(".jsonl.1"))
    except OSError:
        pass
    rec = {**feat, "pattern_mode": decision.get("mode"), "pattern_action": decision.get("would_action"),
           "pattern_mult": decision.get("would_mult"), "pattern_context": decision.get("context")}
    line = (json.dumps(rec, separators=(",", ":"), default=str) + "\n").encode()
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


class PatternWatcher:
    """Cheap mtime-polled view of data/patterns/store.json for engine/dashboard."""

    def __init__(self, data_dir: Path, prior_strength: float = 10.0):
        self.data_dir = Path(data_dir)
        self.prior_strength = prior_strength
        self.path = store_path(self.data_dir)
        self._mtime: Optional[int] = None
        self.store = load_pattern_store(self.data_dir, prior_strength=prior_strength)

    def poll(self) -> PatternStore:
        try:
            m = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            m = None
        if m != self._mtime:
            self._mtime = m
            self.store = load_pattern_store(self.data_dir, prior_strength=self.prior_strength)
        return self.store
