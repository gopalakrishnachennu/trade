import math
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polymarket_bot.config import load_config
from polymarket_bot.executor import settlement_average, round_settlement
from polymarket_bot.fees import edge_after_fees, taker_fee_usd
from polymarket_bot.models import Book, BookLevel, Market, PaperOrder, PricePoint, parse_iso
from polymarket_bot.probability import norm_cdf, prob_settlement_up
from polymarket_bot.risk import RiskManager, RiskState
from polymarket_bot.sizing import kelly_fraction, size_order
from polymarket_bot.store import Store
from polymarket_bot.models import Signal


def sample_market(**kw):
    start = datetime(2026, 11, 24, 15, 30, tzinfo=timezone.utc)
    defaults = dict(
        id="1",
        slug="cpc-btc-updown-15m-2026-11-24-1530z",
        question="BTC Up or Down 15m",
        asset="btc",
        horizon="15m",
        index_symbol="BRTI",
        window_start=start,
        window_end=start + timedelta(minutes=15),
        price_to_beat=60000.0,
        price_decimals=2,
        fee_coefficient=0.095,
        min_trade_qty=0.01,
        tick_size=0.01,
        status="MARKET_STATUS_OPEN",
        active=True,
        closed=False,
    )
    defaults.update(kw)
    return Market(**defaults)


class TestFeesAndSizing(unittest.TestCase):
    def test_taker_fee_bankers_rounding(self):
        # Θ × C × p × (1-p) = 0.095 * 10 * 0.5 * 0.5 = 0.2375 -> 0.24
        self.assertEqual(taker_fee_usd(0.095, 10, 0.5), 0.24)
        # 0.025 -> 0.02 (half to even)
        self.assertEqual(taker_fee_usd(0.1, 1, 0.5), 0.02)  # 0.1*1*0.5*0.5=0.025

    def test_edge_after_fees(self):
        # q=0.7, ask=0.5, theta=0.095, fee=0.095*0.5*0.5=0.02375, edge=0.7-0.5-0.02375
        self.assertAlmostEqual(edge_after_fees(0.7, 0.5, 0.095), 0.17625, places=6)

    def test_kelly_positive_and_zero(self):
        self.assertGreater(kelly_fraction(0.7, 0.5, 0.02), 0)
        self.assertEqual(kelly_fraction(0.5, 0.5, 0.02), 0.0)

    def test_size_respects_caps(self):
        cfg = load_config(ROOT / "config.yaml")
        sig = Signal(
            market_slug="x", asset="btc", horizon="15m", side="up",
            model_prob=0.8, ask=0.40, fee_per_contract=0.095 * 0.4 * 0.6,
            edge_after_fees=0.2, spread=0.02, liquidity_contracts=1000,
            spot=60100, price_to_beat=60000, seconds_remaining=400,
            annualized_vol=0.5, passed=True,
        )
        contracts, cost, fee = size_order(sig, sample_market(), cash=1000, cfg=cfg)
        self.assertGreater(contracts, 0)
        self.assertLessEqual(cost, cfg.max_per_bet_usd + 1e-9)
        self.assertLessEqual(contracts, cfg.max_contracts)
        self.assertGreaterEqual(cost, contracts * sig.ask)


class TestProbability(unittest.TestCase):
    def test_norm_cdf_center(self):
        self.assertAlmostEqual(norm_cdf(0.0), 0.5, places=6)

    def test_deep_in_the_money(self):
        now = 1_000_000
        pts = [PricePoint(ts=now - i, close=65000.0, tick_count=1) for i in range(120, 0, -1)]
        p = prob_settlement_up(pts, strike=60000.0, window_end_ts=now + 600, now_ts=now, annualized_vol=0.5)
        self.assertIsNotNone(p)
        self.assertGreater(p, 0.9)

    def test_inside_settlement_minute_locks_prints(self):
        end = 2_000_000
        # 50 known seconds all at 100, 10 remaining. Strike 100. Already decided almost surely up.
        pts = [PricePoint(ts=t, close=100.0, tick_count=1) for t in range(end - 60, end - 10)]
        pts.append(PricePoint(ts=end - 10, close=100.0, tick_count=1))
        p = prob_settlement_up(pts, strike=90.0, window_end_ts=end, now_ts=end - 10, annualized_vol=0.5)
        self.assertGreater(p, 0.99)


class TestSettlement(unittest.TestCase):
    def test_average_interval(self):
        end = 5_000_000
        # only [end-60, end) should count; a print at end is excluded
        pts = [PricePoint(ts=end - 60 + i, close=float(i), tick_count=1) for i in range(60)]
        pts.append(PricePoint(ts=end, close=999.0, tick_count=1))
        avg = settlement_average(pts, end)
        self.assertIsNotNone(avg)
        self.assertAlmostEqual(avg, sum(range(60)) / 60.0)
        self.assertEqual(round_settlement(10.125, 2), 10.12)  # python round half even? 10.125 -> 10.12
        # 10.135 -> 10.14? depends; just check two-decimal rounding works
        self.assertEqual(round_settlement(10.124, 2), 10.12)


class TestParseIso(unittest.TestCase):
    def test_z_and_long_fraction(self):
        dt = parse_iso("2026-11-24T15:43:44.517872055Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo is not None or dt.utcoffset() is not None, True)
        self.assertEqual(parse_iso("2026-11-24T15:45:00Z").minute, 45)


class TestRiskAndStore(unittest.TestCase):
    def test_kill_switch_and_store_roundtrip(self):
        cfg = load_config(ROOT / "config.yaml")
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            store = Store(d / "t.sqlite", d / "t.csv", d / "e.csv", d / "eq.csv")
            risk = RiskManager(cfg)
            state = RiskState(day=date_today(), starting_day_equity=cfg.starting_day_equity)
            order = PaperOrder(
                id="abc", ts="2026-11-24T10:00:00-05:00", market_slug="m",
                asset="btc", horizon="5m", side="up", contracts=1, price=0.4,
                fee_usd=0.02, cost_usd=0.42, model_prob=0.7, edge_after_fees=0.1,
                price_to_beat=1.0, window_end="2026-11-24T16:00:00+00:00", status="open",
            )
            store.write_order(order)
            risk.register_open(state, order)
            reason = risk.allow(state, "m", 10, cash=1000)
            self.assertIn("already have an open position", reason)
            opened = store.open_orders()
            self.assertEqual(len(opened), 1)
            self.assertEqual(opened[0].market_slug, "m")
            store.close()

    def test_config_rejects_live_mode(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "c.yaml"
            p.write_text("mode: live\n")
            with self.assertRaises(ValueError):
                load_config(p)


def date_today():
    from datetime import date
    return date.today()


class TestBookHelpers(unittest.TestCase):
    def test_spread_and_liquidity(self):
        b = Book(
            slug="s", best_bid=0.48, best_ask=0.52, long_quote=0.52, short_quote=0.53,
            bid_shares=100, ask_shares=80,
            bids=[BookLevel(0.48, 100)], offers=[BookLevel(0.52, 80)],
        )
        self.assertAlmostEqual(b.spread, 0.05)
        self.assertEqual(b.up_ask, 0.52)
        self.assertEqual(b.down_ask, 0.53)
        self.assertEqual(b.liquidity_contracts("up"), 80)
        self.assertEqual(b.liquidity_contracts("down"), 100)


class TestDailyRiskAndStrategy(unittest.TestCase):
    def test_strategy_file_is_merged(self):
        cfg = load_config(ROOT / "config.yaml")
        self.assertEqual(cfg.starting_cash, 1000.0)
        self.assertEqual(cfg.daily_profit_target_pct, 0.05)
        self.assertEqual(cfg.daily_loss_limit_pct, 0.10)
        self.assertAlmostEqual(cfg.daily_profit_target_usd, 50.0)
        self.assertAlmostEqual(cfg.daily_loss_limit_usd, 100.0)
        self.assertEqual(cfg.min_edge, 0.04)
        self.assertEqual(cfg.strategy_name, "default")
        self.assertEqual(cfg.stop_at, "23:00")

    def test_profit_target_blocks_new_entries_only(self):
        # Legacy fixed-target mode (profit_lock.mode: fixed).
        from polymarket_bot.config import Config, deep_merge
        base = load_config(ROOT / "config.yaml")
        cfg = Config(raw=deep_merge(base.raw, {"risk": {"profit_lock": {"mode": "fixed"}}}), root=base.root)
        risk = RiskManager(cfg)
        state = RiskState(day=date_today(), starting_day_equity=1000.0, realized_pnl=50.0)
        reason = risk.allow(state, "new-market", cost=10, cash=1000)
        self.assertIsNotNone(reason)
        self.assertIn("profit target", reason)
        # Open inventory is not force-closed by allow(); settlement path does not call allow().
        self.assertIsNotNone(risk.daily_halt_reason(state))

    def test_loss_limit_blocks_new_entries(self):
        cfg = load_config(ROOT / "config.yaml")
        risk = RiskManager(cfg)
        state = RiskState(day=date_today(), starting_day_equity=1000.0, realized_pnl=-100.0)
        reason = risk.allow(state, "new-market", cost=10, cash=900)
        self.assertIsNotNone(reason)
        self.assertIn("loss limit", reason)

    def test_under_limits_still_allowed(self):
        cfg = load_config(ROOT / "config.yaml")
        risk = RiskManager(cfg)
        state = RiskState(day=date_today(), starting_day_equity=1000.0, realized_pnl=49.99)
        self.assertIsNone(risk.allow(state, "m", cost=10, cash=1000))
        state.realized_pnl = -99.99
        self.assertIsNone(risk.allow(state, "m", cost=10, cash=1000))


if __name__ == "__main__":
    unittest.main()


class LiveStateTests(unittest.TestCase):
    def test_book_mark_price_down_from_ask(self):
        from polymarket_bot.models import Book

        book = Book(slug="x", best_bid=0.42, best_ask=0.44)
        self.assertAlmostEqual(book.mark_price("up"), 0.42)
        self.assertAlmostEqual(book.down_bid, 0.56)
        self.assertAlmostEqual(book.mark_price("down"), 0.56)

    def test_atomic_live_state_write_and_rate_limit(self):
        import json
        import tempfile
        from pathlib import Path

        from polymarket_bot.live_state import LiveStateWriter, write_atomic

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "live_state.json"
            write_atomic(path, {"ok": True, "n": 1})
            self.assertEqual(json.loads(path.read_text())["n"], 1)
            writer = LiveStateWriter(path, min_interval=60.0)
            writer.maybe_write({"n": 2})
            self.assertEqual(json.loads(path.read_text())["n"], 2)
            writer.maybe_write({"n": 3})
            self.assertEqual(json.loads(path.read_text())["n"], 2)


class TestWindowDiscovery(unittest.TestCase):
    def test_expected_slug_and_alignment(self):
        from datetime import datetime, timezone
        from polymarket_bot.discovery import expected_slug, window_start_utc, horizon_minutes

        now = datetime(2026, 11, 24, 17, 1, 30, tzinfo=timezone.utc)
        self.assertEqual(window_start_utc(now, "5m").strftime("%H%M"), "1700")
        self.assertEqual(window_start_utc(now, "5m", 1).strftime("%H%M"), "1705")
        self.assertEqual(window_start_utc(now, "15m").strftime("%H%M"), "1700")
        self.assertEqual(window_start_utc(now, "1h").strftime("%H%M"), "1700")
        self.assertEqual(horizon_minutes("5m"), 5)
        self.assertEqual(
            expected_slug("btc", "5m", window_start_utc(now, "5m")),
            "cpc-btc-updown-5m-2026-11-24-1700z",
        )
        self.assertEqual(
            expected_slug("eth", "5m", window_start_utc(now, "5m")),
            "cpceth-eth-updown-5m-2026-11-24-1700z",
        )

    def test_current_market_ignores_ended(self):
        from datetime import datetime, timedelta, timezone
        from polymarket_bot.discovery import current_market
        from polymarket_bot.models import Market

        now = datetime(2026, 11, 24, 17, 0, 10, tzinfo=timezone.utc)
        def M(**kw):
            base=dict(question="", index_symbol="BRTI", price_to_beat=None, price_decimals=2,
                      fee_coefficient=None, min_trade_qty=0.01, tick_size=0.01,
                      status="MARKET_STATUS_OPEN", active=True, closed=False)
            base.update(kw)
            return Market(**base)
        ended = M(id="1", slug="old", asset="btc", horizon="5m",
                  window_start=now - timedelta(minutes=5),
                  window_end=now - timedelta(seconds=10))
        live = M(id="2", slug="new", asset="btc", horizon="5m",
                 window_start=now - timedelta(seconds=10),
                 window_end=now + timedelta(minutes=5),
                 price_to_beat=100.0)
        self.assertEqual(current_market([ended, live], "btc", "5m", now).slug, "new")
        self.assertIsNone(current_market([ended], "btc", "5m", now))

    def test_should_rediscover_near_end_or_missing(self):
        from datetime import datetime, timedelta, timezone
        from polymarket_bot.config import load_config
        from polymarket_bot.discovery import should_rediscover
        from polymarket_bot.models import Market

        cfg = load_config(ROOT / "config.yaml")
        now = datetime(2026, 11, 24, 16, 59, 55, tzinfo=timezone.utc)
        def mk(asset, horizon, rem):
            end = now + timedelta(seconds=rem)
            start = end - timedelta(minutes={"5m": 5, "15m": 15, "1h": 60}[horizon])
            return Market(
                id="", slug=f"{asset}-{horizon}", question="", asset=asset, horizon=horizon,
                index_symbol="", window_start=start, window_end=end, price_to_beat=1.0,
                price_decimals=2, fee_coefficient=None, min_trade_qty=0.01, tick_size=0.01,
                status="MARKET_STATUS_OPEN", active=True, closed=False,
            )
        almost = [mk(a, h, 30) for a in cfg.assets for h in cfg.horizons]
        almost = [m if not (m.asset == "btc" and m.horizon == "5m") else mk("btc", "5m", 8) for m in almost]
        self.assertTrue(should_rediscover(almost, cfg, now=now, lead=10))
        full = [mk(a, h, 120) for a in cfg.assets for h in cfg.horizons]
        self.assertFalse(should_rediscover(full, cfg, now=now, lead=10))
        self.assertTrue(should_rediscover([], cfg, now=now))


class TestBookFallback(unittest.TestCase):
    def test_parse_book_derives_quotes_from_levels(self):
        from polymarket_bot.feeds import parse_book

        payload = {
            "marketData": {
                "marketSlug": "x",
                "bids": [{"px": {"value": "0.32", "currency": "USD"}, "qty": "10"},
                         {"px": {"value": "0.31", "currency": "USD"}, "qty": "5"}],
                "offers": [{"px": {"value": "0.34", "currency": "USD"}, "qty": "8"},
                           {"px": {"value": "0.35", "currency": "USD"}, "qty": "4"}],
            }
        }
        book = parse_book("x", {}, payload)
        self.assertAlmostEqual(book.best_bid, 0.32)
        self.assertAlmostEqual(book.best_ask, 0.34)
        self.assertAlmostEqual(book.up_ask, 0.34)
        self.assertAlmostEqual(book.down_ask, 0.68)
