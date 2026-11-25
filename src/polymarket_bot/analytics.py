"""Ledger analytics for the dashboard and reports.

Every timestamp goes through parse_ts so mixed formats sort by time, never
as text. After the 13:05 ET migration the equity table held two shapes:
"2026-11-24T11:10:10-05:00" (old monolith) and "2026-11-24 17:04:42.1"
(no offset, written in local ET by the runtime worker). Sorted as strings,
the space-separated rows sorted before every "T" row, so the curve went
backwards in time.
"""

from __future__ import annotations

import math
import re
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/New_York"
FILL_STATUSES = ("open", "settled")


def parse_ts(value: Any, tz_name: str = DEFAULT_TZ) -> Optional[datetime]:
    """Return a tz-aware datetime, or None.

    Accepts ISO strings with "T" or a space, with or without an offset, "Z",
    and unix seconds (int/float or numeric strings). Naive values are local
    time in tz_name, because that is the clock the workers wrote them with.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            return None
        return datetime.fromtimestamp(float(value), timezone.utc)
    else:
        s = str(value).strip()
        try:
            return datetime.fromtimestamp(float(s), timezone.utc)
        except ValueError:
            pass
        s = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            # Trim >6 fractional digits ("…12.1234567+00:00").
            head, dot, rest = s.partition(".")
            if not dot:
                return None
            i = 0
            while i < len(rest) and rest[i].isdigit():
                i += 1
            try:
                dt = datetime.fromisoformat(f"{head}.{rest[:min(i, 6)]}{rest[i:]}")
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt


def ts_unix(value: Any, tz_name: str = DEFAULT_TZ) -> Optional[float]:
    dt = parse_ts(value, tz_name)
    return None if dt is None else dt.timestamp()


def sort_chronological(rows: Iterable[dict], key: str = "ts", tz_name: str = DEFAULT_TZ) -> list[dict]:
    """Sort by parsed time; rows with unparseable timestamps go last, in input order."""
    indexed = list(enumerate(rows))

    def k(item):
        i, row = item
        t = ts_unix(row.get(key), tz_name)
        return (t is None, t if t is not None else 0.0, i)

    return [row for _, row in sorted(indexed, key=k)]


def iso_local(dt: Optional[datetime] = None, tz_name: str = DEFAULT_TZ) -> str:
    """Timezone-aware ISO string in tz_name (what writers should store)."""
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.astimezone(ZoneInfo(tz_name)).isoformat()


def fmt_et(value: Any, tz_name: str = DEFAULT_TZ, seconds: bool = True) -> str:
    dt = parse_ts(value, tz_name)
    if dt is None:
        return "—"
    return dt.astimezone(ZoneInfo(tz_name)).strftime("%-I:%M:%S %p" if seconds else "%-I:%M %p")


# ---------------------------------------------------------------- ledger

@dataclass
class Fill:
    id: str
    ts: float              # entry, unix seconds
    market_slug: str
    asset: str
    horizon: str
    side: str
    price: float
    contracts: float
    cost_usd: float
    fee_usd: float
    model_prob: Optional[float]
    edge: Optional[float]
    status: str            # open | settled
    result: Optional[str]  # win | loss
    pnl_usd: Optional[float]
    settle_ts: Optional[float]   # window end (settlement time)
    settlement_price: Optional[float]
    price_to_beat: Optional[float]
    note: str = ""


def _f(x) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


_FLAT_AT = re.compile(r"^flattened .* at (\d{4}-\d\d-\d\dT[0-9:.+-]+)$")


def _settle_ts(r: dict, tz_name: str) -> Optional[float]:
    """Settlement time: the flatten time for operator-flattened orders, else window end."""
    m = _FLAT_AT.match(str(r.get("reject_reason") or ""))
    if m and str(r.get("status")) == "settled":
        t = ts_unix(m.group(1), tz_name)
        if t is not None:
            return t
    return ts_unix(r.get("window_end"), tz_name)


def fills_from_rows(rows: Iterable[dict], tz_name: str = DEFAULT_TZ) -> list[Fill]:
    out = []
    for r in rows:
        status = str(r.get("status") or "")
        if status not in FILL_STATUSES:
            continue
        t = ts_unix(r.get("ts"), tz_name)
        if t is None:
            continue
        out.append(Fill(
            id=str(r.get("id") or ""),
            ts=t,
            market_slug=str(r.get("market_slug") or ""),
            asset=str(r.get("asset") or "").lower(),
            horizon=str(r.get("horizon") or ""),
            side=str(r.get("side") or ""),
            price=_f(r.get("price")) or 0.0,
            contracts=_f(r.get("contracts")) or 0.0,
            cost_usd=_f(r.get("cost_usd")) or 0.0,
            fee_usd=_f(r.get("fee_usd")) or 0.0,
            model_prob=_f(r.get("model_prob")),
            edge=_f(r.get("edge_after_fees")),
            status=status,
            result=(str(r["result"]) if r.get("result") else None),
            pnl_usd=_f(r.get("pnl_usd")),
            settle_ts=_settle_ts(r, tz_name),
            settlement_price=_f(r.get("settlement_price")),
            price_to_beat=_f(r.get("price_to_beat")),
            note=str(r.get("reject_reason") or ""),
        ))
    out.sort(key=lambda f: (f.ts, f.id))
    return out


_LEDGER_CACHE: dict[str, tuple[tuple, list[dict]]] = {}


def load_order_rows(db_path: Path) -> list[dict]:
    """Fills only (open/settled). Cached on the sqlite file's mtime/size.

    Read-only URI so the dashboard can never write the ledger.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    st = os.stat(db_path)
    wal = db_path.with_name(db_path.name + "-wal")
    key = (st.st_mtime_ns, st.st_size, wal.stat().st_mtime_ns if wal.exists() else 0)
    hit = _LEDGER_CACHE.get(str(db_path))
    if hit and hit[0] == key:
        return hit[1]
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    try:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM paper_orders WHERE status IN ('open','settled')"
        )]
    finally:
        con.close()
    _LEDGER_CACHE[str(db_path)] = (key, rows)
    return rows


def day_of(t: float, tz_name: str = DEFAULT_TZ) -> date:
    return datetime.fromtimestamp(t, ZoneInfo(tz_name)).date()


def fills_for_day(fills: list[Fill], day: date, tz_name: str = DEFAULT_TZ) -> list[Fill]:
    return [f for f in fills if day_of(f.ts, tz_name) == day]


# ---------------------------------------------------------------- metrics

def _r(x: Optional[float], n: int = 2) -> Optional[float]:
    return None if x is None else round(x, n)


def equity_curve(fills: list[Fill], start_equity: float, now: Optional[float] = None,
                 day_start: Optional[float] = None) -> list[dict]:
    """Realized equity steps: one point per settlement at its settle time.

    Starts at start_equity at the first entry (or day_start), ends at `now`.
    """
    settled = sorted(
        (f for f in fills if f.status == "settled" and f.pnl_usd is not None),
        key=lambda f: ((f.settle_ts or f.ts), f.ts, f.id),
    )
    t0 = day_start
    if t0 is None:
        t0 = min((f.ts for f in fills), default=now or 0.0)
    pts = [{"ts": t0, "equity": round(start_equity, 2), "pnl": 0.0, "kind": "start"}]
    eq = start_equity
    for f in settled:
        eq += f.pnl_usd or 0.0
        t = max(f.settle_ts or f.ts, pts[-1]["ts"])
        pts.append({
            "ts": t, "equity": round(eq, 2), "pnl": round(f.pnl_usd or 0.0, 2),
            "kind": "win" if (f.pnl_usd or 0) > 0 else "loss",
            "label": f"{f.asset.upper()} {f.horizon} {f.side.upper()}", "id": f.id,
        })
    if now is not None and now > pts[-1]["ts"]:
        pts.append({"ts": now, "equity": round(eq, 2), "pnl": 0.0, "kind": "now"})
    return pts


def drawdown_series(curve: list[dict]) -> tuple[list[dict], float, float]:
    """Return (series of {ts, dd_usd, dd_pct}, max_dd_usd, max_dd_pct). dd is <= 0."""
    peak = None
    out = []
    max_usd = 0.0
    max_pct = 0.0
    for p in curve:
        v = float(p["equity"])
        peak = v if peak is None else max(peak, v)
        dd = v - peak
        pct = 0.0 if peak <= 0 else dd / peak
        out.append({"ts": p["ts"], "dd_usd": round(dd, 2), "dd_pct": round(pct, 6)})
        max_usd = min(max_usd, dd)
        max_pct = min(max_pct, pct)
    return out, round(max_usd, 2), round(max_pct, 6)


def compute_kpis(fills: list[Fill], start_equity: float, target_pct: float, loss_pct: float,
                 now: Optional[float] = None) -> dict[str, Any]:
    settled = [f for f in fills if f.status == "settled" and f.pnl_usd is not None]
    opens = [f for f in fills if f.status == "open"]
    wins = [f for f in settled if (f.pnl_usd or 0) > 0]
    losses = [f for f in settled if (f.pnl_usd or 0) <= 0]
    gross_win = sum(f.pnl_usd or 0 for f in wins)
    gross_loss = sum(f.pnl_usd or 0 for f in losses)  # <= 0
    net = gross_win + gross_loss
    staked = sum(f.cost_usd for f in fills)
    staked_settled = sum(f.cost_usd for f in settled)
    open_exposure = sum(f.cost_usd for f in opens)
    fees = sum(f.fee_usd for f in fills)
    target_usd = start_equity * target_pct
    loss_usd = start_equity * loss_pct
    curve = equity_curve(fills, start_equity, now=now)
    _, max_dd_usd, max_dd_pct = drawdown_series(curve)
    decided = len(wins) + len(losses)
    return {
        "starting_bankroll": round(start_equity, 2),
        "equity": round(start_equity + net, 2),
        "cash": round(start_equity + net - open_exposure, 2),
        "staked_today": round(staked, 2),
        "bets": len(fills),
        "settled": len(settled),
        "open_count": len(opens),
        "open_exposure": round(open_exposure, 2),
        "gross_winnings": round(gross_win, 2),
        "gross_losses": round(gross_loss, 2),
        "net_pnl": round(net, 2),
        "net_pnl_pct": round(net / start_equity, 6) if start_equity else None,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / decided, 6) if decided else None,
        "roi_on_staked": round(net / staked_settled, 6) if staked_settled else None,
        "avg_stake": _r(staked / len(fills)) if fills else None,
        "avg_win": _r(gross_win / len(wins)) if wins else None,
        "avg_loss": _r(gross_loss / len(losses)) if losses else None,
        "profit_factor": round(gross_win / -gross_loss, 4) if gross_loss < 0 else None,
        "fees_paid": round(fees, 2),
        "max_drawdown_usd": max_dd_usd,
        "max_drawdown_pct": max_dd_pct,
        "target_pct": target_pct,
        "loss_limit_pct": loss_pct,
        "target_usd": round(target_usd, 2),
        "loss_limit_usd": round(loss_usd, 2),
        "to_target_usd": round(target_usd - net, 2),
        "to_loss_limit_usd": round(net + loss_usd, 2),
        "target_hit": net + 1e-9 >= target_usd,
        "loss_limit_hit": net - 1e-9 <= -loss_usd,
    }


def fills_table(fills: list[Fill], start_equity: float, tz_name: str = DEFAULT_TZ) -> list[dict]:
    """Blotter rows, newest first, with running equity after each settlement."""
    running: dict[str, float] = {}
    eq = start_equity
    for f in sorted((f for f in fills if f.status == "settled" and f.pnl_usd is not None),
                    key=lambda f: ((f.settle_ts or f.ts), f.ts, f.id)):
        eq += f.pnl_usd or 0
        running[f.id] = round(eq, 2)
    rows = []
    for f in sorted(fills, key=lambda f: (f.ts, f.id), reverse=True):
        rows.append({
            "id": f.id,
            "ts": f.ts,
            "time_et": fmt_et(f.ts, tz_name),
            "market": f.market_slug,
            "asset": f.asset,
            "horizon": f.horizon,
            "side": f.side,
            "entry_price": f.price,
            "contracts": f.contracts,
            "stake_usd": round(f.cost_usd, 2),
            "fee_usd": round(f.fee_usd, 2),
            "model_prob": f.model_prob,
            "edge": f.edge,
            "status": f.status if f.status == "open" else (f.result or "settled"),
            "pnl_usd": _r(f.pnl_usd),
            "running_equity": running.get(f.id),
            "settle_ts": f.settle_ts,
            "settle_time_et": fmt_et(f.settle_ts, tz_name) if f.settle_ts else "—",
            "settlement_price": f.settlement_price,
            "price_to_beat": f.price_to_beat,
            "note": f.note,
        })
    return rows


def buckets(fills: list[Fill], assets: Iterable[str], horizons: Iterable[str]) -> list[dict]:
    out = []
    for a in assets:
        for h in horizons:
            fs = [f for f in fills if f.asset == a and f.horizon == h]
            st = [f for f in fs if f.status == "settled" and f.pnl_usd is not None]
            w = sum(1 for f in st if (f.pnl_usd or 0) > 0)
            out.append({
                "asset": a, "horizon": h, "label": f"{a.upper()} {h}",
                "n": len(fs), "settled": len(st), "wins": w, "losses": len(st) - w,
                "win_rate": round(w / len(st), 4) if st else None,
                "pnl": round(sum(f.pnl_usd or 0 for f in st), 2),
                "staked": round(sum(f.cost_usd for f in fs), 2),
            })
    return out


def pnl_by_hour(fills: list[Fill], tz_name: str = DEFAULT_TZ) -> list[dict]:
    """Realized P&L grouped by the ET hour the bet was entered."""
    hours: dict[int, dict] = {}
    for f in fills:
        if f.status != "settled" or f.pnl_usd is None:
            continue
        h = datetime.fromtimestamp(f.ts, ZoneInfo(tz_name)).hour
        b = hours.setdefault(h, {"hour": h, "pnl": 0.0, "n": 0, "wins": 0})
        b["pnl"] += f.pnl_usd
        b["n"] += 1
        b["wins"] += 1 if f.pnl_usd > 0 else 0
    out = []
    for h in sorted(hours):
        b = hours[h]
        b["pnl"] = round(b["pnl"], 2)
        b["label"] = datetime(2000, 1, 1, h).strftime("%-I %p")
        out.append(b)
    return out


def cumulative_by_bet(fills: list[Fill]) -> list[dict]:
    out = []
    cum = 0.0
    for i, f in enumerate(sorted((f for f in fills if f.status == "settled" and f.pnl_usd is not None),
                                 key=lambda f: ((f.settle_ts or f.ts), f.ts, f.id)), start=1):
        cum += f.pnl_usd or 0
        out.append({"n": i, "pnl": round(f.pnl_usd or 0, 2), "cum": round(cum, 2),
                    "label": f"{f.asset.upper()} {f.horizon} {f.side.upper()}"})
    return out


def edge_histogram(edges: Iterable[float], lo: float = -0.25, hi: float = 0.25, width: float = 0.01) -> dict:
    """Counts per edge bin. Values outside [lo, hi) go to the end bins."""
    n = int(round((hi - lo) / width))
    counts = [0] * n
    total = 0
    for e in edges:
        try:
            v = float(e)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            continue
        i = int(math.floor((v - lo) / width))
        i = max(0, min(n - 1, i))
        counts[i] += 1
        total += 1
    centers = [round(lo + width * (i + 0.5), 6) for i in range(n)]
    return {"lo": lo, "hi": hi, "width": width, "centers": centers, "counts": counts, "total": total}


def lock_state_for(rows, fills, start_equity, lock_params, tz_name, day,
                   now: Optional[float] = None, window_mode: str = "calendar_day",
                   window_hours: float = 24.0) -> Optional[dict]:
    """Profit-lock state for the chart/KPIs over the active risk window."""
    if lock_params is None:
        return None
    import time as _time
    from . import profit_lock as pl
    from .profit_lock import DayStat
    from .risk import window_start_ts
    now_ts = _time.time() if now is None else float(now)
    all_fills = fills_from_rows(rows, tz_name)
    prior: dict = {}
    for f in all_fills:
        d = day_of(f.ts, tz_name)
        if d >= day or f.status != "settled" or f.pnl_usd is None:
            continue
        r = prior.setdefault(d, [0.0, 0, 0, []])
        r[0] += f.pnl_usd
        r[1 if f.pnl_usd > 0 else 2] += 1
        if f.edge is not None:
            r[3].append(f.edge)
    hist = []
    running_eq = float(start_equity)
    for d, r in sorted(prior.items()):
        denom = max(1.0, running_eq)
        hist.append(DayStat(d.isoformat(), r[0] / denom, r[1], r[2],
                            (sum(r[3]) / len(r[3])) if r[3] else None))
        running_eq += r[0]
    tpct, info = pl.adaptive_target(lock_params, hist)
    ws = window_start_ts(now_ts, tz_name, window_mode, window_hours)
    settled = [f for f in all_fills if f.status == "settled" and f.pnl_usd is not None]
    prior_pnl = sum(f.pnl_usd or 0.0 for f in settled if (f.settle_ts or f.ts) < ws)
    window_start_eq = round(max(1.0, float(start_equity) + prior_pnl), 6)
    window_settles = [(f.settle_ts or f.ts, f.pnl_usd) for f in settled if (f.settle_ts or f.ts) >= ws]
    st = pl.evaluate(lock_params, window_start_eq, window_settles, tpct, info, now=now_ts)
    wlab = f"rolling {window_hours:g}h" if window_mode == "rolling_24h" else "calendar day ET"
    st.target_info = f"{st.target_info} · {wlab}" if st.target_info else wlab
    d = st.as_dict()
    d["window_mode"] = window_mode
    d["window_start_ts"] = ws
    d["window_hours"] = window_hours
    return d


def ledger_bundle(rows: list[dict], start_equity: float, target_pct: float, loss_pct: float,
                  assets: Iterable[str], horizons: Iterable[str], tz_name: str = DEFAULT_TZ,
                  now: Optional[float] = None, day: Optional[date] = None, lock_params=None,
                  window_mode: str = "calendar_day", window_hours: float = 24.0) -> dict[str, Any]:
    """Continuous lifetime ledger analytics plus separate today & active-window risk state."""
    import time as _time
    now = now or _time.time()
    day = day or day_of(now, tz_name)
    all_fills = fills_from_rows(rows, tz_name)
    curve = equity_curve(all_fills, start_equity, now=now)
    dd, _, _ = drawdown_series(curve)
    kpis = compute_kpis(all_fills, start_equity, target_pct, loss_pct, now=now)

    today_placed = [f for f in all_fills if day_of(f.ts, tz_name) == day]
    today_settled = [
        f for f in all_fills
        if f.status == "settled" and f.pnl_usd is not None and day_of(f.settle_ts or f.ts, tz_name) == day
    ]
    today_wins = sum(1 for f in today_settled if (f.pnl_usd or 0) > 0)
    today_losses = sum(1 for f in today_settled if (f.pnl_usd or 0) <= 0)
    today_pnl = sum(f.pnl_usd or 0.0 for f in today_settled)
    today_staked = sum(f.cost_usd for f in today_placed)
    today_fees = sum(f.fee_usd for f in today_placed)
    kpis["staked_total"] = kpis["staked_today"]
    kpis["staked_today"] = round(today_staked, 2)
    kpis["today"] = {
        "day": day.isoformat(),
        "bets": len(today_placed),
        "settled": len(today_settled),
        "wins": today_wins,
        "losses": today_losses,
        "net_pnl": round(today_pnl, 2),
        "staked": round(today_staked, 2),
        "fees": round(today_fees, 2),
    }

    lock = lock_state_for(
        rows, all_fills, start_equity, lock_params, tz_name, day,
        now=now, window_mode=window_mode, window_hours=window_hours,
    )
    if lock is not None:
        kpis["lock"] = lock
        kpis["target_usd"] = lock["target_usd"]
        kpis["target_pct"] = lock["target_pct"]
        kpis["loss_limit_usd"] = lock["loss_limit_usd"]
        kpis["to_target_usd"] = round(lock["target_usd"] - lock["pnl"], 2)
        kpis["to_loss_limit_usd"] = round(lock["pnl"] + lock["loss_limit_usd"], 2)
        kpis["target_hit"] = lock["phase"] in ("LOCKED", "COOLDOWN", "STOPPED_SESSION", "STOPPED_FLOOR", "STOPPED_TARGET") or lock["pnl"] >= lock["target_usd"]
        kpis["loss_limit_hit"] = lock["phase"] == "STOPPED_LOSS" or lock["pnl"] - 1e-9 <= -lock["loss_limit_usd"]
    from .risk import trailing_hwm_state
    settled_pairs = [(f.settle_ts or f.ts, f.pnl_usd) for f in all_fills if f.status == "settled" and f.pnl_usd is not None]
    kpis["rolling_guard"] = trailing_hwm_state(settled_pairs, start_equity, now, lookback_hours=window_hours, drawdown_pct=0.10)
    table = fills_table(all_fills, start_equity, tz_name)
    sig = (len(all_fills), round(kpis["net_pnl"], 2), max((f.ts for f in all_fills), default=0),
           sum(1 for f in all_fills if f.status == "settled"), lock["phase"] if lock else "")
    return {
        "day": day.isoformat(),
        "version": "|".join(str(x) for x in sig),
        "kpis": kpis,
        "fills": table[:500],
        "total_fills": len(table),
        "open_positions": [r for r in table if r["status"] == "open"],
        "curve": curve,
        "drawdown": dd,
        "buckets": buckets(all_fills, assets, horizons),
        "by_hour": pnl_by_hour(all_fills, tz_name),
        "cumulative": cumulative_by_bet(all_fills),
    }
