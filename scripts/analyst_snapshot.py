#!/usr/bin/env python3
"""Compact JSON snapshot for a human/agent analyst.

  PYTHONPATH=src .venv/bin/python scripts/analyst_snapshot.py [--data /opt/polymarket-bot/data]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from polymarket_bot import attribution as attr  # noqa: E402
from polymarket_bot import calibration as cal  # noqa: E402
from polymarket_bot import learning_runtime as learn  # noqa: E402
from polymarket_bot import patterns as pat  # noqa: E402
from polymarket_bot import rules_lab as rl  # noqa: E402
from polymarket_bot.bandit import load_bandit  # noqa: E402
from polymarket_bot.config import load_config  # noqa: E402
from polymarket_bot.store import Store  # noqa: E402


def _ci(row: dict) -> dict:
    return {
        "key": row.get("key") or row.get("label") or row.get("context"),
        "label": row.get("label") or row.get("key"),
        "n": row.get("n"),
        "wins": row.get("wins"),
        "post_wr": row.get("post_wr"),
        "se_wr": row.get("se_wr"),
        "post_ev": row.get("post_ev"),
        "ev_lcb": row.get("ev_lcb"),
        "ev_ucb": row.get("ev_ucb"),
        "pnl_usd": row.get("pnl_usd") or row.get("pnl"),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Print a compact analyst snapshot as JSON")
    ap.add_argument("--data", default=None, help="data directory (default: from config.yaml)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--min-n", type=int, default=8)
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    data_dir = Path(a.data) if a.data else cfg.sqlite_path.parent
    # If --data is a sibling of a different sqlite
    sqlite = data_dir / "paper_trades.sqlite" if a.data else cfg.sqlite_path
    ev_csv = data_dir / "evaluations.csv" if a.data else cfg.evaluations_csv
    store = Store(sqlite, data_dir / "paper_trades.csv", ev_csv, data_dir / "equity.csv")

    # Attribution of all settled trades
    tagged = []
    patterns = store.all_trade_patterns()
    existing = store.all_attributions()
    eval_rows = learn.load_eval_rows(ev_csv)
    if cfg.attribution_mode != "off" and len(existing) < sum(1 for o in store.all_orders() if o.status == "settled" and o.pnl_usd is not None):
        learn.backfill_attribution(store, eval_rows, cfg)
        existing = store.all_attributions()
    for o in store.all_orders():
        if o.status != "settled" or o.pnl_usd is None:
            continue
        rec = existing.get(o.id)
        if rec:
            tagged.append({
                "order_id": o.id, "primary": rec.get("primary_reason") or rec.get("primary"),
                "label": rec.get("label"), "tags": rec.get("tags") or [],
                "detail": rec.get("detail"), "fees_usd": rec.get("fees_usd"),
                "pnl_usd": rec.get("pnl_usd"), "cost_usd": rec.get("cost_usd"),
                "outcome": rec.get("outcome"), "asset": o.asset, "horizon": o.horizon,
                "side": o.side,
            })
    attrib = attr.summarize(tagged)

    pstore = pat.load_pattern_store(data_dir, prior_strength=cfg.pattern_prior_strength)
    summary = pstore.summary(min_samples=cfg.pattern_min_samples, skip_ev_threshold=cfg.pattern_skip_ev_threshold)
    contexts = [c for c in (summary.get("tables") or {}).get("context") or [] if (c.get("n") or 0) >= a.min_n]
    worst = sorted(contexts, key=lambda r: (r.get("post_ev") is None, r.get("post_ev") or 0, r.get("n") or 0))[:8]
    best = sorted(contexts, key=lambda r: (r.get("post_ev") is None, -(r.get("post_ev") or 0), -(r.get("n") or 0)))[:8]

    calib = cal.load_report(data_dir)
    if calib is None and eval_rows and cfg.calibration_mode != "off":
        calib = learn.refresh_calibration(eval_rows, cfg, data_dir)
    calib_out = None
    if calib:
        calib_out = {
            "method": calib.get("method"),
            "mode": calib.get("mode") or cfg.calibration_mode,
            "overall": calib.get("overall"),
            "by_asset_horizon": [
                {k: g[k] for k in ("key", "asset", "horizon", "n", "brier", "log_loss", "pnl") if k in g}
                | {"buckets": g.get("buckets")}
                for g in (calib.get("by_asset_horizon") or [])
            ],
        }

    bstore = load_bandit(data_dir, prior_strength=cfg.pattern_prior_strength)
    bandit = bstore.summary()

    q = rl.load_queue(data_dir)
    snap = {
        "as_of_et": __import__("datetime").datetime.now(__import__("zoneinfo").ZoneInfo("America/New_York")).strftime("%Y-%m-%d %I:%M:%S %p %Z"),
        "data_dir": str(data_dir),
        "modes": {
            "pattern": cfg.pattern_mode,
            "attribution": cfg.attribution_mode,
            "counterfactual": cfg.counterfactual_mode,
            "cf_weight": cfg.cf_weight,
            "calibration": cfg.calibration_mode,
            "calibration_method": cfg.calibration_method,
            "bandit": cfg.bandit_mode,
            "rules_lab": cfg.rules_lab_mode,
        },
        "attribution": {
            "n": attrib["n"], "wins": attrib["wins"], "losses": attrib["losses"],
            "pnl": attrib["pnl"], "fees": attrib["fees"], "fees_on_losers": attrib["fees_on_losers"],
            "by_primary": attrib["by_primary"], "by_tag": attrib["by_tag"],
            "losers": [
                {k: r.get(k) for k in ("order_id", "primary", "label", "tags", "detail", "pnl_usd", "fees_usd", "asset", "horizon", "side")}
                for r in tagged if r.get("outcome") != "win"
            ],
        },
        "calibration": calib_out,
        "contexts": {
            "min_n": a.min_n,
            "worst": [_ci(r) for r in worst],
            "best": [_ci(r) for r in best],
            "global": summary.get("global"),
            "cf": summary.get("cf"),
        },
        "bandit_shadow": bandit.get("shadow"),
        "bandit_top_contexts": bandit.get("contexts", [])[:12],
        "rules": {
            "active": [
                {"id": p.get("id"), "status": p.get("status"), "rule": p.get("rule"), "rationale": p.get("rationale"),
                 "backtest": p.get("backtest"), "shadow": p.get("shadow"), "gate": p.get("gate")}
                for p in q if p.get("status") == "active"
            ],
            "queue": [
                {"id": p.get("id"), "status": p.get("status"), "name": (p.get("rule") or {}).get("name"),
                 "action": (p.get("rule") or {}).get("action"), "rationale": p.get("rationale"),
                 "backtest": p.get("backtest"), "shadow": p.get("shadow"), "gate": p.get("gate")}
                for p in q
            ],
        },
        "how_to_propose": (
            "PYTHONPATH=src .venv/bin/python scripts/propose_rule.py "
            "--name skip-high-vol-late-5m --action skip "
            "--when 'horizon=eq:5m,vol_regime=eq:high,minutes_left=lt:3' "
            "--rationale 'late 5m entries in high vol have been fee-eaten' "
            "[--data /opt/polymarket-bot/data]"
        ),
    }
    print(json.dumps(snap, indent=2, default=str))
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
