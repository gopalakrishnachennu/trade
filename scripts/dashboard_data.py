#!/usr/bin/env python3
"""Build a JSON snapshot of today's paper-trading session for the dashboard Page.

SQLite (data/paper_trades.sqlite) is the source of truth for trades and equity.
Evaluations and bot status come from the CSV and pid/log files beside it.

The Page embeds this snapshot at write time; it does not refresh itself.
Re-run this script, then ask the bot to refresh the Page (see README).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polymarket_bot.config import load_config  # noqa: E402
from polymarket_bot.models import PaperOrder  # noqa: E402
from polymarket_bot.risk import order_date_in_tz  # noqa: E402
from polymarket_bot import analytics  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Emit today's paper-trading dashboard JSON")
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    p.add_argument("--db", default=None)
    p.add_argument("--day", default=None, help="YYYY-MM-DD in the config timezone (default: today)")
    p.add_argument("-o", "--out", default=None, help="Write JSON to this path (default: stdout only)")
    p.add_argument("--pretty", action="store_true", default=True)
    return p.parse_args()


def _money(x: float | None, signed: bool = False) -> str:
    if x is None:
        return "—"
    if signed:
        return f"${x:,.2f}" if x < 0 else f"+${x:,.2f}"
    return f"${x:,.2f}"


def _pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:+.2f}%"


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _read_pid(path: Path) -> int | None:
    if not path.exists():
        return None
    text = path.read_text().strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _last_log_line(path: Path) -> tuple[str | None, str | None]:
    if not path.exists():
        return None, None
    last = ""
    with path.open(errors="replace") as f:
        for line in f:
            if line.strip():
                last = line.rstrip()
    if not last:
        return None, None
    # "2026-11-24 11:26:27 EST INFO ..."
    parts = last.split(" ", 3)
    if len(parts) >= 3 and parts[2] in {"EST", "EDT", "ET"}:
        return f"{parts[0]} {parts[1]} {parts[2]}", last
    return None, last


def _fmt_et(ts: str, tz_name: str, with_seconds: bool = True) -> str:
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    local = dt.astimezone(ZoneInfo(tz_name))
    fmt = "%-I:%M:%S %p" if with_seconds else "%-I:%M %p"
    return local.strftime(fmt)


def _in_day(ts: str | None, day: date, tz: str) -> bool:
    return order_date_in_tz(ts or "", tz) == day


def _load_orders(db: Path) -> tuple[list[PaperOrder], list[dict]]:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    # Fills only: rejected rows can number in the tens of thousands and are not trades.
    rows = [dict(r) for r in con.execute("SELECT * FROM paper_orders WHERE status IN ('open','settled')")]
    snaps = [dict(r) for r in con.execute(
        "SELECT ts, cash, open_exposure, realized_pnl, equity, open_positions FROM equity"
    )]
    con.close()
    # ts strings mix naive local ("2026-11-24 17:04:42.1") and ISO with offsets;
    # text ORDER BY puts them out of order, so sort on the parsed instant.
    rows.sort(key=lambda r: analytics.ts_unix(r.get("ts")) or 0.0)
    snaps.sort(key=lambda r: analytics.ts_unix(r.get("ts")) or 0.0)
    return [PaperOrder(**r) for r in rows], snaps


def _process_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    return _pid_running(pid)


def _skip_reason_key(reasons: str) -> str:
    if not reasons:
        return "(none)"
    first = reasons.split(" | ")[0].strip()
    if ":" in first:
        return first.split(":", 1)[0].strip()
    return first


def _runtime_status(rt: Path) -> dict:
    """Status of the fail-closed runtime from its bus files (heartbeats, HALT)."""
    import time as _t
    out = {"present": rt.exists(), "fresh": False, "halted": False, "halt": None, "heartbeats": {}, "controls_version": 0}
    if not rt.exists():
        return out
    now = _t.time()
    for hb in sorted((rt / "heartbeats").glob("*.json")) if (rt / "heartbeats").exists() else []:
        try:
            d = json.loads(hb.read_text())
            out["heartbeats"][hb.stem] = {"age_s": round(now - float(d.get("wall_ts") or 0), 1), "status": d.get("status")}
        except Exception:
            continue
    ages = [v["age_s"] for k, v in out["heartbeats"].items() if k != "watchdog"]
    out["newest_heartbeat_age_s"] = min(ages) if ages else None
    out["fresh"] = bool(ages) and min(ages) < 120
    try:
        h = json.loads((rt / "HALT.json").read_text())
        out["halted"] = bool(h.get("halted", True))
        out["halt"] = h
    except Exception:
        pass
    try:
        out["controls_version"] = int(json.loads((rt / "controls.json").read_text()).get("version") or 0)
        out["controls"] = json.loads((rt / "controls.json").read_text()).get("values")
    except Exception:
        pass
    return out


def build_snapshot(cfg, day: date, root: Path) -> dict:
    tz = cfg.timezone
    now = datetime.now(ZoneInfo(tz))
    as_of = now.strftime("%b %-d, %Y, %-I:%M:%S %p %Z")
    db = cfg.sqlite_path
    orders, snaps = _load_orders(db)
    day_orders = [o for o in orders if _in_day(o.ts, day, tz)]
    fills = [o for o in day_orders if o.status in ("open", "settled")]
    settled = [o for o in fills if o.status == "settled"]
    open_pos = [o for o in fills if o.status == "open"]

    start = cfg.starting_day_equity
    target = cfg.daily_profit_target_usd
    loss_lim = cfg.daily_loss_limit_usd
    fees_all = sum(o.fee_usd or 0 for o in fills)
    fees_settled = sum(o.fee_usd or 0 for o in settled)
    gross_settled = sum((o.payout_usd or 0) - (o.contracts or 0) * (o.price or 0) for o in settled)
    realized = sum(o.pnl_usd or 0 for o in settled)
    open_cost = sum(o.cost_usd or 0 for o in open_pos)
    equity = start + realized
    wins = [o for o in settled if o.result == "win"]
    losses = [o for o in settled if o.result == "loss"]
    win_rate = (len(wins) / len(settled)) if settled else None
    target_hit = realized + 1e-9 >= target
    loss_hit = realized - 1e-9 <= -loss_lim
    if target_hit:
        halt = "profit_target"
        halt_label = "Profit target hit"
        halt_detail = (
            f"New paper entries stopped after realized P&L reached "
            f"{_money(realized, signed=True)} ({_pct(realized / start * 100)}), "
            f"above the +{cfg.daily_profit_target_pct:.0%} target of {_money(target)}. "
            f"Open positions are still left to settle."
        )
    elif loss_hit:
        halt = "loss_limit"
        halt_label = "Loss limit hit"
        halt_detail = (
            f"New paper entries stopped after realized P&L reached "
            f"{_money(realized, signed=True)} ({_pct(realized / start * 100)}), "
            f"at or below the -{cfg.daily_loss_limit_pct:.0%} limit of {_money(-loss_lim, signed=True)}. "
            f"Open positions are still left to settle."
        )
    else:
        halt = "none"
        halt_label = "Open to new entries"
        halt_detail = (
            f"Realized P&L is {_money(realized, signed=True)} "
            f"({_pct(realized / start * 100) if start else '—'}), "
            f"between the +{cfg.daily_profit_target_pct:.0%} target ({_money(target)}) "
            f"and the -{cfg.daily_loss_limit_pct:.0%} limit ({_money(-loss_lim, signed=True)})."
        )

    bot_pid = _read_pid(root / "data" / "bot.pid")
    wrapper_pid = _read_pid(root / "data" / "wrapper.pid")
    bot_running = _process_alive(bot_pid)
    wrapper_running = _process_alive(wrapper_pid)
    runtime = _runtime_status(root / "data" / "runtime")
    running = bot_running or wrapper_running or runtime["fresh"]
    last_ts, last_line = _last_log_line(root / "data" / "bot.log")
    kill_on = (root / "data" / "KILL").exists()

    day_snaps = [s for s in snaps if _in_day(str(s.get("ts") or ""), day, tz)]
    a_fills = analytics.fills_for_day(analytics.fills_from_rows(
        [{k: getattr(o, k, None) for k in ("id", "ts", "market_slug", "asset", "horizon", "side", "price", "contracts",
          "cost_usd", "fee_usd", "model_prob", "edge_after_fees", "status", "result", "pnl_usd", "window_end",
          "settlement_price", "price_to_beat", "reject_reason")} for o in fills], tz), day, tz)
    kpis = analytics.compute_kpis(a_fills, start, cfg.daily_profit_target_pct, cfg.daily_loss_limit_pct)
    step_curve = analytics.equity_curve(a_fills, start, now=now.timestamp())
    equity_points = []
    for s in day_snaps:
        eq = float(s["equity"]) if s.get("equity") is not None else float(s.get("cash") or 0) + float(s.get("open_exposure") or 0)
        equity_points.append({
            "ts": s["ts"],
            "time_et": _fmt_et(str(s["ts"]), tz, with_seconds=False),
            "time_et_full": _fmt_et(str(s["ts"]), tz, with_seconds=True),
            "equity": round(eq, 2),
            "cash": round(float(s.get("cash") or 0), 2),
            "open_exposure": round(float(s.get("open_exposure") or 0), 2),
            "realized_pnl": round(float(s.get("realized_pnl") or 0), 2),
            "open_positions": int(s.get("open_positions") or 0),
        })

    n = len(equity_points)
    label_idx = set()
    if n:
        label_idx.add(0)
        label_idx.add(n - 1)
        if n > 2:
            steps = min(4, n - 2)
            for i in range(1, steps + 1):
                label_idx.add(round(i * (n - 1) / (steps + 1)))
    chart_points = []
    for i, p in enumerate(step_curve):
        chart_points.append({
            "label": analytics.fmt_et(p["ts"], tz, seconds=False),
            "value": p["equity"],
            "ts": analytics.iso_local(datetime.fromtimestamp(p["ts"], ZoneInfo(tz)), tz),
            "kind": p.get("kind"),
            "bet": p.get("label"),
        })

    eq_vals = [p["equity"] for p in step_curve] or [equity]
    eq_min, eq_max = min(eq_vals), max(eq_vals)
    # Keep the daily loss limit and a bit of headroom in view when possible.
    floor = min(start - loss_lim, eq_min)
    ceiling = max(start + target, eq_max)
    pad = max(10.0, (ceiling - floor) * 0.08)
    floor = max(0.0, floor - pad)
    ceiling = ceiling + pad

    def fill_row(o: PaperOrder) -> dict:
        return {
            "id": o.id,
            "ts": o.ts,
            "time_et": _fmt_et(o.ts, tz),
            "asset": (o.asset or "?").upper(),
            "horizon": o.horizon or "?",
            "side": (o.side or "").capitalize(),
            "price": o.price,
            "price_display": f"${o.price:.3f}" if o.price is not None else "—",
            "contracts": o.contracts,
            "contracts_display": f"{o.contracts:.2f}" if o.contracts is not None else "—",
            "fee_usd": o.fee_usd,
            "fee_display": _money(o.fee_usd),
            "cost_usd": o.cost_usd,
            "cost_display": _money(o.cost_usd),
            "model_prob": o.model_prob,
            "model_prob_display": f"{o.model_prob * 100:.1f}%" if o.model_prob is not None else "—",
            "edge_after_fees": o.edge_after_fees,
            "edge_display": f"{o.edge_after_fees:.3f}" if o.edge_after_fees is not None else "—",
            "status": o.status,
            "result": "Open" if o.status == "open" else (o.result or o.status or "—").capitalize(),
            "pnl_usd": o.pnl_usd,
            "pnl_display": _money(o.pnl_usd, signed=True) if o.status == "settled" and o.pnl_usd is not None else "—",
            "settlement_price": o.settlement_price,
            "price_to_beat": o.price_to_beat,
            "window_end": o.window_end,
            "window_end_et": _fmt_et(o.window_end, tz, with_seconds=False) if o.window_end else None,
            "market_slug": o.market_slug,
        }

    by: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"fills": 0, "settled": 0, "wins": 0, "losses": 0, "open": 0, "fees": 0.0, "net": 0.0}
    )
    for o in fills:
        row = by[(o.asset or "?", o.horizon or "?")]
        row["fills"] += 1
        row["fees"] += o.fee_usd or 0
        if o.status == "open":
            row["open"] += 1
        elif o.status == "settled":
            row["settled"] += 1
            row["net"] += o.pnl_usd or 0
            if o.result == "win":
                row["wins"] += 1
            elif o.result == "loss":
                row["losses"] += 1
    by_rows = []
    for (asset, hz), r in sorted(by.items()):
        wr = (r["wins"] / r["settled"]) if r["settled"] else None
        by_rows.append({
            "asset": asset.upper(),
            "horizon": hz,
            "fills": r["fills"],
            "open": r["open"],
            "settled": r["settled"],
            "wins": r["wins"],
            "losses": r["losses"],
            "win_rate_display": f"{wr * 100:.0f}%" if wr is not None else "—",
            "fees": round(r["fees"], 2),
            "fees_display": _money(r["fees"]),
            "net": round(r["net"], 2),
            "net_display": _money(r["net"], signed=True) if r["settled"] else "—",
        })

    skip_counts: list[dict] = []
    eval_path = cfg.evaluations_csv
    eval_total = 0
    eval_passed = 0
    if eval_path.exists():
        with eval_path.open() as f:
            for row in csv.DictReader(f):
                if not _in_day(row.get("ts"), day, tz):
                    continue
                eval_total += 1
                if str(row.get("passed")) in {"1", "True", "true"}:
                    eval_passed += 1
                    continue
                key = _skip_reason_key(row.get("reasons") or "")
                # friendly labels
                skip_counts.append(key)  # type: ignore
        c = Counter(skip_counts)
        skip_counts = [
            {"reason": k, "count": v, "share_display": f"{v / max(eval_total - eval_passed, 1) * 100:.0f}% of skips"}
            for k, v in c.most_common(10)
        ]

    return {
        "paper_only": True,
        "as_of_et": as_of,
        "day": day.isoformat(),
        "timezone": tz,
        "strategy": cfg.strategy_name,
        "stop_at": cfg.stop_at,
        "bot": {
            "running": running,
            "status_label": "Running" if running else "Stopped",
            "bot_pid": bot_pid,
            "wrapper_pid": wrapper_pid,
            "bot_process_alive": bot_running,
            "wrapper_process_alive": wrapper_running,
            "last_log_et": last_ts,
            "last_log_line": last_line,
            "kill_switch_on": kill_on,
            "mode": "paper",
        },
        "metrics": {
            "starting_equity": start,
            "starting_equity_display": _money(start),
            "equity": round(equity, 2),
            "equity_display": _money(equity),
            "realized_pnl": round(realized, 2),
            "realized_pnl_display": _money(realized, signed=True),
            "realized_pnl_pct": round((realized / start * 100) if start else 0.0, 2),
            "realized_pnl_pct_display": _pct((realized / start * 100) if start else None),
            "win_rate": win_rate,
            "win_rate_display": f"{win_rate * 100:.0f}%" if win_rate is not None else "—",
            "wins": len(wins),
            "losses": len(losses),
            "fills": len(fills),
            "settled": len(settled),
            "open": len(open_pos),
            "fees_all": round(fees_all, 2),
            "fees_all_display": _money(fees_all),
            "fees_settled": round(fees_settled, 2),
            "fees_settled_display": _money(fees_settled),
            "gross_settled": round(gross_settled, 2),
            "gross_settled_display": _money(gross_settled, signed=True),
            "open_cost": round(open_cost, 2),
            "open_cost_display": _money(open_cost),
            "max_drawdown": 0.0,  # filled below
            "max_drawdown_display": "$0.00",
        },
        "risk": {
            "profit_target_pct": cfg.daily_profit_target_pct,
            "loss_limit_pct": cfg.daily_loss_limit_pct,
            "profit_target_usd": target,
            "loss_limit_usd": loss_lim,
            "profit_target_display": f"+{cfg.daily_profit_target_pct:.0%} ({_money(target)})",
            "loss_limit_display": f"-{cfg.daily_loss_limit_pct:.0%} ({_money(-loss_lim, signed=True)})",
            "halt": halt,
            "halt_label": halt_label,
            "halt_detail": halt_detail,
        },
        "equity_curve": {
            "points": chart_points,
            "floor": round(floor, 2),
            "ceiling": round(ceiling, 2),
            "start": start,
            "target": start + target,
            "loss_limit": start - loss_lim,
            "snapshot_count": n,
            "points_source": "ledger settlements (one step per settled bet)",
        },
        "kpis": kpis,
        "runtime": runtime,
        "fills": [fill_row(o) for o in fills],
        "open_positions": [fill_row(o) for o in open_pos],
        "by_asset_horizon": by_rows,
        "evaluations": {
            "total": eval_total,
            "passed": eval_passed,
            "skipped": eval_total - eval_passed,
            "top_skip_reasons": skip_counts,
        },
        "source": {
            "sqlite": str(db),
            "evaluations_csv": str(cfg.evaluations_csv),
            "bot_log": str(root / "data" / "bot.log"),
        },
    }


def _fill_drawdown(snap: dict) -> None:
    vals = [p["value"] for p in snap["equity_curve"]["points"]]
    if not vals:
        return
    peak = vals[0]
    max_dd = 0.0
    for v in vals:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
    snap["metrics"]["max_drawdown"] = round(max_dd, 2)
    snap["metrics"]["max_drawdown_display"] = _money(max_dd)


def main() -> int:
    args = _parse_args()
    cfg = load_config(args.config)
    if args.db:
        # storage paths are on the config object; caller can still pass --db
        object.__setattr__(cfg, "raw", {**cfg.raw, "storage": {**(cfg.raw.get("storage") or {}), "sqlite_path": args.db}})
        # simpler: just use the path directly via a local override in build
    day = date.fromisoformat(args.day) if args.day else datetime.now(ZoneInfo(cfg.timezone)).date()
    # If --db was set, temporarily point sqlite by constructing after load
    if args.db:
        from dataclasses import replace
        # Config is frozen; pass path into build by monkeypatching property use
        cfg_for_build = cfg
        orig = cfg.sqlite_path
        # We'll just open that db by swapping after a small wrapper
        class _Cfg:
            def __getattr__(self, name):
                if name == "sqlite_path":
                    return Path(args.db)
                return getattr(cfg, name)
        cfg_for_build = _Cfg()
    else:
        cfg_for_build = cfg
    root = Path(args.config).resolve().parent if Path(args.config).exists() else ROOT
    # config.root is the repo root when loaded from /workspace/polymarket-bot/config.yaml
    root = cfg.root if hasattr(cfg, "root") else root
    snap = build_snapshot(cfg_for_build, day, root)
    _fill_drawdown(snap)
    text = json.dumps(snap, indent=2 if args.pretty else None, default=str)
    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n")
        print(f"Wrote {out}", file=sys.stderr)
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
