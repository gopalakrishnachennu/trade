"""Compose a dashboard payload from the runtime bus. Status comes from data age + latch."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .bus import Bus
from .interlock import HaltLatch
from .watchdog import DEFAULT_STALE, Watchdog

ET = ZoneInfo("America/New_York")
RULES_CATALOG = [
    ("price_to_beat_known", "Strike (priceToBeat) must be stamped before a trade.", "require_price_to_beat"),
    ("window_timing", "No trade in the first seconds of a window or the last seconds before expiry.", "min_seconds_into_window / no_trade_final_seconds"),
    ("max_spread", "Up/Down spread cannot exceed the configured cap.", "max_spread"),
    ("min_liquidity", "Book must have enough size in contracts and dollars.", "min_liquidity_contracts / min_liquidity_usd"),
    ("ask_bounds", "Ask must sit inside the configured price band.", "min_ask / max_ask"),
    ("min_edge_after_fees", "Model probability must beat the ask by the fee-adjusted edge.", "min_edge_after_fees"),
]


def _payload(bus: Bus, name: str) -> dict:
    snap = bus.snapshot(name)
    if not snap:
        return {}
    return snap.get("payload") or {}



def _parse_iso(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _select_grid(rows: list, now: float) -> list:
    """One row per asset x horizon: live window if any, else the next, never a stale ended one when a successor exists."""
    now_dt = datetime.fromtimestamp(now, timezone.utc)
    groups: dict[tuple, list] = {}
    order = []
    for r in rows:
        key = (str(r.get("asset") or "").lower(), str(r.get("horizon") or ""))
        if key not in groups:
            order.append(key)
        groups.setdefault(key, []).append(r)
    out = []
    for key in order:
        cands = groups[key]
        live, future, ended = [], [], []
        for r in cands:
            start, end = _parse_iso(r.get("window_start")), _parse_iso(r.get("window_end"))
            if start and end and start <= now_dt < end:
                live.append(r)
            elif start and start > now_dt:
                future.append((start, r))
            else:
                ended.append(r)
        if live:
            chosen = live[0]
        elif future:
            future.sort(key=lambda x: x[0])
            chosen = dict(future[0][1])
            chosen["status"] = chosen.get("status") or "awaiting_next"
        elif ended:
            chosen = dict(ended[-1])
            chosen["status"] = "awaiting_next"
        else:
            continue
        end = _parse_iso(chosen.get("window_end"))
        if end:
            chosen["seconds_remaining"] = (end - now_dt).total_seconds()
        out.append(chosen)
    return out


def compose(bus: Bus, latch: HaltLatch, cfg=None, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    halt = latch.read()
    health = Watchdog(bus, latch, assets=getattr(cfg, "assets", ("btc", "eth", "sol", "xrp")),
                      horizons=getattr(cfg, "horizons", ("5m", "15m", "1h"))).health(now)
    stale_any = any(h.stale and h.component != "watchdog" for h in health)
    # Status is derived from data age and the latch, NEVER from "is a pid alive".
    if halt.halted:
        system = "HALTED"
        system_reason = f"{halt.reason} ({halt.component}): {halt.details}".strip()
    elif stale_any:
        system = "STALE"
        bad = [h for h in health if h.stale and h.component != "watchdog"]
        system_reason = ", ".join(f"{h.component} {h.age_seconds}s" for h in bad)
    else:
        system = "TRADING"
        system_reason = ""

    discovery = _payload(bus, "discovery")
    indexes_p = _payload(bus, "indexes")
    books_p = _payload(bus, "books")
    engine = _payload(bus, "engine")
    execution = _payload(bus, "execution")

    idx = indexes_p.get("indexes") or {}
    raw_hist = indexes_p.get("history") or {}
    history = {k: (v or []) if isinstance(v, list) else v for k,v in raw_hist.items()}
    # Prefer engine grid, then discovery windows; always collapse to the live
    # (or next) window so an ended row cannot linger as "current".
    grid = engine.get("grid") or discovery.get("windows") or []
    if discovery.get("windows"):
        # Merge so a successor already known to discovery can replace an ended engine row.
        grid = list(grid) + list(discovery.get("windows") or [])
    grid = _select_grid(grid, now)
    books = books_p.get("books") or {}
    rows = []
    for row in grid:
        r = dict(row)
        slug = r.get("slug")
        if slug and slug in books and "up_ask" not in r:
            r["up_ask"] = books[slug].get("up_ask")
            r["down_ask"] = books[slug].get("down_ask")
            r["spread"] = books[slug].get("spread")
        asset = (r.get("asset") or "").lower()
        if r.get("index") is None and asset in idx:
            r["index"] = (idx[asset] or {}).get("price")
        strike = r.get("price_to_beat")
        if r.get("distance") is None and strike is not None and r.get("index") is not None:
            try:
                r["distance"] = float(r["index"]) - float(strike)
            except (TypeError, ValueError):
                r["distance"] = None
        r["stale"] = system != "TRADING"
        rows.append(r)

    # Freshness of each snapshot
    snap_ages = {}
    for name in ("discovery", "indexes", "books", "engine", "execution"):
        s = bus.snapshot(name)
        snap_ages[name] = None if not s else round(now - float(s.get("ts_unix") or now), 2)

    as_of = datetime.fromtimestamp(now, ET).strftime("%Y-%m-%d %I:%M:%S %p %Z")
    data_ts = None
    for name in ("indexes", "engine", "discovery"):
        s = bus.snapshot(name)
        if s and s.get("ts_unix"):
            data_ts = max(data_ts or 0, float(s["ts_unix"]))
    data_age = None if data_ts is None else round(now - data_ts, 2)

    rules = []
    if cfg is not None:
        rules = [
            {"name": "price_to_beat_known", "threshold": bool(getattr(cfg, "require_price_to_beat", True)), "unit": "required"},
            {"name": "min_seconds_into_window", "threshold": getattr(cfg, "min_seconds_into_window", None), "unit": "seconds"},
            {"name": "no_trade_final_seconds", "threshold": getattr(cfg, "no_trade_final_seconds", None), "unit": "seconds"},
            {"name": "max_spread", "threshold": getattr(cfg, "max_spread", None), "unit": "price"},
            {"name": "min_liquidity_contracts", "threshold": getattr(cfg, "min_liquidity_contracts", None), "unit": "contracts"},
            {"name": "min_liquidity_usd", "threshold": getattr(cfg, "min_liquidity_usd", None), "unit": "usd"},
            {"name": "min_ask", "threshold": getattr(cfg, "min_ask", None), "unit": "price"},
            {"name": "max_ask", "threshold": getattr(cfg, "max_ask", None), "unit": "price"},
            {"name": "min_edge_after_fees", "threshold": getattr(cfg, "min_edge", None), "unit": "edge"},
            {"name": "kelly_fraction", "threshold": getattr(cfg, "kelly_fraction", None), "unit": "fraction"},
            {"name": "daily_profit_target_pct", "threshold": getattr(cfg, "daily_profit_target_pct", None), "unit": "pct"},
            {"name": "daily_loss_limit_pct", "threshold": getattr(cfg, "daily_loss_limit_pct", None), "unit": "pct"},
        ]

    return {
        "ts_unix": now,
        "as_of_et": as_of,
        "data_ts_unix": data_ts,
        "data_age_seconds": data_age,
        "paper_only": True,
        "system": system,
        "system_reason": system_reason,
        "halt": halt.as_dict(),
        "health": [
            {"component": h.component, "present": h.present, "age_seconds": h.age_seconds,
             "stale": h.stale, "ok": h.ok, "status": h.status, "threshold": h.threshold, "seq": h.seq}
            for h in health
        ],
        "snapshot_ages": snap_ages,
        "indexes": idx,
        "history": history,
        "grid": rows,
        "signals": [],
        # Keep the public snapshot lean so the tunnel can refresh every second.
        "recent_decisions": (engine.get("recent_decisions") or [])[-60:],
        "pnl": execution.get("pnl") or {},
        "open_positions": execution.get("open_positions") or [],
        "fills": execution.get("fills") or [],
        "equity_curve": execution.get("curve") or [],
        "events": bus.recent_events(250),
        "rules": rules,
        "rules_catalog": [
            {"name": n, "summary": s, "config_key": k} for n, s, k in RULES_CATALOG
        ],
        "stale_thresholds": DEFAULT_STALE,
    }
