#!/usr/bin/env python3
"""Summarize a paper-trading day from SQLite (and optional CSVs)."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polymarket_bot.config import load_config  # noqa: E402
from polymarket_bot.models import PaperOrder  # noqa: E402
from polymarket_bot.risk import order_date_in_tz  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Daily paper-trading report")
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    p.add_argument("--db", default=None, help="Override SQLite path")
    p.add_argument("--day", default=None, help="YYYY-MM-DD in the config timezone (default: today)")
    p.add_argument("--out", default=None, help="Also write the report to this path")
    return p.parse_args()


def _load_orders(db: Path) -> tuple[list[PaperOrder], list[dict]]:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    rows = list(con.execute("SELECT * FROM paper_orders ORDER BY ts"))
    equity = list(con.execute("SELECT ts, cash, open_exposure, realized_pnl, equity, open_positions FROM equity ORDER BY ts"))
    con.close()
    orders = [PaperOrder(**dict(r)) for r in rows]
    snaps = [dict(r) for r in equity]
    return orders, snaps


def _in_day(ts: str | None, day: date, tz: str) -> bool:
    d = order_date_in_tz(ts or "", tz)
    return d == day


def _fmt_money(x: float | None) -> str:
    if x is None:
        return "—"
    return f"${x:,.2f}"


def build_report(cfg, day: date, orders: list[PaperOrder], snaps: list[dict]) -> str:
    tz = cfg.timezone
    start = cfg.starting_day_equity
    target = cfg.daily_profit_target_usd
    loss_lim = cfg.daily_loss_limit_usd
    day_orders = [o for o in orders if _in_day(o.ts, day, tz)]
    fills = [o for o in day_orders if o.status in ("open", "settled")]
    settled = [o for o in fills if o.status == "settled"]
    open_pos = [o for o in fills if o.status == "open"]
    rejected = [o for o in day_orders if o.status == "rejected"]

    fees_all = sum(o.fee_usd or 0 for o in fills)
    notional = sum((o.contracts or 0) * (o.price or 0) for o in fills)
    gross_settled = sum((o.payout_usd or 0) - (o.contracts or 0) * (o.price or 0) for o in settled)
    fees_settled = sum(o.fee_usd or 0 for o in settled)
    net_settled = sum(o.pnl_usd or 0 for o in settled)
    realized = net_settled
    open_cost = sum(o.cost_usd or 0 for o in open_pos)
    wins = [o for o in settled if o.result == "win"]
    losses = [o for o in settled if o.result == "loss"]
    win_rate = (len(wins) / len(settled) * 100) if settled else None

    by: dict[tuple[str, str], dict] = defaultdict(lambda: {"fills": 0, "settled": 0, "wins": 0, "losses": 0, "fees": 0.0, "net": 0.0, "open": 0})
    for o in fills:
        key = (o.asset or "?", o.horizon or "?")
        row = by[key]
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

    # Max drawdown on settled equity (starting + realized), from snapshots in the day.
    day_snaps = [s for s in snaps if _in_day(str(s.get("ts") or ""), day, tz)]
    equity_path = []
    if day_snaps:
        for s in day_snaps:
            if s.get("equity") is not None:
                equity_path.append(float(s["equity"]))
            else:
                equity_path.append(float(s["cash"] or 0) + float(s["open_exposure"] or 0))
    else:
        equity_path = [start + realized]
    running_peak = equity_path[0]
    peak = equity_path[0]
    trough = equity_path[0]
    max_dd = 0.0
    for eq in equity_path:
        if eq > running_peak:
            running_peak = eq
        dd = running_peak - eq
        if dd > max_dd:
            max_dd = dd
            peak = running_peak
            trough = eq
    if max_dd == 0.0:
        peak = running_peak
    max_dd_pct = (max_dd / peak * 100) if peak else 0.0

    target_hit = realized + 1e-9 >= target
    loss_hit = realized - 1e-9 <= -loss_lim
    halt = "profit target" if target_hit else ("loss limit" if loss_hit else "no")

    lines = []
    a = lines.append
    a(f"Paper trading daily report")
    a(f"Day: {day.isoformat()} ({tz})")
    a(f"Strategy: {cfg.strategy_name}")
    a(f"Starting day equity: {_fmt_money(start)}")
    a(f"Daily profit target: +{cfg.daily_profit_target_pct:.0%} = {_fmt_money(target)}")
    a(f"Daily loss limit:    -{cfg.daily_loss_limit_pct:.0%} = {_fmt_money(loss_lim)}")
    a(f"Target/limit hit: {halt}")
    a("")
    a("Activity")
    a(f"  Fills: {len(fills)}   Settled: {len(settled)}   Open: {len(open_pos)}   Rejected rows: {len(rejected)}")
    if settled:
        a(f"  Wins: {len(wins)}   Losses: {len(losses)}   Win rate: {win_rate:.1f}%")
    else:
        a("  Wins/losses: none settled yet")
    a("")
    a("P&L (settled trades)")
    a(f"  Gross (payout − price*contracts): {_fmt_money(gross_settled)}")
    a(f"  Fees on settled:                  {_fmt_money(fees_settled)}")
    a(f"  Net realized P&L:                 {_fmt_money(net_settled)}  ({(realized/start*100) if start else 0:+.2f}% of starting equity)")
    a(f"  Fees on all fills (incl. open):   {_fmt_money(fees_all)}")
    a(f"  Open cost still at risk:          {_fmt_money(open_cost)}")
    a(f"  Equity (start + realized):        {_fmt_money(start + realized)}")
    a(f"  Max drawdown (equity snapshots):  {_fmt_money(max_dd)}  ({max_dd_pct:.2f}% from peak {_fmt_money(peak)})")
    a("")
    a("By asset / horizon")
    if not by:
        a("  (no fills)")
    else:
        a(f"  {'asset':<6} {'hz':<4} {'fills':>6} {'open':>5} {'set':>5} {'W':>4} {'L':>4} {'win%':>7} {'fees':>10} {'net':>10}")
        for asset, hz in sorted(by):
            r = by[(asset, hz)]
            wr = f"{r['wins']/r['settled']*100:5.1f}%" if r["settled"] else "    —"
            a(
                f"  {asset:<6} {hz:<4} {r['fills']:6d} {r['open']:5d} {r['settled']:5d} "
                f"{r['wins']:4d} {r['losses']:4d} {wr:>7} {_fmt_money(r['fees']):>10} {_fmt_money(r['net']):>10}"
            )
    if open_pos:
        a("")
        a("Open positions")
        for o in open_pos:
            a(
                f"  {o.ts}  {o.asset} {o.horizon} {o.side}  {o.contracts:.2f} ct @ {o.price:.3f}  "
                f"cost={_fmt_money(o.cost_usd)}  window_end={o.window_end}  {o.market_slug}"
            )
    if settled:
        a("")
        a("Settled trades")
        for o in settled:
            a(
                f"  {o.ts}  {o.asset} {o.horizon} {o.side}  {o.contracts:.2f} ct @ {o.price:.3f}  "
                f"{o.result}  pnl={_fmt_money(o.pnl_usd)}  settle={o.settlement_price}  {o.market_slug}"
            )
    a("")
    a(f"Notional traded (price×contracts, all fills): {_fmt_money(notional)}")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = _parse_args()
    cfg = load_config(args.config)
    db = Path(args.db) if args.db else cfg.sqlite_path
    if not db.exists():
        print(f"No SQLite database at {db}", file=sys.stderr)
        return 1
    if args.day:
        day = date.fromisoformat(args.day)
    else:
        day = datetime.now(ZoneInfo(cfg.timezone)).date()
    orders, snaps = _load_orders(db)
    report = build_report(cfg, day, orders, snaps)
    sys.stdout.write(report)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report)
        print(f"Wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
