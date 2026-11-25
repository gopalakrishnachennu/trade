from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .client import PublicClient
from .config import Config, load_config
from .discovery import discover_open_markets, should_rediscover
from .executor import PaperExecutor, _ts
from .feeds import BookFeed, PriceFeed
from .live_state import LiveStateWriter, build_live_state
from .risk import RiskManager, restore_state
from .rules import RulesEngine
from .store import Store

log = logging.getLogger(__name__)


def setup_logging(level: str, tz_name: str, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    tz = ZoneInfo(tz_name)

    class TZFormatter(logging.Formatter):
        def formatTime(self, record, datefmt=None):
            dt = datetime.fromtimestamp(record.created, tz)
            return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S %Z")

    fmt = TZFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(log_path)):
        handler.setFormatter(fmt)
        root.addHandler(handler)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Polymarket US crypto Up/Down paper-trading bot")
    p.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    p.add_argument("--duration", type=int, default=None, help="Override run_seconds (0 = until stop_at or signal)")
    p.add_argument("--stop-at", default=None, help="Local HH:MM to exit (timezone from config, default America/New_York)")
    return p.parse_args(argv)


def past_stop_at(stop_at: str | None, tz_name: str) -> bool:
    if not stop_at:
        return False
    parts = str(stop_at).strip().split(":")
    if len(parts) < 2:
        raise ValueError(f"stop_at must be HH:MM, got {stop_at!r}")
    hh, mm = int(parts[0]), int(parts[1])
    now = datetime.now(ZoneInfo(tz_name))
    return now.hour > hh or (now.hour == hh and now.minute >= mm)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        # prefer CWD, then the package root (parent of src)
        cand = Path.cwd() / cfg_path
        cfg_path = cand if cand.exists() else Path(__file__).resolve().parents[2] / cfg_path
    cfg = load_config(cfg_path)
    duration = cfg.run_seconds if args.duration is None else args.duration
    stop_at = args.stop_at if args.stop_at is not None else cfg.stop_at
    setup_logging(cfg.log_level, cfg.timezone, cfg.root / "data" / "bot.log")

    pid_path = cfg.root / "data" / "bot.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))

    log.info("starting paper bot mode=%s duration=%ss stop_at=%s config=%s pid=%s", cfg.mode, duration, stop_at, cfg_path, os.getpid())
    log.info("strategy=%s — no API keys will be used; no real orders will be sent", cfg.strategy_name)
    log.info("public API %s", cfg.public_base)
    log.info(
        "daily risk on $%.2f starting equity: target +%.0f%% ($%.2f), loss limit -%.0f%% ($%.2f)",
        cfg.starting_day_equity,
        cfg.daily_profit_target_pct * 100,
        cfg.daily_profit_target_usd,
        cfg.daily_loss_limit_pct * 100,
        cfg.daily_loss_limit_usd,
    )

    client = PublicClient(cfg.public_base, cfg.user_agent, cfg.timeout_seconds, cfg.max_rps)
    store = Store(cfg.sqlite_path, cfg.trades_csv, cfg.evaluations_csv, cfg.equity_csv)
    risk = RiskManager(cfg)
    state, cash = restore_state(cfg, store, risk)
    engine = RulesEngine(cfg)
    prices = PriceFeed(client, cfg)
    books = BookFeed(client)
    executor = PaperExecutor(cfg, store, risk, state)
    executor.cash = cash
    live = LiveStateWriter(cfg.live_state_path, min_interval=1.0)
    signals_by_slug: dict = {}
    try:
        wrapper_pid = int((cfg.root / "data" / "wrapper.pid").read_text().strip())
    except (OSError, ValueError):
        wrapper_pid = None
    if state.open_count or state.realized_pnl or state.kill_reason:
        log.info(
            "restored session: cash=$%.2f open=%d exposure=$%.2f realized=$%.2f halt=%s",
            executor.cash, state.open_count, state.open_exposure, state.realized_pnl, state.kill_reason or "none",
        )

    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True
        log.info("stop requested")

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    markets = []
    last_discovery = 0.0
    last_prices = 0.0
    last_books = 0.0
    last_equity = 0.0
    started = time.monotonic()
    loops = 0
    eval_count = 0
    pass_count = 0
    fill_count = 0
    settle_count = 0

    try:
        while not stop["flag"]:
            now_mono = time.monotonic()
            if duration > 0 and (now_mono - started) >= duration:
                log.info("run duration reached (%ss)", duration)
                break
            if past_stop_at(stop_at, cfg.timezone):
                log.info("stop_at %s %s reached — exiting (open positions left to settle on the next run if still open)", stop_at, cfg.timezone)
                break

            if now_mono - last_discovery >= cfg.discovery_refresh_seconds or should_rediscover(markets, cfg):
                try:
                    markets = discover_open_markets(client, cfg)
                    last_discovery = time.monotonic()
                except Exception:
                    log.exception("discovery failed")
                    time.sleep(2)
                    continue

            if now_mono - last_prices >= cfg.price_poll_seconds:
                assets = sorted({m.asset for m in markets}) or list(cfg.assets)
                try:
                    prices.poll(assets)
                    last_prices = time.monotonic()
                    for a in assets:
                        pt = prices.latest(a)
                        if pt:
                            log.info("index %s ts=%s close=%.6f ticks=%s n=%d", a, pt.ts, pt.close, pt.tick_count, len(prices.points(a)))
                except Exception:
                    log.exception("price poll failed")

            if now_mono - last_books >= cfg.book_poll_seconds and markets:
                now_utc = datetime.now(timezone.utc)
                for m in markets:
                    if not m.slug:
                        continue
                    books.note_window(m.slug, m.window_start)
                    # Do not evaluate or treat an already-ended window as current.
                    if m.window_end <= now_utc:
                        continue
                    book = books.poll_bbo(m.slug)
                    if book is None:
                        continue
                    if m.window_start > now_utc:
                        continue  # prefetch quotes only; do not trade the next window yet
                    log.info(
                        "book %s %s/%s up_ask=%s down_ask=%s spread=%s bid_sh=%s ask_sh=%s ptb=%s rem=%.0fs",
                        m.asset, m.horizon, m.slug,
                        book.up_ask, book.down_ask,
                        f"{book.spread:.3f}" if book.spread is not None else None,
                        book.bid_shares, book.ask_shares,
                        m.price_to_beat, m.seconds_remaining(),
                    )
                    sigs = engine.evaluate(m, book, prices.points(m.asset))
                    signals_by_slug[m.slug] = sigs
                    for sig in sigs:
                        eval_count += 1
                        store.write_evaluation(_ts(cfg.timezone), sig)
                        if sig.passed:
                            pass_count += 1
                            log.info(
                                "SIGNAL PASS %s %s p=%.3f ask=%.3f edge=%.4f rem=%.0fs",
                                sig.market_slug, sig.side, sig.model_prob, sig.ask,
                                sig.edge_after_fees, sig.seconds_remaining,
                            )
                            filled = executor.try_fill(sig, m)
                            if filled and filled.status == "open":
                                fill_count += 1
                        else:
                            log.debug(
                                "signal skip %s %s: %s",
                                sig.market_slug, sig.side, "; ".join(sig.reasons),
                            )
                last_books = time.monotonic()

            price_map = {a: prices.points(a) for a in cfg.assets}
            settled = executor.settle_due({m.slug: m for m in markets}, price_map)
            settle_count += len(settled)

            if now_mono - last_equity >= 10:
                store.write_equity(
                    _ts(cfg.timezone),
                    executor.cash,
                    state.open_exposure,
                    state.realized_pnl,
                    state.open_count,
                )
                last_equity = time.monotonic()

            loops += 1
            try:
                live.maybe_write(build_live_state(
                    cfg,
                    markets=markets,
                    prices=prices,
                    books=books,
                    signals_by_slug=signals_by_slug,
                    store=store,
                    risk=risk,
                    state=state,
                    cash=executor.cash,
                    pid=os.getpid(),
                    loops=loops,
                    stop_at=stop_at,
                    wrapper_pid=wrapper_pid,
                ))
            except Exception:
                log.exception("live_state write failed")
            time.sleep(0.25)
    finally:
        # final equity snapshot
        store.write_equity(
            _ts(cfg.timezone),
            executor.cash,
            state.open_exposure,
            state.realized_pnl,
            state.open_count,
        )
        orders = store.all_orders()
        open_n = sum(1 for o in orders if o.status == "open")
        filled_n = sum(1 for o in orders if o.status in ("open", "settled"))
        rejected_n = sum(1 for o in orders if o.status == "rejected")
        settled_n = sum(1 for o in orders if o.status == "settled")
        wins = sum(1 for o in orders if o.result == "win")
        losses = sum(1 for o in orders if o.result == "loss")
        realized = sum((o.pnl_usd or 0) for o in orders if o.status == "settled")
        log.info("=" * 60)
        log.info("RUN SUMMARY (America/New_York times in log lines)")
        log.info("loops=%d evaluations=%d passed_rules=%d", loops, eval_count, pass_count)
        log.info("paper fills this process=%d settlements this process=%d", fill_count, settle_count)
        log.info(
            "store orders: total=%d filled=%d open=%d settled=%d rejected=%d wins=%d losses=%d realized_pnl=$%.2f",
            len(orders), filled_n, open_n, settled_n, rejected_n, wins, losses, realized,
        )
        log.info("ending cash=$%.2f starting=$%.2f open_exposure=$%.2f", executor.cash, cfg.starting_cash, state.open_exposure)
        log.info("sqlite=%s", cfg.sqlite_path)
        log.info("trades csv=%s", cfg.trades_csv)
        log.info("evaluations csv=%s", cfg.evaluations_csv)
        store.close()
    return 0
