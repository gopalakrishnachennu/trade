"""Pluggable entry strategies (paper only).

A strategy never replaces the data-quality rules. Every strategy requires all
non-edge rule checks from the rules engine to pass (strike stamped, timing
window, spread, liquidity, ask band). It may then set its own edge threshold
(never below HARD_MIN_EDGE) and add filters. Parameters are clamped to
PARAM_BOUNDS wherever they come from (YAML, operator, LLM reviewer).

The active strategy is stored in data/strategies/active.json. The engine
applies it to the signals it publishes; execution re-applies it before every
fill and skips the fill if the two disagree (fail closed).
"""

from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

HARD_MIN_EDGE = 0.02
HORIZON_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}

PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "min_edge": (HARD_MIN_EDGE, 0.25),
    "min_prob": (0.0, 0.95),
    "min_ask": (0.0, 0.95),
    "max_ask": (0.05, 1.0),
    "max_frac_left": (0.05, 1.0),
}

# min_edge None = use the configured rules-engine threshold (baseline).
STRATEGIES: dict[str, dict[str, Any]] = {
    "baseline": {
        "description": "The original rules engine: all rule checks, configured min edge.",
        "params": {"min_edge": None, "min_prob": 0.0, "min_ask": 0.0, "max_ask": 1.0, "max_frac_left": 1.0},
    },
    "high_conviction": {
        "description": "Fewer, stronger bets: edge ≥ 8% and model probability ≥ 60%.",
        "params": {"min_edge": 0.08, "min_prob": 0.60, "min_ask": 0.0, "max_ask": 1.0, "max_frac_left": 1.0},
    },
    "late_window": {
        "description": "Only the last 35% of a window, when the strike/spot gap carries most information; edge ≥ 3%.",
        "params": {"min_edge": 0.03, "min_prob": 0.0, "min_ask": 0.0, "max_ask": 1.0, "max_frac_left": 0.35},
    },
    "favorite": {
        "description": "Back the likely side only: model probability ≥ 65%, ask ≥ 55¢, edge ≥ 3%.",
        "params": {"min_edge": 0.03, "min_prob": 0.65, "min_ask": 0.55, "max_ask": 1.0, "max_frac_left": 1.0},
    },
}


def names() -> list[str]:
    return list(STRATEGIES)


def clamp_params(params: dict[str, Any], base: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Merge params onto base and clamp every known key. Unknown keys are dropped."""
    out = deepcopy(base or {})
    for k, v in (params or {}).items():
        if k not in PARAM_BOUNDS:
            continue
        if v is None:
            out[k] = None
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv != fv:  # NaN
            continue
        lo, hi = PARAM_BOUNDS[k]
        out[k] = round(min(hi, max(lo, fv)), 4)
    return out


def default_params(name: str) -> dict[str, Any]:
    return clamp_params(STRATEGIES[name]["params"], STRATEGIES[name]["params"])


def _num(x) -> Optional[float]:
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def evaluate(name: str, params: Optional[dict], sig: dict, cfg_min_edge: float) -> tuple[bool, list[dict]]:
    """Apply strategy `name` to one engine signal dict.

    Returns (passed, rule_checks). The base checks are kept; the edge check is
    re-evaluated at the strategy's threshold and strategy filters are added.
    """
    if name not in STRATEGIES:
        return False, list(sig.get("rule_checks") or []) + [
            {"name": "strategy", "passed": False, "detail": f"unknown strategy {name!r}"}]
    p = clamp_params(params or {}, default_params(name))
    base = list(sig.get("rule_checks") or [])
    if not base:
        return False, base
    min_edge = p.get("min_edge")
    min_edge = max(HARD_MIN_EDGE, float(cfg_min_edge if min_edge is None else min_edge))
    edge = _num(sig.get("edge_after_fees"))
    checks: list[dict] = []
    for c in base:
        if c.get("name") == "min_edge_after_fees":
            c = dict(c, threshold=min_edge, passed=edge is not None and edge >= min_edge)
        checks.append(c)
    extra = []
    prob, ask = _num(sig.get("model_prob")), _num(sig.get("ask"))
    if p.get("min_prob", 0) > 0:
        ok = prob is not None and prob >= p["min_prob"]
        extra.append({"name": "strategy", "passed": ok,
                      "detail": None if ok else f"{name}: model P {0 if prob is None else prob:.0%} < {p['min_prob']:.0%}"})
    if p.get("min_ask", 0) > 0 or p.get("max_ask", 1) < 1:
        ok = ask is not None and p.get("min_ask", 0) <= ask <= p.get("max_ask", 1)
        extra.append({"name": "strategy", "passed": ok,
                      "detail": None if ok else f"{name}: ask {0 if ask is None else ask*100:.0f}¢ outside {p.get('min_ask',0)*100:.0f}–{p.get('max_ask',1)*100:.0f}¢"})
    if p.get("max_frac_left", 1) < 1:
        total = HORIZON_SECONDS.get(str(sig.get("horizon")), 0)
        left = _num(sig.get("seconds_remaining"))
        ok = bool(total) and left is not None and left <= p["max_frac_left"] * total
        extra.append({"name": "strategy", "passed": ok,
                      "detail": None if ok else f"{name}: waits for last {p['max_frac_left']:.0%} of window"})
    checks.extend(extra)
    return all(bool(c.get("passed")) for c in checks), checks


# ----------------------------------------------------------- active selection

def strategies_dir(data_dir: Path) -> Path:
    d = Path(data_dir) / "strategies"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_active(data_dir: Path) -> dict[str, Any]:
    """Active strategy record. Missing/corrupt file → baseline (the original behaviour)."""
    path = strategies_dir(data_dir) / "active.json"
    try:
        rec = json.loads(path.read_text())
        name = rec.get("active")
        if name not in STRATEGIES:
            raise ValueError(name)
        rec["params"] = clamp_params(rec.get("params") or {}, default_params(name))
        return rec
    except Exception:
        return {"active": "baseline", "params": default_params("baseline"), "version": 0,
                "changed_at": None, "reason": "default"}


def write_active(data_dir: Path, name: str, params: Optional[dict], reason: str, actor: str) -> dict[str, Any]:
    if name not in STRATEGIES:
        raise ValueError(f"unknown strategy {name!r}")
    cur = read_active(data_dir)
    rec = {
        "active": name,
        "params": clamp_params(params or {}, default_params(name)),
        "version": int(cur.get("version") or 0) + 1,
        "changed_at": time.time(),
        "previous": cur.get("active"),
        "reason": reason,
        "actor": actor,
    }
    path = strategies_dir(data_dir) / "active.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=2))
    os.replace(tmp, path)
    return rec


class ActiveWatcher:
    """Cheap mtime-polled view of active.json for the engine/execution loops."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.path = strategies_dir(self.data_dir) / "active.json"
        self._mtime = None
        self.rec = read_active(self.data_dir)

    def poll(self) -> dict[str, Any]:
        try:
            m = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            m = None
        if m != self._mtime:
            self._mtime = m
            self.rec = read_active(self.data_dir)
        return self.rec
