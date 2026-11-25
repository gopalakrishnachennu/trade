from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from .models import PaperOrder, Signal

TRADES_FIELDS = [
    "id", "ts", "market_slug", "asset", "horizon", "side", "contracts", "price",
    "fee_usd", "cost_usd", "model_prob", "edge_after_fees", "price_to_beat",
    "window_end", "status", "settlement_price", "result", "payout_usd", "pnl_usd",
    "reject_reason",
]

EVAL_FIELDS = [
    "ts", "market_slug", "asset", "horizon", "side", "passed", "model_prob",
    "ask", "edge_after_fees", "spread", "liquidity_contracts", "spot",
    "price_to_beat", "seconds_remaining", "annualized_vol", "reasons",
]


from .archive import append_csv_locked


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


class Store:
    def __init__(self, sqlite_path: Path, trades_csv: Path, evaluations_csv: Path, equity_csv: Path):
        self.sqlite_path = sqlite_path
        self.trades_csv = trades_csv
        self.evaluations_csv = evaluations_csv
        self.equity_csv = equity_csv
        _ensure_parent(sqlite_path)
        _ensure_parent(trades_csv)
        _ensure_parent(evaluations_csv)
        _ensure_parent(equity_csv)
        self.conn = sqlite3.connect(str(sqlite_path))
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_orders (
                id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                market_slug TEXT NOT NULL,
                asset TEXT,
                horizon TEXT,
                side TEXT,
                contracts REAL,
                price REAL,
                fee_usd REAL,
                cost_usd REAL,
                model_prob REAL,
                edge_after_fees REAL,
                price_to_beat REAL,
                window_end TEXT,
                status TEXT,
                settlement_price REAL,
                result TEXT,
                payout_usd REAL,
                pnl_usd REAL,
                reject_reason TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS equity (
                ts TEXT NOT NULL,
                cash REAL,
                open_exposure REAL,
                realized_pnl REAL,
                equity REAL,
                open_positions INTEGER
            )
            """
        )
        self.conn.commit()
        self._init_csv(self.trades_csv, TRADES_FIELDS)
        self._init_csv(self.evaluations_csv, EVAL_FIELDS)
        self._init_csv(self.equity_csv, ["ts", "cash", "open_exposure", "realized_pnl", "equity", "open_positions"])

    def _init_csv(self, path: Path, fields: list[str]) -> None:
        if not path.exists():
            with path.open("w", newline="") as f:
                csv.DictWriter(f, fieldnames=fields).writeheader()

    def _order_row(self, order: PaperOrder) -> dict:
        return {k: getattr(order, k) for k in TRADES_FIELDS}

    def write_order(self, order: PaperOrder) -> None:
        row = self._order_row(order)
        cols = ",".join(TRADES_FIELDS)
        placeholders = ",".join(["?"] * len(TRADES_FIELDS))
        updates = ",".join(f"{k}=excluded.{k}" for k in TRADES_FIELDS if k != "id")
        self.conn.execute(
            f"INSERT INTO paper_orders ({cols}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {updates}",
            [row[k] for k in TRADES_FIELDS],
        )
        self.conn.commit()
        append_csv_locked(self.trades_csv, TRADES_FIELDS, row)

    def update_order(self, order: PaperOrder) -> None:
        # rewrite sqlite; append a settlement snapshot row to CSV so the file is an audit log
        self.write_order(order)

    def write_evaluation(self, ts: str, signal: Signal) -> None:
        row = {
            "ts": ts,
            "market_slug": signal.market_slug,
            "asset": signal.asset,
            "horizon": signal.horizon,
            "side": signal.side,
            "passed": int(signal.passed),
            "model_prob": signal.model_prob,
            "ask": signal.ask,
            "edge_after_fees": signal.edge_after_fees,
            "spread": signal.spread,
            "liquidity_contracts": signal.liquidity_contracts,
            "spot": signal.spot,
            "price_to_beat": signal.price_to_beat,
            "seconds_remaining": signal.seconds_remaining,
            "annualized_vol": signal.annualized_vol,
            "reasons": " | ".join(signal.reasons),
        }
        append_csv_locked(self.evaluations_csv, EVAL_FIELDS, row)

    def write_equity(self, ts: str, cash: float, open_exposure: float, realized_pnl: float, open_positions: int) -> None:
        equity = cash + open_exposure  # cash already net of open costs; exposure is at cost
        # true mark-to-market would need live mids; paper equity here is cash + cost basis of opens
        # After a fill, cash decreases by cost, so equity stays flat until settlement. Good.
        # cash already has open costs deducted, so cash + open_exposure
        # equals starting equity + realized P&L (flat until a settlement).
        row = {
            "ts": ts,
            "cash": cash,
            "open_exposure": open_exposure,
            "realized_pnl": realized_pnl,
            "equity": cash + open_exposure,
            "open_positions": open_positions,
        }
        self.conn.execute(
            "INSERT INTO equity (ts, cash, open_exposure, realized_pnl, equity, open_positions) VALUES (?,?,?,?,?,?)",
            [row["ts"], row["cash"], row["open_exposure"], row["realized_pnl"], row["equity"], row["open_positions"]],
        )
        self.conn.commit()
        append_csv_locked(self.equity_csv, list(row), row)

    def open_orders(self) -> list[PaperOrder]:
        cur = self.conn.execute("SELECT * FROM paper_orders WHERE status='open'")
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            data = dict(zip(cols, r))
            out.append(PaperOrder(**data))
        return out

    def all_orders(self) -> list[PaperOrder]:
        cur = self.conn.execute("SELECT * FROM paper_orders ORDER BY ts")
        cols = [d[0] for d in cur.description]
        return [PaperOrder(**dict(zip(cols, r))) for r in cur.fetchall()]

    def active_orders(self) -> list[PaperOrder]:
        """Open and settled orders only (rejects can number in the tens of thousands)."""
        cur = self.conn.execute("SELECT * FROM paper_orders WHERE status IN ('open','settled') ORDER BY ts")
        cols = [d[0] for d in cur.description]
        return [PaperOrder(**dict(zip(cols, r))) for r in cur.fetchall()]

    def active_order_ids(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT id FROM paper_orders WHERE status IN ('open','settled')")]

    def equity_snapshots(self) -> list[dict]:
        """Equity rows in true chronological order (ts strings mix naive and tz-aware forms)."""
        from .analytics import ts_unix
        cur = self.conn.execute("SELECT ts, cash, open_exposure, realized_pnl, equity, open_positions FROM equity")
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        rows.sort(key=lambda r: ts_unix(r["ts"]) or 0.0)
        return rows

    def close(self) -> None:
        self.conn.close()
