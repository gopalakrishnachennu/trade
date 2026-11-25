#!/usr/bin/env python3
"""Win rate and net EV by entry-time feature buckets (walk-forward, fee-aware).

Populations:
  first-passed  — first would-trade snapshot per slug+side (same as rules_lab.backtest_rule)
  all-passed    — every would-trade snapshot
  fills         — settled paper fills, features as-of the fill timestamp

  PYTHONPATH=src .venv/bin/python scripts/entry_feature_report.py --all --stake 10
  PYTHONPATH=src .venv/bin/python scripts/entry_feature_report.py --all --probe-rule skip \
      --when 'mom_1m=lt:-0.5'
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from polymarket_bot import calibration as cal  # noqa: E402
from polymarket_bot import rules_lab as rl  # noqa: E402
from polymarket_bot.analytics import ts_unix  # noqa: E402
from polymarket_bot.attribution import p_bucket  # noqa: E402
from polymarket_bot.patterns import (  # noqa: E402
    crosses_bucket_for,
    entry_features,
    extract_features,
    horizon_seconds,
    minutes_bucket_for,
    mom_bucket_for,
    paths_from_eval_rows,
)


def load_eval(paths):
    rows = []
    for p in paths:
        with open(p, newline="") as f:
            rows.extend(csv.DictReader(f))
    rows.sort(key=lambda r: ts_unix(r.get("ts")) or 0.0)
    return rows


def _bucket_table(rows, keyfn):
    acc = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0, "cost": 0.0})
    for r in rows:
        k = keyfn(r)
        d = acc[k]
        d["n"] += 1
        if r.get("won"):
            d["wins"] += 1
        d["pnl"] += float(r.get("pnl_usd") or 0)
        d["cost"] += float(r.get("cost_usd") or 0)
    out = []
    for k in sorted(acc, key=lambda x: (str(x) == "unknown", str(x))):
        d = acc[k]
        n = d["n"]
        wr = d["wins"] / n if n else 0.0
        ev = d["pnl"] / d["cost"] if d["cost"] else 0.0
        out.append({"bucket": k, "n": n, "wins": d["wins"], "win_rate": wr, "pnl": d["pnl"], "ev": ev})
    return out


def fmt_table(title, rows):
    lines = [title, f"  {'bucket':20} {'n':>7} {'wins':>6} {'win%':>7} {'pnl':>10} {'EV':>8}"]
    for r in rows:
        lines.append(
            f"  {str(r['bucket']):20} {r['n']:7d} {r['wins']:6d} {r['win_rate']*100:6.1f}% "
            f"{r['pnl']:+10.2f} {r['ev']*100:7.2f}%"
        )
    return "\n".join(lines)


def feat_of(e):
    return e.get("feat") or {}


def k_mom(e, name):
    f = feat_of(e)
    return f.get(f"{name}_bucket") or mom_bucket_for(f.get(name))


def k_cross(e):
    f = feat_of(e)
    return f.get("crosses_bucket") or crosses_bucket_for(f.get("strike_cross_count"))


def k_min(e):
    f = feat_of(e)
    return f.get("minutes_bucket") or minutes_bucket_for(f.get("minutes_left"))


def k_drift(e):
    return feat_of(e).get("drift_dir") or "unknown"


def k_p(e):
    return p_bucket(e.get("model_prob"))


def first_passed(examples):
    first = {}
    for e in sorted(examples, key=lambda x: (float(x.get("settle_ts") or 0), float(x.get("ts") or 0))):
        if not e.get("passed"):
            continue
        key = (e.get("market_slug"), e.get("side"))
        if key not in first:
            first[key] = e
    return list(first.values())


def load_settled_fills(data_dir: Path) -> list[dict]:
    db = data_dir / "paper_trades.sqlite"
    rows = []
    if db.exists():
        con = sqlite3.connect(str(db))
        con.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM paper_orders WHERE status='settled' ORDER BY ts"
            )]
        finally:
            con.close()
    if not rows:
        csv_path = data_dir / "paper_trades.csv"
        if csv_path.exists():
            with open(csv_path, newline="") as f:
                rows = [r for r in csv.DictReader(f) if (r.get("status") or "") == "settled"]
    return rows


def fills_as_examples(fills: list[dict], eval_rows: list[dict]) -> list[dict]:
    paths = paths_from_eval_rows(eval_rows)
    out = []
    for r in fills:
        ts = ts_unix(r.get("ts")) or 0.0
        we = r.get("window_end")
        end = ts_unix(we) if we else 0.0
        if not end and we:
            try:
                end = datetime.fromisoformat(str(we).replace("Z", "+00:00")).timestamp()
            except Exception:
                end = 0.0
        hz = r.get("horizon") or "5m"
        hs = horizon_seconds(hz)
        sec_left = max(0.0, end - ts) if end else None
        # nearest eval row for vol / seconds if needed
        slug = r.get("market_slug")
        path = paths.get(slug) or []
        row = {
            "ts": ts, "asset": r.get("asset"), "horizon": hz, "side": r.get("side"),
            "seconds_remaining": sec_left, "annualized_vol": None,
            "spot": None, "price_to_beat": r.get("price_to_beat"),
            "ask": r.get("price") or r.get("ask"),
            "model_prob": r.get("model_prob"),
            "edge_after_fees": r.get("edge_after_fees"),
            "market_slug": slug,
        }
        # pull vol / last spot from eval rows of this slug at or before fill
        vol = None
        spot = None
        for ev in eval_rows:
            if ev.get("market_slug") != slug:
                continue
            et = ts_unix(ev.get("ts")) or 0.0
            if et > ts:
                break
            if ev.get("annualized_vol"):
                try:
                    vol = float(ev["annualized_vol"])
                except (TypeError, ValueError):
                    pass
            if ev.get("spot"):
                try:
                    spot = float(ev["spot"])
                except (TypeError, ValueError):
                    pass
        row["annualized_vol"] = vol
        row["spot"] = spot
        feat = extract_features(row, ts=ts, ticks=path)
        pnl = float(r.get("pnl_usd") or 0)
        cost = float(r.get("cost_usd") or 0)
        won = str(r.get("result") or "").lower() == "win" or pnl > 0
        out.append({
            "ts": ts, "settle_ts": end or ts, "market_slug": slug, "side": r.get("side"),
            "horizon": hz, "asset": r.get("asset"), "passed": True,
            "model_prob": float(r.get("model_prob") or 0),
            "won": won, "pnl_usd": pnl, "cost_usd": cost, "feat": feat,
        })
    return out


def report(subset, title, per_horizon=True):
    print(f"===== {title} (n={len(subset)}) =====")
    if not subset:
        print("  (empty)\n")
        return
    print(fmt_table("signed mom_1m", _bucket_table(subset, lambda e: k_mom(e, "mom_1m"))))
    print(fmt_table("signed mom_3m", _bucket_table(subset, lambda e: k_mom(e, "mom_3m"))))
    print(fmt_table("signed mom_window", _bucket_table(subset, lambda e: k_mom(e, "mom_window"))))
    print(fmt_table("strike crosses", _bucket_table(subset, lambda e: k_cross(e))))
    print(fmt_table("drift_dir", _bucket_table(subset, lambda e: k_drift(e))))
    print(fmt_table("minutes_left", _bucket_table(subset, lambda e: k_min(e))))
    print(fmt_table("model P bucket", _bucket_table(subset, lambda e: k_p(e))))
    # late + many crosses
    late_chop = [
        e for e in subset
        if (feat_of(e).get("minutes_left") is not None and float(feat_of(e)["minutes_left"]) < 3
            and (feat_of(e).get("strike_cross_count") or 0) >= 2)
    ]
    print(fmt_table("late (<3m) AND crosses>=2", _bucket_table(late_chop, lambda e: k_cross(e))))
    if per_horizon:
        by_h = defaultdict(list)
        for e in subset:
            by_h[e.get("horizon") or "?"].append(e)
        for hz, xs in sorted(by_h.items()):
            print(fmt_table(f"[{hz}] signed mom_1m", _bucket_table(xs, lambda e: k_mom(e, "mom_1m"))))
            print(fmt_table(f"[{hz}] signed mom_window", _bucket_table(xs, lambda e: k_mom(e, "mom_window"))))
            print(fmt_table(f"[{hz}] strike crosses", _bucket_table(xs, lambda e: k_cross(e))))
            print(fmt_table(f"[{hz}] minutes_left", _bucket_table(xs, lambda e: k_min(e))))
            print(fmt_table(f"[{hz}] model P", _bucket_table(xs, lambda e: k_p(e))))
    print()


def parse_when(s: str, combine: str) -> dict:
    conds = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SystemExit(f"bad condition {part!r}")
        field, rest = part.split("=", 1)
        if ":" not in rest:
            raise SystemExit(f"bad condition {part!r}")
        op, val = rest.split(":", 1)
        val = val.strip()
        try:
            if val.lower() in ("true", "false"):
                coerced = val.lower() == "true"
            elif "." in val:
                coerced = float(val)
            else:
                coerced = int(val)
        except ValueError:
            coerced = val
        conds.append({"field": field.strip(), "op": op.strip(), "value": coerced})
    return {combine: conds}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", nargs="*", default=["data/evaluations.csv"])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--data", default="data")
    ap.add_argument("--stake", type=float, default=10.0)
    ap.add_argument("--theta", type=float, default=0.095)
    ap.add_argument("--probe-rule", default=None, help="skip or size_mult; dry-run backtest, do not enqueue")
    ap.add_argument("--when", default=None)
    ap.add_argument("--combine", choices=("all", "any"), default="all")
    ap.add_argument("--size-mult", type=float, default=None)
    ap.add_argument("--json-out", default=None)
    a = ap.parse_args(argv)
    data_dir = Path(a.data)
    paths = list(a.csv)
    if a.all:
        paths = sorted(glob.glob(str(data_dir / "archive/**/evaluations.csv"), recursive=True)
                       + [str(data_dir / "evaluations.csv")])
        paths = [p for p in paths if Path(p).exists()]
    rows = load_eval(paths)
    outcome, settle = cal.index_outcomes(rows)
    examples = cal.asof(
        cal.iter_counterfactuals(rows, outcome, settle, theta=a.theta, stake=a.stake),
        time.time(),
    )
    taken = first_passed(examples)
    all_passed = [e for e in examples if e.get("passed")]
    fills = fills_as_examples(load_settled_fills(data_dir), rows)

    print(
        f"Loaded {len(rows)} eval rows, {len(examples)} CF examples, "
        f"{len(all_passed)} would-trade snapshots, {len(taken)} first-passed unique, "
        f"{len(fills)} settled fills. Stake ${a.stake:.2f}, fee-aware, no lookahead.\n"
    )
    report(taken, "FIRST would-trade snapshot per slug+side (matches backtest_rule)")
    report(all_passed, "ALL would-trade snapshots", per_horizon=False)
    report(fills, "REAL SETTLED FILLS (features as-of fill time, actual P&L)", per_horizon=True)

    probes = []
    if a.probe_rule:
        if not a.when:
            raise SystemExit("--probe-rule needs --when")
        rule = rl.validate_rule({
            "name": "probe", "action": a.probe_rule, "size_mult": a.size_mult,
            "when": parse_when(a.when, a.combine), "rationale": "probe",
        })
        result = rl.backtest_rule(rule, examples, champion_rules=[], stake=a.stake)
        print("===== PROBE backtest_rule (first-passed, vs no-rule champion) =====")
        print(json.dumps(result, indent=2, default=str))
        # also on real fills
        fill_bt = rl.backtest_rule(rule, fills, champion_rules=[], stake=1.0)  # fills already in USD
        # wait: backtest scales pnl_usd * stake; fills are already actual USD, so stake=1
        print("===== PROBE on real settled fills (stake=1, actual USD) =====")
        print(json.dumps(fill_bt, indent=2, default=str))
        probes.append({"rule": rule, "cf": result, "fills": fill_bt})

    if a.json_out:
        Path(a.json_out).write_text(json.dumps({
            "n_eval": len(rows), "n_cf": len(examples), "n_first": len(taken),
            "n_all_passed": len(all_passed), "n_fills": len(fills),
            "probes": probes,
        }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
