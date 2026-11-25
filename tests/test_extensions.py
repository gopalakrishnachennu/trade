"""Strategies + gate + reviewer, archiving, health probes, integrations key store, new API routes."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from polymarket_bot import archive as ar
from polymarket_bot import health as hl
from polymarket_bot import reviewer as rv
from polymarket_bot import strategies as strat
from polymarket_bot.integrations import KeyStore, scrub
from polymarket_bot.strategy_lab import compute_metrics, read_params

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


def sig(edge=0.05, prob=0.62, ask=0.5, left=100, hz="5m", base_ok=True):
    checks = [{"name": "price_to_beat_known", "passed": True}, {"name": "max_spread", "passed": base_ok},
              {"name": "min_edge_after_fees", "passed": edge >= 0.04, "value": edge, "threshold": 0.04}]
    return {"edge_after_fees": edge, "model_prob": prob, "ask": ask, "seconds_remaining": left, "horizon": hz,
            "rule_checks": checks, "passed": base_ok and edge >= 0.04}


class StrategyTests(unittest.TestCase):
    def test_baseline_matches_rules_engine(self):
        for e in (0.03, 0.04, 0.09):
            for ok in (True, False):
                s = sig(edge=e, base_ok=ok)
                self.assertEqual(strat.evaluate("baseline", None, s, 0.04)[0], s["passed"])

    def test_strategies_never_bypass_data_quality(self):
        for n in strat.names():
            self.assertFalse(strat.evaluate(n, None, sig(edge=0.3, prob=0.9, ask=0.7, left=10, base_ok=False), 0.04)[0], n)
            self.assertFalse(strat.evaluate(n, None, {**sig(), "rule_checks": []}, 0.04)[0], n)

    def test_filters(self):
        self.assertTrue(strat.evaluate("late_window", None, sig(edge=0.035, left=60), 0.04)[0])   # relaxed edge, last 35%
        self.assertFalse(strat.evaluate("late_window", None, sig(edge=0.035, left=200), 0.04)[0])
        self.assertFalse(strat.evaluate("high_conviction", None, sig(edge=0.07, prob=0.7), 0.04)[0])
        self.assertTrue(strat.evaluate("high_conviction", None, sig(edge=0.09, prob=0.7), 0.04)[0])
        self.assertFalse(strat.evaluate("favorite", None, sig(edge=0.05, prob=0.7, ask=0.4), 0.04)[0])
        self.assertTrue(strat.evaluate("favorite", None, sig(edge=0.05, prob=0.7, ask=0.6), 0.04)[0])

    def test_clamp_and_hard_floor(self):
        p = strat.clamp_params({"min_edge": -1, "min_prob": 5, "bogus": 1, "max_frac_left": "x"})
        self.assertEqual(p["min_edge"], strat.HARD_MIN_EDGE)
        self.assertEqual(p["min_prob"], 0.95)
        self.assertNotIn("bogus", p)
        # even with min_edge clamped to the floor, an edge below it fails
        self.assertFalse(strat.evaluate("late_window", {"min_edge": 0.0}, sig(edge=0.015, left=10), 0.04)[0])

    def test_active_file_corrupt_falls_back_to_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self.assertEqual(strat.read_active(d)["active"], "baseline")
            (d / "strategies" / "active.json").write_text("{nope")
            self.assertEqual(strat.read_active(d)["active"], "baseline")
            rec = strat.write_active(d, "favorite", {"min_prob": 0.99}, "t", "test")
            self.assertEqual(rec["params"]["min_prob"], 0.95)
            self.assertEqual(strat.read_active(d)["version"], 1)
            with self.assertRaises(ValueError):
                strat.write_active(d, "yolo", {}, "t", "test")


def mk_orders(pnls, prob=0.6):
    out = []
    for i, p in enumerate(pnls):
        out.append({"status": "settled", "pnl_usd": p, "model_prob": prob, "result": "win" if p > 0 else "loss",
                    "window_end": f"2026-11-24T10:{i:02d}:00-05:00", "cost_usd": 10, "ts": f"2026-11-24T10:{i:02d}:00-05:00"})
    return out


class MetricsGateTests(unittest.TestCase):
    def test_metrics(self):
        m = compute_metrics(mk_orders([10, -5, -5, 20]), 1000)
        self.assertEqual(m["trades"], 4)
        self.assertEqual(m["pnl"], 20)
        self.assertAlmostEqual(m["win_rate"], 0.5)
        self.assertEqual(m["max_drawdown"], 10)
        self.assertAlmostEqual(m["profit_factor"], 3.0)
        self.assertAlmostEqual(m["brier"], ((0.6 - 1) ** 2 * 2 + 0.6 ** 2 * 2) / 4, places=4)

    def _metrics(self, a, c):
        return {"baseline": {"metrics": compute_metrics(mk_orders(a), 1000)},
                "favorite": {"metrics": compute_metrics(mk_orders(c, prob=0.7), 1000)}}

    def _wf(self, a_pnl=80, c_pnl=200, a_n=40, c_n=40, a_dd=20, c_dd=15):
        return {"baseline": {"trades": a_n, "pnl": a_pnl, "max_drawdown": a_dd},
                "favorite": {"trades": c_n, "pnl": c_pnl, "max_drawdown": c_dd}}

    def test_gate_rules(self):
        s = rv.clamp_settings({"min_trades": 5})
        wf = self._wf()
        good = self._metrics([1, -1] * 5, [5, 5, -1, 5, 5, -1, 5, 5])
        g = rv.gate(good, "baseline", "favorite", s, 1000, None, walkforward=wf)
        self.assertTrue(g["passed"], g)
        few = self._metrics([1, -1] * 5, [50, 50])
        self.assertFalse(rv.gate(few, "baseline", "favorite", s, 1000, None, walkforward=wf)["passed"])
        thin = self._metrics([1, -1] * 5, [1, 1, 1, 1, -1, 1])
        self.assertFalse(rv.gate(thin, "baseline", "favorite", s, 1000, None, walkforward=wf)["passed"])  # margin
        dd = self._metrics([1, -1] * 5, [40, -30, 40, -30, 40, -30, 40])
        self.assertFalse(rv.gate(dd, "baseline", "favorite", s, 1000, None, walkforward=wf)["passed"])  # drawdown worse
        self.assertFalse(rv.gate(good, "baseline", "favorite", s, 1000, time.time() - 60, walkforward=wf)["passed"])  # cooldown
        self.assertFalse(rv.gate(good, "baseline", "baseline", s, 1000, None, walkforward=wf)["passed"])
        self.assertFalse(rv.gate(good, "baseline", "nope", s, 1000, None, walkforward=wf)["passed"])

    def test_gate_requires_positive_live_and_walkforward_beating_champion(self):
        s = rv.clamp_settings({"min_trades": 5})
        good = self._metrics([1, -1] * 5, [5, 5, -1, 5, 5, -1, 5, 5])
        # Missing walk-forward: fail closed (the late_window bug class)
        g = rv.gate(good, "baseline", "favorite", s, 1000, None, walkforward=None)
        self.assertFalse(g["passed"], g)
        self.assertTrue(any(c["rule"].startswith("fee-aware walk-forward") and not c["passed"] for c in g["checks"]))
        # Live book looks great, but historical fee-aware replay is negative
        late_live = {"baseline": {"metrics": compute_metrics(mk_orders([1, 2] * 20), 1000)},
                     "late_window": {"metrics": compute_metrics(mk_orders([5, 3, -1] * 15, prob=0.7), 1000)}}
        wf_bad = {"baseline": {"trades": 80, "pnl": 490.0, "max_drawdown": 40},
                  "late_window": {"trades": 80, "pnl": -105.0, "max_drawdown": 50}}
        g = rv.gate(late_live, "baseline", "late_window", s, 1000, None, walkforward=wf_bad)
        self.assertFalse(g["passed"], g)
        failed = {c["rule"] for c in g["checks"] if not c["passed"]}
        self.assertTrue(any("walk-forward P&L is positive" in r for r in failed), failed)
        # Walk-forward positive but does not beat the champion
        wf_thin = {"baseline": {"trades": 80, "pnl": 490.0, "max_drawdown": 40},
                   "favorite": {"trades": 80, "pnl": 495.0, "max_drawdown": 30}}
        g = rv.gate(good, "baseline", "favorite", s, 1000, None, walkforward=wf_thin)
        self.assertFalse(g["passed"], g)
        # Walk-forward n too small
        wf_few = self._wf(c_n=3)
        self.assertFalse(rv.gate(good, "baseline", "favorite", s, 1000, None, walkforward=wf_few)["passed"])
        # Walk-forward drawdown worse
        wf_dd = self._wf(c_dd=80, a_dd=20)
        self.assertFalse(rv.gate(good, "baseline", "favorite", s, 1000, None, walkforward=wf_dd)["passed"])
        # Live virtual book is negative even if it "beats" a worse champion
        live_loss = self._metrics([1, -1] * 20, [-3] * 20)
        wf_ok = self._wf()
        g = rv.gate(live_loss, "baseline", "favorite", s, 1000, None, walkforward=wf_ok)
        self.assertFalse(g["passed"], g)
        self.assertTrue(any("live virtual book P&L is positive" in c["rule"] and not c["passed"] for c in g["checks"]))
        # Both books positive, candidate beats champion, min n, DD not worse
        g = rv.gate(good, "baseline", "favorite", s, 1000, None, walkforward=self._wf())
        self.assertTrue(g["passed"], g)

    def test_tweaks_are_clamped_and_step_limited(self):
        cur = {"favorite": strat.default_params("favorite"), "baseline": strat.default_params("baseline")}
        t = rv.clamp_tweaks({"favorite": {"min_prob": 0.0, "min_edge": 9, "evil": 1}, "baseline": {"min_edge": 0.1}, "x": {}}, cur, 0.25)
        self.assertNotIn("baseline", t)  # baseline edge follows config
        lo, hi = strat.PARAM_BOUNDS["min_prob"]
        self.assertAlmostEqual(t["favorite"]["min_prob"], round(0.65 - 0.25 * (hi - lo), 4))
        self.assertLessEqual(t["favorite"]["min_edge"], 0.03 + 0.25 * (0.25 - 0.02) + 1e-9)
        self.assertNotIn("evil", t["favorite"])


class ReviewerTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.d = Path(self.td.name)
        sdir = strat.strategies_dir(self.d)
        good = {"baseline": {"metrics": compute_metrics(mk_orders([1, -1] * 20), 1000)},
                "favorite": {"metrics": compute_metrics(mk_orders([5, 5, -1] * 12, prob=0.7), 1000)},
                "high_conviction": {"metrics": compute_metrics(mk_orders([]), 1000)},
                "late_window": {"metrics": compute_metrics(mk_orders([-2, 1]), 1000)}}
        (sdir / "metrics.json").write_text(json.dumps({"active": "baseline", "starting_equity": 1000, "strategies": good}))
        self.wf = {
            "baseline": {"trades": 80, "pnl": 50.0, "max_drawdown": 40, "pattern_mode": "off"},
            "favorite": {"trades": 80, "pnl": 200.0, "max_drawdown": 30, "pattern_mode": "active"},
            "high_conviction": {"trades": 80, "pnl": 120.0, "max_drawdown": 35, "pattern_mode": "active"},
            "late_window": {"trades": 80, "pnl": -105.0, "max_drawdown": 50, "pattern_mode": "off"},
        }

    def tearDown(self):
        self.td.cleanup()

    def test_llm_error_keeps_strategy_and_degrades(self):
        def boom(s, u):
            raise RuntimeError("HTTP 401: Incorrect API key provided: sk-abcdefghijklmnop")
        rec = rv.run_review(self.d, "manual", llm_call=boom)
        self.assertEqual(rec["kind"], "error")
        self.assertNotIn("abcdefghijklmnop", json.dumps(rec))
        self.assertEqual(strat.read_active(self.d)["active"], "baseline")
        self.assertFalse(rv.read_status(self.d)["ok"])
        self.assertEqual(hl.probe_reviewer(self.d).status, hl.DEGRADED)

    def test_garbage_reply_is_rejected(self):
        rec = rv.run_review(self.d, "manual", llm_call=lambda s, u: '{"suggested_active": "rm -rf"}')
        self.assertEqual(rec["kind"], "error")
        self.assertEqual(strat.read_active(self.d)["active"], "baseline")

    def test_llm_suggestion_passes_gate_and_switches(self):
        reply = json.dumps({"ranking": ["favorite", "baseline"], "suggested_active": "favorite",
                            "param_tweaks": {"favorite": {"min_prob": 0.1}}, "rationale": "better pnl"})
        payload = {}

        def llm(s, u):
            payload["u"] = u
            return "Here you go:\n" + reply
        rec = rv.run_review(self.d, "manual", llm_call=llm, walkforward=self.wf)
        self.assertEqual(rec["status"], "applied", rec)
        self.assertEqual(strat.read_active(self.d)["active"], "favorite")
        self.assertNotIn("token", payload["u"].lower())
        self.assertGreaterEqual(read_params(self.d)["favorite"]["min_prob"], 0.65 - 0.25 * 0.95 - 1e-9)
        kinds = [d["kind"] for d in rv.read_decisions(self.d)]
        self.assertIn("switch", kinds)
        # immediate second review: cooldown blocks switching back
        rec2 = rv.run_review(self.d, "manual", llm_call=lambda s, u: reply.replace('"suggested_active": "favorite"', '"suggested_active": "baseline"'), walkforward=self.wf)
        self.assertEqual(strat.read_active(self.d)["active"], "favorite")
        self.assertEqual(rec2["status"], "pending")

    def test_deterministic_without_llm_and_manual_approve(self):
        rv.write_settings(self.d, {"auto_promote": False})
        rec = rv.run_review(self.d, "manual", llm_call=None, walkforward=self.wf)
        self.assertEqual(rec["source"], "deterministic")
        self.assertEqual(rec["suggested"], "favorite")
        self.assertEqual(rec["status"], "pending")
        self.assertEqual(strat.read_active(self.d)["active"], "baseline")
        rv.resolve(self.d, rec["id"], "approve")
        self.assertEqual(strat.read_active(self.d)["active"], "favorite")
        with self.assertRaises(ValueError):
            rv.resolve(self.d, rec["id"], "approve")


class ArchiveTests(unittest.TestCase):
    def test_split_archive_ack_prune(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            ev = d / "evaluations.csv"
            ev.write_text("ts,x\n2026-11-22T10:00:00-05:00,1\n2026-11-23T23:59:00-05:00,2\n2026-11-24T00:01:00-05:00,3\n")
            con = sqlite3.connect(d / "paper_trades.sqlite")
            con.execute("CREATE TABLE paper_orders (id TEXT PRIMARY KEY, ts TEXT, status TEXT, market_slug TEXT)")
            con.execute("CREATE TABLE equity (ts TEXT, cash REAL, open_exposure REAL, realized_pnl REAL, equity REAL, open_positions INTEGER)")
            con.executemany("INSERT INTO paper_orders VALUES (?,?,?,?)", [
                ("a", "2026-11-22T10:00:00-05:00", "settled", "m"), ("b", "2026-11-22T10:00:00-05:00", "rejected", "m"),
                ("c", "2026-11-24T10:00:00-05:00", "rejected", "m"), ("d", "2026-11-23T10:00:00-05:00", "open", "m")])
            con.executemany("INSERT INTO equity VALUES (?,?,?,?,?,?)", [("2026-11-22T10:00:00", 1, 0, 0, 1, 0), ("2026-11-24T10:00:00", 1, 0, 0, 1, 0)])
            con.commit(); con.close()
            (d / "runtime" / "rotated").mkdir(parents=True)
            (d / "runtime" / "rotated" / "events-1.jsonl").write_text('{"a":1}\n')
            (d / "runtime" / "engine.log").write_text("line\n")
            now = datetime(2026, 11, 24, 12, 0, tzinfo=ET)
            self.assertIsNone(ar.run_archive(d, now=now))  # below threshold
            man = ar.run_archive(d, now=now, force=True)
            self.assertTrue(man["archive"].startswith("part-0001-20261122-20261124"), man["archive"])
            self.assertEqual(ev.read_text().splitlines(), ["ts,x", "2026-11-24T00:01:00-05:00,3"])
            files = {f["path"]: f for f in man["files"]}
            self.assertEqual(files["evaluations.csv"]["rows"], 2)
            self.assertIn("sqlite/paper_trades.snapshot.sqlite", files)
            self.assertEqual(files["sqlite/paper_trades.snapshot.sqlite"]["integrity"], "ok")
            self.assertFalse((d / "runtime" / "rotated" / "events-1.jsonl").exists())
            self.assertEqual((d / "runtime" / "engine.log").read_text(), "")
            tar = d / "archive" / man["archive"]
            self.assertEqual(ar._sha256(tar), man["sha256"])
            # nothing is pruned or deleted before an ack
            self.assertEqual(ar.prune_acked(d, keep_days=0, now=time.time() + 1e6), [])
            con = sqlite3.connect(d / "paper_trades.sqlite")
            self.assertEqual(con.execute("SELECT COUNT(*) FROM paper_orders").fetchone()[0], 4)
            con.close()
            with self.assertRaises(ValueError):
                ar.ack(d, man["archive"], "0" * 64)
            with self.assertRaises(ValueError):
                ar.ack(d, "../../etc/passwd", man["sha256"])
            res = ar.ack(d, man["archive"], man["sha256"])
            self.assertEqual(res["pruned"], {"rejected_orders": 1, "equity_rows": 1})
            con = sqlite3.connect(d / "paper_trades.sqlite")
            ids = sorted(r[0] for r in con.execute("SELECT id FROM paper_orders"))
            con.close()
            self.assertEqual(ids, ["a", "c", "d"])  # ledger (settled/open) untouched; today's reject kept
            self.assertEqual(ar.prune_acked(d, keep_days=3), [])
            self.assertEqual(ar.prune_acked(d, keep_days=3, now=time.time() + 4 * 86400), [man["archive"]])
            self.assertFalse(tar.exists())
            self.assertTrue((d / "archive" / "acked" / ar.manifest_path(tar).name).exists())

    def test_resolve_name_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            for n in ("../x", "state.json", "part-1-2-3.tar.zst", ".archive.lock"):
                self.assertIsNone(ar.resolve_name(Path(td), n))

    def test_locked_append_writes_header_once(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.csv"
            ar.append_csv_locked(p, ["ts", "v"], {"ts": "t1", "v": 1})
            ar.append_csv_locked(p, ["ts", "v"], {"ts": "t2", "v": 2})
            self.assertEqual(p.read_text().splitlines(), ["ts,v", "t1,1", "t2,2"])


DU = namedtuple("DU", "total used free")


class HealthTests(unittest.TestCase):
    def test_disk(self):
        G = 1e9
        self.assertEqual(hl.probe_disk(Path("/"), lambda p: DU(25 * G, 5 * G, 20 * G)).status, hl.OK)
        self.assertEqual(hl.probe_disk(Path("/"), lambda p: DU(25 * G, 23 * G, 2 * G)).status, hl.DEGRADED)
        it = hl.probe_disk(Path("/"), lambda p: DU(25 * G, 24.5 * G, 0.5 * G))
        self.assertEqual(it.status, hl.FAILED)
        self.assertTrue(it.critical)

    def test_memory(self):
        mk = lambda avail, sw_free: {"MemTotal": 1000, "MemAvailable": avail, "SwapTotal": 1000, "SwapFree": sw_free}
        self.assertEqual(hl.probe_memory(mk(400, 1000)).status, hl.OK)
        self.assertEqual(hl.probe_memory(mk(100, 1000)).status, hl.DEGRADED)
        self.assertEqual(hl.probe_memory(mk(20, 50)).status, hl.FAILED)

    def test_clock(self):
        out = {"show": "yes", "ts": "Offset: +2.820ms\n"}
        run = lambda cmd: out["show"] if "show" in cmd else out["ts"]
        self.assertEqual(hl.probe_clock(run).status, hl.OK)
        out["ts"] = "Offset: -1.5s"
        self.assertEqual(hl.probe_clock(run).status, hl.FAILED)
        out["ts"] = "Offset: +400ms"
        self.assertEqual(hl.probe_clock(run).status, hl.DEGRADED)
        out["ts"] = "Offset: +900us"
        out["show"] = "no"
        self.assertEqual(hl.probe_clock(run).status, hl.DEGRADED)

    def test_monitor_needs_consecutive_failures(self):
        with tempfile.TemporaryDirectory() as td:
            m = hl.HealthMonitor(Path(td), Path(td) / "x.sqlite", confirm=2)
            m.SCHEDULE = {"disk": 0}
            calls = {"n": 0}

            def fake(key):
                calls["n"] += 1
                return [hl.Item("disk", hl.FAILED, True, "r")]
            m._run_probe = fake
            m.tick(now=1)
            self.assertIsNone(m.critical_fault())
            m.tick(now=2)
            self.assertEqual(m.critical_fault().name, "disk")
            m._run_probe = lambda key: [hl.Item("disk", hl.OK, True, "r")]
            m.tick(now=3)
            self.assertIsNone(m.critical_fault())

    def test_noncritical_never_faults(self):
        with tempfile.TemporaryDirectory() as td:
            m = hl.HealthMonitor(Path(td), Path(td) / "x.sqlite", confirm=1)
            m.SCHEDULE = {"x": 0}
            m._run_probe = lambda key: [hl.Item("llm_reviewer", hl.FAILED, False, "r")]
            m.tick(now=1); m.tick(now=2)
            self.assertIsNone(m.critical_fault())

    def test_sqlite_integrity(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "a.sqlite"
            sqlite3.connect(db).execute("CREATE TABLE t(x)").connection.commit()
            self.assertEqual(hl.probe_sqlite_integrity(db).status, hl.OK)
            db.write_bytes(b"SQLite format 3\x00" + b"\xff" * 2000)
            self.assertNotEqual(hl.probe_sqlite_integrity(db).status, hl.OK)


class KeyStoreTests(unittest.TestCase):
    def test_store_encrypts_masks_and_restricts(self):
        with tempfile.TemporaryDirectory() as td:
            ks = KeyStore(Path(td))
            key = "sk-test-ABCDEFGHIJKLMNOP1234"  # gitleaks:allow (fake test key)
            pub = ks.set("openai", key, model="gpt-4o-mini")
            self.assertEqual(pub["masked"], "••••1234")
            raw = (Path(td) / "secrets" / "integrations.enc").read_bytes()
            self.assertNotIn(key.encode(), raw)
            if ks.encrypted():
                self.assertFalse(raw.startswith(b"{"))
            for f in ("integrations.enc", "master.key"):
                mode = stat.S_IMODE((Path(td) / "secrets" / f).stat().st_mode)
                self.assertEqual(mode, 0o600, f)
            self.assertEqual(stat.S_IMODE((Path(td) / "secrets").stat().st_mode), 0o700)
            self.assertNotIn(key, json.dumps(ks.public()))
            self.assertNotIn(key, (Path(td) / "integrations.json").read_text())
            self.assertEqual(ks.secret("openai")["key"], key)
            with self.assertRaises(ValueError):
                ks.set("cursor", key)
            with self.assertRaises(ValueError):
                ks.set("custom", key, base_url="http://evil.example.com/v1")
            ks.set_reviewer("openai", "gpt-4o-mini")
            ks.delete("openai")
            self.assertIsNone(ks.secret("openai"))
            self.assertIsNone(ks.meta()["reviewer"]["provider"])

    def test_scrub(self):
        self.assertNotIn("abcdef123456", scrub("Incorrect API key provided: sk-proj-abcdef123456"))  # gitleaks:allow
        self.assertEqual(scrub("my key XYZ", key="XYZ"), "my key ***")


class NewApiTests(unittest.TestCase):
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
        cls.client = TestClient(appmod.app)

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("DASHBOARD_DATA", None)
        os.environ.pop("DASHBOARD_TOKEN", None)
        cls.td.cleanup()

    T = "test-token-123"
    H = {"X-Dashboard-Token": "test-token-123"}

    def test_public_and_protected(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 503)  # no supervisor running here
        self.assertEqual(set(r.json()), {"status", "halted"})
        self.assertEqual(self.client.get("/assets/common.js").status_code, 200)
        for path in ("/metrics", "/api/health", "/api/archives", "/api/strategies", "/api/integrations", "/health",
                     "/strategies", "/integrations", "/archives", "/api/export/trades.csv",
                     "/patterns", "/api/patterns", "/attribution", "/api/attribution",
                     "/rules-lab", "/api/rules-lab"):
            self.assertEqual(self.client.get(path).status_code, 401, path)
            self.assertEqual(self.client.get(f"{path}?token={self.T}").status_code, 200, path)
        m = self.client.get(f"/metrics?token={self.T}").text
        self.assertIn("pmbot_component_status", m)
        self.assertIn('pmbot_halted 0', m)

    def test_writes_need_header(self):
        for path in ("/api/archives/run", "/api/strategies/review", "/api/strategies/settings",
                     "/api/integrations/openai", "/api/integrations/reviewer", "/api/integrations/openai/test",
                     "/api/archives/part-0001-20260101-20260101.tar.zst/ack", "/api/strategies/proposals/x/approve",
                     "/api/rules-lab/propose", "/api/rules-lab/x/approve"):
            self.assertEqual(self.client.post(f"{path}?token={self.T}", json={}).status_code, 403, path)

    def test_integration_key_never_returned(self):
        key = "sk-unit-test-key-9876543210"
        r = self.client.post("/api/integrations/openai", headers=self.H, json={"key": key, "model": "gpt-4o-mini"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(key, r.text)
        g = self.client.get(f"/api/integrations?token={self.T}")
        self.assertNotIn(key, g.text)
        self.assertEqual(g.json()["providers"]["openai"]["masked"], "••••3210")
        self.assertEqual(self.client.post("/api/integrations/cursor", headers=self.H, json={"key": key}).status_code, 400)
        ev = (Path(self.td.name) / "runtime" / "events.jsonl").read_text()
        self.assertNotIn(key, ev)
        self.assertEqual(self.client.delete("/api/integrations/openai", headers=self.H).status_code, 200)

    def test_archive_endpoints(self):
        d = Path(self.td.name)
        (d / "equity.csv").write_text("ts,v\n2020-01-01T00:00:00,1\n")
        r = self.client.post("/api/archives/run", headers=self.H, json={})
        self.assertEqual(r.status_code, 200, r.text)
        man = r.json()["manifest"]
        lst = self.client.get(f"/api/archives?token={self.T}").json()["archives"]
        self.assertEqual(lst[-1]["name"], man["archive"])
        dl = self.client.get(f"/api/archives/{man['archive']}?token={self.T}")
        self.assertEqual(dl.status_code, 200)
        import hashlib
        self.assertEqual(hashlib.sha256(dl.content).hexdigest(), man["sha256"])
        self.assertEqual(self.client.get(f"/api/archives/state.json?token={self.T}").status_code, 404)
        bad = self.client.post(f"/api/archives/{man['archive']}/ack", headers=self.H, json={"sha256": "00"})
        self.assertEqual(bad.status_code, 400)
        ok = self.client.post(f"/api/archives/{man['archive']}/ack", headers=self.H, json={"sha256": man["sha256"]})
        self.assertEqual(ok.status_code, 200, ok.text)

    def test_review_now_without_llm(self):
        r = self.client.post("/api/strategies/review", headers=self.H, json={})
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()


class ReplayTests(unittest.TestCase):
    def test_replay_runs_on_tiny_tape(self):
        import importlib.util
        import io
        from contextlib import redirect_stdout
        spec = importlib.util.spec_from_file_location("replay", ROOT / "scripts" / "replay.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "e.csv"
            p.write_text(
                "ts,market_slug,asset,horizon,side,passed,model_prob,ask,edge_after_fees,spread,liquidity_contracts,spot,price_to_beat,seconds_remaining,annualized_vol,reasons\n"
                "2026-11-24T10:00:00-05:00,m1,btc,5m,up,1,0.8,0.6,0.15,0.01,100,101,100,200,0.4,\n"
                "2026-11-24T10:04:50-05:00,m1,btc,5m,up,0,0.9,0.95,-0.05,0.01,100,102,100,10,0.4,ask_bounds: ask 0.95 outside [0.05, 0.90]\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                mod.main(["--csv", str(p)])
            out = buf.getvalue()
            self.assertIn("baseline", out)
            line = next(l for l in out.splitlines() if l.startswith("baseline"))
            self.assertIn("+6.67", line)   # $10 at 0.60 that won
