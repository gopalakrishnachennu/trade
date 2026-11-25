"""Supervised worker loops. Each role is a separate process. Fail closed."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from ..client import PublicClient
from ..config import load_config
from ..discovery import (
    current_market,
    discover_open_markets,
    expected_slug,
    horizon_minutes,
    next_market,
    should_rediscover,
    window_start_utc,
)
from ..executor import PaperExecutor
from ..feeds import BookFeed, PriceFeed
from ..models import Market, PricePoint
from ..risk import RiskManager, restore_state
from ..rules import DEFAULT_RULES, RulesEngine
from ..store import Store
from .bus import Bus
from .codec import book_to_dict, market_to_dict, points_to_list, signal_to_dict
from .heartbeat import HeartbeatKeeper
from .interlock import HaltLatch

log = logging.getLogger(__name__)


def _setup_log(path: Path, name: str, tz_name: str = "America/New_York") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)

    class TZFormatter(logging.Formatter):
        def formatTime(self, record, datefmt=None):
            dt = datetime.fromtimestamp(record.created, tz)
            return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S %Z")

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fmt = TZFormatter(f"%(asctime)s %(levelname)s {name}: %(message)s")
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(path)):
        h.setFormatter(fmt)
        root.addHandler(h)


def _stop_flag():
    flag = {"stop": False}

    def _handle(*_):
        flag["stop"] = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    return flag


def _run_guarded(role: str, latch: HaltLatch, bus: Bus, fn) -> int:
    # Heartbeats run on their own thread so a blocking fetch cannot starve the watchdog.
    keeper = HeartbeatKeeper(bus, role, interval=1.0)
    keeper.start()
    try:
        return fn()
    except Exception as e:
        log.exception("%s crashed", role)
        latch.trip("exception", role, f"{type(e).__name__}: {e}")
        bus.event("exception", role, f"{type(e).__name__}: {e}")
        try:
            bus.beat(role, ok=False, status="exception", details=str(e)[:300])
        except Exception:
            pass
        return 2
    finally:
        keeper.stop()



def _discovery_windows(cfg, markets, now):
    """Clock-accurate current/next slots from the last discovered market list."""
    from datetime import timedelta
    windows = []
    for asset in cfg.assets:
        for horizon in cfg.horizons:
            m = current_market(markets, asset, horizon, now)
            nxt = next_market(markets, asset, horizon, now)
            if m is None and nxt is not None and nxt.window_start <= now < nxt.window_end:
                m = nxt
            if m is None:
                start = window_start_utc(now, horizon, 0)
                end = start + timedelta(minutes=horizon_minutes(horizon))
                slug = expected_slug(asset, horizon, start)
                if nxt is not None and nxt.window_start == start:
                    m = nxt
                else:
                    windows.append({
                        "asset": asset, "horizon": horizon, "status": "awaiting_next",
                        "slug": slug, "price_to_beat": None,
                        "window_start": start.isoformat(), "window_end": end.isoformat(),
                        "seconds_remaining": (end - now).total_seconds(),
                        "next_slug": nxt.slug if nxt else None,
                    })
                    continue
            status = "open"
            if m.window_start > now:
                status = "awaiting_next"
            elif m.seconds_remaining(now) <= 0:
                status = "awaiting_next"
            elif m.price_to_beat is None:
                status = "waiting_book"
            rem = m.seconds_remaining(now)
            windows.append({
                "asset": asset, "horizon": horizon, "status": status,
                "slug": m.slug, "price_to_beat": m.price_to_beat,
                "window_start": m.window_start.isoformat(),
                "window_end": m.window_end.isoformat(),
                "seconds_remaining": rem,
                "next_slug": nxt.slug if nxt else None,
            })
    return windows


def run_discovery(cfg, bus: Bus, latch: HaltLatch, stop: dict) -> int:
    client = PublicClient(cfg.public_base, cfg.user_agent, min(5.0, cfg.timeout_seconds), cfg.max_rps)

    def loop():
        # Publish clock-accurate windows from the cached market list every second.
        # HTTP refresh is separate, so a long slug lookahead cannot freeze rollover
        # or leave an ended window as "current" across the expiry tick.
        markets = []
        last_http = 0.0
        while not stop["stop"]:
            bus.beat("discovery", ok=True, status="polling")
            now = datetime.now(timezone.utc)
            if markets:
                bus.publish("discovery", {
                    "markets": [market_to_dict(m) for m in markets],
                    "windows": _discovery_windows(cfg, markets, now),
                })
                bus.beat("discovery", ok=True, status="ok", n=len(markets))
            need = (not markets) or should_rediscover(markets, cfg, now) or (time.time() - last_http > 10)
            if need:
                try:
                    markets = discover_open_markets(client, cfg)
                    last_http = time.time()
                except Exception as e:
                    latch.trip("discovery_failure", "discovery", f"{type(e).__name__}: {e}")
                    bus.event("halt", "discovery", str(e))
                    bus.beat("discovery", ok=False, status="error", details=str(e)[:300])
                    return 2
                now = datetime.now(timezone.utc)
                bus.publish("discovery", {
                    "markets": [market_to_dict(m) for m in markets],
                    "windows": _discovery_windows(cfg, markets, now),
                })
                bus.beat("discovery", ok=True, status="ok", n=len(markets))
            time.sleep(1)
        return 0

    return _run_guarded("discovery", latch, bus, loop)


def run_index_feed(cfg, bus: Bus, latch: HaltLatch, stop: dict) -> int:
    client = PublicClient(cfg.public_base, cfg.user_agent, cfg.timeout_seconds, cfg.max_rps)
    prices = PriceFeed(client, cfg)
    history: dict[str, list] = {a: [] for a in cfg.assets}

    def loop():
        while not stop["stop"]:
            bus.beat("index_feed", ok=True, status="polling")
            try:
                prices.poll(list(cfg.assets))
            except Exception as e:
                latch.trip("feed_error", "index_feed", f"{type(e).__name__}: {e}")
                bus.event("halt", "index_feed", str(e))
                bus.beat("index_feed", ok=False, status="error", details=str(e)[:300])
                return 2
            indexes = {}
            for a in cfg.assets:
                pt = prices.latest(a)
                if pt:
                    history[a] = (history[a] + [{"ts": pt.ts, "close": pt.close}])[-300:]
                    indexes[a] = {"price": pt.close, "ts": pt.ts, "tick_count": pt.tick_count}
            if len(indexes) < len(cfg.assets):
                missing = [a for a in cfg.assets if a not in indexes]
                latch.trip("stale_feed", "index_feed", f"missing assets: {missing}")
                bus.event("halt", "index_feed", f"missing {missing}")
                bus.beat("index_feed", ok=False, status="missing", details=str(missing))
                return 2
            bus.publish("indexes", {"indexes": indexes, "history": history})
            bus.beat("index_feed", ok=True, status="ok")
            time.sleep(1)
        return 0

    return _run_guarded("index_feed", latch, bus, loop)


def run_book_feed(cfg, bus: Bus, latch: HaltLatch, stop: dict) -> int:
    client = PublicClient(cfg.public_base, cfg.user_agent, min(8.0, cfg.timeout_seconds), cfg.max_rps)
    books = BookFeed(client)

    def loop():
        while not stop["stop"]:
            bus.beat("book_feed", ok=True, status="polling")
            snap = bus.snapshot("discovery")
            markets = []
            if snap:
                for raw in (snap.get("payload") or {}).get("markets") or []:
                    try:
                        markets.append(__import__("polymarket_bot.runtime.codec", fromlist=["market_from_dict"]).market_from_dict(raw))
                    except Exception:
                        continue
            now = datetime.now(timezone.utc)
            out = {}
            for m in markets:
                if not m.slug:
                    continue
                if m.window_end <= now and (now - m.window_end).total_seconds() > 2:
                    continue
                books.note_window(m.slug, m.window_start)
                book = books.poll_bbo(m.slug)
                if book:
                    d = book_to_dict(book)
                    d["quote_source"] = books.quote_source.get(m.slug)
                    d["quote_lag_seconds"] = books.lag_seconds(m.slug)
                    out[m.slug] = d
            bus.publish("books", {"books": out})
            bus.beat("book_feed", ok=True, status="ok", n=len(out))
            time.sleep(2)
        return 0

    return _run_guarded("book_feed", latch, bus, loop)


def _book_from_dict(d: dict):
    from ..models import Book
    return Book(
        slug=d.get("slug") or "",
        best_bid=d.get("best_bid"),
        best_ask=d.get("best_ask"),
        long_quote=d.get("long_quote"),
        short_quote=d.get("short_quote"),
        bid_shares=float(d.get("bid_shares") or 0),
        ask_shares=float(d.get("ask_shares") or 0),
    )


class _ControlsWatcher:
    """Reload data/runtime/controls.json when its version changes."""

    def __init__(self, base_cfg, bus: Bus, role: str):
        self.base = base_cfg
        self.bus = bus
        self.role = role
        self.version = -1
        self.cfg = base_cfg
        self.controls: dict = {"version": 0, "values": {}, "flatten_id": 0}
        self._last_check = 0.0

    def poll(self) -> bool:
        from . import controls as ctl
        if time.monotonic() - self._last_check < 0.5 and self.version >= 0:
            return False
        self._last_check = time.monotonic()
        c = ctl.load(self.bus.root)
        if c["version"] == self.version:
            return False
        eff = ctl.effective_config(self.base, c)  # raises if anything would be non-paper
        first = self.version < 0
        self.version, self.cfg, self.controls = c["version"], eff, c
        if not first or c["version"] > 0:
            self.bus.event("control", self.role, f"{self.role} applied controls v{c['version']}",
                           version=c["version"], values=c.get("values"))
        return True


def run_engine(cfg, bus: Bus, latch: HaltLatch, stop: dict) -> int:
    from .codec import market_from_dict
    from . import controls as ctl
    from ..sizing import size_order
    from ..verdict import decide
    import math

    from .. import strategies as strat
    from .. import patterns as pat
    from .. import bandit as ban
    from .. import learning_runtime as learn
    from .. import calibration as cal
    watcher = _ControlsWatcher(cfg, bus, "engine")
    active_w = strat.ActiveWatcher(cfg.sqlite_path.parent)
    pat_w = pat.PatternWatcher(cfg.sqlite_path.parent, prior_strength=cfg.pattern_prior_strength)
    data_dir = cfg.sqlite_path.parent
    bholder = [ban.load_bandit(data_dir, prior_strength=cfg.pattern_prior_strength)]
    b_mtime = [None]
    cholder = [cal.load_report(data_dir)]
    c_mtime = [None]
    engine = RulesEngine(cfg)
    store = Store(cfg.sqlite_path, cfg.trades_csv, cfg.evaluations_csv, cfg.equity_csv)
    recent: list[dict] = []
    last_decision: dict[str, tuple] = {}
    last_eval: dict[tuple, tuple] = {}
    tz = cfg.timezone

    def stake_for(sig, m, cash, eff, pattern_mult=1.0):
        if sig is None or m is None:
            return None
        try:
            if not (math.isfinite(float(sig.model_prob)) and float(sig.ask) > 0):
                return None
            _, cost, _ = size_order(sig, m, cash, eff, size_mult, pattern_mult=pattern_mult)
            return float(cost)
        except Exception:
            return None

    def loop():
        nonlocal recent, engine
        while not stop["stop"]:
            bus.beat("engine", ok=True, status="eval")
            if watcher.poll():
                engine = RulesEngine(watcher.cfg)
            eff = watcher.cfg
            controls = watcher.controls
            disc = bus.snapshot("discovery")
            idx = bus.snapshot("indexes")
            bk = bus.snapshot("books")
            ex = ((bus.snapshot("execution") or {}).get("payload") or {})
            pnl = ex.get("pnl") or {}
            cash = float(pnl.get("cash") or eff.starting_cash)
            risk_halt = pnl.get("risk_halt")
            size_mult = float(pnl.get("size_mult") or 1.0)
            open_slugs = {o.get("market_slug") for o in ex.get("open_positions") or []}
            open_count = len(open_slugs)
            halted = latch.is_halted()
            markets = [market_from_dict(r) for r in ((disc or {}).get("payload") or {}).get("markets") or []]
            indexes = ((idx or {}).get("payload") or {}).get("indexes") or {}
            hist = ((idx or {}).get("payload") or {}).get("history") or {}
            books_d = ((bk or {}).get("payload") or {}).get("books") or {}
            now = datetime.now(timezone.utc)
            now_unix = time.time()
            signals = []
            grid = []
            act = active_w.poll()
            pstore = pat_w.poll()
            try:
                bm = ban.bandit_path(data_dir).stat().st_mtime_ns
            except Exception:
                bm = None
            if bm != b_mtime[0]:
                b_mtime[0] = bm
                bholder[0] = ban.load_bandit(data_dir, prior_strength=eff.pattern_prior_strength)
            try:
                cm = cal.calibrator_path(data_dir).stat().st_mtime_ns
            except Exception:
                cm = None
            if cm != c_mtime[0]:
                c_mtime[0] = cm
                cholder[0] = cal.load_report(data_dir)

            def apply_strategy(s):
                cal_info = learn.tighten_with_calibration(s, eff, cholder[0])
                if cal_info.get("applied") and cal_info.get("sized_p") is not None:
                    s.model_prob = cal_info["sized_p"]
                    if cal_info.get("cal_edge") is not None:
                        s.edge_after_fees = cal_info["cal_edge"]
                sd = signal_to_dict(s)
                base_checks = list(sd.get("rule_checks") or [])
                passed, checks = strat.evaluate(act["active"], act["params"], sd, eff.min_edge)
                feat = pat.extract_features(sd, ts=now_unix, tz_name=tz, cfg=eff, strategy=act["active"])
                layer = learn.decisions_for(feat, eff, pstore, bholder[0], data_dir, record_shadow=False)
                pdec, bdec, rdec, composed = layer["pattern"], layer["bandit"], layer["rule"], layer["composed"]
                if checks is not None:
                    checks = list(checks)
                    if eff.pattern_mode != "off":
                        ev_str = "—" if pdec["post_ev"] is None else f"{pdec['post_ev']:+.1%}"
                        checks.append({
                            "name": "pattern", "passed": pdec["passed"], "detail": pdec["detail"],
                            "value": f"{ev_str} ({pdec['would_mult']:.2f}×, n={pdec['n']})",
                            "threshold": f"n≥{eff.pattern_min_samples} · {eff.pattern_mode}",
                        })
                    if getattr(eff, "bandit_mode", "off") != "off":
                        checks.append({
                            "name": "bandit", "passed": bdec["passed"] if eff.bandit_mode == "active" else True,
                            "detail": bdec.get("detail"),
                            "value": f"{bdec.get('would_action')} ({bdec.get('would_mult')}×) · {eff.bandit_mode}",
                            "threshold": "Thompson · fee-aware ROI",
                        })
                    if getattr(eff, "rules_lab_mode", "off") != "off" and rdec.get("detail"):
                        checks.append({
                            "name": "rule", "passed": rdec.get("passed", True) if eff.rules_lab_mode == "active" else True,
                            "detail": rdec.get("detail"),
                            "value": rdec.get("would_action") or "noop",
                            "threshold": f"rules_lab {eff.rules_lab_mode}",
                        })
                    if getattr(eff, "calibration_mode", "off") != "off":
                        checks.append({
                            "name": "calibration",
                            "passed": cal_info.get("passed", True) if eff.calibration_mode == "active" else True,
                            "detail": cal_info.get("detail"),
                            "value": None if cal_info.get("cal_p") is None else f"{cal_info['cal_p']:.3f}",
                            "threshold": f"{eff.calibration_mode} · tighten-only",
                        })
                    if eff.pattern_mode == "active" and not pdec["passed"]:
                        passed = False
                    if eff.bandit_mode == "active" and not bdec.get("passed", True):
                        passed = False
                    if eff.rules_lab_mode == "active" and not rdec.get("passed", True):
                        passed = False
                    if getattr(eff, "calibration_mode", "off") == "active" and cal_info.get("fail_edge"):
                        passed = False
                sd.update({"base_passed": bool(sd.get("passed")), "base_checks": base_checks,
                           "passed": passed, "rule_checks": checks,
                           "strategy": act["active"], "strategy_version": act.get("version", 0),
                           "pattern": pdec, "bandit": bdec, "learning": composed,
                           "pattern_features": feat})
                if not passed and not sd.get("reasons"):
                    sd["reasons"] = [c.get("detail") or c.get("name") for c in checks if not c.get("passed")]
                elif not composed.get("passed", True) and composed.get("detail"):
                    sd["reasons"] = list(sd.get("reasons") or []) + [composed["detail"]]
                return sd
            for asset in cfg.assets:
                for horizon in cfg.horizons:
                    m = current_market(markets, asset, horizon, now)
                    row = {"asset": asset, "horizon": horizon}
                    up = down = None
                    sds: dict = {}
                    if m:
                        row.update(market_to_dict(m))
                        row["seconds_remaining"] = m.seconds_remaining(now)
                    bdict = books_d.get(m.slug) if m else None
                    book = _book_from_dict(bdict) if bdict else None
                    if m and book:
                        pts = [PricePoint(ts=p["ts"], close=p["close"], tick_count=60) for p in hist.get(asset) or []]
                        if not pts and asset in indexes:
                            pts = [PricePoint(ts=int(indexes[asset]["ts"]), close=float(indexes[asset]["price"]), tick_count=60)]
                        sigs = engine.evaluate(m, book, pts, now)
                        sds = {}
                        for s in sigs:
                            sd = apply_strategy(s)
                            sds[s.side] = sd
                            signals.append(sd)
                            recent.append({**{k: v for k, v in sd.items() if k != "base_checks"}, "ts_unix": now_unix})
                        up = next((s for s in sigs if s.side == "up"), None)
                        down = next((s for s in sigs if s.side == "down"), None)
                        if up is not None:
                            up.passed = sds["up"]["passed"]
                        if down is not None:
                            down.passed = sds["down"]["passed"]
                        row.update({
                            "up_ask": book.up_ask, "down_ask": book.down_ask, "spread": book.spread,
                            "up_bid": book.mark_price("up"), "down_bid": book.mark_price("down"),
                            "index": (indexes.get(asset) or {}).get("price"),
                            "model_prob_up": signal_to_dict(up)["model_prob"] if up else None,
                            "model_prob_down": signal_to_dict(down)["model_prob"] if down else None,
                            "edge_up": signal_to_dict(up)["edge_after_fees"] if up else None,
                            "edge_down": signal_to_dict(down)["edge_after_fees"] if down else None,
                            "rules_pass_up": bool(up.passed) if up else False,
                            "rules_pass_down": bool(down.passed) if down else False,
                            "reasons_up": list(up.reasons) if up else [],
                            "reasons_down": list(down.reasons) if down else [],
                            "checks_up": sds["up"]["rule_checks"] if up else [],
                            "checks_down": sds["down"]["rule_checks"] if down else [],
                            "strategy": act["active"],
                            "quote_source": (bdict or {}).get("quote_source"),
                        })
                    elif m:
                        row.update({"index": (indexes.get(asset) or {}).get("price"), "up_ask": None, "down_ask": None})
                    is_enabled = ctl.enabled(controls, asset, horizon)
                    v = decide(
                        row,
                        sds.get("up") if (m and book and up) else None,
                        sds.get("down") if (m and book and down) else None,
                        halted=halted, paused=ctl.paused(controls), enabled=is_enabled,
                        risk_halt=risk_halt, holding=bool(m and m.slug in open_slugs),
                        at_max_positions=open_count >= eff.max_concurrent_positions,
                        max_positions=eff.max_concurrent_positions,
                        stake_up=stake_for(up, m, cash, eff, float((((sds.get("up") or {}).get("learning") or (sds.get("up") or {}).get("pattern") or {}).get("mult") or 1.0))),
                        stake_down=stake_for(down, m, cash, eff, float((((sds.get("down") or {}).get("learning") or (sds.get("down") or {}).get("pattern") or {}).get("mult") or 1.0))),
                    )
                    row.update({
                        "verdict": v["verdict"], "verdict_side": v["side"], "reason": v["reason"],
                        "reason_code": v["code"], "stake_preview": v["stake"], "enabled": is_enabled,
                        "checklist": {"up": v["up"]["checks"], "down": v["down"]["checks"], "gates": v["gates"],
                                      "up_reason": v["up"]["reason"], "down_reason": v["down"]["reason"]},
                    })
                    grid.append(row)
                    # Decision event on verdict/reason change (throttled per slug).
                    slug = row.get("slug") or f"{asset}-{horizon}"
                    key = (v["verdict"], v["code"])
                    prev = last_decision.get(slug)
                    if prev is None or (prev[0] != key and (prev[0][0] != key[0] or now_unix - prev[1] >= 30)):
                        last_decision[slug] = (key, now_unix)
                        bus.event("decision", "engine", f"{asset.upper()} {horizon} {v['verdict']}: {v['reason']}",
                                  slug=row.get("slug"), asset=asset, horizon=horizon, verdict=v["verdict"],
                                  code=v["code"], edge_up=row.get("edge_up"), edge_down=row.get("edge_down"),
                                  stake=v["stake"])
                    # Evaluations CSV: on pass/fail or reason change, else every 30s per side.
                    for s, sv in ((up, v["up"]), (down, v["down"])):
                        if s is None:
                            continue
                        ek = (s.market_slug, s.side)
                        sig_key = (bool(s.passed), sv.get("code"))
                        pe = last_eval.get(ek)
                        if pe is None or pe[0] != sig_key or now_unix - pe[1] >= 30:
                            last_eval[ek] = (sig_key, now_unix)
                            try:
                                store.write_evaluation(datetime.now(ZoneInfo(tz)).isoformat(timespec="seconds"), s)
                                sd_cur = sds.get(s.side) or {}
                                if sd_cur.get("pattern_features"):
                                    pat.append_pattern_eval(
                                        cfg.sqlite_path.parent,
                                        sd_cur["pattern_features"],
                                        sd_cur.get("pattern") or {},
                                    )
                            except Exception as e:  # never let logging stop evaluation
                                log.warning("evaluation write failed: %s", e)
            if len(last_decision) > 200:
                for k in sorted(last_decision, key=lambda k: last_decision[k][1])[:100]:
                    last_decision.pop(k, None)
            if len(last_eval) > 400:
                for k in sorted(last_eval, key=lambda k: last_eval[k][1])[:200]:
                    last_eval.pop(k, None)
            recent = recent[-400:]
            bus.publish("engine", {"signals": signals[-80:], "grid": grid, "recent_decisions": recent[-200:],
                                   "controls_version": watcher.version,
                                   "strategy": {"active": act["active"], "version": act.get("version", 0)}})
            bus.beat("engine", ok=True, status="ok", n=len(signals), controls_version=watcher.version)
            time.sleep(1)
        store.close()
        return 0

    return _run_guarded("engine", latch, bus, loop)


def _order_rec(o) -> dict:
    return {
        "id": o.id, "ts": o.ts, "asset": o.asset, "horizon": o.horizon, "side": o.side,
        "price": o.price, "contracts": o.contracts, "cost_usd": o.cost_usd,
        "fee_usd": o.fee_usd, "status": o.status, "result": o.result,
        "pnl_usd": o.pnl_usd, "market_slug": o.market_slug, "window_end": o.window_end,
        "model_prob": o.model_prob, "edge_after_fees": o.edge_after_fees,
        "settlement_price": o.settlement_price, "price_to_beat": o.price_to_beat,
    }


def run_execution(cfg, bus: Bus, latch: HaltLatch, stop: dict) -> int:
    if cfg.mode != "paper":
        latch.trip("mode_not_paper", "execution", f"mode={cfg.mode}")
        return 2
    from .codec import market_from_dict
    from . import controls as ctl
    from ..fees import taker_fee_usd
    from ..models import Signal
    from .. import strategies as strat
    from .. import patterns as pat
    from .. import bandit as ban
    from .. import learning_runtime as learn
    store = Store(cfg.sqlite_path, cfg.trades_csv, cfg.evaluations_csv, cfg.equity_csv)
    risk = RiskManager(cfg)
    state, cash = restore_state(cfg, store, risk)
    executor = PaperExecutor(cfg, store, risk, state)
    active_w = strat.ActiveWatcher(cfg.sqlite_path.parent)
    data_dir = cfg.sqlite_path.parent
    pstore = pat.load_pattern_store(data_dir, prior_strength=cfg.pattern_prior_strength)
    pstore.sync_from_orders(store.active_orders(), tz_name=cfg.timezone, cfg=cfg,
                            trade_patterns=store.all_trade_patterns())
    bstore = ban.load_bandit(data_dir, prior_strength=cfg.pattern_prior_strength)
    # learn.startup (CF ingest + calibration of ~48k rows) can take longer than the
    # watchdog's 8s first-beat grace, so it MUST run inside the guarded loop after
    # HeartbeatKeeper has already started. See 2026-11-25 04:11 AM ET stale_heartbeat.
    open_features: dict[str, tuple[dict, dict]] = {}
    shadow_logged: set[str] = set()
    executor.cash = cash
    seen_ids: set[str] = set(store.active_order_ids())
    blocked: dict[str, str] = {}          # cid -> gate reason already logged
    rejected_at: dict[str, float] = {}    # cid -> monotonic time of last reject
    last_equity = 0.0
    curve: list[dict] = []
    watcher = _ControlsWatcher(cfg, bus, "execution")
    watcher.poll()
    risk.cfg = executor.cfg = watcher.cfg
    risk.apply_daily_halts(state)
    handled_flatten = int(watcher.controls.get("flatten_id") or 0)
    tz = cfg.timezone

    def record_settled_pattern(order) -> None:
        if order.pnl_usd is None:
            return
        saved = open_features.pop(order.id, None)
        if saved is not None:
            feat, pdec = saved
        else:
            feat = pat.extract_features(order, tz_name=tz, cfg=executor.cfg)
            pdec = {"mode": executor.cfg.pattern_mode, "would_action": None, "would_mult": 1.0, "context": None}
        feat_rec = {
            **feat,
            "pattern_shadow_action": pdec.get("would_action") if pdec.get("mode") == "shadow" else None,
        }
        cost = float(order.cost_usd or 0.0)
        roi = round(float(order.pnl_usd) / max(0.01, cost), 4)
        store.write_trade_pattern({
            "order_id": order.id,
            "ts": order.ts,
            "window_end": order.window_end,
            "market_slug": order.market_slug,
            "asset": order.asset,
            "horizon": order.horizon,
            "side": order.side,
            "hour_et": feat.get("hour_et"),
            "hour_bucket": feat.get("hour_bucket"),
            "weekday": feat.get("weekday"),
            "minutes_left": feat.get("minutes_left"),
            "distance_sigma": feat.get("distance_sigma"),
            "distance_bucket": feat.get("distance_bucket"),
            "vol_regime": feat.get("vol_regime"),
            "annualized_vol": feat.get("annualized_vol"),
            "model_prob": order.model_prob,
            "ask": order.price,
            "edge": order.edge_after_fees,
            "spread": feat.get("spread"),
            "depth_contracts": feat.get("depth_contracts"),
            "depth_usd": feat.get("depth_usd"),
            "strategy": feat.get("strategy"),
            "outcome": order.result,
            "pnl_usd": order.pnl_usd,
            "cost_usd": cost,
            "roi": roi,
            "pattern_mode": pdec.get("mode"),
            "pattern_action": pdec.get("would_action"),
            "pattern_mult": pdec.get("would_mult"),
            "pattern_context": pdec.get("context"),
        })
        if pstore.update_trade(feat_rec, order_id=order.id, pnl_usd=order.pnl_usd,
                               cost_usd=cost, outcome=order.result or ("win" if order.pnl_usd > 0 else "loss")):
            pat.save_pattern_store(data_dir, pstore)
        try:
            learn.on_settlement(order, feat_rec, {"pattern": pdec, "bandit": (pdec or {}).get("_bandit"),
                                                   "composed": (pdec or {}).get("_composed")},
                                executor.cfg, store, pstore, bstore, data_dir)
        except Exception:
            pass

    def flatten(books_d: dict, flatten_id: int) -> None:
        closed, total = 0, 0.0
        for order in list(state.open_orders.values()):
            bd = books_d.get(order.market_slug)
            mark = _book_from_dict(bd).mark_price(order.side) if bd else None
            if mark is None:
                bus.event("flatten", "execution", f"cannot flatten {order.market_slug} {order.side}: no bid; left to settle",
                          flatten_id=flatten_id)
                continue
            fee = taker_fee_usd(executor.cfg.fee_theta, order.contracts, mark)
            payout = order.contracts * mark - fee
            pnl = payout - order.cost_usd
            order.status = "settled"
            order.settlement_price = mark
            order.result = "win" if pnl > 0 else "loss"
            order.payout_usd = payout
            order.pnl_usd = pnl
            order.reject_reason = (f"flattened at bid {mark:.3f} by operator (flatten #{flatten_id}) "
                                   f"at {datetime.now(ZoneInfo(tz)).isoformat(timespec='seconds')}")
            executor.cash += payout
            risk.register_settle(state, order)
            store.update_order(order)
            record_settled_pattern(order)
            closed += 1
            total += pnl
            bus.event("settle", "execution", f"FLATTEN {order.side.upper()} {order.asset.upper()} {order.horizon} @ {mark:.3f} pnl ${pnl:+.2f}",
                      pnl=pnl, slug=order.market_slug, flatten_id=flatten_id)
        bus.event("flatten", "execution", f"flatten #{flatten_id} done: closed {closed}, pnl ${total:+.2f}; new entries paused",
                  flatten_id=flatten_id, closed=closed, pnl=total)

    started_mono = time.monotonic()
    last_loop_mono = [time.monotonic()]

    def watchdog_lost() -> Optional[str]:
        """Fail closed if the supervisor/watchdog stopped beating (it is the only thing that can halt us)."""
        now_m = time.monotonic()
        gap = now_m - last_loop_mono[0]
        last_loop_mono[0] = now_m
        if now_m - started_mono < 45 or gap > 5:   # startup grace / we were the ones paused
            return None
        hb = bus.read_heartbeat("watchdog")
        if hb is None:
            return "watchdog heartbeat missing"
        age = time.time() - hb.wall_ts
        return f"watchdog heartbeat {age:.0f}s old" if age > 30 else None

    def loop():
        nonlocal last_equity, handled_flatten, pstore, bstore
        try:
            stats = learn.startup(cfg, store, pstore, bstore, data_dir)
            bus.event("learning", "execution",
                      f"phase6 backfill attribution={stats['attribution']} cf={stats['counterfactuals']} evals={stats['eval_rows']}")
        except Exception as e:
            bus.event("learning", "execution", f"phase6 startup skipped: {e}")
        pat.save_pattern_store(data_dir, pstore)
        ban.save_bandit(data_dir, bstore)
        while not stop["stop"]:
            halted = latch.is_halted()
            lost = None if halted else watchdog_lost()
            if lost:
                latch.trip("watchdog_lost", "execution", lost)
                bus.event("halt", "execution", lost)
                halted = True
            bus.beat("execution", ok=True, status="halted" if halted else "trading")
            if watcher.poll():
                risk.cfg = executor.cfg = watcher.cfg
                risk.apply_daily_halts(state)
                blocked.clear()
            controls = watcher.controls
            disc = bus.snapshot("discovery")
            eng = bus.snapshot("engine")
            idx = bus.snapshot("indexes")
            bk = bus.snapshot("books")
            books_d = ((bk or {}).get("payload") or {}).get("books") or {}
            markets = {m.slug: m for m in (
                market_from_dict(r) for r in ((disc or {}).get("payload") or {}).get("markets") or []
            ) if m.slug}
            hist = ((idx or {}).get("payload") or {}).get("history") or {}
            price_map = {
                a: [PricePoint(ts=p["ts"], close=p["close"], tick_count=60) for p in hist.get(a) or []]
                for a in cfg.assets
            }
            # Settle always (even while halted) so open paper P&L can close.
            try:
                for o in executor.settle_due(markets, price_map):
                    record_settled_pattern(o)
                    bus.event("settle", "execution",
                              f"SETTLED {o.side.upper()} {o.asset.upper()} {o.horizon} {o.result} pnl ${o.pnl_usd:+.2f}",
                              pnl=o.pnl_usd, slug=o.market_slug, result=o.result)
            except Exception as e:
                latch.trip("exception", "execution", f"settle: {e}")
                bus.event("halt", "execution", str(e))
                return 2

            fid = int(controls.get("flatten_id") or 0)
            if fid > handled_flatten:
                handled_flatten = fid
                flatten(books_d, fid)

            if not halted:
                gate_common = (
                    risk.kill_switch_on()
                    or ("entries paused by operator" if ctl.paused(controls) else None)
                    or state.kill_reason
                    or risk.daily_halt_reason(state)
                )
                act = active_w.poll()
                for raw in ((eng or {}).get("payload") or {}).get("signals") or []:
                    if not raw.get("passed"):
                        continue
                    # Defence in depth: re-apply the active strategy to the engine's
                    # base checks; skip if engine and execution disagree (mid-switch).
                    if raw.get("strategy") != act["active"] or int(raw.get("strategy_version") or 0) != int(act.get("version") or 0):
                        continue
                    ok2, _ = strat.evaluate(act["active"], act["params"],
                                            {**raw, "rule_checks": raw.get("base_checks") or []}, watcher.cfg.min_edge)
                    if not ok2:
                        continue
                    slug = raw.get("market_slug")
                    m = markets.get(slug)
                    if not m:
                        continue
                    cid = f"paper:{slug}:{raw.get('side')}:{int(m.window_start.timestamp())}"
                    if cid in seen_ids:
                        continue
                    gate = gate_common or (None if ctl.enabled(controls, m.asset, m.horizon) else "disabled by operator")
                    if gate:
                        # Blocked before sizing: nothing written to the ledger, one event per market/side.
                        if blocked.get(cid) != gate:
                            blocked[cid] = gate
                            bus.event("blocked", "execution",
                                      f"{raw.get('side','').upper()} {m.asset.upper()} {m.horizon} passes rules; entry blocked: {gate}",
                                      slug=slug, edge=raw.get("edge_after_fees"))
                        continue
                    if time.monotonic() - rejected_at.get(cid, -1e9) < 30:
                        continue
                    sig = Signal(
                        market_slug=slug, asset=raw.get("asset"), horizon=raw.get("horizon"),
                        side=raw.get("side"), model_prob=raw.get("model_prob") or 0,
                        ask=raw.get("ask") or 0, fee_per_contract=0.0,
                        edge_after_fees=raw.get("edge_after_fees") or 0,
                        spread=raw.get("spread"), liquidity_contracts=raw.get("liquidity_contracts") or 0,
                        spot=raw.get("spot"), price_to_beat=raw.get("price_to_beat"),
                        seconds_remaining=raw.get("seconds_remaining") or 0,
                        annualized_vol=raw.get("annualized_vol"),
                        reasons=list(raw.get("reasons") or []), passed=True,
                        rule_checks=list(raw.get("rule_checks") or []),
                    )
                    raw_feat = raw.get("pattern_features") or pat.extract_features(raw, tz_name=tz, cfg=executor.cfg, strategy=act["active"])
                    layer = learn.decisions_for(
                        raw_feat, executor.cfg, pstore, bstore, data_dir,
                        record_shadow=(cid not in shadow_logged),
                    )
                    raw_pat = dict(layer["pattern"])
                    raw_pat["_bandit"] = layer["bandit"]
                    raw_pat["_composed"] = layer["composed"]
                    if cid not in shadow_logged:
                        shadow_logged.add(cid)
                        if len(shadow_logged) > 2000:
                            shadow_logged.clear()
                        if raw_pat.get("would_action") in ("skip", "reduce", "boost"):
                            bus.event("pattern", "execution", str(raw_pat.get("detail") or ""), slug=slug)
                        if layer["bandit"].get("would_action") and layer["bandit"].get("mode") == "shadow":
                            if layer["bandit"]["would_action"] != "1.0":
                                bus.event("bandit", "execution", str(layer["bandit"].get("detail") or ""), slug=slug)
                        pat.save_pattern_store(data_dir, pstore)
                        ban.save_bandit(data_dir, bstore)
                    composed = layer["composed"]
                    if not composed.get("passed", True):
                        continue
                    pm = float(composed.get("mult") or 1.0)
                    # Interlock check immediately before the fill — never skip this.
                    if latch.is_halted():
                        break
                    filled = executor.try_fill(sig, m, pattern_mult=pm)
                    if filled and filled.status == "open":
                        seen_ids.add(cid)
                        seen_ids.add(filled.id)
                        open_features[filled.id] = (raw_feat, raw_pat)
                        bus.event("fill", "execution",
                                  f"FILL {filled.side.upper()} {filled.asset.upper()} {filled.horizon} {filled.contracts:g} ct @ {filled.price:.3f} stake ${filled.cost_usd:.2f}",
                                  amount=filled.cost_usd, slug=slug, edge=filled.edge_after_fees)
                    elif filled and filled.status == "rejected":
                        rejected_at[cid] = time.monotonic()
                        bus.event("reject", "execution", f"{slug} {filled.side}: {filled.reject_reason}", slug=slug)
                if len(blocked) > 500:
                    blocked.clear()
                if len(rejected_at) > 500:
                    rejected_at.clear()

            now_ts = time.time()
            lock_now = risk.lock_state(state, now=now_ts)
            state.kill_reason = lock_now.reason
            base_cash = state.starting_cash if state.starting_cash is not None else executor.cfg.starting_cash
            from ..risk import trailing_hwm_state
            guard = trailing_hwm_state(
                state.all_settles or [],
                float(base_cash),
                now_ts,
                lookback_hours=executor.cfg.rolling_guard_hours,
                drawdown_pct=executor.cfg.rolling_guard_pct,
            )
            if (not halted) and executor.cfg.rolling_guard_enabled and guard.get("triggered"):
                detail = (
                    f"equity ${guard['equity']:.2f} is {abs(guard['drawdown_pct_actual']):.1%} "
                    f"below {guard['lookback_hours']:g}h HWM ${guard['hwm_equity']:.2f} "
                    f"(limit {guard['drawdown_pct']:.0%})"
                )
                latch.trip("rolling_drawdown", "execution", detail)
                bus.event("halt", "execution", detail)
                halted = True
            now_eq = {
                "ts_unix": now_ts,
                "cash": executor.cash,
                "realized": state.realized_pnl,
                "window_pnl": lock_now.pnl,
                "open_count": state.open_count,
                "open_cost": state.open_exposure,
                "starting_equity": base_cash,
                "window_start_equity": state.starting_day_equity,
                "window_mode": executor.cfg.window_mode,
                "equity": base_cash + state.realized_pnl,
                "halted": halted,
                "risk_halt": lock_now.reason,
                "lock": lock_now.as_dict(),
                "rolling_guard": guard,
                "size_mult": lock_now.size_mult,
                "entries_paused": ctl.paused(controls),
                "controls_version": watcher.version,
                "pattern_mode": executor.cfg.pattern_mode,
            }
            if time.monotonic() - last_equity >= 2:
                store.write_equity(
                    datetime.now(ZoneInfo(tz)).isoformat(timespec="milliseconds"),
                    executor.cash, state.open_exposure, state.realized_pnl, state.open_count,
                )
                last_equity = time.monotonic()
                curve.append({"ts": now_eq["ts_unix"], "equity": now_eq["equity"], "realized": now_eq["realized"]})
                curve[:] = curve[-2000:]
            opens, fills = [], []
            active_all = store.active_orders()
            for o in active_all[-500:]:
                rec = _order_rec(o)
                if o.status == "open":
                    opens.append(rec)
                fills.append(rec)
            if len(active_all) > 500:
                for o in active_all[:-500]:
                    if o.status == "open":
                        opens.append(_order_rec(o))
            bus.publish("execution", {"pnl": now_eq, "open_positions": opens, "fills": fills, "curve": curve[-500:],
                                      "controls_version": watcher.version})
            bus.beat("execution", ok=True, status="halted" if halted else "trading",
                     open=state.open_count, realized=state.realized_pnl, controls_version=watcher.version)
            time.sleep(1)
        store.close()
        return 0

    return _run_guarded("execution", latch, bus, loop)


def run_strategy_lab(cfg, bus: Bus, latch: HaltLatch, stop: dict) -> int:
    """Non-critical: shadow books + reviewer. Never trips the HALT latch."""
    from ..strategy_lab import run_lab
    keeper = HeartbeatKeeper(bus, "strategy_lab", interval=2.0)
    keeper.start()
    try:
        return run_lab(cfg, bus, stop)
    except Exception as e:
        log.exception("strategy_lab crashed")
        bus.event("degraded", "strategy_lab", f"{type(e).__name__}: {e}")
        try:
            bus.beat("strategy_lab", ok=False, status="exception", details=str(e)[:300])
        except Exception:
            pass
        return 1
    finally:
        keeper.stop()


ROLES = {
    "discovery": run_discovery,
    "index_feed": run_index_feed,
    "book_feed": run_book_feed,
    "engine": run_engine,
    "execution": run_execution,
    "strategy_lab": run_strategy_lab,
}


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--role", required=True, choices=list(ROLES))
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--runtime", default=None)
    args = p.parse_args(argv)
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cand = Path.cwd() / cfg_path
        cfg_path = cand if cand.exists() else Path(__file__).resolve().parents[3] / cfg_path
    cfg = load_config(cfg_path)
    runtime = Path(args.runtime) if args.runtime else (cfg.root / "data" / "runtime")
    bus = Bus(runtime)
    latch = HaltLatch(runtime / "HALT.json")
    _setup_log(cfg.root / "data" / "runtime" / f"{args.role}.log", args.role, cfg.timezone)
    log.info("start role=%s pid=%s paper_only=yes", args.role, os.getpid())
    stop = _stop_flag()
    return ROLES[args.role](cfg, bus, latch, stop)


if __name__ == "__main__":
    raise SystemExit(main())
