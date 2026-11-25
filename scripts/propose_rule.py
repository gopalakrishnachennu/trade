#!/usr/bin/env python3
"""Submit an analyst rule to the rules-lab queue and auto-backtest it.

  PYTHONPATH=src .venv/bin/python scripts/propose_rule.py \\
    --name skip-high-vol-late-5m --action skip \\
    --when 'horizon=eq:5m,vol_regime=eq:high,minutes_left=lt:3' \\
    --rationale 'late 5m high-vol entries have been fee-eaten'

  PYTHONPATH=src .venv/bin/python scripts/propose_rule.py --json rule.json

`--when` is a comma-separated list of field=op:value (joined with `all`).
`--combine any` uses any. Action is `skip` or `size_mult` (then `--size-mult 0.5`).
The rule is validated (allowlisted features only; cannot touch caps, mode, or HALT),
walk-forward backtested on stored evaluations, then placed in shadow.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from polymarket_bot import calibration as cal  # noqa: E402
from polymarket_bot import learning_runtime as learn  # noqa: E402
from polymarket_bot import rules_lab as rl  # noqa: E402
from polymarket_bot.config import load_config  # noqa: E402


def _parse_when(s: str, combine: str) -> dict:
    conds = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SystemExit(f"bad condition {part!r}; expected field=op:value")
        field, rest = part.split("=", 1)
        if ":" not in rest:
            raise SystemExit(f"bad condition {part!r}; expected field=op:value")
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
    if not conds:
        raise SystemExit("--when is empty")
    return {combine: conds}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--data", default=None)
    ap.add_argument("--json", dest="json_path", default=None, help="path to a rule JSON object")
    ap.add_argument("--name", default=None)
    ap.add_argument("--action", choices=("skip", "size_mult"), default="skip")
    ap.add_argument("--size-mult", type=float, default=None)
    ap.add_argument("--when", default=None, help="field=op:value,field=op:value (all)")
    ap.add_argument("--combine", choices=("all", "any"), default="all")
    ap.add_argument("--rationale", default="")
    ap.add_argument("--actor", default="analyst")
    ap.add_argument("--stake", type=float, default=10.0)
    ap.add_argument("--all", action="store_true", help="include data/archive/**/evaluations.csv")
    ap.add_argument("--applies-to", default=None,
                    help="comma-separated strategy names this rule applies to (default: all)")
    a = ap.parse_args(argv)

    if a.json_path:
        rule = json.loads(Path(a.json_path).read_text())
    else:
        if not a.when:
            raise SystemExit("provide --when or --json")
        rule = {
            "name": a.name or "rule",
            "action": a.action,
            "size_mult": a.size_mult,
            "when": _parse_when(a.when, a.combine),
            "rationale": a.rationale,
        }
    if a.rationale and not rule.get("rationale"):
        rule["rationale"] = a.rationale
    if a.name:
        rule["name"] = a.name
    if a.applies_to:
        rule["applies_to"] = a.applies_to

    cfg = load_config(a.config)
    data_dir = Path(a.data) if a.data else cfg.sqlite_path.parent
    ev_csv = data_dir / "evaluations.csv" if a.data else cfg.evaluations_csv
    rec = rl.enqueue_proposal(data_dir, rule, actor=a.actor, rationale=rule.get("rationale") or "", source="analyst")

    paths = [ev_csv]
    if a.all:
        paths = [Path(p) for p in sorted(glob.glob(str(data_dir / "archive/**/evaluations.csv"), recursive=True))] + [ev_csv]
    rows = []
    for pth in paths:
        rows.extend(learn.load_eval_rows(pth))
    outcome, settle = cal.index_outcomes(rows, tz_name=cfg.timezone)
    examples = cal.iter_counterfactuals(rows, outcome, settle, tz_name=cfg.timezone, theta=cfg.fee_theta)
    # only rows that already settled; already implicit if we use all historical
    import time
    examples = cal.asof(examples, time.time())
    result = rl.backtest_rule(rec["rule"], examples, champion_rules=rl.active_rules(data_dir), stake=a.stake)
    rl.attach_backtest(data_dir, rec["id"], result)
    rec = rl.get_proposal(data_dir, rec["id"])
    print(json.dumps({"ok": True, "id": rec["id"], "status": rec["status"], "rule": rec["rule"],
                      "backtest": rec.get("backtest")}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
