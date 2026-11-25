"""Dashboard redesign: ledger metrics, timestamp ordering, controls validation, verdict reasons, API auth."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from polymarket_bot import analytics
from polymarket_bot.config import load_config
from polymarket_bot.runtime import controls as ctl
from polymarket_bot.runtime.bus import Bus
from polymarket_bot.verdict import decide, describe_check

ROOT = Path(__file__).resolve().parents[1]
TZ = "America/New_York"


def order(i, ts, pnl, *, status="settled", cost=10.0, fee=0.5, end=None, asset="btc", hz="15m", side="up", note=None):
    return {
        "id": f"o{i}", "ts": ts, "market_slug": f"m{i}", "asset": asset, "horizon": hz, "side": side,
        "price": 0.4, "contracts": 25.0, "cost_usd": cost, "fee_usd": fee, "model_prob": 0.6,
        "edge_after_fees": 0.1, "status": status, "result": None if status == "open" else ("win" if pnl > 0 else "loss"),
        "pnl_usd": None if status == "open" else pnl, "window_end": end or ts, "settlement_price": None,
        "price_to_beat": 100.0, "reject_reason": note,
    }


class TimestampTests(unittest.TestCase):
    def test_mixed_formats_parse_to_same_instant(self):
        a = analytics.ts_unix("2026-11-24 17:04:42.1")           # naive local (old writer)
        b = analytics.ts_unix("2026-11-24T17:04:42.100000-05:00")
        c = analytics.ts_unix("2026-11-24T22:04:42.1Z")
        d = analytics.ts_unix("2026-11-24T17:04:42.1234567-05:00")  # >6 fractional digits
        self.assertAlmostEqual(a, b, places=3)
        self.assertAlmostEqual(b, c, places=3)
        self.assertAlmostEqual(b, d, places=0)
        self.assertEqual(analytics.ts_unix(1795558000), 1795558000.0)
        self.assertIsNone(analytics.ts_unix("garbage"))

    def test_sort_is_chronological_not_textual(self):
        rows = [
            {"ts": "2026-11-24T16:00:00-05:00"},   # 4 PM
            {"ts": "2026-11-24 17:00:00"},          # 5 PM naive: text-sorts BEFORE the "T" row
            {"ts": "2026-11-24T15:00:00-05:00"},   # 3 PM
        ]
        self.assertLess("2026-11-24 17:00:00", "2026-11-24T15:00:00-05:00")  # the old bug
        out = [r["ts"] for r in analytics.sort_chronological(rows)]
        self.assertEqual(out, ["2026-11-24T15:00:00-05:00", "2026-11-24T16:00:00-05:00", "2026-11-24 17:00:00"])

    def test_store_equity_snapshots_sorted(self):
        from polymarket_bot.store import Store
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            s = Store(td / "x.sqlite", td / "t.csv", td / "e.csv", td / "q.csv")
            s.write_equity("2026-11-24 17:00:00", 1, 0, 0, 0)
            s.write_equity("2026-11-24T15:00:00-05:00", 2, 0, 0, 0)
            s.write_equity("2026-11-24T16:00:00-05:00", 3, 0, 0, 0)
            self.assertEqual([r["cash"] for r in s.equity_snapshots()], [2, 3, 1])
            s.close()


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            order(1, "2026-11-24T11:00:00-05:00", 10.0, end="2026-11-24T11:15:00-05:00"),
            order(2, "2026-11-24T11:05:00-05:00", -8.0, end="2026-11-24T11:20:00-05:00", asset="eth"),
            order(3, "2026-11-24T11:10:00-05:00", -4.0, end="2026-11-24T11:25:00-05:00", hz="5m"),
            order(4, "2026-11-24 11:12:00", 6.0, end="2026-11-24T11:30:00-05:00"),       # naive ts
            order(5, "2026-11-24T11:40:00-05:00", 0.0, status="open", cost=12.0, end="2026-11-24T12:00:00-05:00"),
            order(6, "2026-11-23T11:40:00-05:00", 99.0),                                   # other day
            {**order(7, "2026-11-24T11:41:00-05:00", 0.0), "status": "rejected"},           # not a fill
        ]
        self.fills = analytics.fills_for_day(analytics.fills_from_rows(self.rows), analytics.day_of(analytics.ts_unix("2026-11-24T12:00:00-05:00")))

    def test_kpis(self):
        k = analytics.compute_kpis(self.fills, 1000.0, 0.05, 0.10)
        self.assertEqual(k["bets"], 5)
        self.assertEqual(k["settled"], 4)
        self.assertEqual(k["open_count"], 1)
        self.assertAlmostEqual(k["net_pnl"], 4.0)
        self.assertAlmostEqual(k["gross_winnings"], 16.0)
        self.assertAlmostEqual(k["gross_losses"], -12.0)
        self.assertEqual((k["wins"], k["losses"]), (2, 2))
        self.assertAlmostEqual(k["win_rate"], 0.5)
        self.assertAlmostEqual(k["profit_factor"], 16 / 12, places=3)
        self.assertAlmostEqual(k["staked_today"], 52.0)
        self.assertAlmostEqual(k["open_exposure"], 12.0)
        self.assertAlmostEqual(k["cash"], 1000 + 4 - 12)
        self.assertAlmostEqual(k["equity"], 1004.0)
        self.assertAlmostEqual(k["fees_paid"], 2.5)
        self.assertAlmostEqual(k["avg_win"], 8.0)
        self.assertAlmostEqual(k["avg_loss"], -6.0)
        self.assertAlmostEqual(k["roi_on_staked"], 4.0 / 40.0, places=4)
        # peak 1010 after #1, trough 998 after #2,#3 -> -12
        self.assertAlmostEqual(k["max_drawdown_usd"], -12.0)
        self.assertAlmostEqual(k["to_target_usd"], 46.0)
        self.assertAlmostEqual(k["to_loss_limit_usd"], 104.0)
        self.assertFalse(k["target_hit"])

    def test_equity_curve_steps_in_time_order(self):
        cv = analytics.equity_curve(self.fills, 1000.0, now=analytics.ts_unix("2026-11-24T12:30:00-05:00"))
        self.assertEqual([p["equity"] for p in cv], [1000, 1010, 1002, 998, 1004, 1004])
        ts = [p["ts"] for p in cv]
        self.assertEqual(ts, sorted(ts))
        self.assertEqual([p["kind"] for p in cv][1:5], ["win", "loss", "loss", "win"])

    def test_flattened_order_settles_at_flatten_time(self):
        r = order(9, "2026-11-24T17:19:00-05:00", -1.0, end="2026-11-24T18:00:00-05:00",
                  note="flattened at bid 0.220 by operator (flatten #2) at 2026-11-24T17:22:57-05:00")
        f = analytics.fills_from_rows([r])[0]
        self.assertEqual(f.settle_ts, analytics.ts_unix("2026-11-24T17:22:57-05:00"))

    def test_fills_table_running_equity_newest_first(self):
        t = analytics.fills_table(self.fills, 1000.0)
        self.assertEqual(len(t), 5)
        self.assertGreaterEqual(t[0]["ts"], t[-1]["ts"])
        self.assertIn("running_equity", t[0])

    def test_buckets_and_hist(self):
        b = {x["label"]: x for x in analytics.buckets(self.fills, ["btc", "eth"], ["5m", "15m"])}
        self.assertEqual(b["BTC 15m"]["n"], 3)
        self.assertEqual(b["ETH 15m"]["losses"], 1)
        h = analytics.edge_histogram([0.041, 0.049, -0.3, 0.3, float("nan")])
        self.assertEqual(h["total"], 4)
        self.assertEqual(sum(h["counts"]), 4)

    def test_real_ledger_if_present(self):
        db = ROOT / "data" / "paper_trades.sqlite"
        if not db.exists():
            self.skipTest("no ledger")
        rows = analytics.load_order_rows(db)
        self.assertTrue(all(r["status"] in ("open", "settled") for r in rows))


class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.rt = Path(self.td.name)
        self.cfg = load_config(ROOT / "config.yaml")

    def tearDown(self):
        self.td.cleanup()

    def test_rejects_unknown_and_live_keys(self):
        for bad in ({"mode": "live"}, {"live": True}, {"api_key": "x"}, {"rules": {"min_edge_after_fees": 0.01}}):
            with self.assertRaises(ctl.ControlError):
                ctl.validate(bad)

    def test_bounds(self):
        with self.assertRaises(ctl.ControlError):
            ctl.validate({"min_edge_after_fees": 0.5})
        with self.assertRaises(ctl.ControlError):
            ctl.validate({"max_per_bet_usd": 1000})
        with self.assertRaises(ctl.ControlError):
            ctl.validate({"kelly_fraction": float("nan")})
        with self.assertRaises(ctl.ControlError):
            ctl.validate({"entries_paused": "yes"})
        with self.assertRaises(ctl.ControlError):
            ctl.validate({"assets": ["btc", "doge"]})
        with self.assertRaises(ctl.ControlError):
            ctl.validate({"daily_loss_limit_pct": 0.5})
        with self.assertRaises(ctl.ControlError):
            ctl.validate({})
        ok = ctl.validate({"min_edge_after_fees": "0.05", "assets": ["XRP", "btc"], "entries_paused": True})
        self.assertEqual(ok, {"min_edge_after_fees": 0.05, "assets": ["btc", "xrp"], "entries_paused": True})

    def test_partial_invalid_change_applies_nothing(self):
        with self.assertRaises(ctl.ControlError):
            ctl.apply(self.rt, {"min_edge_after_fees": 0.05, "mode": "live"})
        self.assertEqual(ctl.load(self.rt)["version"], 0)

    def test_versioned_apply_and_overlay_stays_paper(self):
        r1 = ctl.apply(self.rt, {"min_edge_after_fees": 0.06, "max_per_bet_usd": 20}, note="t",
                       baseline=ctl.defaults_from(self.cfg))
        self.assertEqual(r1["controls"]["version"], 1)
        self.assertEqual(r1["record"]["changes"]["min_edge_after_fees"], {"from": 0.04, "to": 0.06})
        r2 = ctl.apply(self.rt, {"entries_paused": True})
        self.assertEqual(r2["controls"]["version"], 2)
        c = ctl.load(self.rt)
        self.assertEqual(c["values"]["max_per_bet_usd"], 20)
        eff = ctl.effective_config(self.cfg, c)
        self.assertEqual(eff.mode, "paper")
        self.assertAlmostEqual(eff.min_edge, 0.06)
        self.assertAlmostEqual(eff.max_per_bet_usd, 20)
        self.assertAlmostEqual(self.cfg.min_edge, 0.04)  # base untouched
        self.assertTrue(ctl.paused(c))

    def test_tampered_file_cannot_smuggle_keys(self):
        (self.rt / "controls.json").write_text(json.dumps({"version": 7, "values": {"mode": "live", "min_edge_after_fees": 0.05}}))
        c = ctl.load(self.rt)
        self.assertEqual(c["values"], {})
        self.assertTrue(c.get("invalid_on_disk"))
        self.assertEqual(ctl.effective_config(self.cfg, c).mode, "paper")

    def test_flatten_pauses(self):
        r = ctl.apply(self.rt, {}, flatten=True)
        self.assertEqual(r["controls"]["flatten_id"], r["controls"]["version"])
        self.assertTrue(r["controls"]["values"]["entries_paused"])

    def test_enabled_filter(self):
        c = {"values": {"assets": ["btc"], "horizons": ["5m", "1h"]}}
        self.assertTrue(ctl.enabled(c, "btc", "5m"))
        self.assertFalse(ctl.enabled(c, "eth", "5m"))
        self.assertFalse(ctl.enabled(c, "btc", "15m"))
        self.assertTrue(ctl.enabled({}, "sol", "15m"))


def sig(side, edge, *, checks=None, passed=None):
    checks = checks if checks is not None else [
        {"name": "price_to_beat_known", "passed": True, "value": 100.0},
        {"name": "window_timing", "passed": True, "seconds_left": 300, "seconds_into": 60, "final_cutoff": 15, "min_into": 5},
        {"name": "max_spread", "passed": True, "value": 0.02, "threshold": 0.06},
        {"name": "min_liquidity", "passed": True, "value": 500, "threshold": 50, "value_usd": 200, "threshold_usd": 25},
        {"name": "ask_bounds", "passed": True, "value": 0.4, "lo": 0.05, "hi": 0.9},
        {"name": "min_edge_after_fees", "passed": edge >= 0.04, "value": edge, "threshold": 0.04},
    ]
    return {"side": side, "edge_after_fees": edge, "ask": 0.4, "model_prob": 0.5,
            "passed": all(c["passed"] for c in checks) if passed is None else passed, "rule_checks": checks}


class VerdictTests(unittest.TestCase):
    row = {"slug": "m1"}

    def test_edge_reason_has_numbers(self):
        v = decide(self.row, sig("up", 0.008), sig("down", -0.05))
        self.assertEqual(v["verdict"], "NO TRADE")
        self.assertEqual(v["reason"], "UP: edge +0.8% < min 4.0%")

    def test_too_late(self):
        c = sig("up", 0.1)["rule_checks"]
        c[1] = {"name": "window_timing", "passed": False, "seconds_left": 12, "seconds_into": 288, "final_cutoff": 15, "min_into": 5}
        v = decide(self.row, sig("up", 0.1, checks=c), sig("down", -0.1))
        self.assertIn("too late: 12s left < 15s cutoff", v["reason"])

    def test_gates_in_order(self):
        up = sig("up", 0.1)
        self.assertEqual(decide(self.row, up, None, halted=True)["code"], "halted")
        self.assertEqual(decide(self.row, up, None, enabled=False)["verdict"], "OFF")
        self.assertIn("entries paused by operator", decide(self.row, up, None, paused=True)["reason"])
        v = decide(self.row, up, None, risk_halt="daily profit target hit (+5% = $50.00)")
        self.assertIn("daily target hit", v["reason"])
        self.assertEqual(decide(self.row, up, None, holding=True)["verdict"], "HOLD")
        self.assertIn("max 4 open", decide(self.row, up, None, at_max_positions=True, max_positions=4)["reason"])
        buy = decide(self.row, up, sig("down", -0.1), stake_up=12.34)
        self.assertEqual(buy["verdict"], "BUY UP")
        self.assertEqual(buy["stake"], 12.34)

    def test_no_quote(self):
        v = decide(self.row, {"side": "up", "reasons": ["up ask missing"], "rule_checks": []}, None)
        self.assertEqual(v["verdict"], "WAIT")
        self.assertIn("no quote", v["reason"])

    def test_describe_spread_and_liquidity(self):
        d = describe_check({"name": "max_spread", "passed": False, "value": 0.08, "threshold": 0.06})
        self.assertEqual(d["reason"], "spread too wide: 8.0¢ > 6.0¢")
        d = describe_check({"name": "min_liquidity", "passed": False, "value": 20, "threshold": 50, "value_usd": 8, "threshold_usd": 25})
        self.assertIn("thin book: 20 ct", d["reason"])


class BusEventsTests(unittest.TestCase):
    def test_tail_read_newest_last(self):
        with tempfile.TemporaryDirectory() as td:
            bus = Bus(Path(td))
            for i in range(500):
                bus.event("decision", "engine", f"m{i}" + "x" * 300)
            ev = bus.recent_events(50)
            self.assertEqual(len(ev), 50)
            self.assertTrue(ev[-1]["message"].startswith("m499"))
            self.assertTrue(ev[0]["message"].startswith("m450"))


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import httpx  # noqa: F401
            from fastapi.testclient import TestClient
        except Exception:
            raise unittest.SkipTest("fastapi TestClient/httpx not installed")
        cls.td = tempfile.TemporaryDirectory()
        os.environ["DASHBOARD_DATA"] = cls.td.name
        os.environ["DASHBOARD_TOKEN"] = "test-token-123"
        os.environ["DASHBOARD_CONFIG"] = str(ROOT / "config.yaml")
        (Path(cls.td.name) / "runtime").mkdir()
        sys.path.insert(0, str(ROOT / "web"))
        sys.modules.pop("app", None)
        import app as appmod
        cls.appmod = appmod
        cls.client = TestClient(appmod.app)

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("DASHBOARD_DATA", None)
        os.environ.pop("DASHBOARD_TOKEN", None)
        cls.td.cleanup()

    def test_auth(self):
        self.assertEqual(self.client.get("/api/state").status_code, 401)
        self.assertEqual(self.client.get("/?token=wrong").status_code, 401)
        self.assertEqual(self.client.get("/api/state?part=tick&token=test-token-123").status_code, 200)
        self.assertEqual(self.client.get("/vendor/uPlot.iife.min.js").status_code, 200)  # public library only

    def test_controls_round_trip(self):
        T = "test-token-123"
        r = self.client.post(f"/api/controls?token={T}", json={"changes": {"min_edge_after_fees": 0.05}})
        self.assertEqual(r.status_code, 403)  # query token alone cannot write
        r = self.client.post("/api/controls", headers={"X-Dashboard-Token": T}, json={"changes": {"mode": "live"}})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/controls", headers={"X-Dashboard-Token": T},
                             json={"changes": {"min_edge_after_fees": 0.05}, "note": "unit"})
        self.assertEqual(r.status_code, 200, r.text)
        v = r.json()["version"]
        g = self.client.get(f"/api/controls?token={T}").json()
        self.assertEqual(g["controls"]["version"], v)
        self.assertEqual(g["controls"]["values"]["min_edge_after_fees"], 0.05)
        ev = Bus(Path(os.environ["DASHBOARD_DATA"]) / "runtime").recent_events(10)
        self.assertTrue(any(e["kind"] == "control" and "0.05" in e["message"] for e in ev))
        self.assertEqual(self.client.post("/api/controls/flatten", headers={"X-Dashboard-Token": T}, json={"confirm": "no"}).status_code, 400)
        self.assertEqual(self.client.post("/api/halt/reset", headers={"X-Dashboard-Token": T}).status_code, 405)


if __name__ == "__main__":
    unittest.main()


class ProfitLockTests(unittest.TestCase):
    from polymarket_bot import profit_lock as pl

    def P(self, **kw):
        from polymarket_bot.profit_lock import LockParams
        return LockParams(**{"mode": "profit_lock", "target_pct": 0.05, "loss_limit_pct": 0.10,
                             "lock_ratio": 0.70, "locked_size_mult": 0.5, "adaptive": False, **kw})

    def test_trading_below_target(self):
        from polymarket_bot.profit_lock import evaluate
        st = evaluate(self.P(), 1000, [(1, 20), (2, 10)])
        self.assertEqual(st.phase, "TRADING")
        self.assertEqual(st.size_mult, 1.0)
        self.assertIsNone(st.reason)
        self.assertIsNone(st.floor_usd)

    def test_lock_ratchet_and_reduced_size(self):
        from polymarket_bot.profit_lock import evaluate
        st = evaluate(self.P(), 1000, [(1, 30), (2, 25), (3, 15), (4, -10)])  # 30,55,70,60
        self.assertEqual(st.phase, "LOCKED")
        self.assertAlmostEqual(st.peak_usd, 70)
        self.assertAlmostEqual(st.floor_usd, 49.0)          # ratcheted 38.5 -> 49
        self.assertEqual([f for _, f in st.floor_history], [38.5, 49.0])
        self.assertEqual(st.size_mult, 0.5)
        self.assertIsNone(st.reason)                        # still allowed to trade

    def test_floor_stop_is_sticky(self):
        from polymarket_bot.profit_lock import evaluate
        st = evaluate(self.P(), 1000, [(1, 60), (2, -20), (3, 30)])  # 60 -> 40 (< 42) stop, later +30 ignored
        self.assertEqual(st.phase, "STOPPED_FLOOR")
        self.assertIn("profit lock floor hit", st.reason)
        self.assertAlmostEqual(st.floor_usd, 42.0)

    def test_loss_limit_and_fixed_mode(self):
        from polymarket_bot.profit_lock import evaluate
        st = evaluate(self.P(), 1000, [(1, -60), (2, -45)])
        self.assertEqual(st.phase, "STOPPED_LOSS")
        self.assertIn("daily loss limit hit", st.reason)
        fx = evaluate(self.P(mode="fixed"), 1000, [(1, 30), (2, 25)])
        self.assertEqual(fx.phase, "STOPPED_TARGET")
        self.assertIn("daily profit target hit", fx.reason)

    def test_same_timestamp_batches_apply_together(self):
        from polymarket_bot.profit_lock import evaluate
        # 60 then a batch of (-25, +20) at t=2: net 55, never dips to 35 inside the batch.
        st = evaluate(self.P(), 1000, [(1, 60), (2, -25), (2, 20)])
        self.assertEqual(st.phase, "LOCKED")

    def test_adaptive_target_bounded(self):
        from polymarket_bot.profit_lock import DayStat, adaptive_target
        p = self.P(adaptive=True, target_min_pct=0.03, target_max_pct=0.10, min_days=3, lookback_days=7)
        self.assertEqual(adaptive_target(p, [DayStat("2026-11-20", 0.05, 8, 2)])[0], 0.05)  # too few days
        good = [DayStat(f"2026-11-2{i}", 0.06, 9, 1, 0.09) for i in range(5)]
        bad = [DayStat(f"2026-11-2{i}", -0.02, 2, 8, 0.01) for i in range(5)]
        tg, _ = adaptive_target(p, good)
        tb, _ = adaptive_target(p, bad)
        self.assertGreater(tg, 0.05)
        self.assertLess(tb, 0.05)
        self.assertLessEqual(tg, 0.10)
        self.assertGreaterEqual(tb, 0.03)

    def test_risk_manager_uses_lock(self):
        from polymarket_bot.risk import RiskManager, RiskState
        from datetime import date
        cfg = load_config(ROOT / "config.yaml")
        rm = RiskManager(cfg)
        st = RiskState(day=date.today(), starting_day_equity=1000.0, realized_pnl=60.0, settles=[(1.0, 60.0)])
        self.assertIsNone(rm.daily_halt_reason(st))
        self.assertEqual(rm.size_multiplier(st), cfg.locked_size_mult)
        st.realized_pnl, st.settles = 40.0, [(1.0, 60.0), (2.0, -20.0)]
        self.assertIn("floor", rm.daily_halt_reason(st))

    def test_controls_validate_lock_fields(self):
        self.assertEqual(ctl.validate({"target_mode": "fixed"}), {"target_mode": "fixed"})
        with self.assertRaises(ctl.ControlError):
            ctl.validate({"target_mode": "yolo"})
        with self.assertRaises(ctl.ControlError):
            ctl.validate({"lock_ratio": 0.99})
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ctl.ControlError):
                ctl.apply(Path(td), {"target_min_pct": 0.2, "target_max_pct": 0.1})
