"""Fee-aware walk-forward replay shared by scripts/replay.py and the promotion gate.

Zero lookahead: a trade is settled into the PatternStore only after its market
window has ended. Fees use calibration.unit_pnl (configured theta, default 0.095).
"""

from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from . import patterns as pat
from . import rules_lab as rl
from . import strategies as strat
from .analytics import DEFAULT_TZ, ts_unix
from .bandit import compose_learning
from .calibration import unit_pnl

CACHE_NAME = "walkforward.json"
DEFAULT_THETA = 0.095
DEFAULT_STAKE = 10.0


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def to_sig(r, cfg_min_edge):
    reasons = [x.strip() for x in (r.get("reasons") or "").split(" | ") if x.strip()]
    dq_ok = all(x.startswith("min_edge_after_fees") for x in reasons) and f(r.get("model_prob")) is not None and f(r.get("ask"))
    edge = f(r.get("edge_after_fees"))
    checks = [{"name": "data_quality", "passed": bool(dq_ok)},
              {"name": "min_edge_after_fees", "passed": edge is not None and edge >= cfg_min_edge, "value": edge}]
    return {"edge_after_fees": edge, "model_prob": f(r.get("model_prob")), "ask": f(r.get("ask")),
            "seconds_remaining": f(r.get("seconds_remaining")), "horizon": r.get("horizon"), "rule_checks": checks}


def pf_and_dd(pnls: list[float]) -> tuple[str, float]:
    gw = sum(x for x in pnls if x > 0)
    gl = -sum(x for x in pnls if x <= 0)
    pf = f"{gw / gl:.2f}" if gl > 0 else ("inf" if gw > 0 else "-")
    eq = pk = 0.0
    dd = 0.0
    for x in pnls:
        eq += x
        if eq > pk:
            pk = eq
        if eq - pk < dd:
            dd = eq - pk
    return pf, dd


def eval_csv_paths(data_dir: Path) -> list[Path]:
    paths = sorted(data_dir.glob("archive/*/evaluations.csv"))
    live = data_dir / "evaluations.csv"
    if live.exists():
        paths.append(live)
    return [p for p in paths if p.is_file()]


def load_eval_rows(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for p in paths:
        rp = str(Path(p).resolve())
        if rp in seen:
            continue
        seen.add(rp)
        with open(p, newline="") as fh:
            rows += list(csv.DictReader(fh))
    rows.sort(key=lambda r: r.get("ts") or "")
    return rows


def outcomes_from_rows(rows: list[dict], tz_name: str = DEFAULT_TZ) -> tuple[dict[str, str], dict[str, float]]:
    outcome: dict[str, str] = {}
    settle_ts_map: dict[str, float] = {}
    for r in rows:
        sr, spot, ptb = f(r.get("seconds_remaining")), f(r.get("spot")), f(r.get("price_to_beat"))
        slug = r.get("market_slug") or ""
        t_row = ts_unix(r.get("ts"), tz_name)
        if t_row is not None and sr is not None:
            settle_ts_map[slug] = max(settle_ts_map.get(slug, 0.0), t_row + sr)
        if sr is not None and sr <= 30 and spot is not None and ptb is not None:
            outcome[slug] = "up" if spot >= ptb else "down"
    return outcome, settle_ts_map


def load_tape(data_dir: Path, tz_name: str = DEFAULT_TZ) -> tuple[list[dict], dict[str, str], dict[str, float]]:
    rows = load_eval_rows(eval_csv_paths(data_dir))
    outcome, settle = outcomes_from_rows(rows, tz_name)
    return rows, outcome, settle


def run_combined_walkforward(
    name: str,
    rows: list[dict],
    outcome: dict[str, str],
    settle_ts_map: dict[str, float],
    *,
    stake: float,
    min_edge: float,
    pattern_mode: str,
    min_samples: int,
    prior_strength: float,
    skip_ev_threshold: float,
    rules: list[dict] | None = None,
    theta: float = DEFAULT_THETA,
    tz_name: str = DEFAULT_TZ,
    use_fees: bool = True,
    paths: dict | None = None,
) -> dict:
    """Walk-forward of one strategy with optional active pattern + skip rules (fee-aware)."""
    store = pat.PatternStore(prior_strength=prior_strength)
    if paths is None:
        paths = pat.paths_from_eval_rows(rows, tz_name)
    taken = set()
    pending: list[tuple[float, dict]] = []
    pnls: list[float] = []
    wins = 0
    skipped_pattern = 0
    skipped_rules = 0

    def flush_until(t_now: float) -> None:
        nonlocal pending
        still, ready = [], []
        for s_ts, rec in pending:
            (ready if s_ts <= t_now else still).append((s_ts, rec))
        ready.sort(key=lambda x: x[0])
        for _, rec in ready:
            store.update_trade(rec["feats"], order_id=rec["order_id"], pnl_usd=rec["pnl_usd"],
                               cost_usd=rec["cost_usd"], outcome=rec["outcome"])
        pending = still

    scoped = [r for r in (rules or []) if rl.rule_applies_to_strategy(r, name)]

    for i, r in enumerate(rows):
        t_row = ts_unix(r.get("ts"), tz_name)
        if t_row is None:
            continue
        flush_until(t_row)
        slug = r.get("market_slug") or ""
        side = r.get("side") or ""
        key = (slug, side)
        if key in taken or slug not in outcome:
            continue
        ok, _ = strat.evaluate(name, None, to_sig(r, min_edge), min_edge)
        if not ok:
            continue
        taken.add(key)
        ask = f(r.get("ask"))
        if not ask or ask <= 0 or ask >= 1:
            continue
        sr = f(r.get("seconds_remaining")) or 0.0
        s_ts = settle_ts_map.get(slug) or (t_row + sr)
        feats = pat.extract_features(r, ts=t_row, tz_name=tz_name, strategy=name, ticks=paths.get(slug) or [])
        dec = store.evaluate(feats, mode=pattern_mode, min_samples=min_samples,
                             skip_ev_threshold=skip_ev_threshold)
        rule_dec = rl.apply_active_rules(scoped, feats)
        composed = compose_learning(dec, None, rule_dec, pattern_mode=pattern_mode,
                                    bandit_mode="off", rules_mode="active" if scoped else "off")
        if not composed.get("passed", True):
            if not dec.get("passed", True) and pattern_mode == "active":
                skipped_pattern += 1
            if not rule_dec.get("passed", True):
                skipped_rules += 1
            continue
        won = outcome[slug] == side
        m = float(composed.get("mult") or 1.0)
        if use_fees:
            pnl, cost, _fee = unit_pnl(ask, won, theta=theta, stake=stake * m)
        else:
            cost = stake * m
            pnl = cost * (1.0 / ask - 1.0) if won else -cost
        pnls.append(pnl)
        if won:
            wins += 1
        pending.append((s_ts, {
            "feats": feats, "order_id": f"comb-{name}-{i}",
            "outcome": "win" if won else "loss", "pnl_usd": pnl, "cost_usd": cost,
        }))
    flush_until(float("inf"))
    n = len(pnls)
    tot = sum(pnls)
    pf, dd = pf_and_dd(pnls)
    return {
        "strategy": name, "pattern_mode": pattern_mode, "trades": n, "wins": wins,
        "win_rate": (wins / n) if n else 0.0, "pnl": tot, "avg": (tot / n) if n else 0.0,
        "pf": pf, "max_dd": dd, "skipped_pattern": skipped_pattern, "skipped_rules": skipped_rules,
        "candidates": len(taken),
    }


def as_gate_metrics(res: dict) -> dict[str, Any]:
    dd = res.get("max_drawdown")
    if dd is None:
        dd = res.get("max_dd") or 0.0
    pf = res.get("profit_factor", res.get("pf"))
    try:
        pf_f = float(pf) if pf not in (None, "-", "inf") else None
    except (TypeError, ValueError):
        pf_f = None
    return {
        "trades": int(res.get("trades") or 0),
        "pnl": float(res.get("pnl") or 0),
        "max_drawdown": abs(float(dd)),
        "profit_factor": pf_f,
        "win_rate": float(res.get("win_rate") or 0),
        "pattern_mode": res.get("pattern_mode"),
        "skipped_rules": int(res.get("skipped_rules") or 0),
        "skipped_pattern": int(res.get("skipped_pattern") or 0),
        "candidates": int(res.get("candidates") or 0),
    }


def _live_pattern_mode(cfg, name: str) -> str:
    """Shadow pattern does not change the live book; treat it as off for promotion replay."""
    if cfg is None:
        return "off"
    try:
        mode = cfg.pattern_mode_for(name)
    except Exception:
        mode = "off"
    return "active" if mode == "active" else "off"


def _fingerprint(data_dir: Path, cfg, rules: list[dict], *, theta: float, stake: float,
                 min_edge: float, n_rows: int | None, paths: list[Path]) -> dict[str, Any]:
    by_s = {}
    if cfg is not None:
        try:
            by_s = {n: _live_pattern_mode(cfg, n) for n in strat.names()}
        except Exception:
            by_s = {}
    mtimes = {str(p): int(p.stat().st_mtime) for p in paths}
    rule_fp = []
    for r in rules:
        rule_fp.append({
            "id": r.get("id") or r.get("name"),
            "name": r.get("name"),
            "action": r.get("action"),
            "applies_to": r.get("applies_to"),
            "when": r.get("when"),
            "size_mult": r.get("size_mult"),
        })
    return {
        "theta": theta,
        "stake": stake,
        "min_edge": min_edge,
        "n_rows": n_rows,
        "pattern_modes": by_s,
        "rules": rule_fp,
        "files": mtimes,
    }


def _cache_path(data_dir: Path) -> Path:
    return strat.strategies_dir(data_dir) / CACHE_NAME


def cached_gate_walkforward(data_dir: Path, cfg=None, *, force: bool = False,
                            stake: float = DEFAULT_STAKE) -> dict[str, dict]:
    """Per-strategy fee-aware walk-forward used by the promotion gate.

    Each strategy is replayed with its live pattern mode (active or off) and
    the currently active skip rules that apply to it. Missing tape → {}.
    Failures are raised so the reviewer can fail the gate closed.
    """
    data_dir = Path(data_dir)
    tz_name = DEFAULT_TZ
    theta = DEFAULT_THETA
    min_edge = 0.04
    min_samples, prior_strength, skip_ev = 20, 10.0, -0.02
    if cfg is not None:
        try:
            tz_name = cfg.timezone
        except Exception:
            pass
        try:
            theta = float(cfg.fee_theta)
        except Exception:
            pass
        try:
            min_edge = float(cfg.min_edge)
        except Exception:
            try:
                min_edge = float(cfg.raw.get("rules", {}).get("min_edge") or 0.04)
            except Exception:
                pass
        try:
            min_samples = int(cfg.pattern_min_samples)
            prior_strength = float(cfg.pattern_prior_strength)
            skip_ev = float(cfg.pattern_skip_ev_threshold)
        except Exception:
            pass

    try:
        rules = rl.active_rules(data_dir)
    except Exception:
        rules = []

    paths = eval_csv_paths(data_dir)
    if not paths:
        return {}

    # Cheap fingerprint without loading rows: file mtimes only. n_rows filled after load.
    prelim = _fingerprint(data_dir, cfg, rules, theta=theta, stake=stake, min_edge=min_edge,
                          n_rows=None, paths=paths)
    cache_p = _cache_path(data_dir)
    if not force and cache_p.exists():
        try:
            cached = json.loads(cache_p.read_text())
            cf = cached.get("fingerprint") or {}
            # Compare everything except n_rows (unknown until load)
            if all(cf.get(k) == prelim.get(k) for k in ("theta", "stake", "min_edge", "pattern_modes", "rules", "files")):
                strategies = cached.get("strategies") or {}
                if strategies:
                    return {n: as_gate_metrics(m) for n, m in strategies.items()}
        except Exception:
            pass

    rows = load_eval_rows(paths)
    if not rows:
        return {}
    outcome, settle = outcomes_from_rows(rows, tz_name)
    tick_paths = pat.paths_from_eval_rows(rows, tz_name)
    strategies: dict[str, dict] = {}
    for name in strat.names():
        pmode = _live_pattern_mode(cfg, name)
        res = run_combined_walkforward(
            name, rows, outcome, settle,
            stake=stake, min_edge=min_edge, pattern_mode=pmode,
            min_samples=min_samples, prior_strength=prior_strength,
            skip_ev_threshold=skip_ev, rules=rules, theta=theta,
            tz_name=tz_name, paths=tick_paths,
        )
        strategies[name] = as_gate_metrics(res)

    fp = _fingerprint(data_dir, cfg, rules, theta=theta, stake=stake, min_edge=min_edge,
                      n_rows=len(rows), paths=paths)
    doc = {"ts": time.time(), "fingerprint": fp, "strategies": strategies}
    cache_p.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2, default=str))
    os.replace(tmp, cache_p)
    return strategies
