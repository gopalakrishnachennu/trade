"""Per-strategy pattern mode, analyst-rule activation, and automatic safety demotion."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from polymarket_bot.config import Config, load_config
from polymarket_bot import rules_lab as rl
from polymarket_bot import learning_runtime as learn
from polymarket_bot import verdict as vd
from polymarket_bot.bandit import compose_learning
from polymarket_bot.runtime import controls as ctl


def _cfg(raw=None, root=None):
    base = load_config.__wrapped__ if False else None
    return Config(raw=raw or {
        "mode": "paper",
        "patterns": {
            "mode": "shadow",
            "by_strategy": {
                "high_conviction": "active",
                "favorite": "active",
                "baseline": "shadow",
                "late_window": "shadow",
            },
            "auto_demote": True,
            "demote_min_trades": 20,
            "demote_margin_usd": 20.0,
        },
        "rules_lab": {
            "mode": "shadow",
            "auto_promote": False,
            "auto_demote": True,
            "demote_min_trades": 20,
            "demote_margin_usd": 20.0,
            "demote_on_floor_breach": True,
        },
    }, root=root)


class TestPatternModePerStrategy(unittest.TestCase):
    def test_default_yaml_by_strategy(self):
        cfg = load_config("config.yaml")
        self.assertEqual(cfg.pattern_mode, "shadow")
        self.assertEqual(cfg.pattern_mode_for("baseline"), "shadow")
        self.assertEqual(cfg.pattern_mode_for("late_window"), "shadow")
        self.assertEqual(cfg.pattern_mode_for("high_conviction"), "active")
        self.assertEqual(cfg.pattern_mode_for("favorite"), "active")
        self.assertEqual(cfg.pattern_mode_for(None), "shadow")
        self.assertEqual(cfg.bandit_mode, "shadow")
        self.assertEqual(cfg.calibration_mode, "shadow")
        self.assertFalse(cfg.rules_lab_auto_promote)
        self.assertTrue(cfg.rules_lab_auto_demote)

    def test_controls_overlay_per_strategy(self):
        cfg = _cfg()
        overlay = {
            "values": {
                "pattern_mode_favorite": "shadow",
                "pattern_mode_high_conviction": "active",
            }
        }
        # Build via validate + effective_config
        runtime_values = ctl.validate({
            "pattern_mode": "shadow",
            "pattern_mode_favorite": "shadow",
            "pattern_mode_high_conviction": "off",
            "bandit_mode": "shadow",
            "calibration_mode": "shadow",
        })
        eff = ctl.effective_config(cfg, {"values": runtime_values})
        self.assertEqual(eff.mode, "paper")
        self.assertEqual(eff.pattern_mode_for("favorite"), "shadow")
        self.assertEqual(eff.pattern_mode_for("high_conviction"), "off")
        self.assertEqual(eff.pattern_mode_for("baseline"), "shadow")
        self.assertEqual(eff.bandit_mode, "shadow")
        self.assertEqual(eff.calibration_mode, "shadow")


class TestRuleDemoteThreshold(unittest.TestCase):
    def test_underperform_beyond_margin_demotes(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            rec = rl.enqueue_proposal(data, {
                "name": "skip-x", "action": "skip",
                "when": {"all": [{"field": "horizon", "op": "eq", "value": "5m"}]},
                "rationale": "test",
            }, actor="test")
            rl.set_status(data, rec["id"], "active", actor="test")
            # 20 trades: candidate worse than champ by $25 (> $20 margin)
            book = {
                "trades": 20,
                "pnl": -40.0,
                "champ_pnl": -15.0,
                "max_dd": -40.0,
                "champ_dd": -15.0,
                "pnls": [-2.0] * 20,
                "champ_pnls": [-0.75] * 20,
            }
            rl.save_shadow(data, {rec["id"]: book})
            evs = rl.tick_lifecycle(data, {
                "auto_demote": True, "auto_promote": False,
                "demote_min_trades": 20, "demote_margin_usd": 20.0,
                "min_trades": 30,
            })
            self.assertTrue(any(e.get("event") == "auto_demoted" for e in evs))
            self.assertEqual(rl.get_proposal(data, rec["id"])["status"], "demoted")

    def test_small_gap_or_short_sample_does_not_demote(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            rec = rl.enqueue_proposal(data, {
                "name": "skip-y", "action": "skip",
                "when": {"all": [{"field": "horizon", "op": "eq", "value": "5m"}]},
                "rationale": "test",
            }, actor="test")
            rl.set_status(data, rec["id"], "active", actor="test")
            # 20 trades, only $5 under champ — below $20 margin
            rl.save_shadow(data, {rec["id"]: {
                "trades": 20, "pnl": -10.0, "champ_pnl": -5.0,
                "max_dd": -10.0, "champ_dd": -5.0,
            }})
            evs = rl.tick_lifecycle(data, {
                "auto_demote": True, "demote_min_trades": 20, "demote_margin_usd": 20.0,
                "min_trades": 30,
            })
            self.assertFalse(any(e.get("event") == "auto_demoted" for e in evs))
            self.assertEqual(rl.get_proposal(data, rec["id"])["status"], "active")
            # 10 trades, huge gap — not enough n
            rl.save_shadow(data, {rec["id"]: {
                "trades": 10, "pnl": -100.0, "champ_pnl": 0.0,
                "max_dd": -100.0, "champ_dd": 0.0,
            }})
            evs = rl.tick_lifecycle(data, {
                "auto_demote": True, "demote_min_trades": 20, "demote_margin_usd": 20.0,
            })
            self.assertFalse(any(e.get("event") == "auto_demoted" for e in evs))
            self.assertEqual(rl.get_proposal(data, rec["id"])["status"], "active")


class TestPatternDemoteAndFloorBreach(unittest.TestCase):
    def test_pattern_layer_demotes_and_writes_controls(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            (data / "runtime").mkdir()
            (data / "patterns").mkdir()
            cfg = _cfg(root=Path(d))
            # seed a losing pattern book
            learn.save_layer_book(data, {"pattern": {
                "trades": 20,
                "pnls": [-5.0] * 20,
                "champ_pnls": [-2.0] * 20,
            }})
            evs = learn.maybe_demote_pattern(data, cfg)
            self.assertTrue(any(e.get("event") == "auto_demoted_pattern" for e in evs))
            doc = ctl.load(data / "runtime")
            self.assertEqual(doc["values"].get("pattern_mode"), "shadow")
            self.assertEqual(doc["values"].get("pattern_mode_favorite"), "shadow")
            self.assertEqual(doc["values"].get("pattern_mode_high_conviction"), "shadow")

    def test_floor_breach_demotes_only_when_pattern_sized_up_a_loser(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            (data / "runtime").mkdir()
            cfg = _cfg(root=Path(d))
            order = SimpleNamespace(pnl_usd=-30.0)
            feat = {"strategy": "favorite"}
            # 1.5× size on a loser — extra loss $10, caused by pattern
            evs = learn.on_floor_breach(
                order, feat,
                {"composed": {"mult": 1.5}, "pattern": {"would_mult": 1.5}},
                cfg, data,
            )
            self.assertTrue(any(e.get("reason") == "floor_breach" for e in evs))
            # 1.0× — not caused by the layer
            evs2 = learn.on_floor_breach(
                order, feat,
                {"composed": {"mult": 1.0}, "pattern": {"would_mult": 1.0}},
                cfg, data,
            )
            self.assertEqual(evs2, [])


class TestVerdictRuleSkip(unittest.TestCase):
    def test_checklist_shows_analyst_rule_skip_reason(self):
        sig = {
            "side": "up",
            "passed": False,
            "rule_checks": [
                {"name": "min_edge_after_fees", "passed": True, "value": 0.08, "threshold": 0.04},
                {"name": "rule", "passed": False,
                 "detail": "rule skip-mid-prob-0.4-0.5: skip",
                 "value": "skip", "threshold": "rules_lab active"},
            ],
        }
        view = vd.side_view(sig)
        names = [c["name"] for c in view["checks"]]
        self.assertIn("rule", names)
        rule = next(c for c in view["checks"] if c["name"] == "rule")
        self.assertFalse(rule["passed"])
        self.assertIn("skip-mid-prob-0.4-0.5", rule["reason"] or "")
        self.assertEqual(rule["code"], "rule_skip")
        self.assertEqual(rule["label"], "Analyst rule")


class TestComposeAndHeartbeat(unittest.TestCase):
    def test_shadow_rules_do_not_skip(self):
        r = {"passed": False, "mult": 0.0, "detail": "rule skip-x: skip"}
        c = compose_learning(None, None, r, pattern_mode="shadow", bandit_mode="shadow", rules_mode="shadow")
        self.assertTrue(c["passed"])
        self.assertEqual(c["mult"], 1.0)
        c2 = compose_learning(None, None, r, pattern_mode="shadow", bandit_mode="shadow", rules_mode="active")
        self.assertFalse(c2["passed"])

    def test_execution_heartbeat_starts_before_learning_backfill(self):
        src = (Path(__file__).resolve().parents[1] / "src/polymarket_bot/runtime/workers.py").read_text()
        start = src.index("def run_execution")
        loop = src.index("def loop():", start)
        self.assertNotIn("learn.startup(", src[start:loop])
        self.assertIn("learn.startup(", src[loop:])
        # pending-skip settlement also stays inside the guarded loop
        self.assertIn("settle_pending_skips(", src[loop:])
        self.assertNotIn("settle_pending_skips(", src[start:loop])
