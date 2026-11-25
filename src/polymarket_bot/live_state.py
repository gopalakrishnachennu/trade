"""Atomic live_state.json snapshot for the paper dashboard.

Written at most once per second from the bot loop. The file is replaced
with os.replace so the dashboard never reads a partial write.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .config import Config
from .discovery import current_market, expected_slug, horizon_minutes, next_market, window_start_utc
from .feeds import BookFeed, PriceFeed
from .models import Book, Market, PaperOrder, Signal
from .risk import RiskManager, RiskState, order_date_in_tz, today_in_tz
from .store import Store


def _finite(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _round(x: Optional[float], n: int = 4) -> Optional[float]:
    v = _finite(x)
    return None if v is None else round(v, n)


def write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="live_state.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, separators=(",", ":"), default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _fill_dict(order: PaperOrder, tz: str, book: Optional[Book] = None, now: Optional[datetime] = None) -> dict:
    mark_price = book.mark_price(order.side) if book is not None else None
    mark_value = None
    unrealized = None
    if mark_price is not None and order.contracts is not None:
        mark_value = round(order.contracts * mark_price, 4)
        if order.cost_usd is not None:
            unrealized = round(mark_value - order.cost_usd, 4)
    window_end = order.window_end
    rem = None
    if window_end:
        try:
            end = datetime.fromisoformat(str(window_end).replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            rem = (end - (now or datetime.now(timezone.utc))).total_seconds()
        except ValueError:
            rem = None
    return {
        "id": order.id,
        "ts": order.ts,
        "asset": order.asset,
        "horizon": order.horizon,
        "side": order.side,
        "market_slug": order.market_slug,
        "price": _round(order.price, 4),
        "contracts": _round(order.contracts, 4),
        "cost_usd": _round(order.cost_usd, 2),
        "fee_usd": _round(order.fee_usd, 2),
        "model_prob": _round(order.model_prob, 4),
        "edge_after_fees": _round(order.edge_after_fees, 4),
        "status": order.status,
        "result": order.result,
        "pnl_usd": _round(order.pnl_usd, 2),
        "settlement_price": _round(order.settlement_price, 6) if order.settlement_price is not None else None,
        "window_end": window_end,
        "seconds_remaining": _round(rem, 1),
        "mark_price": _round(mark_price, 4),
        "mark_value": _round(mark_value, 2),
        "unrealized_pnl": _round(unrealized, 2),
    }


def build_live_state(
    cfg: Config,
    *,
    markets: list[Market],
    prices: PriceFeed,
    books: BookFeed,
    signals_by_slug: dict[str, list[Signal]],
    store: Store,
    risk: RiskManager,
    state: RiskState,
    cash: float,
    pid: int,
    loops: int,
    stop_at: Optional[str],
    wrapper_pid: Optional[int] = None,
) -> dict[str, Any]:
    tz = cfg.timezone
    now = datetime.now(ZoneInfo(tz))
    now_utc = datetime.now(timezone.utc)
    day = today_in_tz(tz)
    halt = risk.daily_halt_reason(state) or risk.kill_switch_on() or state.kill_reason
    halt_kind = None
    if halt:
        if "profit target" in halt:
            halt_kind = "profit_target"
        elif "loss limit" in halt:
            halt_kind = "loss_limit"
        elif "kill switch" in halt:
            halt_kind = "kill_switch"
        else:
            halt_kind = "halt"

    indexes: dict[str, Any] = {}
    for asset in cfg.assets:
        pt = prices.latest(asset)
        if not pt:
            continue
        indexes[asset] = {
            "price": _round(pt.close, 6),
            "ts": pt.ts,
            "tick_count": pt.tick_count,
        }

    windows: list[dict] = []
    for asset in cfg.assets:
        for horizon in cfg.horizons:
            m = current_market(markets, asset, horizon, now_utc)
            nxt = next_market(markets, asset, horizon, now_utc)
            if m is None:
                # Never present an ended window as current. Show the live/next slot instead.
                start = window_start_utc(now_utc, horizon, 0)
                end = start + timedelta(minutes=horizon_minutes(horizon))
                slug = expected_slug(asset, horizon, start)
                # If we're somehow past end (shouldn't happen), jump to next boundary.
                if end <= now_utc:
                    start = window_start_utc(now_utc, horizon, 1)
                    end = start + timedelta(minutes=horizon_minutes(horizon))
                    slug = expected_slug(asset, horizon, start)
                if nxt is not None and nxt.window_start <= end:
                    m = nxt
                    status = "awaiting_next"
                else:
                    rem = (end - now_utc).total_seconds()
                    windows.append({
                        "asset": asset,
                        "horizon": horizon,
                        "status": "awaiting_next",
                        "slug": slug,
                        "price_to_beat": None,
                        "window_start": start.isoformat(),
                        "window_end": end.isoformat(),
                        "seconds_remaining": _round(rem, 1),
                        "up_ask": None,
                        "down_ask": None,
                        "model_prob_up": None,
                        "model_prob_down": None,
                        "edge_up": None,
                        "edge_down": None,
                        "rules_pass_up": False,
                        "rules_pass_down": False,
                        "would_trade_up": False,
                        "would_trade_down": False,
                        "block_reason": "waiting for next window",
                        "index": indexes.get(asset, {}).get("price"),
                    })
                    continue
            book = books.get(m.slug) if m.slug else None
            sigs = signals_by_slug.get(m.slug) or [] if m.slug else []
            by_side = {s.side: s for s in sigs}
            up = by_side.get("up")
            down = by_side.get("down")
            block = halt
            started = m.window_start <= now_utc
            rem = m.seconds_remaining(now_utc)
            if not started:
                status = "awaiting_next"
            elif rem <= 0:
                status = "awaiting_next"
            elif m.price_to_beat is None or book is None or (book.up_ask is None and book.down_ask is None):
                status = "waiting_book"
            else:
                status = "open"
            lag = books.lag_seconds(m.slug) if m.slug and hasattr(books, "lag_seconds") else None
            windows.append({
                "asset": asset,
                "horizon": horizon,
                "status": status,
                "slug": m.slug or None,
                "price_to_beat": _round(m.price_to_beat, 6),
                "window_start": m.window_start.isoformat(),
                "window_end": m.window_end.isoformat(),
                "seconds_remaining": _round(max(rem, 0.0) if started else (m.window_start - now_utc).total_seconds(), 1),
                "up_ask": _round(book.up_ask if book else None, 4),
                "down_ask": _round(book.down_ask if book else None, 4),
                "best_bid": _round(book.best_bid if book else None, 4),
                "spread": _round(book.spread if book else None, 4),
                "index": indexes.get(asset, {}).get("price"),
                "model_prob_up": _round(up.model_prob if up else None, 4),
                "model_prob_down": _round(down.model_prob if down else None, 4),
                "edge_up": _round(up.edge_after_fees if up else None, 4),
                "edge_down": _round(down.edge_after_fees if down else None, 4),
                "rules_pass_up": bool(up.passed) if up else False,
                "rules_pass_down": bool(down.passed) if down else False,
                "would_trade_up": bool(up.passed) and not block if up else False,
                "would_trade_down": bool(down.passed) and not block if down else False,
                "block_reason": (
                    "waiting for next window" if status == "awaiting_next"
                    else "waiting for book" if status == "waiting_book"
                    else block or ("; ".join(up.reasons) if up and not up.passed and down and not down.passed else None)
                ),
                "reasons_up": list(up.reasons) if up else [],
                "reasons_down": list(down.reasons) if down else [],
                "quote_lag_seconds": lag,
                "quote_source": getattr(books, "quote_source", {}).get(m.slug) if m.slug else None,
            })

    opens: list[dict] = []
    unrealized_total = 0.0
    have_unreal = False
    for order in list(state.open_orders.values()):
        book = books.get(order.market_slug)
        row = _fill_dict(order, tz, book, now_utc)
        opens.append(row)
        if row["unrealized_pnl"] is not None:
            unrealized_total += row["unrealized_pnl"]
            have_unreal = True
    unrealized = round(unrealized_total, 2) if have_unreal or not opens else None
    if opens and not have_unreal:
        unrealized = 0.0  # no marks yet; treat as flat rather than dropping the field

    fills: list[dict] = []
    for order in store.all_orders():
        if order.status not in ("open", "settled"):
            continue
        if order_date_in_tz(order.ts, tz) != day:
            continue
        book = books.get(order.market_slug) if order.status == "open" else None
        fills.append(_fill_dict(order, tz, book, now_utc))

    start = state.starting_day_equity
    realized = round(state.realized_pnl, 2)
    unreal = 0.0 if unrealized is None else unrealized
    equity = round(start + realized + unreal, 2)
    target = risk.profit_target_usd(state)
    loss = risk.loss_limit_usd(state)

    return {
        "ts": now.isoformat(),
        "ts_unix": time.time(),
        "as_of_et": now.strftime("%Y-%m-%d %I:%M:%S %p %Z"),
        "paper_only": True,
        "mode": "paper",
        "bot": {
            "running": True,
            "pid": pid,
            "wrapper_pid": wrapper_pid,
            "loop": loops,
            "stop_at": stop_at,
            "strategy": cfg.strategy_name,
            "kill_switch": bool(risk.kill_switch_on()),
            "halt": halt_kind,
            "halt_label": halt,
        },
        "indexes": indexes,
        "windows": windows,
        "pnl": {
            "starting_equity": start,
            "cash": round(cash, 2),
            "realized": realized,
            "unrealized": round(unreal, 2),
            "equity": equity,
            "open_cost": round(state.open_exposure, 2),
            "open_count": state.open_count,
            "target_usd": target,
            "limit_usd": loss,
            "target_pct": cfg.daily_profit_target_pct,
            "limit_pct": cfg.daily_loss_limit_pct,
            "target_hit": realized + 1e-9 >= target,
            "limit_hit": realized - 1e-9 <= -loss,
            "halt": halt_kind,
            "halt_label": halt,
        },
        "fills": fills,
        "open_positions": opens,
        "quote_lag": books.lag_report() if hasattr(books, "lag_report") else [],
    }


class LiveStateWriter:
    """Rate-limited atomic writer so the bot loop is not delayed."""

    def __init__(self, path: Path, min_interval: float = 1.0):
        self.path = path
        self.min_interval = min_interval
        self._last = 0.0

    def maybe_write(self, payload: dict) -> None:
        now = time.monotonic()
        if now - self._last < self.min_interval:
            return
        write_atomic(self.path, payload)
        self._last = now
