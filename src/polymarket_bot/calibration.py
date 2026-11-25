"""Walk-forward counterfactual outcomes and probability calibration.

Uses stored evaluation rows with known end-of-window prints (spot vs price_to_beat).
Outcomes are applied only after the market window has ended (zero lookahead).
Counterfactual P&L is computed net of official taker fees at the recorded ask
for a $1 stake (or a given stake). Recalibration (isotonic / Platt) is fitted
per asset|horizon and is shadow-first: active mode is the only time it rewrites
model_prob for live edge.
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

from .analytics import DEFAULT_TZ, ts_unix
from .fees import fee_per_contract, taker_fee_usd
from .patterns import _f, extract_features

CF_PREFIX = "cf:"


def unit_pnl(ask: float, won: bool, theta: float = 0.095, stake: float = 1.0) -> tuple[float, float, float]:
    """P&L, cost, and fee for `stake` dollars of notional at the ask, net of fees."""
    if ask is None or ask <= 0 or ask >= 1:
        return 0.0, 0.0, 0.0
    contracts = float(stake) / float(ask)
    fee = taker_fee_usd(theta, contracts, ask)
    cost = contracts * float(ask) + fee
    payout = (contracts * 1.0) if won else 0.0
    return round(payout - cost, 6), round(cost, 6), round(fee, 6)


def outcome_from_print(spot: Optional[float], strike: Optional[float]) -> Optional[str]:
    if spot is None or strike is None:
        return None
    return "up" if float(spot) >= float(strike) else "down"


def index_outcomes(rows: Iterable[dict], tz_name: str = DEFAULT_TZ, sr_cutoff: float = 30.0) -> tuple[dict[str, str], dict[str, float]]:
    """Best end-of-window print per market_slug, plus settle timestamp (ts + seconds_remaining)."""
    outcome: dict[str, str] = {}
    settle_ts: dict[str, float] = {}
    best_sr: dict[str, float] = {}
    for r in rows:
        slug = str(r.get("market_slug") or "")
        if not slug:
            continue
        t = ts_unix(r.get("ts"), tz_name)
        sr = _f(r.get("seconds_remaining"))
        if t is not None and sr is not None:
            settle_ts[slug] = max(settle_ts.get(slug, 0.0), t + sr)
        spot, ptb = _f(r.get("spot")), _f(r.get("price_to_beat"))
        if sr is not None and sr <= sr_cutoff and spot is not None and ptb is not None:
            prev = best_sr.get(slug, 1e9)
            if sr <= prev:
                best_sr[slug] = sr
                outcome[slug] = outcome_from_print(spot, ptb) or outcome.get(slug, "down")
    return outcome, settle_ts


def iter_counterfactuals(
    rows: Iterable[dict],
    outcome: dict[str, str],
    settle_ts: dict[str, float],
    *,
    tz_name: str = DEFAULT_TZ,
    theta: float = 0.095,
    stake: float = 1.0,
    include_no_trade: bool = True,
) -> list[dict[str, Any]]:
    """Walk-forward: an evaluation becomes a CF example only once its window has ended.

    Rows must be time-sorted. A row at time t whose settle_ts > t is held until later
    (the caller can also just filter settle_ts <= now at the end). We emit an example
    per (slug, side, ts) so NO TRADE rows are included when include_no_trade is True.
    """
    rows = list(rows)
    out: list[dict[str, Any]] = []
    for r in rows:
        slug = str(r.get("market_slug") or "")
        side = str(r.get("side") or "").lower()
        if slug not in outcome or side not in ("up", "down"):
            continue
        t = ts_unix(r.get("ts"), tz_name)
        s_ts = settle_ts.get(slug)
        if t is None or s_ts is None:
            continue
        # Zero lookahead: the example is only valid as of settle time.
        ask = _f(r.get("ask"))
        if not ask or ask <= 0 or ask >= 1:
            continue
        passed = str(r.get("passed") or "").strip() in ("1", "true", "True")
        if not include_no_trade and not passed:
            continue
        won = outcome[slug] == side
        pnl, cost, fee = unit_pnl(ask, won, theta=theta, stake=stake)
        feat = extract_features(r, ts=r.get("ts"), tz_name=tz_name)
        out.append({
            "id": f"{CF_PREFIX}{slug}:{side}:{t}",
            "ts": t,
            "settle_ts": s_ts,
            "market_slug": slug,
            "side": side,
            "passed": passed,
            "won": won,
            "pnl_usd": pnl,
            "cost_usd": cost,
            "fee_usd": fee,
            "model_prob": _f(r.get("model_prob")),
            "ask": ask,
            "edge": _f(r.get("edge_after_fees")),
            "asset": feat.get("asset") or r.get("asset"),
            "horizon": feat.get("horizon") or r.get("horizon"),
            "feat": feat,
        })
    return out


def asof(examples: list[dict], now: float) -> list[dict]:
    """Keep only examples whose market has already settled by `now`."""
    return [e for e in examples if float(e["settle_ts"]) <= float(now) + 1e-9]


# ----- calibration metrics & maps -----

def _bucket(p: float, width: float = 0.1) -> str:
    lo = int(p / width) * width
    hi = min(1.0, lo + width)
    return f"{lo:.1f}-{hi:.1f}"


def reliability(examples: list[dict], width: float = 0.1) -> list[dict]:
    buckets: dict[str, dict] = {}
    for e in examples:
        p = e.get("model_prob")
        if p is None or not math.isfinite(float(p)):
            continue
        p = min(1.0, max(0.0, float(p)))
        k = _bucket(p, width)
        d = buckets.setdefault(k, {"n": 0, "wins": 0, "sum_p": 0.0, "sum_pnl": 0.0})
        d["n"] += 1
        d["wins"] += int(bool(e.get("won")))
        d["sum_p"] += p
        d["sum_pnl"] += float(e.get("pnl_usd") or 0.0)
    out = []
    for k in sorted(buckets, key=lambda s: float(s.split("-")[0])):
        d = buckets[k]
        freq = d["wins"] / d["n"] if d["n"] else None
        mean_p = d["sum_p"] / d["n"] if d["n"] else None
        out.append({
            "bucket": k, "n": d["n"], "wins": d["wins"],
            "mean_p": None if mean_p is None else round(mean_p, 4),
            "realized": None if freq is None else round(freq, 4),
            "gap": None if freq is None or mean_p is None else round(mean_p - freq, 4),
            "pnl": round(d["sum_pnl"], 2),
        })
    return out


def brier_logloss(examples: list[dict]) -> dict[str, Any]:
    n = 0
    brier = 0.0
    logloss = 0.0
    for e in examples:
        p = e.get("model_prob")
        if p is None or not math.isfinite(float(p)):
            continue
        p = min(1.0 - 1e-12, max(1e-12, float(p)))
        y = 1.0 if e.get("won") else 0.0
        brier += (p - y) ** 2
        logloss -= y * math.log(p) + (1.0 - y) * math.log(1.0 - p)
        n += 1
    return {
        "n": n,
        "brier": None if not n else round(brier / n, 6),
        "log_loss": None if not n else round(logloss / n, 6),
    }


def group_key(e: dict) -> str:
    return f"{str(e.get('asset') or '').lower()}|{e.get('horizon') or ''}"


def report(examples: list[dict]) -> dict[str, Any]:
    by_ah: dict[str, list] = defaultdict(list)
    for e in examples:
        by_ah[group_key(e)].append(e)
    groups = []
    for k in sorted(by_ah):
        xs = by_ah[k]
        asset, hz = (k.split("|") + [""])[:2]
        groups.append({
            "key": k, "asset": asset, "horizon": hz,
            **brier_logloss(xs),
            "buckets": reliability(xs),
            "pnl": round(sum(float(e.get("pnl_usd") or 0) for e in xs), 2),
        })
    return {
        "overall": {**brier_logloss(examples), "buckets": reliability(examples),
                    "pnl": round(sum(float(e.get("pnl_usd") or 0) for e in examples), 2),
                    "n_rows": len(examples)},
        "by_asset_horizon": groups,
    }


# ----- isotonic (PAV) and Platt -----

def _logit(p: float) -> float:
    p = min(1.0 - 1e-12, max(1e-12, p))
    return math.log(p / (1.0 - p))


def fit_isotonic(pairs: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """PAV on (p_raw, y) sorted by p_raw. Returns step points (p, calibrated)."""
    pts = sorted(((float(p), float(y)) for p, y in pairs), key=lambda x: x[0])
    if not pts:
        return []
    # blocks: [sum_y, n, p_lo, p_hi]
    blocks = [[y, 1, p, p] for p, y in pts]
    i = 0
    while i < len(blocks) - 1:
        mean_i = blocks[i][0] / blocks[i][1]
        mean_n = blocks[i + 1][0] / blocks[i + 1][1]
        if mean_i > mean_n + 1e-15:
            blocks[i][0] += blocks[i + 1][0]
            blocks[i][1] += blocks[i + 1][1]
            blocks[i][3] = blocks[i + 1][3]
            del blocks[i + 1]
            if i:
                i -= 1
        else:
            i += 1
    out = []
    for s, n, lo, hi in blocks:
        m = s / n
        out.append((lo, m))
        if hi != lo:
            out.append((hi, m))
    return out


def apply_isotonic(p: float, steps: list[tuple[float, float]]) -> float:
    if not steps:
        return p
    if p <= steps[0][0]:
        return steps[0][1]
    for i in range(1, len(steps)):
        if p <= steps[i][0]:
            x0, y0 = steps[i - 1]
            x1, y1 = steps[i]
            if x1 <= x0:
                return y1
            t = (p - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return steps[-1][1]


def fit_platt(pairs: list[tuple[float, float]], iters: int = 80) -> tuple[float, float]:
    """P' = sigmoid(a * logit(p) + b). Newton on regularized log-loss."""
    xs = [(_logit(p), 1.0 if y >= 0.5 else 0.0) for p, y in pairs]
    if len(xs) < 8:
        return 1.0, 0.0
    a, b = 1.0, 0.0
    for _ in range(iters):
        g_a = g_b = h_aa = h_ab = h_bb = 0.0
        for x, y in xs:
            z = a * x + b
            z = max(-30.0, min(30.0, z))
            q = 1.0 / (1.0 + math.exp(-z))
            g = q - y
            w = max(q * (1.0 - q), 1e-9)
            g_a += g * x
            g_b += g
            h_aa += w * x * x
            h_ab += w * x
            h_bb += w
        # mild ridge
        h_aa += 1e-4
        h_bb += 1e-4
        det = h_aa * h_bb - h_ab * h_ab
        if abs(det) < 1e-12:
            break
        da = (h_bb * g_a - h_ab * g_b) / det
        db = (-h_ab * g_a + h_aa * g_b) / det
        a -= da
        b -= db
        if abs(da) + abs(db) < 1e-8:
            break
    return a, b


def apply_platt(p: float, ab: tuple[float, float]) -> float:
    a, b = ab
    z = a * _logit(p) + b
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


class Calibrator:
    """Per asset|horizon maps, plus a global fallback. Shadow-safe: apply() is pure."""

    def __init__(self, method: str = "isotonic"):
        self.method = "platt" if method == "platt" else "isotonic"
        self.maps: dict[str, Any] = {}  # key -> steps or (a,b)
        self.global_map: Any = None
        self.fitted_n = 0

    def fit(self, examples: list[dict]) -> "Calibrator":
        by: dict[str, list] = defaultdict(list)
        all_pairs: list[tuple[float, float]] = []
        for e in examples:
            p = e.get("model_prob")
            if p is None or not math.isfinite(float(p)):
                continue
            pair = (min(1.0, max(0.0, float(p))), 1.0 if e.get("won") else 0.0)
            by[group_key(e)].append(pair)
            all_pairs.append(pair)
        self.fitted_n = len(all_pairs)
        self.maps = {}
        for k, pairs in by.items():
            if len(pairs) < 12:
                continue
            self.maps[k] = fit_platt(pairs) if self.method == "platt" else fit_isotonic(pairs)
        self.global_map = (fit_platt(all_pairs) if self.method == "platt" else fit_isotonic(all_pairs)) if all_pairs else None
        return self

    def apply(self, p: Optional[float], asset: str = "", horizon: str = "") -> Optional[float]:
        if p is None or not math.isfinite(float(p)):
            return p
        p0 = min(1.0, max(0.0, float(p)))
        key = f"{asset.lower()}|{horizon}"
        mp = self.maps.get(key, self.global_map)
        if mp is None:
            return p0
        if self.method == "platt":
            return round(apply_platt(p0, mp), 6)
        return round(apply_isotonic(p0, mp), 6)

    def to_dict(self) -> dict:
        return {"method": self.method, "fitted_n": self.fitted_n, "maps": self.maps, "global_map": self.global_map}

    @classmethod
    def from_dict(cls, raw: dict) -> "Calibrator":
        c = cls(method=str(raw.get("method") or "isotonic"))
        c.fitted_n = int(raw.get("fitted_n") or 0)
        c.maps = dict(raw.get("maps") or {})
        c.global_map = raw.get("global_map")
        return c


def calibrator_path(data_dir: Path) -> Path:
    d = Path(data_dir) / "patterns"
    d.mkdir(parents=True, exist_ok=True)
    return d / "calibration.json"


def save_report(data_dir: Path, doc: dict) -> None:
    p = calibrator_path(data_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, separators=(",", ":"), default=str))
    os.replace(tmp, p)


def load_report(data_dir: Path) -> Optional[dict]:
    p = calibrator_path(data_dir)
    try:
        return json.loads(p.read_text())
    except Exception:
        return None
