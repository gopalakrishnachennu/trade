"""Fail-closed interlock: injected faults must halt and stay halted until a manual reset."""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polymarket_bot.runtime.bus import Bus
from polymarket_bot.runtime.interlock import HaltLatch
from polymarket_bot.runtime.watchdog import Watchdog


class InterlockTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.bus = Bus(self.root)
        self.latch = HaltLatch(self.root / "HALT.json")
        self.wd = Watchdog(
            self.bus, self.latch,
            stale={"index_feed": 2.0, "book_feed": 2.0, "discovery": 3.0, "engine": 2.0, "execution": 2.0},
            grace={"5m": 1.0, "15m": 1.0, "1h": 1.0},
            clock_skew_seconds=1.0,
            wall_mono_drift_seconds=1.0,
        )

    def tearDown(self):
        self.td.cleanup()

    def _fresh(self, *names: str):
        for n in names or ("discovery", "index_feed", "book_feed", "engine", "execution"):
            self.bus.beat(n, ok=True, status="ok")

    def test_first_halt_reason_wins(self):
        a = self.latch.trip("stale_feed", "index_feed", "first")
        b = self.latch.trip("exception", "engine", "second")
        self.assertTrue(a.halted)
        self.assertEqual(b.reason, "stale_feed")
        self.assertEqual(b.details, "first")

    def test_reset_requires_explicit_yes(self):
        self.latch.trip("stale_feed", "index_feed", "x")
        with self.assertRaises(PermissionError):
            self.latch.reset()
        self.assertTrue(self.latch.is_halted())
        self.latch.reset(yes=True)
        self.assertFalse(self.latch.is_halted())

    def test_stale_feed_halts_and_stays_halted(self):
        self._fresh()
        # Age the index heartbeat well past the 2s threshold.
        p = self.root / "heartbeats" / "index_feed.json"
        data = p.read_text()
        import json
        obj = json.loads(data)
        obj["wall_ts"] = time.time() - 8.0
        p.write_text(json.dumps(obj))
        fault = self.wd.check_and_trip()
        self.assertIsNotNone(fault)
        self.assertEqual(fault[0], "stale_heartbeat")
        self.assertEqual(fault[1], "index_feed")
        self.assertTrue(self.latch.is_halted())
        reason = self.latch.read().reason
        # A later clean beat must not auto-clear the latch.
        self.bus.beat("index_feed", ok=True, status="ok")
        self.assertTrue(self.latch.is_halted())
        self.assertEqual(self.latch.read().reason, reason)
        # Manual reset is the only way out.
        self.latch.reset(yes=True)
        self.assertFalse(self.latch.is_halted())

    def test_exception_flag_via_unhealthy_heartbeat(self):
        self._fresh()
        self.bus.beat("engine", ok=False, status="exception", details="ZeroDivisionError: boom")
        fault = self.wd.check_and_trip()
        self.assertIsNotNone(fault)
        self.assertEqual(fault[0], "component_unhealthy")
        self.assertEqual(fault[1], "engine")
        self.assertTrue(self.latch.is_halted())
        self.bus.beat("engine", ok=True)
        self.assertTrue(self.latch.is_halted())

    def test_missing_ended_window_halts(self):
        self._fresh()
        now = datetime.now(timezone.utc)
        ended = (now - timedelta(seconds=30)).isoformat()
        start = (now - timedelta(minutes=5)).isoformat()
        windows = []
        for asset in ("btc", "eth", "sol", "xrp"):
            for horizon in ("5m", "15m", "1h"):
                windows.append({
                    "asset": asset, "horizon": horizon, "status": "awaiting_next",
                    "slug": f"{asset}-{horizon}-old",
                    "window_start": start, "window_end": ended,
                })
        self.bus.publish("discovery", {"windows": windows, "markets": []})
        # Ended 30s ago, well past the 1s test grace — must trip and stay halted.
        fault = self.wd.inspect()
        self.assertIsNotNone(fault)
        self.assertEqual(fault[0], "missing_window")
        self.wd.check_and_trip()
        self.assertTrue(self.latch.is_halted())
        self.latch.reset(yes=True)
        self.assertFalse(self.latch.is_halted())


    def test_just_ended_window_waits_for_grace(self):
        self._fresh()
        now = datetime.now(timezone.utc)
        ended = (now - timedelta(seconds=0.2)).isoformat()
        start = (now - timedelta(minutes=5)).isoformat()
        windows = []
        for asset in ("btc", "eth", "sol", "xrp"):
            for horizon in ("5m", "15m", "1h"):
                windows.append({
                    "asset": asset, "horizon": horizon, "status": "open",
                    "slug": f"{asset}-{horizon}-old",
                    "window_start": start, "window_end": ended,
                })
        self.bus.publish("discovery", {"windows": windows, "markets": []})
        self.assertIsNone(self.wd.inspect())
        self.assertFalse(self.latch.is_halted())

    def test_ended_plus_live_next_does_not_halt(self):
        self._fresh()
        now = datetime.now(timezone.utc)
        old_end = now.isoformat()
        old_start = (now - timedelta(minutes=5)).isoformat()
        new_start = now.isoformat()
        new_end = (now + timedelta(minutes=5)).isoformat()
        windows = []
        markets = []
        for asset in ("btc", "eth", "sol", "xrp"):
            for horizon in ("5m", "15m", "1h"):
                windows.append({
                    "asset": asset, "horizon": horizon, "status": "open",
                    "slug": f"{asset}-{horizon}-old",
                    "window_start": old_start, "window_end": old_end,
                    "next_slug": f"{asset}-{horizon}-new",
                })
                windows.append({
                    "asset": asset, "horizon": horizon, "status": "open",
                    "slug": f"{asset}-{horizon}-new",
                    "window_start": new_start, "window_end": new_end,
                })
                markets.append({
                    "asset": asset, "horizon": horizon,
                    "window_start": new_start, "window_end": new_end,
                    "slug": f"{asset}-{horizon}-new",
                })
        self.bus.publish("discovery", {"windows": windows, "markets": markets})
        self.assertIsNone(self.wd.inspect())
        self.assertFalse(self.latch.is_halted())

    def test_clock_skew_halts(self):
        self._fresh()
        # Pretend wall time jumped 10s while monotonic barely moved.
        self.wd._wall0 = time.time() - 10
        self.wd._mono0 = time.monotonic()
        fault = self.wd.inspect()
        self.assertIsNotNone(fault)
        self.assertEqual(fault[0], "clock_skew")
        self.wd.check_and_trip()
        self.assertTrue(self.latch.is_halted())
        self.assertIn("clock_skew", self.latch.read().reason)

    def test_sequence_backwards_halts(self):
        self._fresh()
        self.wd.inspect()  # record seq
        import json
        p = self.root / "heartbeats" / "book_feed.json"
        obj = json.loads(p.read_text())
        obj["seq"] = 0  # backwards
        p.write_text(json.dumps(obj))
        fault = self.wd.check_and_trip()
        self.assertIsNotNone(fault)
        self.assertEqual(fault[0], "state_mismatch")
        self.assertTrue(self.latch.is_halted())

    def test_healthy_beats_do_not_halt(self):
        self._fresh()
        now = datetime.now(timezone.utc)
        windows = []
        for asset in ("btc", "eth", "sol", "xrp"):
            for horizon in ("5m", "15m", "1h"):
                windows.append({
                    "asset": asset, "horizon": horizon, "status": "open",
                    "slug": f"{asset}-{horizon}",
                    "window_start": (now - timedelta(seconds=30)).isoformat(),
                    "window_end": (now + timedelta(minutes=3)).isoformat(),
                })
        self.bus.publish("discovery", {"windows": windows, "markets": []})
        self.assertIsNone(self.wd.inspect())
        self.assertIsNone(self.wd.check_and_trip())
        self.assertFalse(self.latch.is_halted())


if __name__ == "__main__":
    unittest.main()


class HeartbeatAndRetryTests(unittest.TestCase):
    def test_heartbeat_keeper_stays_fresh_during_blocked_fetch(self):
        """A blocked main thread must not starve heartbeats past the stale threshold."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        bus = Bus(root)
        latch = HaltLatch(root / "HALT.json")
        from polymarket_bot.runtime.heartbeat import HeartbeatKeeper
        keeper = HeartbeatKeeper(bus, "discovery", interval=0.2)
        keeper.start()
        try:
            # Main thread is "blocked" in a slow fetch longer than the 0.8s stale limit.
            time.sleep(1.6)
            wd = Watchdog(
                bus, latch,
                stale={"index_feed": 10.0, "book_feed": 10.0, "discovery": 0.8, "engine": 10.0, "execution": 10.0},
                grace={"5m": 30.0, "15m": 30.0, "1h": 30.0},
            )
            # Other required roles need a recent beat so only discovery is under test.
            for name in ("index_feed", "book_feed", "engine", "execution"):
                bus.beat(name, ok=True, status="ok")
            fault = wd.inspect()
            hb = bus.read_heartbeat("discovery")
            self.assertIsNotNone(hb)
            self.assertLess(hb.age(), 0.8, f"heartbeat age {hb.age():.2f}s should stay under 0.8s")
            self.assertIsNone(fault, f"watchdog tripped during blocked fetch: {fault}")
            self.assertFalse(latch.is_halted())
        finally:
            keeper.stop()

    def test_without_keeper_a_blocked_fetch_goes_stale(self):
        """Sanity: the same stale window without a keeper is a halt (the 1:21 bug)."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        bus = Bus(root)
        latch = HaltLatch(root / "HALT.json")
        bus.beat("discovery", ok=True, status="polling")
        for name in ("index_feed", "book_feed", "engine", "execution"):
            bus.beat(name, ok=True, status="ok")
        time.sleep(1.0)
        # Refresh the others so only discovery is stale.
        for name in ("index_feed", "book_feed", "engine", "execution"):
            bus.beat(name, ok=True, status="ok")
        wd = Watchdog(
            bus, latch,
            stale={"index_feed": 10.0, "book_feed": 10.0, "discovery": 0.8, "engine": 10.0, "execution": 10.0},
            grace={"5m": 30.0, "15m": 30.0, "1h": 30.0},
        )
        fault = wd.inspect()
        self.assertIsNotNone(fault)
        self.assertEqual(fault[0], "stale_heartbeat")
        self.assertEqual(fault[1], "discovery")

    def test_heartbeat_keeper_stays_fresh_during_hanging_urlopen(self):
        """Regression for 2:02: a real blocking urlopen must not starve the keeper.

        The 1:21 test used time.sleep, which always releases the GIL. Confirm
        urllib.request.urlopen also lets the keeper thread run.
        """
        import socket
        import urllib.request

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        bus = Bus(root)
        latch = HaltLatch(root / "HALT.json")
        from polymarket_bot.runtime.heartbeat import HeartbeatKeeper
        keeper = HeartbeatKeeper(bus, "discovery", interval=0.2)
        keeper.start()
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/hang", timeout=1.5)
            except Exception:
                pass
            for name in ("index_feed", "book_feed", "engine", "execution"):
                bus.beat(name, ok=True, status="ok")
            wd = Watchdog(
                bus, latch,
                stale={"index_feed": 10.0, "book_feed": 10.0, "discovery": 0.8, "engine": 10.0, "execution": 10.0},
                grace={"5m": 30.0, "15m": 30.0, "1h": 30.0},
            )
            fault = wd.inspect()
            hb = bus.read_heartbeat("discovery")
            self.assertIsNotNone(hb)
            self.assertLess(hb.age(), 0.8, f"heartbeat age {hb.age():.2f}s after hanging urlopen")
            self.assertIsNone(fault, f"watchdog tripped during hanging urlopen: {fault}")
        finally:
            keeper.stop()
            srv.close()

    def _beat_all(self, bus, *, discovery_ago=0.0):
        now = time.time()
        for name in ("discovery", "index_feed", "book_feed", "engine", "execution"):
            hb = bus.beat(name, ok=True, status="ok")
            if name == "discovery" and discovery_ago:
                hb.wall_ts = now - discovery_ago
                hb.mono_ts = time.monotonic() - discovery_ago
                bus._write_heartbeat(hb)

    def test_host_stall_does_not_trip_stale_on_resumed_tick(self):
        """2:02 ET: supervisor was also frozen 29.9s; do not blame discovery."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        bus = Bus(root)
        latch = HaltLatch(root / "HALT.json")
        wd = Watchdog(
            bus, latch,
            stale={"index_feed": 8.0, "book_feed": 8.0, "discovery": 25.0, "engine": 8.0, "execution": 8.0},
            grace={"5m": 30.0, "15m": 30.0, "1h": 30.0},
        )
        wd.host_stall_seconds = 5.0
        t0 = time.monotonic()
        w0 = time.time()
        self.assertIsNone(wd.observe_scheduler(now_mono=t0, now_wall=w0))
        self._beat_all(bus)
        # Guest unscheduled for 30s; clocks kept running; no new beats yet.
        stall = wd.observe_scheduler(now_mono=t0 + 30.0, now_wall=w0 + 30.0)
        self.assertIsNotNone(stall)
        self.assertIn("scheduler gap 30.0s", stall)
        # Supervisor skips inspect on this tick. Workers then resume and beat.
        self._beat_all(bus)
        self.assertIsNone(wd.observe_scheduler(now_mono=t0 + 31.0, now_wall=w0 + 31.0))
        self.assertIsNone(wd.inspect())
        self.assertFalse(latch.is_halted())

    def test_host_stall_then_still_stale_worker_halts_on_next_tick(self):
        """After a host stall, a worker that stays stale on the next timely tick still halts."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        bus = Bus(root)
        latch = HaltLatch(root / "HALT.json")
        wd = Watchdog(
            bus, latch,
            stale={"index_feed": 8.0, "book_feed": 8.0, "discovery": 25.0, "engine": 8.0, "execution": 8.0},
            grace={"5m": 30.0, "15m": 30.0, "1h": 30.0},
        )
        wd.host_stall_seconds = 5.0
        t0 = time.monotonic()
        w0 = time.time()
        self._beat_all(bus)
        self.assertIsNone(wd.observe_scheduler(now_mono=t0, now_wall=w0))
        self.assertIsNotNone(wd.observe_scheduler(now_mono=t0 + 30.0, now_wall=w0 + 30.0))
        # Next timely tick: other workers recover, discovery does not.
        self._beat_all(bus, discovery_ago=29.7)
        self.assertIsNone(wd.observe_scheduler(now_mono=t0 + 31.0, now_wall=w0 + 31.0))
        fault = wd.inspect()
        self.assertIsNotNone(fault)
        self.assertEqual(fault[0], "stale_heartbeat")
        self.assertEqual(fault[1], "discovery")
        self.assertRegex(fault[2], r"heartbeat age 29\.[0-9]s > 25\.0s")

    def test_wall_clock_jump_is_treated_as_host_stall(self):
        """Wall stepped ~30s, monotonic did not: skip inspect, do not clock_skew."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        bus = Bus(root)
        latch = HaltLatch(root / "HALT.json")
        wd = Watchdog(bus, latch, grace={"5m": 30.0, "15m": 30.0, "1h": 30.0})
        wd.host_stall_seconds = 5.0
        t0 = time.monotonic()
        w0 = time.time()
        self._beat_all(bus)
        self.assertIsNone(wd.observe_scheduler(now_mono=t0, now_wall=w0))
        stall = wd.observe_scheduler(now_mono=t0 + 1.0, now_wall=w0 + 30.0)
        self.assertIsNotNone(stall)
        self.assertIn("wall clock jumped", stall)
        self._beat_all(bus)
        self.assertIsNone(wd.observe_scheduler(now_mono=t0 + 2.0, now_wall=w0 + 31.0))
        self.assertIsNone(wd.inspect())
        self.assertFalse(latch.is_halted())

    def test_timely_loop_stale_discovery_still_halts(self):
        """A single worker going stale while the supervisor stays scheduled still halts."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        bus = Bus(root)
        latch = HaltLatch(root / "HALT.json")
        wd = Watchdog(
            bus, latch,
            stale={"index_feed": 8.0, "book_feed": 8.0, "discovery": 25.0, "engine": 8.0, "execution": 8.0},
            grace={"5m": 30.0, "15m": 30.0, "1h": 30.0},
        )
        wd.host_stall_seconds = 5.0
        t0 = time.monotonic()
        w0 = time.time()
        self._beat_all(bus)
        self.assertIsNone(wd.observe_scheduler(now_mono=t0, now_wall=w0))
        self.assertIsNone(wd.observe_scheduler(now_mono=t0 + 1.0, now_wall=w0 + 1.0))
        self._beat_all(bus, discovery_ago=29.7)
        self.assertIsNone(wd.observe_scheduler(now_mono=t0 + 2.0, now_wall=w0 + 2.0))
        fault = wd.inspect()
        self.assertIsNotNone(fault)
        self.assertEqual(fault[0], "stale_heartbeat")
        self.assertEqual(fault[1], "discovery")

    def test_get_retries_are_bounded(self):
        """Default/capped retries plus backoff cap must finish well under 25s."""
        from unittest import mock
        from polymarket_bot.client import PublicClient, DEFAULT_ATTEMPTS, MAX_ATTEMPTS, BACKOFF_CAP_SECONDS

        client = PublicClient("https://example.test", "test-agent", timeout=0.05, max_rps=1000)
        calls = {"n": 0}

        def hang(*_a, **_k):
            calls["n"] += 1
            raise TimeoutError("simulated gateway hang")

        t0 = time.monotonic()
        with mock.patch("urllib.request.urlopen", side_effect=hang):
            with self.assertRaises(TimeoutError):
                client.get("/v1/events")
        elapsed = time.monotonic() - t0
        self.assertEqual(calls["n"], DEFAULT_ATTEMPTS)
        # 2 attempts * 0.05s timeout (not actually slept by the mock) + one capped backoff <= 1s, plus limiter.
        self.assertLess(elapsed, 2.0, f"bounded retry took {elapsed:.2f}s")
        # Even the old 4-attempt, 2**n backoff recipe against a 5s timeout was ~27s.
        # New caps must stay under the 25s discovery threshold even in the worst case.
        worst = MAX_ATTEMPTS * 5.0 + (MAX_ATTEMPTS - 1) * BACKOFF_CAP_SECONDS
        self.assertLess(worst, 25.0, f"worst-case {worst}s must stay under the 25s heartbeat threshold")

    def test_get_caps_excessive_attempts(self):
        from unittest import mock
        from polymarket_bot.client import PublicClient, MAX_ATTEMPTS

        client = PublicClient("https://example.test", "test-agent", timeout=0.01, max_rps=1000)
        calls = {"n": 0}

        def hang(*_a, **_k):
            calls["n"] += 1
            raise TimeoutError("simulated gateway hang")

        with mock.patch("urllib.request.urlopen", side_effect=hang):
            with self.assertRaises(TimeoutError):
                client.get("/v1/events", attempts=99)
        self.assertEqual(calls["n"], MAX_ATTEMPTS)


class SequenceEpochTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.bus = Bus(self.root)
        self.latch = HaltLatch(self.root / "HALT.json")
        self.wd = Watchdog(
            self.bus, self.latch,
            stale={"index_feed": 10.0, "book_feed": 10.0, "discovery": 10.0, "engine": 10.0, "execution": 10.0},
            grace={"5m": 30.0, "15m": 30.0, "1h": 30.0},
        )

    def tearDown(self):
        self.td.cleanup()

    def _fresh_all(self, **beat_kw):
        for n in ("discovery", "index_feed", "book_feed", "engine", "execution"):
            self.bus.beat(n, ok=True, status="ok", **beat_kw)

    def test_next_seq_is_atomic_under_contention(self):
        """Threaded increments must stay unique and monotonic (the 1:46 race)."""
        import threading
        n_threads = 8
        n_each = 80
        got = []
        lock = threading.Lock()

        def worker():
            local = []
            for _ in range(n_each):
                local.append(self.bus._next_seq("hb:execution"))
            with lock:
                got.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(len(got), n_threads * n_each)
        self.assertEqual(len(set(got)), len(got), f"duplicate seq values: {len(got)-len(set(got))}")
        self.assertEqual(min(got), 1)
        self.assertEqual(max(got), n_threads * n_each)

    def test_true_backward_seq_same_pid_halts(self):
        """A living process jumping backwards is still a halt."""
        self._fresh_all(pid=4242)
        self.wd.inspect()
        # Same pid, seq goes  ? -> 1 (or 0)
        import json
        p = self.root / "heartbeats" / "execution.json"
        obj = json.loads(p.read_text())
        prev = obj["seq"]
        self.assertGreater(prev, 0)
        obj["seq"] = 1 if prev > 1 else 0
        obj["pid"] = 4242
        p.write_text(json.dumps(obj))
        fault = self.wd.check_and_trip()
        self.assertIsNotNone(fault)
        self.assertEqual(fault[0], "state_mismatch")
        self.assertEqual(fault[1], "execution")
        self.assertTrue(self.latch.is_halted())

    def test_new_process_seq_reset_is_not_mismatch(self):
        """Supervised restart: new pid may start seq at 1 without tripping HALT."""
        self._fresh_all(pid=100)
        # Give execution a high seq, same pid.
        for _ in range(5):
            self.bus.beat("execution", ok=True, status="ok", pid=100)
        fault = self.wd.inspect()
        self.assertIsNone(fault)
        last = self.bus.read_heartbeat("execution")
        self.assertGreaterEqual(last.seq, 5)
        # New process, seq restarts at 1 (new seq file / new epoch).
        self.bus.beat("execution", ok=True, status="starting", pid=200)
        # Overwrite seq to 1 with the new pid, mimicking a fresh counter.
        import json
        p = self.root / "heartbeats" / "execution.json"
        obj = json.loads(p.read_text())
        obj["seq"] = 1
        obj["pid"] = 200
        p.write_text(json.dumps(obj))
        fault = self.wd.inspect()
        self.assertIsNone(fault, f"restart artifact was treated as mismatch: {fault}")
        self.assertFalse(self.latch.is_halted())
        # Tracking follows the new epoch; a later same-pid backward jump still halts.
        obj["seq"] = 0
        obj["pid"] = 200
        p.write_text(json.dumps(obj))
        fault = self.wd.check_and_trip()
        self.assertIsNotNone(fault)
        self.assertEqual(fault[0], "state_mismatch")

    def test_reset_generation_clears_last_seen_seq(self):
        self._fresh_all(pid=1)
        self.wd.inspect()
        self.assertIn("execution", self.wd._last_seq)
        self.wd.reset_generation()
        self.assertEqual(self.wd._last_seq, {})
        self.assertEqual(self.wd._last_pid, {})
        # After a clean restart, seq 1 is not a mismatch.
        self._fresh_all(pid=99)
        self.bus.beat("execution", ok=True, status="starting", pid=99)
        import json
        p = self.root / "heartbeats" / "execution.json"
        obj = json.loads(p.read_text())
        obj["seq"] = 1
        obj["pid"] = 99
        p.write_text(json.dumps(obj))
        self.assertIsNone(self.wd.inspect())

    def test_keeper_touch_does_not_bump_seq(self):
        from polymarket_bot.runtime.heartbeat import HeartbeatKeeper
        self.bus.beat("discovery", ok=True, status="ok")
        seq1 = self.bus.read_heartbeat("discovery").seq
        keeper = HeartbeatKeeper(self.bus, "discovery", interval=0.15)
        # Don't call start() which seeds a new beat; just run touches.
        self.bus.touch("discovery")
        self.bus.touch("discovery")
        seq2 = self.bus.read_heartbeat("discovery").seq
        self.assertEqual(seq1, seq2)
        keeper.stop()
