#!/usr/bin/env python3
"""Walk-forward replay/backtest over recorded evaluations (zero lookahead).

    PYTHONPATH=src .venv/bin/python scripts/replay.py --all [--min-samples 20]

Also compares the old sticky full-session STOPPED_FLOOR profit lock against the
Phase 5 escalating-cooldown design (and the rolling 24h HWM HALT) on the same
walk-forward tape.

Input: evaluations.csv files (live, or extracted from archive parts). Each row is
one evaluation of one market side with model_prob, ask, edge, spread, spot,
price_to_beat, seconds_remaining and the failing rule reasons.

Walk-forward rules (zero lookahead):
  * Before evaluating any row at timestamp t, every previously entered trade
    whose market window has ended (settle_ts <= t) is settled and fed into
    the PatternStore online. Future outcomes are never visible at entry time.
  * Pattern OFF: fixed stake (default $10), no pattern multiplier or skip.
  * Pattern ON:  PatternStore in 'active' mode, scaling stake within [0.25x, 1.50x]
                 and skipping contexts with n >= min_samples and EV <= -2% (UCB < 0).
"""

from __future__ import annotations

import argparse
import csv
import glob
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from polymarket_bot import patterns as pat  # noqa: E402
from polymarket_bot import strategies as strat  # noqa: E402
from polymarket_bot.analytics import DEFAULT_TZ, ts_unix  # noqa: E402
from polymarket_bot.profit_lock import LockParams, evaluate as eval_lock  # noqa: E402
from polymarket_bot.risk import trailing_hwm_state, window_start_ts  # noqa: E402


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load(paths):
    rows = []
    seen = set()
    for p in paths:
        rp = str(Path(p).resolve())
        if rp in seen:
            continue
        seen.add(rp)
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


def _pf_and_dd(pnls: list[float]) -> tuple[str, float]:
    gw = sum(x for x in pnls if x > 0)
    gl = -sum(x for x in pnls if x <= 0)
    pf = f"{gw / gl:.2f}" if gl > 0 else ("inf" if gw > 0 else "-")
    eq = 0.0
    pk = 0.0
    dd = 0.0
    for x in pnls:
        eq += x
        if eq > pk:
            pk = eq
        if eq - pk < dd:
            dd = eq - pk
    return pf, dd


def run_strategy_walkforward(
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
    tz_name: str = DEFAULT_TZ,
) -> dict:
    store = pat.PatternStore(prior_strength=prior_strength)
    taken = set()
    pending: list[tuple[float, dict]] = []  # (settle_ts, trade_record)
    pnls: list[float] = []
    per = defaultdict(float)
    wins = 0
    skipped = 0
    boosted = 0
    trimmed = 0
    shadow_delta = 0.0

    def flush_until(t_now: float) -> None:
        nonlocal pending
        still = []
        ready = []
        for s_ts, rec in pending:
            if s_ts <= t_now:
                ready.append((s_ts, rec))
            else:
                still.append((s_ts, rec))
        ready.sort(key=lambda x: x[0])
        for _, rec in ready:
            store.update_trade(
                rec["feats"],
                order_id=rec["order_id"],
                pnl_usd=rec["pnl_usd"],
                cost_usd=rec["stake_usd"],
                outcome=rec["outcome"],
            )
        pending = still

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
        feats = pat.extract_features(r, ts=t_row, tz_name=tz_name, strategy=name)
        dec = store.evaluate(
            feats,
            mode=pattern_mode,
            min_samples=min_samples,
            skip_ev_threshold=skip_ev_threshold,
        )
        # Also compute shadow evaluation regardless of mode for reporting
        sh_dec = store.evaluate(
            feats,
            mode="active",
            min_samples=min_samples,
            skip_ev_threshold=skip_ev_threshold,
        )
        won = outcome[slug] == side
        base_pnl = stake * (1.0 / ask - 1.0) if won else -stake
        if not sh_dec["passed"]:
            skipped += 1
            shadow_delta += (0.0 - base_pnl)
        elif sh_dec["would_mult"] > 1.0 + 1e-9:
            boosted += 1
            shadow_delta += base_pnl * (sh_dec["would_mult"] - 1.0)
        elif sh_dec["would_mult"] < 1.0 - 1e-9:
            trimmed += 1
            shadow_delta += base_pnl * (sh_dec["would_mult"] - 1.0)

        if pattern_mode == "active" and not dec["passed"]:
            continue

        mult = dec["mult"] if pattern_mode == "active" else 1.0
        eff_stake = stake * mult
        p = eff_stake * (1.0 / ask - 1.0) if won else -eff_stake
        pnls.append(p)
        if won:
            wins += 1
        per[r.get("horizon")] += p

        rec = {
            "feats": feats,
            "order_id": f"rep-{name}-{i}",
            "outcome": "win" if won else "loss",
            "pnl_usd": p,
            "stake_usd": eff_stake,
        }
        pending.append((s_ts, rec))

    flush_until(float("inf"))
    n = len(pnls)
    tot = sum(pnls)
    pf, dd = _pf_and_dd(pnls)
    return {
        "strategy": name,
        "mode": pattern_mode,
        "trades": n,
        "wins": wins,
        "win_rate": (wins / n) if n else 0.0,
        "pnl": tot,
        "avg": (tot / n) if n else 0.0,
        "pf": pf,
        "max_dd": dd,
        "per": dict(per),
        "would_skip": skipped,
        "would_boost": boosted,
        "would_trim": trimmed,
        "shadow_delta": shadow_delta,
    }



def _apply_sticky(st):
    """Rewrite a live lock state into the pre-Phase-5 sticky full-session floor stop."""
    if st.breach_count >= 1:
        first = st.breaches[0] if st.breaches else None
        st.phase = "STOPPED_FLOOR"
        st.reason = "profit lock floor hit (sticky full-session stop)"
        st.cooldown_until = None
        if first:
            st.stopped_at = first.get("ts")
    return st


def run_risk_walkforward(
    name: str,
    rows: list[dict],
    outcome: dict[str, str],
    settle_ts_map: dict[str, float],
    *,
    stake: float,
    min_edge: float,
    start_cash: float = 1000.0,
    policy: str = "cooldown",
    window_mode: str = "calendar_day",
    rolling_guard: bool = True,
    guard_pct: float = 0.10,
    tz_name: str = DEFAULT_TZ,
) -> dict:
    """Walk-forward with calendar-day (or rolling) risk applied at entry time (zero lookahead)."""
    params = LockParams(mode="profit_lock", target_pct=0.05, loss_limit_pct=0.10,
                        lock_ratio=0.70, locked_size_mult=0.5, adaptive=False,
                        max_breaches=3, cooldown_minutes=(60, 120, 240))
    taken = set()
    pending: list[tuple[float, float]] = []  # (settle_ts, pnl)
    all_settles: list[tuple[float, float]] = []
    pnls: list[float] = []
    wins = 0
    blocked = 0
    blocked_cooldown = 0
    blocked_session = 0
    blocked_sticky = 0
    guard_halts = 0
    halted = False
    halt_ts = None
    phases_at_entry = {"TRADING": 0, "LOCKED": 0}

    def flush(t_now: float) -> None:
        nonlocal pending, all_settles
        ready, still = [], []
        for s_ts, pnl in pending:
            (ready if s_ts <= t_now else still).append((s_ts, pnl))
        ready.sort(key=lambda x: x[0])
        all_settles.extend(ready)
        pending = still

    for i, r in enumerate(rows):
        t_row = ts_unix(r.get("ts"), tz_name)
        if t_row is None:
            continue
        flush(t_row)
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

        if halted:
            blocked += 1
            continue

        if rolling_guard:
            g = trailing_hwm_state(all_settles, start_cash, t_row, lookback_hours=24.0, drawdown_pct=guard_pct)
            if g["triggered"]:
                halted = True
                halt_ts = t_row
                guard_halts += 1
                blocked += 1
                continue

        ws = window_start_ts(t_row, tz_name, window_mode, 24.0)
        prior = sum(p for t, p in all_settles if t < ws)
        start_eq = max(1.0, start_cash + prior)
        session = [(t, p) for t, p in all_settles if t >= ws]
        st = eval_lock(params, start_eq, session, now=t_row)
        if policy == "sticky":
            st = _apply_sticky(st)
        if st.reason:
            blocked += 1
            if st.phase == "COOLDOWN":
                blocked_cooldown += 1
            elif st.phase in ("STOPPED_SESSION", "STOPPED_LOSS", "STOPPED_TARGET", "STOPPED_FLOOR"):
                blocked_session += 1
                if policy == "sticky":
                    blocked_sticky += 1
            continue
        if st.phase == "LOCKED":
            phases_at_entry["LOCKED"] += 1
            size = params.locked_size_mult
        else:
            phases_at_entry["TRADING"] += 1
            size = 1.0

        sr = f(r.get("seconds_remaining")) or 0.0
        s_ts = settle_ts_map.get(slug) or (t_row + sr)
        won = outcome[slug] == side
        eff = stake * size
        p = eff * (1.0 / ask - 1.0) if won else -eff
        pnls.append(p)
        if won:
            wins += 1
        pending.append((s_ts, p))

    flush(float("inf"))
    n = len(pnls)
    tot = sum(pnls)
    pf, dd = _pf_and_dd(pnls)
    equity = start_cash + tot
    return {
        "strategy": name, "policy": policy, "trades": n, "wins": wins,
        "win_rate": (wins / n) if n else 0.0, "pnl": tot, "avg": (tot / n) if n else 0.0,
        "pf": pf, "max_dd": dd, "equity": equity, "blocked": blocked,
        "blocked_cooldown": blocked_cooldown, "blocked_session": blocked_session,
        "blocked_sticky": blocked_sticky, "guard_halts": guard_halts, "halt_ts": halt_ts,
        "entries_trading": phases_at_entry["TRADING"], "entries_locked": phases_at_entry["LOCKED"],
    }



def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", nargs="*", default=["data/evaluations.csv"])
    ap.add_argument("--all", action="store_true", help="include all archived + live evaluations.csv")
    ap.add_argument("--stake", type=float, default=10.0)
    ap.add_argument("--min-edge", type=float, default=0.04, help="configured rules-engine min edge (baseline)")
    ap.add_argument("--min-samples", type=int, default=20)
    ap.add_argument("--prior-strength", type=float, default=10.0)
    ap.add_argument("--skip-ev-threshold", type=float, default=-0.02)
    a = ap.parse_args(argv)

    paths = list(a.csv)
    if a.all:
        paths = sorted(glob.glob("data/archive/*/evaluations.csv")) + ["data/evaluations.csv"]
        paths = [p for p in paths if Path(p).exists()]
    rows = load(paths)

    outcome: dict[str, str] = {}
    settle_ts_map: dict[str, float] = {}
    for r in rows:
        sr, spot, ptb = f(r.get("seconds_remaining")), f(r.get("spot")), f(r.get("price_to_beat"))
        slug = r.get("market_slug") or ""
        t_row = ts_unix(r.get("ts"))
        if t_row is not None and sr is not None:
            settle_ts_map[slug] = max(settle_ts_map.get(slug, 0.0), t_row + sr)
        if sr is not None and sr <= 30 and spot is not None and ptb is not None:
            outcome[slug] = "up" if spot >= ptb else "down"

    print(f"{len(rows)} evaluation rows from {len(paths)} file(s), {len(outcome)} markets with end-of-window print")
    print(f"Walk-forward zero-lookahead | stake=${a.stake:.2f} | min_samples={a.min_samples} | skip_ev<={a.skip_ev_threshold:+.1%}\n")
    print(f"{'strategy':16} {'pat':6} {'trades':>6} {'wins':>5} {'win%':>6} {'pnl':>9} {'avg':>7} {'PF':>6} {'maxDD':>8}   horizons / shadow")
    for name in strat.names():
        off = run_strategy_walkforward(
            name, rows, outcome, settle_ts_map,
            stake=a.stake, min_edge=a.min_edge, pattern_mode="off",
            min_samples=a.min_samples, prior_strength=a.prior_strength, skip_ev_threshold=a.skip_ev_threshold,
        )
        on = run_strategy_walkforward(
            name, rows, outcome, settle_ts_map,
            stake=a.stake, min_edge=a.min_edge, pattern_mode="active",
            min_samples=a.min_samples, prior_strength=a.prior_strength, skip_ev_threshold=a.skip_ev_threshold,
        )
        for res in (off, on):
            h_str = " ".join(f"{h}:{v:+.0f}" for h, v in sorted(res["per"].items()))
            sh = f" | shadow skip={res['would_skip']} boost={res['would_boost']} trim={res['would_trim']} delta={res['shadow_delta']:+.2f}" if res["mode"] == "off" else ""
            print(f"{res['strategy']:16} {res['mode']:6} {res['trades']:6d} {res['wins']:5d} {res['win_rate']*100:5.1f}% "
                  f"{res['pnl']:+9.2f} {res['avg']:+7.2f} {res['pf']:>6} {res['max_dd']:+8.2f}   {h_str}{sh}")

    print("\nRisk-layer comparison: sticky full-session floor stop vs Phase 5 escalating cooldown")
    print(f"(calendar_day sessions, ${a.stake:.2f} stake, pattern off, rolling 24h 10% HWM HALT on)\n")
    print(f"{'strategy':16} {'policy':10} {'trades':>6} {'wins':>5} {'win%':>6} {'pnl':>9} {'PF':>6} {'maxDD':>8} "
          f"{'blocked':>7} {'cd':>5} {'stop':>5} {'halts':>5} {'@half':>5}")
    for name in strat.names():
        for policy in ("sticky", "cooldown"):
            res = run_risk_walkforward(
                name, rows, outcome, settle_ts_map,
                stake=a.stake, min_edge=a.min_edge, policy=policy,
            )
            print(f"{res['strategy']:16} {res['policy']:10} {res['trades']:6d} {res['wins']:5d} {res['win_rate']*100:5.1f}% "
                  f"{res['pnl']:+9.2f} {res['pf']:>6} {res['max_dd']:+8.2f} {res['blocked']:7d} "
                  f"{res['blocked_cooldown']:5d} {res['blocked_session']:5d} {res['guard_halts']:5d} {res['entries_locked']:5d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
