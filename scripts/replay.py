#!/usr/bin/env python3
"""Replay/backtest strategies over recorded evaluations (approximate, offline).

    PYTHONPATH=src .venv/bin/python scripts/replay.py [--csv data/evaluations.csv ...] [--stake 10]

Input: evaluations.csv files (live, or extracted from archive parts). Each row is
one evaluation of one market side with model_prob, ask, edge, spread, spot,
price_to_beat, seconds_remaining and the failing rule reasons.

Method (deliberately simple and stated):
  * Data-quality pass = every failing reason is the edge rule (so strike, timing,
    spread, liquidity and ask band all passed at that moment).
  * Entry = the first row per (market, side) the strategy accepts, at that ask,
    fixed stake (default $10), fees ignored except as already in edge.
  * Outcome ≈ the last recorded row of that market with ≤ 30s left: UP wins if
    spot ≥ price_to_beat. The real settlement uses a 60s average, so close
    calls can differ. Markets without such a row are skipped.
It answers "which filter would have done better on this tape", not exact P&L.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from polymarket_bot import strategies as strat  # noqa: E402


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load(paths):
    rows = []
    for p in paths:
        with open(p, newline="") as fh:
            rows += list(csv.DictReader(fh))
    rows.sort(key=lambda r: r.get("ts") or "")
    return rows


def to_sig(r, cfg_min_edge):
    reasons = [x.strip() for x in (r.get("reasons") or "").split(" | ") if x.strip()]
    dq_ok = all(x.startswith("min_edge_after_fees") for x in reasons) and f(r.get("model_prob")) is not None and f(r.get("ask"))
    edge = f(r.get("edge_after_fees"))
    checks = [{"name": "data_quality", "passed": bool(dq_ok)},
              {"name": "min_edge_after_fees", "passed": edge is not None and edge >= cfg_min_edge, "value": edge}]
    return {"edge_after_fees": edge, "model_prob": f(r.get("model_prob")), "ask": f(r.get("ask")),
            "seconds_remaining": f(r.get("seconds_remaining")), "horizon": r.get("horizon"), "rule_checks": checks}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", nargs="*", default=["data/evaluations.csv"])
    ap.add_argument("--stake", type=float, default=10.0)
    ap.add_argument("--min-edge", type=float, default=0.04, help="configured rules-engine min edge (baseline)")
    a = ap.parse_args(argv)
    rows = load(a.csv)
    outcome = {}
    for r in rows:
        sr, spot, ptb = f(r.get("seconds_remaining")), f(r.get("spot")), f(r.get("price_to_beat"))
        if sr is not None and sr <= 30 and spot is not None and ptb is not None:
            outcome[r["market_slug"]] = "up" if spot >= ptb else "down"
    print(f"{len(rows)} evaluation rows, {len(outcome)} markets with an end-of-window print\n")
    print(f"{'strategy':16} {'trades':>6} {'wins':>5} {'win%':>6} {'pnl':>9} {'avg':>7}")
    for name in strat.names():
        taken = set()
        pnl, n, w = 0.0, 0, 0
        per = defaultdict(float)
        for r in rows:
            key = (r["market_slug"], r["side"])
            if key in taken or r["market_slug"] not in outcome:
                continue
            ok, _ = strat.evaluate(name, None, to_sig(r, a.min_edge), a.min_edge)
            if not ok:
                continue
            taken.add(key)
            ask = f(r["ask"])
            won = outcome[r["market_slug"]] == r["side"]
            p = a.stake * (1 / ask - 1) if won else -a.stake
            pnl += p; n += 1; w += won
            per[r.get("horizon")] += p
        print(f"{name:16} {n:6d} {w:5d} {(w / n * 100 if n else 0):5.1f}% {pnl:+9.2f} {(pnl / n if n else 0):+7.2f}   "
              + " ".join(f"{h}:{v:+.0f}" for h, v in sorted(per.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
