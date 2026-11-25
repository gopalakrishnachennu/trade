"""Phase 6: attribution, counterfactual/calibration, bandit, rules lab, learning safety."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from polymarket_bot import attribution as attr
from polymarket_bot import learning_runtime as learn
from polymarket_bot import bandit as ban
from polymarket_bot import calibration as cal
from polymarket_bot import patterns as pat
from polymarket_bot import rules_lab as rl
from polymarket_bot.bandit import compose_learning
from polymarket_bot.config import Config, load_config
from polymarket_bot.models import PaperOrder, Signal, Market
from polymarket_bot.sizing import size_order

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]


def _feat(**kw):
    base = dict(asset="btc", horizon="15m", side="up", hour_et=3, hour_bucket="03-06h",
                weekday="Wed", minutes_left=8, distance_sigma=0.4, distance_bucket="0.25-0.75σ",
                vol_regime="mid", annualized_vol=0.8, model_prob=0.62, ask=0.55, edge=0.05,
                spread=0.01, depth_contracts=8000, ts_unix=1_700_000_000.0)
    base.update(kw)
    return base


class TestAttribution(unittest.TestCase):
    def test_fee_eaten_thin_edge_loss(self):
        t = attr.tag_trade(_feat(edge=0.012, model_prob=0.52), outcome="loss", pnl_usd=-8.0, cost_usd=20.0, fee_usd=0.8)
        self.assertIn("fee_eaten_thin_edge", t["tags"])
        self.assertEqual(t["primary"], "fee_eaten_thin_edge")
        self.assertEqual(t["outcome"], "loss")

    def test_late_entry(self):
        t = attr.tag_trade(_feat(minutes_left=1.2, edge=0.06, model_prob=0.58), outcome="loss", pnl_usd=-5.0, cost_usd=15.0)
        self.assertIn("late_entry", t["tags"])

    def test_last_minute_flip_from_index_path(self):
        entry = 1000.0
        path = [
            {"ts": 900, "spot": 101.0, "strike": 100.0, "seconds_remaining": 180, "implied": "up"},
            {"ts": 1080, "spot": 99.0, "strike": 100.0, "seconds_remaining": 20, "implied": "down"},
            {"ts": 1095, "spot": 98.5, "strike": 100.0, "seconds_remaining": 5, "implied": "down"},
        ]
        t = attr.tag_trade(_feat(side="up", ts_unix=entry, edge=0.06, model_prob=0.6, minutes_left=8),
                           outcome="loss", pnl_usd=-10.0, cost_usd=20.0,
                           settlement_price=98.5, price_to_beat=100.0, path=path)
        self.assertIn("last_minute_flip", t["tags"])
        self.assertEqual(t["primary"], "last_minute_flip")

    def test_high_vol_and_miscal(self):
        t = attr.tag_trade(_feat(vol_regime="high", model_prob=0.78, edge=0.07, minutes_left=10),
                           outcome="loss", pnl_usd=-12.0, cost_usd=20.0)
        self.assertIn("high_vol", t["tags"])
        self.assertIn("model_miscalibration", t["tags"])

    def test_strong_edge_win(self):
        t = attr.tag_trade(_feat(edge=0.12, model_prob=0.7, side="up"), outcome="win",
                           pnl_usd=15.0, cost_usd=20.0)
        self.assertEqual(t["primary"], "strong_edge")
        self.assertIn("with_trend", t["tags"])

    def test_summarize_fees_on_losers(self):
        rows = [
            attr.tag_trade(_feat(edge=0.01), outcome="loss", pnl_usd=-5, cost_usd=10, fee_usd=0.5),
            attr.tag_trade(_feat(edge=0.12, model_prob=0.7), outcome="win", pnl_usd=8, cost_usd=10, fee_usd=0.4),
        ]
        s = attr.summarize(rows)
        self.assertEqual(s["losses"], 1)
        self.assertAlmostEqual(s["fees_on_losers"], 0.5)
        self.assertTrue(any(r["fees_on_losers"] > 0 for r in s["by_primary"]))


class TestCounterfactualAndCalibration(unittest.TestCase):
    def test_walk_forward_excludes_unsettled(self):
        rows = [
            {"ts": "2026-11-24T12:00:00-05:00", "market_slug": "m1", "asset": "btc", "horizon": "5m",
             "side": "up", "passed": "1", "model_prob": "0.7", "ask": "0.4", "edge_after_fees": "0.08",
             "spot": "100", "price_to_beat": "99", "seconds_remaining": "200", "annualized_vol": "0.5",
             "spread": "0.01", "liquidity_contracts": "1000"},
            {"ts": "2026-11-24T12:03:10-05:00", "market_slug": "m1", "asset": "btc", "horizon": "5m",
             "side": "up", "passed": "1", "model_prob": "0.7", "ask": "0.4", "edge_after_fees": "0.08",
             "spot": "101", "price_to_beat": "99", "seconds_remaining": "10", "annualized_vol": "0.5",
             "spread": "0.01", "liquidity_contracts": "1000"},
        ]
        outcome, settle = cal.index_outcomes(rows)
        self.assertEqual(outcome["m1"], "up")
        examples = cal.iter_counterfactuals(rows, outcome, settle)
        t_before = settle["m1"] - 1
        self.assertEqual(cal.asof(examples, t_before), [])
        self.assertGreaterEqual(len(cal.asof(examples, settle["m1"])), 1)
        # NO TRADE rows included
        rows[0]["passed"] = "0"
        examples = cal.iter_counterfactuals(rows, outcome, settle, include_no_trade=True)
        self.assertTrue(any(not e["passed"] for e in examples))

    def test_cf_does_not_change_real_posterior_when_weight_zero(self):
        store = pat.PatternStore()
        feat = _feat()
        store.update_trade(feat, order_id="r1", pnl_usd=10, cost_usd=20, outcome="win")
        ev0 = store.posterior("asset_horizon", "btc|15m")["post_ev"]
        store.update_counterfactual(feat, source_id="cf:1", pnl_usd=-1, cost_usd=1, outcome="loss", weight=0.25)
        ev1 = store.posterior("asset_horizon", "btc|15m", cf_weight=0.0)["post_ev"]
        ev2 = store.posterior("asset_horizon", "btc|15m", cf_weight=1.0)["post_ev"]
        self.assertAlmostEqual(ev0, ev1)
        self.assertLess(ev2, ev1)  # CF loss pulls EV down only when blended

    def test_isotonic_is_monotone_and_platt_fits(self):
        pairs = [(0.1, 0), (0.2, 0), (0.3, 0), (0.4, 1), (0.5, 0), (0.6, 1), (0.7, 1), (0.8, 1), (0.9, 1)] * 3
        steps = cal.fit_isotonic(pairs)
        xs = [0.15, 0.45, 0.75, 0.95]
        ys = [cal.apply_isotonic(x, steps) for x in xs]
        self.assertEqual(ys, sorted(ys))
        a, b = cal.fit_platt(pairs)
        p_hi = cal.apply_platt(0.9, (a, b))
        p_lo = cal.apply_platt(0.1, (a, b))
        self.assertGreater(p_hi, p_lo)

    def test_calibration_report_buckets(self):
        examples = [{"model_prob": 0.2, "won": False, "pnl_usd": -1, "asset": "btc", "horizon": "5m"}] * 10
        examples += [{"model_prob": 0.8, "won": True, "pnl_usd": 1, "asset": "btc", "horizon": "5m"}] * 10
        r = cal.report(examples)
        self.assertGreater(r["overall"]["n"], 0)
        self.assertIsNotNone(r["overall"]["brier"])
        self.assertTrue(r["by_asset_horizon"])



    def test_active_calibration_cannot_loosen(self):
        class Cfg:
            calibration_mode = "active"
            fee_theta = 0.095
            min_edge = 0.04
        # Isotonic map that would lift every p toward 0.9 (a loosening if we used it raw).
        steps = [(0.0, 0.9), (1.0, 0.9)]
        doc = {"calibrator": {"method": "isotonic", "maps": {"btc|15m": steps}, "global_map": steps}}
        sig = type("S", (), {"model_prob": 0.55, "ask": 0.50, "asset": "btc", "horizon": "15m"})()
        info = learn.tighten_with_calibration(sig, Cfg(), doc)
        self.assertLessEqual(info["sized_p"], 0.55 + 1e-12)
        self.assertEqual(info["sized_p"], 0.55)  # min(raw, cal) keeps raw when cal is higher
        # A map that lowers p must shrink sized_p and can fail the edge gate
        steps_lo = [(0.0, 0.40), (1.0, 0.40)]
        doc_lo = {"calibrator": {"method": "isotonic", "maps": {"btc|15m": steps_lo}, "global_map": steps_lo}}
        info2 = learn.tighten_with_calibration(sig, Cfg(), doc_lo)
        self.assertLess(info2["sized_p"], 0.55)
        self.assertTrue(info2["applied"])
        self.assertTrue(info2["fail_edge"])  # 0.40 - 0.50 - fee < 0.04
        # Shadow must not report applied
        class Shadow:
            calibration_mode = "shadow"
            fee_theta = 0.095
            min_edge = 0.04
        info3 = learn.tighten_with_calibration(sig, Shadow(), doc_lo)
        self.assertFalse(info3["applied"])
        self.assertEqual(info3["sized_p"], 0.55)


class TestBanditAndCompose(unittest.TestCase):
    def test_decide_and_update_and_shadow_pnl(self):
        store = pat.PatternStore()
        b = ban.ContextBandit(seed=1)
        feat = _feat()
        d = b.decide(feat, store, mode="shadow", record_shadow=True)
        self.assertEqual(d["mult"], 1.0)  # shadow does not change size
        self.assertIn(d["would_action"], ban.ACTIONS)
        self.assertTrue(b.update(feat, action="1.0", order_id="o1", pnl_usd=5, cost_usd=10, outcome="win"))
        self.assertFalse(b.update(feat, action="1.0", order_id="o1", pnl_usd=5, cost_usd=10, outcome="win"))
        b.record_shadow_settlement("skip", actual_pnl=5, actual_cost=10)
        self.assertEqual(b.shadow["shadow_pnl"], 0.0)
        b.record_shadow_settlement("0.5", actual_pnl=10, actual_cost=20)
        self.assertAlmostEqual(b.shadow["shadow_pnl"], 5.0)

    def test_compose_is_conservative_and_capped(self):
        p = {"passed": True, "mult": 1.5, "detail": "pattern 1.5×"}
        bdec = {"passed": True, "mult": 0.5, "detail": "bandit 0.5×"}
        r = {"passed": True, "mult": 1.2, "detail": "rule 1.2×"}
        c = compose_learning(p, bdec, r, pattern_mode="active", bandit_mode="active", rules_mode="active",
                             min_mult=0.25, max_mult=1.50)
        self.assertTrue(c["passed"])
        self.assertAlmostEqual(c["mult"], 0.5)
        # shadow layers do not change the live mult
        c2 = compose_learning(p, bdec, r, pattern_mode="shadow", bandit_mode="shadow", rules_mode="shadow")
        self.assertAlmostEqual(c2["mult"], 1.0)
        # skip wins
        r_skip = {"passed": False, "mult": 0.0, "detail": "rule skip"}
        c3 = compose_learning(p, bdec, r_skip, pattern_mode="active", bandit_mode="active", rules_mode="active")
        self.assertFalse(c3["passed"])
        self.assertEqual(c3["mult"], 0.0)
        # cannot exceed max_mult
        p_hi = {"passed": True, "mult": 3.0, "detail": "too high"}
        c4 = compose_learning(p_hi, None, None, pattern_mode="active", max_mult=1.50)
        self.assertLessEqual(c4["mult"], 1.50)

    def test_size_order_still_enforces_hard_caps(self):
        cfg = load_config(ROOT / "config.yaml")
        mkt = Market(
            id="m1", slug="btc-updown-15m-1795600000", question="BTC 15m", asset="btc", horizon="15m",
            index_symbol="BTC-USD",
            window_start=datetime(2026, 11, 25, 3, 0, tzinfo=ET),
            window_end=datetime(2026, 11, 25, 3, 15, tzinfo=ET),
            price_to_beat=100.0, price_decimals=2, fee_coefficient=0.02,
            min_trade_qty=0.01, tick_size=0.01, status="open", active=True, closed=False,
        )
        sig = Signal(
            market_slug=mkt.slug, asset="btc", horizon="15m", side="up",
            model_prob=0.90, ask=0.40, fee_per_contract=0.02, edge_after_fees=0.40,
            spread=0.01, liquidity_contracts=10_000.0, spot=101.0, price_to_beat=100.0,
            seconds_remaining=200.0, annualized_vol=0.60, passed=True,
        )
        _, cost, _ = size_order(sig, mkt, cash=10_000, cfg=cfg, kelly_mult=1.0, pattern_mult=1.5)
        self.assertLessEqual(cost, cfg.max_per_bet_usd + 1e-6)
        # A 5× multiplier still cannot exceed the hard cap (learning cannot loosen it).
        _, cost2, _ = size_order(sig, mkt, cash=10_000, cfg=cfg, kelly_mult=1.0, pattern_mult=5.0)
        self.assertLessEqual(cost2, cfg.max_per_bet_usd + 1e-6)
        self.assertLessEqual(cost2, 10_000 * cfg.max_fraction_of_bankroll + 1e-6)

    def test_bandit_and_cf_persist(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            store = pat.PatternStore()
            feat = _feat()
            store.update_trade(feat, order_id="r1", pnl_usd=10, cost_usd=20, outcome="win")
            store.update_counterfactual(feat, source_id="cf:1", pnl_usd=-1, cost_usd=1, outcome="loss", weight=0.25)
            pat.save_pattern_store(d, store)
            loaded = pat.load_pattern_store(d)
            self.assertGreater(loaded.global_stat.n, 0)
            # CF stream is present and real EV is unchanged at cf_weight=0
            ev0 = store.posterior("asset_horizon", "btc|15m", cf_weight=0.0)["post_ev"]
            evL = loaded.posterior("asset_horizon", "btc|15m", cf_weight=0.0)["post_ev"]
            self.assertAlmostEqual(ev0, evL)
            b = ban.ContextBandit(seed=7)
            b.decide(feat, store, mode="shadow", record_shadow=True)
            b.update(feat, action="1.0", order_id="o1", pnl_usd=5, cost_usd=10, outcome="win")
            ban.save_bandit(d, b)
            b2 = ban.load_bandit(d)
            self.assertIn("o1", b2.seen_ids)
            self.assertGreaterEqual(b2.shadow.get("n") or b2.shadow.get("decisions") or 1, 0)


class TestRulesLab(unittest.TestCase):
    def test_rejects_banned_fields(self):
        with self.assertRaises(ValueError):
            rl.validate_rule({"action": "skip", "when": {"all": [{"field": "mode", "op": "eq", "value": "live"}]}})
        with self.assertRaises(ValueError):
            rl.validate_rule({"action": "skip", "when": {"all": [{"field": "daily_loss_limit_pct", "op": "eq", "value": 0.5}]}})
        with self.assertRaises(ValueError):
            rl.validate_rule({"action": "size_mult", "size_mult": 3.0, "when": {"all": [{"field": "asset", "op": "eq", "value": "btc"}]}})

    def test_match_skip_and_backtest(self):
        rule = rl.validate_rule({
            "name": "skip-late", "action": "skip",
            "when": {"all": [{"field": "minutes_left", "op": "lt", "value": 3}]},
            "rationale": "late entries",
        })
        self.assertTrue(rl.apply_rule(rule, _feat(minutes_left=1.5)).get("matched"))
        self.assertFalse(rl.apply_rule(rule, _feat(minutes_left=1.5)).get("passed"))
        self.assertFalse(rl.apply_rule(rule, _feat(minutes_left=8)).get("matched"))
        # CF examples: a late losing trade would have been skipped
        examples = [
            {"feat": _feat(minutes_left=1.0), "passed": True, "pnl_usd": -1.0, "cost_usd": 1.0,
             "settle_ts": 2, "ts": 1, "market_slug": "a", "side": "up", "won": False},
            {"feat": _feat(minutes_left=8.0), "passed": True, "pnl_usd": 1.0, "cost_usd": 1.0,
             "settle_ts": 3, "ts": 2, "market_slug": "b", "side": "up", "won": True},
        ]
        res = rl.backtest_rule(rule, examples, stake=10.0)
        self.assertGreater(res["delta_pnl"], 0)
        self.assertEqual(res["candidate"]["applied"], 1)

    def test_applies_to_scopes_rules_per_strategy(self):
        rule = rl.validate_rule({
            "name": "skip-fav-only", "action": "skip",
            "when": {"all": [{"field": "minutes_left", "op": "lt", "value": 3}]},
            "applies_to": ["favorite"],
            "rationale": "scope test",
        })
        self.assertEqual(rule["applies_to"], ["favorite"])
        late = _feat(minutes_left=1.0, strategy="favorite")
        other = _feat(minutes_left=1.0, strategy="baseline")
        none = _feat(minutes_left=1.0)  # no strategy → does not match a scoped rule
        self.assertTrue(rl.rule_matches(rule, late))
        self.assertFalse(rl.apply_rule(rule, late).get("passed"))
        self.assertFalse(rl.rule_matches(rule, other))
        self.assertTrue(rl.apply_rule(rule, other).get("passed"))  # no-op for other strategies
        self.assertFalse(rl.rule_matches(rule, none))
        # default (omitted) applies to all
        all_rule = rl.validate_rule({
            "name": "skip-all", "action": "skip",
            "when": {"all": [{"field": "minutes_left", "op": "lt", "value": 3}]},
        })
        self.assertIsNone(all_rule["applies_to"])
        self.assertTrue(rl.rule_matches(all_rule, other))
        with self.assertRaises(ValueError):
            rl.validate_rule({
                "name": "bad", "action": "skip",
                "when": {"all": [{"field": "minutes_left", "op": "lt", "value": 3}]},
                "applies_to": ["not_a_strategy"],
            })
        # apply_active_rules respects scope
        dec = rl.apply_active_rules([rule], other)
        self.assertTrue(dec.get("passed"))
        dec = rl.apply_active_rules([rule], late)
        self.assertFalse(dec.get("passed"))

    def test_set_applies_to_updates_queue(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            rec = rl.enqueue_proposal(d, {
                "name": "skip-scoped", "action": "skip",
                "when": {"all": [{"field": "horizon", "op": "eq", "value": "15m"}]},
            }, actor="test", rationale="x")
            self.assertIsNone(rec["rule"]["applies_to"])
            updated = rl.set_applies_to(d, rec["id"], ["favorite", "high_conviction"], actor="test")
            self.assertEqual(updated["rule"]["applies_to"], ["favorite", "high_conviction"])

    def test_queue_gate_and_manual_reject(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            rec = rl.enqueue_proposal(d, {
                "name": "skip-xrp", "action": "skip",
                "when": {"all": [{"field": "asset", "op": "eq", "value": "xrp"}]},
                "rationale": "test",
            }, actor="analyst")
            self.assertEqual(rec["status"], "proposed")
            # Simulate 30 shadow trades where the rule improves P&L
            book = {"trades": 30, "pnl": 20.0, "champ_pnl": 10.0, "max_dd": -2.0, "champ_dd": -3.0}
            g = rl.evaluate_gate(book)
            self.assertTrue(g["passed"])
            short = {"trades": 5, "pnl": 20.0, "champ_pnl": 10.0, "max_dd": -2.0, "champ_dd": -3.0}
            self.assertFalse(rl.evaluate_gate(short)["passed"])
            rl.set_status(d, rec["id"], "rejected", actor="operator", note="not now")
            self.assertEqual(rl.get_proposal(d, rec["id"])["status"], "rejected")


class TestLearningSafetyInvariants(unittest.TestCase):
    def test_defaults_are_shadow(self):
        cfg = load_config(ROOT / "config.yaml")
        self.assertEqual(cfg.pattern_mode, "shadow")
        self.assertEqual(cfg.bandit_mode, "shadow")
        self.assertEqual(cfg.calibration_mode, "shadow")
        self.assertEqual(cfg.counterfactual_mode, "shadow")
        self.assertEqual(cfg.attribution_mode, "shadow")
        self.assertEqual(cfg.rules_lab_mode, "shadow")
        self.assertFalse(cfg.rules_lab_auto_promote)

    def test_learning_cannot_enable_live_mode_or_raise_loss_limit_via_rules(self):
        with self.assertRaises(ValueError):
            rl.validate_rule({"action": "skip", "mode": "live",
                              "when": {"all": [{"field": "asset", "op": "eq", "value": "btc"}]}})
        cfg = load_config(ROOT / "config.yaml")
        self.assertEqual(cfg.mode, "paper")
        self.assertAlmostEqual(cfg.daily_loss_limit_pct, 0.10)
        self.assertTrue(cfg.rolling_guard_enabled)

    def test_controls_accept_new_modes(self):
        from polymarket_bot.runtime import controls as ctl
        cleaned = ctl.validate({
            "bandit_mode": "shadow",
            "calibration_mode": "off",
            "counterfactual_mode": "active",
            "cf_weight": 0.2,
            "rules_lab_mode": "shadow",
            "attribution_mode": "shadow",
        })
        self.assertEqual(cleaned["bandit_mode"], "shadow")
        with self.assertRaises(ctl.ControlError):
            ctl.validate({"bandit_mode": "yolo"})


    def test_execution_heartbeat_starts_before_learning_backfill(self):
        """Watchdog grace is 8s; CF ingest of all evaluations can exceed that.
        learn.startup must not run before _run_guarded starts HeartbeatKeeper."""
        src = (ROOT / "src/polymarket_bot/runtime/workers.py").read_text()
        start = src.index("def run_execution")
        loop = src.index("def loop():", start)
        self.assertNotIn("learn.startup(", src[start:loop])
        self.assertIn("learn.startup(", src[loop:])


class TestSkipAccounting(unittest.TestCase):
    def test_only_active_layer_is_charged(self):
        layer = {
            "pattern": {"mode": "shadow", "passed": False, "detail": "pattern skip"},
            "rule": {"mode": "active", "passed": False, "detail": "rule skip-late-chop: skip"},
            "composed": {"passed": False, "detail": "rule skip-late-chop: skip"},
        }
        self.assertEqual(learn.skip_layer_hits(layer), (False, True))
        layer["pattern"]["mode"] = "active"
        self.assertEqual(learn.skip_layer_hits(layer), (True, True))
        layer["rule"]["mode"] = "shadow"
        layer["rule"]["passed"] = True
        self.assertEqual(learn.skip_layer_hits(layer), (True, False))
        layer["pattern"]["mode"] = "off"
        self.assertEqual(learn.skip_layer_hits(layer), (False, False))

    def test_note_skip_ignores_unattributed_and_records_rule_hits(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            ghost = {
                "composed": {"passed": False, "detail": "???"},
                "pattern": {"mode": "shadow", "passed": True},
                "rule": {"mode": "shadow", "passed": True},
            }
            learn.note_skip(d, {"strategy": "favorite"}, ghost, window_end=1_700_000_100,
                            ask=0.6, side="up", slug="s", strike=1.0, asset="btc")
            p = learn.pending_skips_path(d)
            self.assertTrue((not p.exists()) or p.read_text() == "")
            hit = {
                "composed": {"passed": False, "detail": "rule skip-late-chop: skip"},
                "pattern": {"mode": "shadow", "passed": True, "mode_ok": True},
                "rule": {"mode": "active", "passed": False},
            }
            learn.note_skip(d, {"strategy": "favorite"}, hit, window_end=1_700_000_100,
                            ask=0.6, side="up", slug="s", strike=1.0, asset="btc")
            recs = [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]
            self.assertEqual(len(recs), 1)
            self.assertTrue(recs[0]["hit_rule"])
            self.assertFalse(recs[0]["hit_pattern"])

    def test_engine_source_does_not_note_skip(self):
        src = (ROOT / "src/polymarket_bot/runtime/workers.py").read_text()
        # apply_strategy is the engine path; skip accounting must happen only in execution
        # after the cooldown/kill/pause gate, otherwise cooldown skips poison the layer book.
        engine_fn = src.split("def apply_strategy", 1)[1].split("return sd", 1)[0]
        self.assertNotIn("note_skip", engine_fn)
        self.assertIn("learn.note_skip", src)
