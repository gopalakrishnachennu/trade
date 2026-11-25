"""Entry-time path features: no lookahead, signed vs side, DSL allowlist."""

from __future__ import annotations

import unittest

from polymarket_bot import patterns as pat
from polymarket_bot import rules_lab as rl
from polymarket_bot import calibration as cal


def ticks(pairs):
    return [{"ts": t, "close": p} for t, p in pairs]


class TestEntryFeatures(unittest.TestCase):
    def test_signed_momentum_agrees_with_side(self):
        # Price rose over the last minute: + for UP, - for DOWN, same magnitude.
        now = 1_000_000.0
        path = ticks([(now - 60, 100.0), (now - 30, 100.5), (now, 101.0)])
        up = pat.entry_features(path, now_ts=now, side="up", strike=100.0, vol=0.8,
                                horizon="5m", seconds_remaining=200, spot=101.0)
        dn = pat.entry_features(path, now_ts=now, side="down", strike=100.0, vol=0.8,
                                horizon="5m", seconds_remaining=200, spot=101.0)
        self.assertIsNotNone(up["mom_1m"])
        self.assertGreater(up["mom_1m"], 0)
        self.assertAlmostEqual(up["mom_1m"], -dn["mom_1m"])
        self.assertEqual(up["drift_dir"], "with")
        self.assertEqual(dn["drift_dir"], "against")
        self.assertEqual(up["signed_drift"], 1.0)
        self.assertEqual(dn["signed_drift"], -1.0)

    def test_no_lookahead_on_future_ticks(self):
        now = 2_000_000.0
        # Before now the path is flat at 100; after now it rips to 110 and crosses the strike twice.
        path = ticks([
            (now - 180, 100.0), (now - 60, 100.0), (now, 100.0),
            (now + 1, 110.0), (now + 2, 90.0), (now + 3, 110.0),
        ])
        f = pat.entry_features(path, now_ts=now, side="up", strike=105.0, vol=0.8,
                               horizon="5m", seconds_remaining=200, spot=100.0)
        self.assertEqual(f["mom_1m"], 0.0)
        self.assertEqual(f["mom_3m"], 0.0)
        # Future crosses must not be counted.
        self.assertEqual(f["strike_cross_count"], 0)
        self.assertEqual(f["drift_dir"], "against")  # 100 vs 105, side up

    def test_strike_cross_count(self):
        now = 3_000_000.0
        # Window starts at now-200 (5m, 100s left). 99 -> 101 -> 99 = two crosses of 100.
        path = ticks([
            (now - 190, 99.0), (now - 120, 101.0), (now - 60, 99.0), (now, 99.5),
        ])
        f = pat.entry_features(path, now_ts=now, side="down", strike=100.0, vol=0.8,
                               horizon="5m", seconds_remaining=100, spot=99.5)
        self.assertEqual(f["strike_cross_count"], 2)
        self.assertEqual(f["crosses_bucket"], "2-3")
        self.assertEqual(f["drift_dir"], "with")  # 99.5 < 100, down

    def test_extract_features_passes_ticks_and_allowlist(self):
        now = 4_000_000.0
        row = {
            "ts": now, "asset": "btc", "horizon": "15m", "side": "up",
            "seconds_remaining": 400, "annualized_vol": 0.6,
            "spot": 101.0, "price_to_beat": 100.0, "ask": 0.5, "model_prob": 0.62,
            "edge_after_fees": 0.05, "market_slug": "m1",
        }
        path = ticks([(now - 60, 100.0), (now, 101.0)])
        feat = pat.extract_features(row, ts=now, ticks=path)
        self.assertIsNotNone(feat["mom_1m"])
        self.assertGreater(feat["mom_1m"], 0)
        self.assertEqual(feat["drift_dir"], "with")
        for k in ("mom_1m", "mom_3m", "mom_window", "strike_cross_count", "drift_dir", "signed_drift",
                  "mom_1m_bucket", "crosses_bucket"):
            self.assertIn(k, rl.ALLOWED_FIELDS)

    def test_rule_can_skip_on_against_momentum(self):
        rule = rl.validate_rule({
            "name": "skip-against-mom", "action": "skip",
            "when": {"all": [{"field": "mom_1m", "op": "lt", "value": -0.5}]},
            "rationale": "test",
        })
        miss = pat.extract_features({"asset": "btc", "horizon": "5m", "side": "up", "ask": 0.4}, ts=1.0)
        self.assertFalse(rl.rule_matches(rule, miss))  # missing mom_1m must not match
        hit = {"mom_1m": -0.8, "asset": "btc", "horizon": "5m", "side": "up"}
        self.assertTrue(rl.rule_matches(rule, hit))
        self.assertFalse(rl.apply_rule(rule, hit).get("passed"))

    def test_cf_iter_does_not_use_future_path(self):
        rows = [
            {"ts": "2026-11-24T12:00:00-05:00", "market_slug": "m1", "asset": "btc", "horizon": "5m",
             "side": "up", "passed": "1", "model_prob": "0.7", "ask": "0.4", "edge_after_fees": "0.08",
             "spot": "100", "price_to_beat": "100.5", "seconds_remaining": "200", "annualized_vol": "0.5"},
            {"ts": "2026-11-24T12:00:30-05:00", "market_slug": "m1", "asset": "btc", "horizon": "5m",
             "side": "up", "passed": "1", "model_prob": "0.7", "ask": "0.4", "edge_after_fees": "0.08",
             "spot": "100", "price_to_beat": "100.5", "seconds_remaining": "170", "annualized_vol": "0.5"},
            # A later tick rips through the strike — must not affect the first example.
            {"ts": "2026-11-24T12:03:00-05:00", "market_slug": "m1", "asset": "btc", "horizon": "5m",
             "side": "up", "passed": "1", "model_prob": "0.7", "ask": "0.4", "edge_after_fees": "0.08",
             "spot": "110", "price_to_beat": "100.5", "seconds_remaining": "20", "annualized_vol": "0.5"},
        ]
        outcome, settle = cal.index_outcomes(rows)
        examples = cal.iter_counterfactuals(rows, outcome, settle)
        first = min(examples, key=lambda e: e["ts"])
        # First example's spot is 100 vs 100.5, side up, and later 110 must not create a cross or +mom.
        self.assertEqual(first["feat"].get("strike_cross_count"), 0)
        self.assertEqual(first["feat"].get("drift_dir"), "against")


class TestHeartbeatStillBeforeStartup(unittest.TestCase):
    def test_execution_heartbeat_starts_before_learning_backfill(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "src/polymarket_bot/runtime/workers.py").read_text()
        start = src.index("def run_execution")
        loop = src.index("def loop():", start)
        self.assertNotIn("learn.startup(", src[start:loop])
        self.assertIn("learn.startup(", src[loop:])
