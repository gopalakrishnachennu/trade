"""Deterministic loss/win attribution for settled paper trades.

Tags are derived from stored pattern features plus the index path reconstructed
from evaluation rows for the same market (zero new market data). This is an
observational layer: it never changes entries, sizing, or risk limits.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Any, Iterable, Optional

from .fees import fee_per_contract, taker_fee_usd
from .patterns import extract_features, _f

# Loss priority (first match is the primary reason).
LOSS_PRIORITY = (
    "last_minute_flip",
    "fee_eaten_thin_edge",
    "late_entry",
    "against_trend",
    "model_miscalibration",
    "high_vol",
    "low_depth_spread",
    "other_loss",
)

WIN_PRIORITY = (
    "strong_edge",
    "with_trend",
    "well_priced",
    "other_win",
)

LABELS = {
    "last_minute_flip": "last-minute flip",
    "fee_eaten_thin_edge": "fee-eaten thin edge",
    "late_entry": "late entry",
    "against_trend": "against trend/momentum",
    "model_miscalibration": "model miscalibration (P bucket)",
    "high_vol": "high-vol regime",
    "low_depth_spread": "low depth/spread",
    "other_loss": "other loss",
    "strong_edge": "strong edge",
    "with_trend": "with trend",
    "well_priced": "well priced",
    "other_win": "other win",
}

THIN_EDGE = 0.03
STRONG_EDGE = 0.08
LATE_MINUTES = 2.0
FLIP_SECONDS = 60.0
HIGH_P = 0.65
WIDE_SPREAD = 0.03
THIN_DEPTH = 500.0  # contracts


def _horizon_seconds(horizon: str) -> float:
    return {"5m": 300.0, "15m": 900.0, "1h": 3600.0}.get(str(horizon or ""), 900.0)


def implied_side(spot: Optional[float], strike: Optional[float]) -> Optional[str]:
    if spot is None or strike is None:
        return None
    return "up" if float(spot) >= float(strike) else "down"


def p_bucket(model_prob: Optional[float]) -> str:
    if model_prob is None or not math.isfinite(float(model_prob)):
        return "unknown"
    p = min(1.0, max(0.0, float(model_prob)))
    lo = int(p * 10) / 10.0
    hi = min(1.0, lo + 0.1)
    return f"{lo:.1f}-{hi:.1f}"


def build_index_paths(eval_rows: Iterable[dict]) -> dict[str, list[dict]]:
    """market_slug -> time-ordered [{ts, spot, strike, seconds_remaining, side_implied}]."""
    paths: dict[str, list[dict]] = defaultdict(list)
    for r in eval_rows or []:
        slug = str(r.get("market_slug") or "")
        if not slug:
            continue
        spot, strike = _f(r.get("spot")), _f(r.get("price_to_beat"))
        if spot is None or strike is None:
            continue
        ts = r.get("ts")
        try:
            from .analytics import ts_unix
            tsu = ts_unix(ts) or 0.0
        except Exception:
            tsu = 0.0
        paths[slug].append({
            "ts": tsu,
            "spot": spot,
            "strike": strike,
            "seconds_remaining": _f(r.get("seconds_remaining")),
            "implied": implied_side(spot, strike),
        })
    for slug in paths:
        paths[slug].sort(key=lambda x: x["ts"])
    return paths


def _path_at(path: list[dict], t: float) -> Optional[dict]:
    if not path:
        return None
    chosen = path[0]
    for pt in path:
        if pt["ts"] <= t + 1e-9:
            chosen = pt
        else:
            break
    return chosen


def _late_flip(path: list[dict], entry_t: float, side: str, settle_side: Optional[str]) -> bool:
    if not path or not side:
        return False
    entry = _path_at(path, entry_t)
    last = path[-1]
    # A flip in the final FLIP_SECONDS against the side we held.
    late_pts = [p for p in path if (p.get("seconds_remaining") is not None and p["seconds_remaining"] <= FLIP_SECONDS)
                or (last["ts"] - p["ts"] <= FLIP_SECONDS)]
    if not late_pts:
        return False
    early_impl = entry["implied"] if entry else None
    late_impl = late_pts[-1]["implied"]
    final = settle_side or late_impl
    if early_impl is None or late_impl is None:
        return False
    return early_impl == side and late_impl != side and (final is None or final != side or late_impl != early_impl)


def _against_trend(path: list[dict], entry_t: float, side: str, spot: Optional[float], strike: Optional[float]) -> bool:
    entry = _path_at(path, entry_t) if path else None
    sp = (entry or {}).get("spot", spot)
    st = (entry or {}).get("strike", strike)
    impl = implied_side(sp, st)
    if impl and side and impl != side:
        return True
    if not path or entry is None:
        return False
    # Momentum over ~60s before entry: price moving away from our side.
    prior = [p for p in path if p["ts"] <= entry_t and entry_t - p["ts"] <= 90]
    if len(prior) < 2:
        return False
    delta = prior[-1]["spot"] - prior[0]["spot"]
    if side == "up" and delta < 0:
        return True
    if side == "down" and delta > 0:
        return True
    return False


def tag_trade(
    feat: dict[str, Any],
    *,
    outcome: str,
    pnl_usd: float,
    cost_usd: float,
    fee_usd: Optional[float] = None,
    settlement_price: Optional[float] = None,
    price_to_beat: Optional[float] = None,
    path: Optional[list[dict]] = None,
    theta: float = 0.095,
) -> dict[str, Any]:
    """Return {primary, tags, label, detail, p_bucket, fees_usd, ...}."""
    won = (outcome == "win") or (float(pnl_usd or 0) > 0)
    edge = _f(feat.get("edge") if feat.get("edge") is not None else feat.get("edge_after_fees"))
    ask = _f(feat.get("ask") or feat.get("price"))
    minutes = _f(feat.get("minutes_left"))
    sr = minutes * 60.0 if minutes is not None else None
    vol = str(feat.get("vol_regime") or "")
    spread = _f(feat.get("spread"))
    depth = _f(feat.get("depth_contracts") or feat.get("liquidity_contracts"))
    mp = _f(feat.get("model_prob"))
    side = str(feat.get("side") or "").lower()
    spot = _f(feat.get("spot"))
    strike = _f(feat.get("price_to_beat") if feat.get("price_to_beat") is not None else price_to_beat)
    entry_t = _f(feat.get("ts_unix")) or 0.0
    settle_side = implied_side(_f(settlement_price), strike) if settlement_price is not None else None

    if fee_usd is None and ask is not None and cost_usd:
        # Approximate contracts from cost ≈ C*ask + fee; fall back to cost/ask.
        contracts = float(cost_usd) / ask if ask else 0.0
        fee_usd = taker_fee_usd(theta, contracts, ask) if contracts and ask else 0.0
    fee_usd = float(fee_usd or 0.0)
    fee_pc = fee_per_contract(theta, ask) if ask else None

    tags: list[str] = []
    notes: list[str] = []

    if not won:
        if _late_flip(path or [], entry_t, side, settle_side):
            tags.append("last_minute_flip")
            notes.append("index flipped against the position in the last minute")
        thin = (edge is not None and edge < THIN_EDGE) or (
            fee_pc is not None and edge is not None and edge < 2.0 * fee_pc
        )
        if thin:
            tags.append("fee_eaten_thin_edge")
            notes.append(f"edge {edge:+.2%} was thin vs fees" if edge is not None else "thin edge")
        hz_s = _horizon_seconds(str(feat.get("horizon") or ""))
        late = (minutes is not None and minutes <= LATE_MINUTES) or (
            sr is not None and sr <= max(90.0, 0.20 * hz_s)
        )
        if late:
            tags.append("late_entry")
            notes.append(f"entered with {minutes:.1f}m left" if minutes is not None else "late entry")
        if _against_trend(path or [], entry_t, side, spot, strike):
            tags.append("against_trend")
            notes.append("entry was against the index vs strike / recent momentum")
        if mp is not None and mp >= HIGH_P and not won:
            tags.append("model_miscalibration")
            notes.append(f"model P {mp:.2f} in bucket {p_bucket(mp)} missed")
        if vol == "high":
            tags.append("high_vol")
            notes.append("high-vol regime")
        if (spread is not None and spread >= WIDE_SPREAD) or (depth is not None and depth < THIN_DEPTH):
            tags.append("low_depth_spread")
            notes.append(f"spread {spread} depth {depth}" )
        if not tags:
            tags.append("other_loss")
        primary = next(x for x in LOSS_PRIORITY if x in tags)
    else:
        if edge is not None and edge >= STRONG_EDGE:
            tags.append("strong_edge")
            notes.append(f"edge {edge:+.2%}")
        if not _against_trend(path or [], entry_t, side, spot, strike) and implied_side(spot, strike) in (None, side):
            tags.append("with_trend")
            notes.append("with the index vs strike")
        well = True
        if mp is not None and mp < 0.55:
            well = False
        if spread is not None and spread >= WIDE_SPREAD:
            well = False
        if well and (edge is None or edge >= THIN_EDGE):
            tags.append("well_priced")
            notes.append("ample book / mid-to-high model P")
        if not tags:
            tags.append("other_win")
        primary = next(x for x in WIN_PRIORITY if x in tags)

    return {
        "primary": primary,
        "label": LABELS.get(primary, primary),
        "tags": tags,
        "tag_labels": [LABELS.get(t, t) for t in tags],
        "detail": "; ".join(notes) if notes else LABELS.get(primary, primary),
        "p_bucket": p_bucket(mp),
        "fees_usd": round(fee_usd, 4),
        "pnl_usd": round(float(pnl_usd or 0.0), 4),
        "cost_usd": round(float(cost_usd or 0.0), 4),
        "outcome": "win" if won else "loss",
        "edge": None if edge is None else round(edge, 6),
        "model_prob": mp,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts, P&L, and fees on losers, by primary reason and by tag."""
    by_primary: dict[str, dict] = {}
    by_tag: dict[str, dict] = {}

    def acc(store, key, row):
        d = store.setdefault(key, {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0, "fees": 0.0, "fees_on_losers": 0.0, "staked": 0.0})
        d["n"] += 1
        pnl = float(row.get("pnl_usd") or 0.0)
        fee = float(row.get("fees_usd") or 0.0)
        cost = float(row.get("cost_usd") or 0.0)
        d["pnl"] += pnl
        d["fees"] += fee
        d["staked"] += cost
        if row.get("outcome") == "win" or pnl > 0:
            d["wins"] += 1
        else:
            d["losses"] += 1
            d["fees_on_losers"] += fee

    for r in rows:
        acc(by_primary, r.get("primary") or "other_loss", r)
        for t in r.get("tags") or [r.get("primary")]:
            acc(by_tag, t, r)

    def _fin(store, order):
        out = []
        keys = [k for k in order if k in store] + [k for k in store if k not in order]
        for k in keys:
            d = store[k]
            out.append({
                "reason": k,
                "label": LABELS.get(k, k),
                "n": d["n"],
                "wins": d["wins"],
                "losses": d["losses"],
                "pnl": round(d["pnl"], 2),
                "fees": round(d["fees"], 2),
                "fees_on_losers": round(d["fees_on_losers"], 2),
                "staked": round(d["staked"], 2),
            })
        return out

    losses = [r for r in rows if r.get("outcome") != "win" and float(r.get("pnl_usd") or 0) <= 0]
    wins = [r for r in rows if r not in losses]
    return {
        "n": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "pnl": round(sum(float(r.get("pnl_usd") or 0) for r in rows), 2),
        "fees": round(sum(float(r.get("fees_usd") or 0) for r in rows), 2),
        "fees_on_losers": round(sum(float(r.get("fees_usd") or 0) for r in losses), 2),
        "by_primary": _fin(by_primary, LOSS_PRIORITY + WIN_PRIORITY),
        "by_tag": _fin(by_tag, LOSS_PRIORITY + WIN_PRIORITY),
        "rows": rows,
    }


def features_from_order(order, trade_pattern: Optional[dict] = None, tz_name: str = "America/New_York", cfg=None) -> dict:
    if trade_pattern:
        feat = dict(trade_pattern)
        if "edge" not in feat or feat.get("edge") is None:
            feat["edge"] = trade_pattern.get("edge") or getattr(order, "edge_after_fees", None)
        return feat
    return extract_features(order, tz_name=tz_name, cfg=cfg)
