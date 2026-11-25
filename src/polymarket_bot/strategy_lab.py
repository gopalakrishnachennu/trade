"""Shadow strategy lab: one virtual paper book per strategy, plus metrics.

Runs as the non-critical `strategy_lab` process. It reads the same bus
snapshots as execution (discovery, indexes, engine) and never writes to the
main ledger. Each shadow book is a full PaperExecutor with its own sqlite
under data/strategies/shadow/<name>/, so sizing, risk gates, profit lock and
settlement are identical to the main book. A crash here never trips HALT;
the supervisor restarts the process and Health shows it DEGRADED.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from . import strategies as strat

log = logging.getLogger(__name__)

CAL_BINS = 10


def _get(o, k):
    return o.get(k) if isinstance(o, dict) else getattr(o, k, None)


def compute_metrics(orders: Iterable, starting_equity: float, tz_name: str = "America/New_York") -> dict[str, Any]:
    """Pure metrics over one book's orders (dicts or PaperOrder objects)."""
    from .analytics import _settle_ts
    settled, opens = [], []
    for o in orders:
        st = _get(o, "status")
        if st == "open":
            opens.append(o)
        elif st == "settled" and _get(o, "pnl_usd") is not None:
            t = _settle_ts({"status": st, "reject_reason": _get(o, "reject_reason"),
                            "window_end": _get(o, "window_end")}, tz_name) or 0.0
            settled.append((t, o))
    settled.sort(key=lambda x: x[0])
    pnls = [float(_get(o, "pnl_usd")) for _, o in settled]
    wins = sum(1 for p in pnls if p > 0)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = -sum(p for p in pnls if p < 0)
    eq = peak = 0.0
    max_dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    briers, bins = [], [[0, 0.0, 0] for _ in range(CAL_BINS)]  # n, sum_p, wins
    for _, o in settled:
        p = _get(o, "model_prob")
        try:
            p = float(p)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(p):
            continue
        y = 1.0 if (_get(o, "result") == "win" or float(_get(o, "pnl_usd")) > 0) else 0.0
        briers.append((p - y) ** 2)
        b = min(CAL_BINS - 1, max(0, int(p * CAL_BINS)))
        bins[b][0] += 1
        bins[b][1] += p
        bins[b][2] += y
    n = len(pnls)
    return {
        "trades": n,
        "open": len(opens),
        "open_cost": round(sum(float(_get(o, "cost_usd") or 0) for o in opens), 2),
        "wins": wins,
        "losses": n - wins,
        "win_rate": (wins / n) if n else None,
        "pnl": round(sum(pnls), 2),
        "equity": round(starting_equity + sum(pnls), 2),
        "gross_wins": round(gross_w, 2),
        "gross_losses": round(gross_l, 2),
        "profit_factor": (round(gross_w / gross_l, 3) if gross_l > 0 else (None if gross_w == 0 else 99.0)),
        "max_drawdown": round(max_dd, 2),
        "brier": (round(sum(briers) / len(briers), 4) if briers else None),
        "avg_pnl": (round(sum(pnls) / n, 3) if n else None),
        "calibration": [
            {"bin": i / CAL_BINS, "n": b[0], "avg_p": (b[1] / b[0]) if b[0] else None,
             "freq": (b[2] / b[0]) if b[0] else None}
            for i, b in enumerate(bins)
        ],
        "first_ts": settled[0][0] if settled else None,
        "last_ts": settled[-1][0] if settled else None,
    }


def recent_trades(orders: Iterable, n: int = 40) -> list[dict]:
    keep = ("ts", "asset", "horizon", "side", "price", "cost_usd", "model_prob", "edge_after_fees", "status", "result", "pnl_usd")
    rows = [o for o in orders if _get(o, "status") in ("open", "settled")]
    rows.sort(key=lambda o: str(_get(o, "ts")))
    return [{k: _get(o, k) for k in keep} for o in rows[-n:]]


def read_params(data_dir: Path) -> dict[str, dict]:
    """Shadow params per strategy (tweaked by the reviewer, always clamped)."""
    path = strat.strategies_dir(data_dir) / "params.json"
    try:
        raw = json.loads(path.read_text())
    except Exception:
        raw = {}
    return {name: strat.clamp_params(raw.get(name) or {}, strat.default_params(name)) for name in strat.names()}


def write_params(data_dir: Path, params: dict[str, dict]) -> None:
    path = strat.strategies_dir(data_dir) / "params.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(params, indent=2))
    os.replace(tmp, path)


def _atomic_json(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, default=str))
    os.replace(tmp, path)


class ShadowBook:
    def __init__(self, cfg, name: str, root: Path):
        from .executor import PaperExecutor
        from .risk import RiskManager, restore_state
        from .store import Store
        self.name = name
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        self.store = Store(d / "paper_trades.sqlite", d / "paper_trades.csv", d / "evaluations.csv", d / "equity.csv")
        self.risk = RiskManager(cfg)
        self.state, cash = restore_state(cfg, self.store, self.risk)
        self.executor = PaperExecutor(cfg, self.store, self.risk, self.state)
        self.executor.cash = cash
        self.seen: set[str] = set()
        self.rejected_at: dict[str, float] = {}
        self.day = self.state.day

    def maybe_roll_day(self, cfg) -> None:
        from .risk import restore_state, today_in_tz
        if today_in_tz(cfg.timezone) != self.day:
            self.state, cash = restore_state(cfg, self.store, self.risk)
            self.executor.state = self.state
            self.executor.cash = cash
            self.day = self.state.day


def run_lab(cfg, bus, stop: dict, *, data_dir: Optional[Path] = None, review_every_s: Optional[float] = None) -> int:
    from .models import PricePoint, Signal
    from .runtime.codec import market_from_dict
    from . import reviewer
    data_dir = Path(data_dir or cfg.sqlite_path.parent)
    sdir = strat.strategies_dir(data_dir)
    root = sdir / "shadow"
    books = {name: ShadowBook(cfg, name, root) for name in strat.names()}
    last_metrics = 0.0
    settings = reviewer.read_settings(data_dir)
    every = review_every_s if review_every_s is not None else float(settings["review_interval_min"]) * 60
    state_path = sdir / "lab_state.json"
    try:
        last_review = float(json.loads(state_path.read_text()).get("last_review") or 0)
    except Exception:
        last_review = time.time()  # first hourly review one interval after first start
    review_thread = None

    while not stop["stop"]:
        bus.beat("strategy_lab", ok=True, status="shadowing")
        params = read_params(data_dir)
        disc = bus.snapshot("discovery")
        eng = bus.snapshot("engine")
        idx = bus.snapshot("indexes")
        markets = {m.slug: m for m in (
            market_from_dict(r) for r in ((disc or {}).get("payload") or {}).get("markets") or []
        ) if m.slug}
        hist = ((idx or {}).get("payload") or {}).get("history") or {}
        price_map = {a: [PricePoint(ts=p["ts"], close=p["close"], tick_count=60) for p in hist.get(a) or []]
                     for a in cfg.assets}
        signals = ((eng or {}).get("payload") or {}).get("signals") or []
        fresh = eng is not None and (time.time() - float(eng.get("ts_unix") or 0)) < 10
        for name, b in books.items():
            b.maybe_roll_day(cfg)
            for o in b.executor.settle_due(markets, price_map):
                bus.event("shadow", "strategy_lab", f"[{name}] SETTLED {o.side.upper()} {o.asset.upper()} {o.horizon} {o.result} pnl ${o.pnl_usd:+.2f}")
            if not fresh:
                continue
            gate = b.risk.kill_switch_on() or b.risk.daily_halt_reason(b.state)
            if gate:
                continue
            for raw in signals:
                base = {**raw, "rule_checks": raw.get("base_checks") or raw.get("rule_checks") or []}
                ok, _ = strat.evaluate(name, params[name], base, cfg.min_edge)
                if not ok:
                    continue
                slug = raw.get("market_slug")
                m = markets.get(slug)
                if not m:
                    continue
                cid = f"{slug}:{raw.get('side')}"
                if cid in b.seen or time.monotonic() - b.rejected_at.get(cid, -1e9) < 30:
                    continue
                sig = Signal(
                    market_slug=slug, asset=raw.get("asset"), horizon=raw.get("horizon"),
                    side=raw.get("side"), model_prob=raw.get("model_prob") or 0,
                    ask=raw.get("ask") or 0, fee_per_contract=0.0,
                    edge_after_fees=raw.get("edge_after_fees") or 0,
                    spread=raw.get("spread"), liquidity_contracts=raw.get("liquidity_contracts") or 0,
                    spot=raw.get("spot"), price_to_beat=raw.get("price_to_beat"),
                    seconds_remaining=raw.get("seconds_remaining") or 0,
                    annualized_vol=raw.get("annualized_vol"), reasons=[], passed=True,
                    rule_checks=list(raw.get("rule_checks") or []),
                )
                filled = b.executor.try_fill(sig, m)
                if filled and filled.status == "open":
                    b.seen.add(cid)
                    bus.event("shadow", "strategy_lab", f"[{name}] FILL {filled.side.upper()} {filled.asset.upper()} {filled.horizon} @ {filled.price:.3f} ${filled.cost_usd:.2f}")
                elif filled is not None:
                    b.rejected_at[cid] = time.monotonic()
            if len(b.seen) > 2000:
                b.seen.clear()
            if len(b.rejected_at) > 500:
                b.rejected_at.clear()
        if time.time() - last_metrics >= 10:
            last_metrics = time.time()
            active = strat.read_active(data_dir)
            out = {"ts": time.time(), "starting_equity": cfg.starting_day_equity, "active": active["active"], "active_version": active.get("version", 0), "strategies": {}}
            for name, b in books.items():
                orders = b.store.active_orders()
                met = compute_metrics(orders, cfg.starting_day_equity, cfg.timezone)
                lock = b.risk.lock_state(b.state)
                out["strategies"][name] = {
                    "description": strat.STRATEGIES[name]["description"],
                    "params": params[name],
                    "metrics": met,
                    "today": {"pnl": round(b.state.realized_pnl, 2), "phase": lock.phase, "cash": round(b.executor.cash, 2)},
                    "recent": recent_trades(orders, 40),
                }
            _atomic_json(sdir / "metrics.json", out)
        # Hourly reviewer (non-blocking thread; errors only mark it degraded).
        if every > 0 and time.time() - last_review >= every and (review_thread is None or not review_thread.is_alive()):
            import threading
            last_review = time.time()
            _atomic_json(state_path, {"last_review": last_review})
            review_thread = threading.Thread(target=reviewer.run_review_safe, args=(data_dir, "hourly", bus), daemon=True)
            review_thread.start()
        time.sleep(1)
    for b in books.values():
        b.store.close()
    return 0
