#!/usr/bin/env python3
"""Fee-aware candidate walk-forwards: strategy × pattern × scoped skip rules.

    PYTHONPATH=src .venv/bin/python scripts/replay_candidates.py --all --data data
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from polymarket_bot import patterns as pat  # noqa: E402
from polymarket_bot import rules_lab as rl  # noqa: E402
from polymarket_bot.config import load_config  # noqa: E402
from polymarket_bot.walkforward import (  # noqa: E402
    DEFAULT_STAKE, DEFAULT_THETA, load_eval_rows, outcomes_from_rows,
    run_combined_walkforward, eval_csv_paths,
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--stake", type=float, default=DEFAULT_STAKE)
    ap.add_argument("--out", default=None, help="write JSON results to this path")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    data_dir = Path(a.data)
    paths = eval_csv_paths(data_dir) if a.all else (
        [data_dir / "evaluations.csv"] if (data_dir / "evaluations.csv").exists() else []
    )
    rows = load_eval_rows(paths)
    outcome, settle = outcomes_from_rows(rows, cfg.timezone)
    tick_paths = pat.paths_from_eval_rows(rows, cfg.timezone)
    rules = rl.active_rules(data_dir)
    if not rules:
        # Analysis still wants the skip-rule definitions even if they were demoted.
        rules = []
        for p in rl.load_queue(data_dir):
            rule = dict(p.get("rule") or {})
            if rule.get("action") != "skip":
                continue
            rule["id"] = p.get("id")
            rule["name"] = rule.get("name") or p.get("id")
            rules.append(rule)
        print(f"note: no active rules; using {len(rules)} skip rules from the queue (any status) for analysis")
    print(f"{len(rows)} rows from {len(paths)} file(s), {len(outcome)} settled markets, "
          f"{len(rules)} candidate rules, stake=${a.stake:.2f}, theta={cfg.fee_theta}")
    for r in rules:
        print(f"  rule {r.get('name') or r.get('id')}: applies_to={r.get('applies_to') or 'all'}")

    kw = dict(stake=a.stake, min_edge=cfg.min_edge, min_samples=cfg.pattern_min_samples,
              prior_strength=cfg.pattern_prior_strength, skip_ev_threshold=cfg.pattern_skip_ev_threshold,
              theta=cfg.fee_theta, tz_name=cfg.timezone, paths=tick_paths)

    def run(name, pmode, rule_list, tag):
        res = run_combined_walkforward(name, rows, outcome, settle, pattern_mode=pmode, rules=rule_list, **kw)
        res["tag"] = tag
        res["rule_names"] = [r.get("name") or r.get("id") for r in rule_list]
        return res

    configs = []
    # bare and pattern-only
    for name in ("favorite", "high_conviction", "baseline", "late_window"):
        configs.append((name, "off", [], f"{name} | pattern off | no rules"))
        configs.append((name, "active", [], f"{name} | pattern active | no rules"))
    # each active rule alone on favorite and high_conviction (the profitable-looking books)
    for name in ("favorite", "high_conviction"):
        for r in rules:
            rn = r.get("name") or r.get("id")
            configs.append((name, "active", [r], f"{name} | pattern active | only {rn}"))
        if rules:
            configs.append((name, "active", list(rules), f"{name} | pattern active | all {len(rules)} rules"))
    # baseline / late_window with all rules, pattern off (pattern hurt late_window historically)
    if rules:
        configs.append(("baseline", "off", list(rules), "baseline | pattern off | all rules"))
        configs.append(("late_window", "off", list(rules), "late_window | pattern off | all rules"))

    results = []
    print(f"\n{'tag':56} {'trades':>6} {'wins':>5} {'win%':>6} {'pnl':>9} {'PF':>6} {'maxDD':>8}  skips(pat/rule)")
    for name, pmode, rlist, tag in configs:
        res = run(name, pmode, rlist, tag)
        results.append(res)
        print(f"{tag:56} {res['trades']:6d} {res['wins']:5d} {res['win_rate']*100:5.1f}% "
              f"{res['pnl']:+9.2f} {res['pf']:>6} {res['max_dd']:+8.2f}  "
              f"{res['skipped_pattern']:4d}/{res['skipped_rules']:<4d}")

    # If some individual rules helped favorite (Δ vs pattern-active no rules), also try that subset.
    fav_bare = next(r for r in results if r["tag"] == "favorite | pattern active | no rules")
    helpful = []
    for r in rules:
        rn = r.get("name") or r.get("id")
        one = next((x for x in results if x["tag"] == f"favorite | pattern active | only {rn}"), None)
        if one and one["pnl"] > fav_bare["pnl"] + 0.01 and one["max_dd"] >= fav_bare["max_dd"] - 1e-9:
            helpful.append(r)
            print(f"  helpful for favorite: {rn}  ΔP&L={one['pnl']-fav_bare['pnl']:+.2f}  ΔDD={one['max_dd']-fav_bare['max_dd']:+.2f}")
        elif one:
            print(f"  not helpful for favorite: {rn}  ΔP&L={one['pnl']-fav_bare['pnl']:+.2f}  ΔDD={one['max_dd']-fav_bare['max_dd']:+.2f}")
    if helpful and len(helpful) not in (0, 1, len(rules)):
        tag = "favorite | pattern active | helpful subset (" + ",".join(r.get("name") or r.get("id") for r in helpful) + ")"
        res = run("favorite", "active", helpful, tag)
        results.append(res)
        print(f"{tag:56} {res['trades']:6d} {res['wins']:5d} {res['win_rate']*100:5.1f}% "
              f"{res['pnl']:+9.2f} {res['pf']:>6} {res['max_dd']:+8.2f}  "
              f"{res['skipped_pattern']:4d}/{res['skipped_rules']:<4d}")

    # same helpful-subset idea for high_conviction
    hc_bare = next(r for r in results if r["tag"] == "high_conviction | pattern active | no rules")
    hc_help = []
    for r in rules:
        rn = r.get("name") or r.get("id")
        one = next((x for x in results if x["tag"] == f"high_conviction | pattern active | only {rn}"), None)
        if one and one["pnl"] > hc_bare["pnl"] + 0.01 and one["max_dd"] >= hc_bare["max_dd"] - 1e-9:
            hc_help.append(r)
    if hc_help and len(hc_help) not in (0, 1, len(rules)):
        tag = "high_conviction | pattern active | helpful subset (" + ",".join(r.get("name") or r.get("id") for r in hc_help) + ")"
        res = run("high_conviction", "active", hc_help, tag)
        results.append(res)
        print(f"{tag:56} {res['trades']:6d} {res['wins']:5d} {res['win_rate']*100:5.1f}% "
              f"{res['pnl']:+9.2f} {res['pf']:>6} {res['max_dd']:+8.2f}  "
              f"{res['skipped_pattern']:4d}/{res['skipped_rules']:<4d}")

    ranked = sorted(results, key=lambda r: r["pnl"], reverse=True)
    print("\nRanked by net P&L (fee-aware):")
    for i, r in enumerate(ranked, 1):
        print(f"  {i:2d}. {r['pnl']:+9.2f}  n={r['trades']:4d}  DD={r['max_dd']:+8.2f}  PF={r['pf']:>6}  {r['tag']}")

    if a.out:
        outp = Path(a.out)
        slim = [{k: (round(v, 4) if isinstance(v, float) else v)
                 for k, v in r.items() if k != "rule_names"} | {"rule_names": r.get("rule_names")}
                for r in results]
        outp.write_text(json.dumps({"rows": len(rows), "markets": len(outcome), "results": slim}, indent=2, default=str))
        print(f"\nwrote {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
