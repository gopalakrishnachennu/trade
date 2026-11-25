import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from polymarket_bot import analytics, patterns, profit_lock as pl, reviewer, risk, sizing, verdict
from polymarket_bot.config import Config, load_config
from polymarket_bot.models import Market, PaperOrder, Signal
from polymarket_bot.runtime import controls
from polymarket_bot.store import Store

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


def _mk_store(td: Path) -> Store:
    return Store(td / "paper.sqlite", td / "trades.csv", td / "evaluations.csv", td / "equity.csv")


def _order_dict(oid, ts_dt, settle_dt, *, asset="btc", horizon="15m", side="up",
                cost=20.0, fee=0.20, pnl=10.0, price=0.50, edge=0.08, mp=0.58, status="settled"):
    contracts = cost / price
    return {
        "id": oid,
        "ts": ts_dt.isoformat(),
        "market_slug": f"{asset}-updown-{horizon}-{int(settle_dt.timestamp())}",
        "asset": asset,
        "horizon": horizon,
        "side": side,
        "price": price,
        "contracts": contracts,
        "cost_usd": cost,
        "fee_usd": fee,
        "model_prob": mp,
        "edge_after_fees": edge,
        "status": status,
        "result": ("win" if pnl > 0 else "loss") if status == "settled" else None,
        "pnl_usd": pnl if status == "settled" else None,
        "window_end": settle_dt.isoformat(),
        "settlement_price": 100.0 if status == "settled" else None,
        "price_to_beat": 99.0,
        "reject_reason": None,
    }


def _paper_order(oid, ts_dt, settle_dt, *, pnl=10.0, cost=20.0, price=0.50):
    d = _order_dict(oid, ts_dt, settle_dt, pnl=pnl, cost=cost, price=price)
    return PaperOrder(
        id=d["id"], ts=d["ts"], market_slug=d["market_slug"], asset=d["asset"],
        horizon=d["horizon"], side=d["side"], price=d["price"], contracts=d["contracts"],
        cost_usd=d["cost_usd"], fee_usd=d["fee_usd"], model_prob=d["model_prob"],
        edge_after_fees=d["edge_after_fees"], status=d["status"], result=d["result"],
        pnl_usd=d["pnl_usd"], window_end=d["window_end"],
        settlement_price=d["settlement_price"], price_to_beat=d["price_to_beat"],
        reject_reason=d["reject_reason"],
    )


class TestContinuousAccounting(unittest.TestCase):
    def test_lifetime_equity_and_today_split_across_midnight(self):
        d1 = datetime(2026, 11, 24, 23, 0, tzinfo=ET)
        d1_end = datetime(2026, 11, 24, 23, 15, tzinfo=ET)
        d2 = datetime(2026, 11, 25, 0, 10, tzinfo=ET)
        d2_end = datetime(2026, 11, 25, 0, 25, tzinfo=ET)
        rows = [
            _order_dict("o1", d1, d1_end, pnl=140.59, cost=50.0, fee=0.50),
            _order_dict("o2", d2, d2_end, pnl=12.00, cost=20.0, fee=0.20),
        ]
        now = datetime(2026, 11, 25, 0, 30, tzinfo=ET).timestamp()
        bundle = analytics.ledger_bundle(
            rows, 1000.0, 0.05, 0.10, ["btc"], ["15m"], "America/New_York",
            now=now, day=date(2026, 11, 25), lock_params=pl.LockParams(),
            window_mode="rolling_24h", window_hours=24.0,
        )
        k = bundle["kpis"]
        self.assertAlmostEqual(k["equity"], 1152.59, places=2)
        self.assertAlmostEqual(k["net_pnl"], 152.59, places=2)
        self.assertAlmostEqual(k["staked_total"], 70.00, places=2)
        self.assertEqual(k["bets"], 2)
        self.assertEqual(k["today"]["day"], "2026-11-25")
        self.assertEqual(k["today"]["bets"], 1)
        self.assertAlmostEqual(k["today"]["net_pnl"], 12.00, places=2)
        self.assertAlmostEqual(k["staked_today"], 20.00, places=2)
        self.assertEqual(len(bundle["fills"]), 2)
        self.assertGreaterEqual(len(bundle["curve"]), 3)
        self.assertAlmostEqual(bundle["curve"][-1]["equity"], 1152.59, places=2)


class TestRiskWindowAndRollover(unittest.TestCase):
    def test_midnight_rollover_clears_stale_profit_lock_and_preserves_equity(self):
        cfg = load_config("config.yaml")
        cfg_cal = Config(raw={**cfg.raw, "risk": {**cfg.raw.get("risk", {}), "window_mode": "calendar_day"}}, root=cfg.root)
        cfg_roll = Config(raw={**cfg.raw, "risk": {**cfg.raw.get("risk", {}), "window_mode": "rolling_24h"}}, root=cfg.root)
        with tempfile.TemporaryDirectory() as td:
            db = _mk_store(Path(td))
            t1 = datetime(2026, 11, 24, 22, 0, tzinfo=ET)
            t2 = datetime(2026, 11, 24, 23, 0, tzinfo=ET)
            t3 = datetime(2026, 11, 24, 23, 30, tzinfo=ET)
            t4 = datetime(2026, 11, 24, 23, 50, tzinfo=ET)
            t6 = datetime(2026, 11, 24, 23, 55, tzinfo=ET)
            for o in (
                _paper_order("w1", t1, t2, pnl=181.19),
                _paper_order("l1", t2, t3, pnl=-59.17),  # +122.02 <= 126.83 => STOPPED_FLOOR
                _paper_order("w2", t4, t6, pnl=18.57),   # settled at midnight ET => +140.59
            ):
                db.write_order(o)

            # 1) calendar_day mode: at 00:01 ET on Nov 25, stale Nov 24 lock clears while preserving $1,140.59 equity
            rm_cal = risk.RiskManager(cfg_cal)
            st_cal, cash_cal = risk.restore_state(cfg_cal, db, rm_cal, now=datetime(2026, 11, 24, 23, 56, tzinfo=ET).timestamp())
            self.assertAlmostEqual(st_cal.realized_pnl, 140.59, places=2)
            # Simulate crossing midnight from Nov 24 -> Nov 25 00:01 ET
            t_mid = datetime(2026, 11, 25, 0, 1, tzinfo=ET).timestamp()
            rm_cal.apply_daily_halts(st_cal, now=t_mid)
            self.assertAlmostEqual(cash_cal, 1140.59, places=2)
            self.assertAlmostEqual(st_cal.starting_day_equity, 1140.59, places=2)
            self.assertIsNone(st_cal.kill_reason)
            self.assertIsNone(rm_cal.allow(st_cal, "btc-updown-15m-next", 20.0, cash_cal, now=t_mid))

            # 2) rolling_24h mode: at 00:01 ET Nov 25 the 23:30 floor breach is still inside the 24h
            # window and inside the 60-minute cooldown (until 00:30 ET) -> COOLDOWN, entries blocked
            rm_roll = risk.RiskManager(cfg_roll)
            t_0001 = datetime(2026, 11, 25, 0, 1, tzinfo=ET).timestamp()
            st_roll, cash_roll = risk.restore_state(cfg_roll, db, rm_roll, now=t_0001)
            lk = rm_roll.lock_state(st_roll, now=t_0001)
            self.assertEqual(lk.phase, "COOLDOWN")
            self.assertIsNotNone(st_roll.kill_reason)
            self.assertIsNotNone(rm_roll.allow(st_roll, "btc-updown-15m-next", 20.0, cash_roll, now=t_0001))

            # Once 24h has passed since all Nov 24 settlements, lock clears automatically without losing equity
            t_24h = datetime(2026, 11, 26, 0, 5, tzinfo=ET).timestamp()
            rm_roll.apply_daily_halts(st_roll, now=t_24h)
            lk2 = rm_roll.lock_state(st_roll, now=t_24h)
            self.assertAlmostEqual(cash_roll, 1140.59, places=2)
            self.assertAlmostEqual(st_roll.starting_day_equity, 1140.59, places=2)
            self.assertEqual(lk2.phase, "TRADING")
            self.assertIsNone(st_roll.kill_reason)
            self.assertIsNone(rm_roll.allow(st_roll, "btc-updown-15m-next", 20.0, cash_roll, now=t_24h))

    def test_limits_scale_with_window_start_equity(self):
        cfg = load_config("config.yaml")
        with tempfile.TemporaryDirectory() as td:
            db = _mk_store(Path(td))
            old_t = datetime(2026, 11, 22, 12, 0, tzinfo=ET)
            old_end = datetime(2026, 11, 22, 12, 15, tzinfo=ET)
            db.write_order(_paper_order("old", old_t, old_end, pnl=1000.0))
            rm = risk.RiskManager(cfg)
            t_now = datetime(2026, 11, 25, 12, 0, tzinfo=ET).timestamp()
            st, cash = risk.restore_state(cfg, db, rm, now=t_now)
            lk = rm.lock_state(st, now=t_now)
            self.assertAlmostEqual(cash, 2000.0, places=2)
            self.assertAlmostEqual(st.starting_day_equity, 2000.0, places=2)
            self.assertAlmostEqual(lk.loss_limit_usd, 200.0, places=2)
            self.assertAlmostEqual(lk.target_usd, 100.0, places=2)


class TestPatternLearning(unittest.TestCase):
    def test_feature_extraction_and_bayesian_posteriors(self):
        ts = datetime(2026, 11, 25, 1, 15, tzinfo=ET).timestamp()
        feat = patterns.extract_features(
            {
                "asset": "btc", "horizon": "15m", "side": "up",
                "seconds_remaining": 450.0, "spot": 90500.0, "price_to_beat": 90000.0,
                "annualized_vol": 0.60, "model_prob": 0.65, "ask": 0.50,
                "edge_after_fees": 0.14, "spread": 0.02, "liquidity_contracts": 50.0,
            },
            ts=ts, tz_name="America/New_York", strategy="baseline",
        )
        self.assertEqual(feat["hour_bucket"], "00-03h")
        self.assertEqual(feat["weekday"], "Wed")
        self.assertIsNotNone(feat["distance_sigma"])
        self.assertGreater(feat["distance_sigma"], 0.0)
        self.assertEqual(feat["vol_regime"], "low")

        ps = patterns.PatternStore(prior_strength=10.0)
        d0 = ps.evaluate(feat, mode="active", min_samples=20)
        self.assertTrue(d0["passed"])
        self.assertEqual(d0["mult"], 1.0)

        for i in range(25):
            self.assertTrue(ps.update_trade(feat, order_id=f"lose-{i}", pnl_usd=-10.0, cost_usd=10.0, outcome="loss"))
        self.assertFalse(ps.update_trade(feat, order_id="lose-0", pnl_usd=-10.0, cost_usd=10.0, outcome="loss"))

        d_sh = ps.evaluate(feat, mode="shadow", min_samples=20, skip_ev_threshold=-0.02, record_shadow=True)
        self.assertTrue(d_sh["passed"])
        self.assertEqual(d_sh["mult"], 1.0)
        self.assertEqual(d_sh["would_action"], "skip")
        self.assertEqual(ps.shadow_stats["would_skip"], 1)

        d_act = ps.evaluate(feat, mode="active", min_samples=20, skip_ev_threshold=-0.02)
        self.assertFalse(d_act["passed"])
        self.assertIn("skip", d_act["detail"])

        d_off = ps.evaluate(feat, mode="off", min_samples=20)
        self.assertTrue(d_off["passed"])
        self.assertEqual(d_off["mult"], 1.0)

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            patterns.save_pattern_store(d, ps)
            loaded = patterns.load_pattern_store(d)
            self.assertEqual(loaded.global_stat.n, 25)
            db = _mk_store(d)
            db.write_trade_pattern({**feat, "order_id": "lose-0", "ts": "2026-11-25T01:15:00-05:00", "outcome": "loss", "pnl_usd": -10.0, "cost_usd": 10.0, "roi": -1.0})
            self.assertEqual(len(db.all_trade_patterns()), 1)

    def test_pattern_mult_respects_sizing_hard_caps(self):
        cfg = load_config("config.yaml")
        mkt = Market(
            id="m1", slug="btc-updown-15m-1795600000", question="BTC 15m", asset="btc", horizon="15m",
            index_symbol="BTC-USD",
            window_start=datetime(2026, 11, 25, 0, 0, tzinfo=ET),
            window_end=datetime(2026, 11, 25, 0, 15, tzinfo=ET),
            price_to_beat=90000.0, price_decimals=2, fee_coefficient=0.02,
            min_trade_qty=0.01, tick_size=0.01, status="open", active=True, closed=False,
        )
        sig = Signal(
            market_slug=mkt.slug, asset="btc", horizon="15m", side="up",
            model_prob=0.70, ask=0.50, fee_per_contract=0.005, edge_after_fees=0.19,
            spread=0.02, liquidity_contracts=1000.0, spot=90200.0, price_to_beat=90000.0,
            seconds_remaining=600.0, annualized_vol=0.60, passed=True,
        )
        base_c, base_cost, _ = sizing.size_order(sig, mkt, cash=1000.0, cfg=cfg, pattern_mult=1.0)
        boost_c, boost_cost, _ = sizing.size_order(sig, mkt, cash=1000.0, cfg=cfg, pattern_mult=5.0)
        self.assertLessEqual(boost_cost, cfg.max_per_bet_usd + 1e-6)
        self.assertLessEqual(boost_cost, 1000.0 * cfg.max_fraction_of_bankroll + 1e-6)
        self.assertGreaterEqual(boost_c, base_c)
        self.assertGreaterEqual(boost_cost, base_cost)

    def test_verdict_checklist_includes_pattern_row(self):
        row = {"slug": "btc-updown-15m-1795600000"}
        up_sig = {
            "edge_after_fees": 0.09, "ask": 0.51, "model_prob": 0.62,
            "rule_checks": [
                {"name": "price_to_beat_known", "label": "Strike", "passed": True, "value": 90000.0, "threshold": "known"},
                {"name": "min_edge_after_fees", "label": "Edge", "passed": True, "value": 0.09, "threshold": 0.04},
                {"name": "pattern", "label": "Pattern", "passed": True, "value": "1.00x (shadow)", "threshold": "shadow",
                 "detail": "pattern (shadow): BTC 15m 00-03h, n=5 < 20 -> 1.00x"},
            ],
        }
        v = verdict.decide(row, up_sig, None, stake_up=15.0)
        self.assertEqual(v["verdict"], "BUY UP")
        names = [c["name"] for c in v["up"]["checks"]]
        self.assertIn("pattern", names)

        up_skip = {
            "edge_after_fees": 0.09, "ask": 0.51, "model_prob": 0.62,
            "rule_checks": [
                {"name": "price_to_beat_known", "label": "Strike", "passed": True, "value": 90000.0, "threshold": "known"},
                {"name": "min_edge_after_fees", "label": "Edge", "passed": True, "value": 0.09, "threshold": 0.04},
                {"name": "pattern", "label": "Pattern", "passed": False, "value": "skip", "threshold": "EV > -2%",
                 "detail": "pattern: BTC 15m 00-03h ET, n=42, EV -3.1% -> skip"},
            ],
        }
        v2 = verdict.decide(row, up_skip, None, stake_up=15.0)
        self.assertEqual(v2["verdict"], "NO TRADE")
        self.assertIn("BTC 15m", v2["reason"])


class TestPhase4Integration(unittest.TestCase):
    def test_controls_and_reviewer_payload_and_patterns_api(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            res = controls.apply(
                d, {"window_mode": "calendar_day", "pattern_mode": "active", "pattern_min_samples": 15},
                actor="test",
            )
            vals = res["controls"]["values"]
            self.assertEqual(vals["window_mode"], "calendar_day")
            self.assertEqual(vals["pattern_mode"], "active")
            self.assertEqual(vals["pattern_min_samples"], 15)

        payload = reviewer.build_payload(
            {"active": "baseline", "strategies": {}},
            reviewer.DEFAULT_SETTINGS,
            pattern_summary={"global": {"n": 10}, "tables": {"context": [], "vol_regime": [], "distance_bucket": []}, "shadow_stats": {}},
        )
        self.assertIn("patterns", payload)
        self.assertEqual(payload["patterns"]["global"]["n"], 10)

        td_web = tempfile.TemporaryDirectory()
        try:
            d = Path(td_web.name)
            (d / "runtime").mkdir()
            os.environ["DASHBOARD_DATA"] = str(d)
            os.environ["DASHBOARD_TOKEN"] = "test-token-p4"
            os.environ["DASHBOARD_CONFIG"] = str(ROOT / "config.yaml")
            sys.path.insert(0, str(ROOT / "web"))
            sys.modules.pop("app", None)
            import app as appmod
            client = TestClient(appmod.app)
            r_page = client.get("/patterns?token=test-token-p4")
            self.assertEqual(r_page.status_code, 200)
            self.assertIn("PATTERN LEARNING", r_page.text)
            r_api = client.get("/api/patterns?token=test-token-p4")
            self.assertEqual(r_api.status_code, 200)
            j = r_api.json()
            self.assertIn("heatmap", j)
            self.assertIn("tables", j)
            self.assertIn("config", j)
        finally:
            os.environ.pop("DASHBOARD_DATA", None)
            os.environ.pop("DASHBOARD_TOKEN", None)
            os.environ.pop("DASHBOARD_CONFIG", None)
            td_web.cleanup()


if __name__ == "__main__":
    unittest.main()
