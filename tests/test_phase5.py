"""Phase 5: session calendar-day P&L, escalating profit-lock cooldowns, rolling 24h HWM guard."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from polymarket_bot import profit_lock as pl, risk
from polymarket_bot.config import Config, load_config
from polymarket_bot.models import PaperOrder
from polymarket_bot.runtime import controls as ctl
from polymarket_bot.store import Store
from polymarket_bot.verdict import decide

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]


def P(**kw):
    return pl.LockParams(**{"mode": "profit_lock", "target_pct": 0.05, "loss_limit_pct": 0.10,
                            "lock_ratio": 0.70, "locked_size_mult": 0.5, "adaptive": False,
                            "max_breaches": 3, "cooldown_minutes": (60, 120, 240), **kw})


def ts(*parts):
    return datetime(*parts, tzinfo=ET).timestamp()


def _paper(oid, placed, settled, pnl):
    contracts = 40.0
    return PaperOrder(
        id=oid, ts=placed.isoformat(), market_slug=f"btc-updown-15m-{int(settled.timestamp())}",
        asset="btc", horizon="15m", side="up", price=0.50, contracts=contracts,
        cost_usd=20.0, fee_usd=0.20, model_prob=0.58, edge_after_fees=0.08,
        status="settled", result="win" if pnl > 0 else "loss", pnl_usd=pnl,
        window_end=settled.isoformat(), settlement_price=100.0, price_to_beat=99.0,
    )


class TestCooldownExpiryAndResume(unittest.TestCase):
    def test_cooldown_expires_and_resumes_locked_half(self):
        settles = [(100.0, 60.0), (200.0, -20.0)]  # peak 60, floor 42, cum 40 -> breach 1
        st = pl.evaluate(P(), 1000, settles, now=200.0)
        self.assertEqual(st.phase, "COOLDOWN")
        self.assertEqual(st.breach_count, 1)
        self.assertAlmostEqual(st.cooldown_until, 200.0 + 60 * 60)
        self.assertIn("cooldown after floor breach 1/3", st.reason)
        self.assertIn("resumes", st.reason)
        self.assertEqual(st.display_phase, "COOLDOWN")

        mid = pl.evaluate(P(), 1000, settles, now=200.0 + 30 * 60)
        self.assertEqual(mid.phase, "COOLDOWN")
        self.assertEqual(mid.breach_count, 1)

        done = pl.evaluate(P(), 1000, settles, now=200.0 + 60 * 60)
        self.assertEqual(done.phase, "LOCKED")
        self.assertEqual(done.display_phase, "LOCKED-HALF")
        self.assertIsNone(done.reason)
        self.assertEqual(done.size_mult, 0.5)
        self.assertEqual(done.breach_count, 1)
        # Re-armed from current session P&L; peak tracking restarts there.
        self.assertAlmostEqual(done.segment_base_usd, 40.0)
        self.assertAlmostEqual(done.peak_usd, 40.0)
        self.assertAlmostEqual(done.floor_usd, 40.0)

    def test_rearmed_floor_ratchets_from_new_baseline(self):
        settles = [
            (100.0, 60.0),
            (200.0, -20.0),          # breach 1, cooldown until 3800
            (3900.0, 20.0),          # after resume: cum 60, peak 60, floor = 40 + 0.7*20 = 54
        ]
        st = pl.evaluate(P(), 1000, settles, now=3900.0)
        self.assertEqual(st.phase, "LOCKED")
        self.assertEqual(st.breach_count, 1)
        self.assertAlmostEqual(st.segment_base_usd, 40.0)
        self.assertAlmostEqual(st.peak_usd, 60.0)
        self.assertAlmostEqual(st.floor_usd, 54.0)
        self.assertIsNone(st.reason)

    def test_does_not_breach_at_exact_resume_timestamp(self):
        # A settlement exactly at cooldown expiry is applied after re-arm and must not
        # count as a new breach even if it is a small loss (floor equals cum at resume).
        settles = [(100.0, 60.0), (200.0, -20.0), (3800.0, -1.0)]
        st = pl.evaluate(P(), 1000, settles, now=3800.0)
        self.assertEqual(st.phase, "LOCKED")
        self.assertEqual(st.breach_count, 1)
        self.assertAlmostEqual(st.pnl, 39.0)
        self.assertAlmostEqual(st.floor_usd, 40.0)


class TestEscalationToSessionStop(unittest.TestCase):
    def test_three_breaches_stop_the_session(self):
        settles = [
            (100.0, 60.0),     # LOCKED, floor 42
            (200.0, -20.0),    # breach 1, cooldown 60m until 3800
            (3801.0, -1.0),    # resume at 3800 (floor=40), then -1 -> breach 2, cooldown 120m until 11001
            (11002.0, -1.0),   # resume at 11001 (floor=39), then -1 -> breach 3, SESSION STOP
        ]
        st = pl.evaluate(P(), 1000, settles, now=11002.0)
        self.assertEqual(st.phase, "STOPPED_SESSION")
        self.assertEqual(st.display_phase, "SESSION STOP")
        self.assertEqual(st.breach_count, 3)
        self.assertIn("session stop after floor breach 3/3", st.reason)
        self.assertIsNotNone(st.reason)
        # A later winning settlement cannot reopen the session.
        later = pl.evaluate(P(), 1000, settles + [(12000.0, 80.0)], now=12000.0)
        self.assertEqual(later.phase, "STOPPED_SESSION")
        self.assertEqual(later.breach_count, 3)

    def test_second_breach_uses_120m_cooldown(self):
        settles = [
            (100.0, 60.0),
            (200.0, -20.0),
            (3801.0, -1.0),
        ]
        st = pl.evaluate(P(), 1000, settles, now=3801.0)
        self.assertEqual(st.phase, "COOLDOWN")
        self.assertEqual(st.breach_count, 2)
        self.assertAlmostEqual(st.cooldown_until, 3801.0 + 120 * 60)
        self.assertIn("floor breach 2/3", st.reason)

    def test_max_breaches_is_configurable(self):
        p = P(max_breaches=2, cooldown_minutes=(15, 30, 60))
        settles = [(100.0, 60.0), (200.0, -20.0), (200.0 + 15 * 60 + 1, -1.0)]
        st = pl.evaluate(p, 1000, settles, now=settles[-1][0])
        self.assertEqual(st.phase, "STOPPED_SESSION")
        self.assertEqual(st.breach_count, 2)
        self.assertIn("2/2", st.reason)


class TestMidnightResetsBreachCount(unittest.TestCase):
    def test_calendar_day_clears_breaches_and_cooldown(self):
        cfg = load_config(ROOT / "config.yaml")
        cfg_cal = Config(raw={**cfg.raw, "risk": {**cfg.raw.get("risk", {}), "window_mode": "calendar_day"}}, root=cfg.root)
        with tempfile.TemporaryDirectory() as td:
            db = Store(Path(td) / "p.sqlite", Path(td) / "t.csv", Path(td) / "e.csv", Path(td) / "q.csv")
            # Session Nov 24: target hit, then floor breach (cooldown would run past midnight)
            db.write_order(_paper("w1", datetime(2026, 11, 24, 22, 0, tzinfo=ET),
                                  datetime(2026, 11, 24, 22, 15, tzinfo=ET), pnl=181.19))
            db.write_order(_paper("l1", datetime(2026, 11, 24, 23, 0, tzinfo=ET),
                                  datetime(2026, 11, 24, 23, 15, tzinfo=ET), pnl=-59.17))
            # New session trade Nov 25
            db.write_order(_paper("w2", datetime(2026, 11, 24, 23, 50, tzinfo=ET),
                                  datetime(2026, 11, 25, 0, 10, tzinfo=ET), pnl=18.57))

            rm = risk.RiskManager(cfg_cal)
            t_late = ts(2026, 11, 24, 23, 20)
            st, cash = risk.restore_state(cfg_cal, db, rm, now=t_late)
            lk = rm.lock_state(st, now=t_late)
            self.assertEqual(lk.phase, "COOLDOWN")
            self.assertEqual(lk.breach_count, 1)
            self.assertAlmostEqual(cash, 1140.59, places=2)

            t_new = ts(2026, 11, 25, 0, 15)
            rm.apply_daily_halts(st, now=t_new)
            lk2 = rm.lock_state(st, now=t_new)
            self.assertEqual(lk2.phase, "TRADING")
            self.assertEqual(lk2.breach_count, 0)
            self.assertIsNone(lk2.reason)
            self.assertAlmostEqual(lk2.pnl, 18.57, places=2)
            self.assertAlmostEqual(st.starting_day_equity, 1122.02, places=2)
            self.assertIsNone(rm.allow(st, "btc-updown-15m-next", 20.0, cash, now=t_new))


class TestRollingGuard(unittest.TestCase):
    def test_triggers_across_midnight(self):
        # Peak $1,200 at 18:00 ET Nov 24; drop of $130 at 00:30 ET Nov 25 (10.8% off HWM).
        start = 1000.0
        peak_t = ts(2026, 11, 24, 18, 0)
        drop_t = ts(2026, 11, 25, 0, 30)
        settles = [(peak_t, 200.0), (drop_t, -130.0)]
        g = risk.trailing_hwm_state(settles, start, drop_t, lookback_hours=24.0, drawdown_pct=0.10)
        self.assertAlmostEqual(g["hwm_equity"], 1200.0, places=2)
        self.assertAlmostEqual(g["equity"], 1070.0, places=2)
        self.assertTrue(g["triggered"])
        self.assertLessEqual(g["to_trigger_usd"], 0.0)

    def test_no_false_trigger_on_current_live_drawdown(self):
        # Live snapshot: equity $1,140.59, 24h HWM $1,181.19 (~3.4% DD). Must not trip.
        start = 1000.0
        peak_t = ts(2026, 11, 24, 22, 0)
        dip_t = ts(2026, 11, 24, 23, 30)
        win_t = ts(2026, 11, 24, 23, 55)
        now = ts(2026, 11, 25, 3, 20)
        settles = [(peak_t, 181.19), (dip_t, -59.17), (win_t, 18.57)]
        g = risk.trailing_hwm_state(settles, start, now, lookback_hours=24.0, drawdown_pct=0.10)
        self.assertAlmostEqual(g["hwm_equity"], 1181.19, places=2)
        self.assertAlmostEqual(g["equity"], 1140.59, places=2)
        self.assertFalse(g["triggered"])
        self.assertGreater(g["to_trigger_usd"], 70.0)
        self.assertAlmostEqual(abs(g["drawdown_pct_actual"]), 0.03437, places=3)

    def test_recovered_historical_dip_does_not_trip(self):
        start = 1000.0
        t1 = ts(2026, 11, 24, 12, 0)
        t2 = ts(2026, 11, 24, 13, 0)
        t3 = ts(2026, 11, 25, 2, 0)
        # 1000 -> 1200 -> 1050 (12.5% off HWM, recovered) -> 1180. Current DD is 20/1200 = 1.7%.
        settles = [(t1, 200.0), (t2, -150.0), (t3, 130.0)]
        g = risk.trailing_hwm_state(settles, start, t3, lookback_hours=24.0, drawdown_pct=0.10)
        self.assertAlmostEqual(g["equity"], 1180.0, places=2)
        self.assertFalse(g["triggered"])


class TestRestartPersistence(unittest.TestCase):
    def test_cooldown_survives_process_restart_and_does_not_go_stale(self):
        cfg = load_config(ROOT / "config.yaml")
        cfg_cal = Config(raw={**cfg.raw, "risk": {**cfg.raw.get("risk", {}), "window_mode": "calendar_day"}}, root=cfg.root)
        with tempfile.TemporaryDirectory() as td:
            db = Store(Path(td) / "p.sqlite", Path(td) / "t.csv", Path(td) / "e.csv", Path(td) / "q.csv")
            db.write_order(_paper("w1", datetime(2026, 11, 25, 1, 0, tzinfo=ET),
                                  datetime(2026, 11, 25, 1, 15, tzinfo=ET), pnl=60.0))
            db.write_order(_paper("l1", datetime(2026, 11, 25, 1, 20, tzinfo=ET),
                                  datetime(2026, 11, 25, 1, 30, tzinfo=ET), pnl=-20.0))
            t_in = ts(2026, 11, 25, 1, 45)          # still inside 60m cooldown (until 02:30)
            t_out = ts(2026, 11, 25, 2, 35)         # after expiry
            t_next = ts(2026, 11, 26, 0, 5)         # next session — must not stay in a stale cooldown

            rm1 = risk.RiskManager(cfg_cal)
            st1, _ = risk.restore_state(cfg_cal, db, rm1, now=t_in)
            lk1 = rm1.lock_state(st1, now=t_in)
            self.assertEqual(lk1.phase, "COOLDOWN")
            until = lk1.cooldown_until
            self.assertIsNotNone(until)

            # Simulated process restart: brand-new manager + state rebuilt only from the ledger.
            rm2 = risk.RiskManager(cfg_cal)
            st2, _ = risk.restore_state(cfg_cal, db, rm2, now=t_in)
            lk2 = rm2.lock_state(st2, now=t_in)
            self.assertEqual(lk2.phase, "COOLDOWN")
            self.assertEqual(lk2.breach_count, lk1.breach_count)
            self.assertAlmostEqual(lk2.cooldown_until, until)

            rm3 = risk.RiskManager(cfg_cal)
            st3, _ = risk.restore_state(cfg_cal, db, rm3, now=t_out)
            lk3 = rm3.lock_state(st3, now=t_out)
            self.assertEqual(lk3.phase, "LOCKED")
            self.assertIsNone(lk3.reason)
            self.assertAlmostEqual(lk3.floor_usd, 40.0)

            rm4 = risk.RiskManager(cfg_cal)
            st4, cash4 = risk.restore_state(cfg_cal, db, rm4, now=t_next)
            lk4 = rm4.lock_state(st4, now=t_next)
            self.assertEqual(lk4.phase, "TRADING")
            self.assertEqual(lk4.breach_count, 0)
            self.assertIsNone(lk4.reason)
            self.assertIsNone(lk4.cooldown_until)
            self.assertAlmostEqual(cash4, 1040.0, places=2)
            self.assertIsNone(rm4.allow(st4, "btc-updown-15m-next", 10.0, cash4, now=t_next))


class TestDefaultsAndControls(unittest.TestCase):
    def test_calendar_day_is_the_default_window_mode(self):
        cfg = load_config(ROOT / "config.yaml")
        self.assertEqual(cfg.window_mode, "calendar_day")
        self.assertEqual(cfg.max_breaches, 3)
        self.assertEqual(cfg.cooldown_minutes, (60, 120, 240))
        self.assertTrue(cfg.rolling_guard_enabled)
        self.assertAlmostEqual(cfg.rolling_guard_pct, 0.10)
        self.assertAlmostEqual(cfg.rolling_guard_hours, 24.0)

    def test_controls_accept_new_fields(self):
        cleaned = ctl.validate({
            "max_breaches": 4,
            "cooldown_1_min": 30,
            "cooldown_2_min": 90,
            "cooldown_3_min": 180,
            "rolling_guard_enabled": True,
            "rolling_guard_pct": 0.12,
            "rolling_guard_hours": 24,
            "window_mode": "calendar_day",
        })
        self.assertEqual(cleaned["max_breaches"], 4)
        self.assertEqual(cleaned["cooldown_1_min"], 30)
        with self.assertRaises(ctl.ControlError):
            ctl.validate({"max_breaches": 99})
        with self.assertRaises(ctl.ControlError):
            ctl.validate({"rolling_guard_pct": 0.9})

    def test_verdict_uses_cooldown_reason(self):
        row = {"asset": "btc", "horizon": "15m", "slug": "btc-updown-15m-x", "seconds_remaining": 120,
               "up_ask": 0.4, "down_ask": 0.4, "status": "ok"}
        up = {"side": "up", "passed": True, "edge_after_fees": 0.1, "reasons": [],
              "rule_checks": [{"name": "min_edge_after_fees", "passed": True, "value": 0.1, "label": "edge"}],
              "available": True, "reason": "ok", "code": "ok", "more_fails": 0, "edge": 0.1}
        v = decide(row, up, None, stake_up=10.0,
                   risk_halt="cooldown after floor breach 1/3, resumes 4:15 AM ET")
        self.assertEqual(v["code"], "risk_halt")
        self.assertIn("cooldown after floor breach 1/3, resumes 4:15 AM ET", v["reason"])


class TestDashboardRiskPhases(unittest.TestCase):
    def test_risk_state_labels(self):
        import sys
        sys.path.insert(0, str(ROOT / "web"))
        # Import the dashboard helper without spinning the app server.
        import importlib
        app = importlib.import_module("app")
        halt = {"halted": False}
        self.assertEqual(app.risk_state("OK", "", halt, {"risk_halt": None, "lock": {"phase": "TRADING"}}, {})["state"], "TRADING")
        self.assertEqual(app.risk_state("OK", "", halt, {"risk_halt": None, "lock": {"phase": "LOCKED", "target_usd": 50, "floor_usd": 42, "peak_usd": 60, "size_mult": 0.5, "breach_count": 0, "max_breaches": 3, "display_phase": "LOCKED-HALF"}}, {})["state"], "LOCKED-HALF")
        self.assertEqual(app.risk_state("OK", "", halt, {"risk_halt": "cooldown after floor breach 1/3, resumes 4:15 AM ET", "lock": {"phase": "COOLDOWN", "display_phase": "COOLDOWN", "breach_count": 1, "max_breaches": 3, "cooldown_until": 1, "resume_et": "4:15 AM ET"}}, {})["state"], "COOLDOWN")
        self.assertEqual(app.risk_state("OK", "", halt, {"risk_halt": "session stop after floor breach 3/3", "lock": {"phase": "STOPPED_SESSION"}}, {})["state"], "SESSION STOP")
        self.assertEqual(app.risk_state("HALTED", "rolling_drawdown", {"halted": True, "reason": "rolling_drawdown"}, {"lock": {}}, {})["state"], "HALT")
