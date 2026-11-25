"""Start, watch, and fail-closed supervise trading processes. Dashboard is separate."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import load_config
from .bus import Bus
from .interlock import HaltLatch
from .watchdog import REQUIRED, Watchdog

ET = ZoneInfo("America/New_York")
TRADING_ROLES = ("discovery", "index_feed", "book_feed", "engine", "execution")
# Non-critical helpers: restarted with backoff, never trip HALT.
AUX_ROLES = ("strategy_lab",)
AUX_BACKOFF_S = 30.0
ARCHIVE_EVERY_S = 600.0


def _log(msg: str) -> None:
    ts = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"{ts} supervisor: {msg}"
    print(line, flush=True)


def _spawn(role: str, cfg_path: Path, runtime: Path, python: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    src = str(cfg_path.parent / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    # workers.py lives as a module
    return subprocess.Popen(
        [python, "-m", "polymarket_bot.runtime.workers", "--role", role, "--config", str(cfg_path), "--runtime", str(runtime)],
        env=env,
        cwd=str(cfg_path.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Fail-closed trading supervisor (paper only)")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--runtime", default=None)
    args = p.parse_args(argv)
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cand = Path.cwd() / cfg_path
        cfg_path = cand if cand.exists() else Path(__file__).resolve().parents[3] / cfg_path
    cfg = load_config(cfg_path)
    runtime = Path(args.runtime) if args.runtime else (cfg.root / "data" / "runtime")
    runtime.mkdir(parents=True, exist_ok=True)
    bus = Bus(runtime)
    latch = HaltLatch(runtime / "HALT.json")
    wd = Watchdog(bus, latch, assets=cfg.assets, horizons=cfg.horizons)
    python = sys.executable
    (cfg.root / "data").mkdir(exist_ok=True)
    (cfg.root / "data" / "supervisor.pid").write_text(str(os.getpid()) + "\n")

    procs: dict[str, subprocess.Popen] = {}
    aux: dict[str, subprocess.Popen] = {}
    aux_next: dict[str, float] = {}
    stop = {"flag": False}

    from ..health import HealthMonitor, OK, DEGRADED, FAILED, Item
    data_dir = cfg.sqlite_path.parent

    def _exec_trading() -> bool:
        hb = bus.read_heartbeat("execution")
        return bool(hb and hb.status == "trading" and (time.time() - hb.wall_ts) < 10)

    monitor = HealthMonitor(data_dir, cfg.sqlite_path, execution_trading=_exec_trading)
    last_status: dict = {}
    monitor.start()

    def archive_job():
        from .. import archive as ar
        while not stop["flag"]:
            try:
                man = ar.run_archive(data_dir, cfg.timezone)
                if man:
                    _log(f"archived {man.get('archive')} ({man.get('bytes', 0)/1e6:.1f} MB)")
                    bus.event("archive", "archive", f"created {man.get('archive')} sha256={str(man.get('sha256'))[:12]}…")
                gone = ar.prune_acked(data_dir)
                for g in gone:
                    bus.event("archive", "archive", f"deleted acknowledged {g}")
            except Exception as e:
                _log(f"archive job error: {e}")
                bus.event("degraded", "archive", f"archive job error: {e}")
            for _ in range(int(ARCHIVE_EVERY_S)):
                if stop["flag"]:
                    return
                time.sleep(1)

    import threading
    threading.Thread(target=archive_job, name="archive", daemon=True).start()

    def manage_aux():
        now = time.time()
        for role in AUX_ROLES:
            proc = aux.get(role)
            if proc is not None and proc.poll() is None:
                continue
            if proc is not None:
                code = proc.poll()
                _log(f"{role} (non-critical) exited {code}; restart in {AUX_BACKOFF_S:.0f}s")
                bus.event("degraded", role, f"non-critical process exited {code}; restarting in {AUX_BACKOFF_S:.0f}s")
                aux.pop(role, None)
                aux_next[role] = now + AUX_BACKOFF_S
                continue
            if now >= aux_next.get(role, 0):
                aux[role] = _spawn(role, cfg_path, runtime, python)
                (runtime / f"{role}.pid").write_text(str(aux[role].pid) + "\n")
                _log(f"started {role} (non-critical) pid={aux[role].pid}")

    def health_items() -> list[dict]:
        now = time.time()
        out = []
        for i in wd.health(now):
            st = OK if i.ok else (FAILED if (i.stale and i.present) or not i.present else DEGRADED)
            if latch.is_halted() and i.component != "watchdog":
                st = DEGRADED if not i.ok else OK  # parked by the latch, not a new failure
            out.append(Item(
                name=i.component if i.component != "watchdog" else "supervisor_watchdog",
                status=st, critical=True, group="trading",
                rule=f"heartbeat age ≤ {i.threshold:g}s and ok; a stale/missing beat latches HALT (watchdog)",
                threshold=f"{i.threshold:g}s", value=f"{i.status} · age {i.age_seconds if i.age_seconds is not None else '—'}s · seq {i.seq}",
                last_heartbeat=(now - i.age_seconds) if i.age_seconds is not None else None,
            ).as_dict())
        hb = bus.read_heartbeat("strategy_lab")
        age = None if hb is None else now - hb.wall_ts
        lab_st = OK if (hb and hb.ok and age is not None and age < 15) else DEGRADED
        out.append(Item("strategy_lab", lab_st, False, "shadow books heartbeat ≤ 15s; restarted with 30s backoff on exit",
                        "15s", f"{hb.status if hb else 'missing'} · age {age:.1f}s" if age is not None else "missing",
                        last_heartbeat=hb.wall_ts if hb else None, group="trading").as_dict())
        out += [i.as_dict() for i in monitor.items()]
        return out

    def shutdown(*_):
        stop["flag"] = True

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    def start_trading():
        # Drop leftover heartbeats from a previous generation so the watchdog
        # does not instantly see a 3-minute-old beat and trip HALT on startup.
        wd.reset_generation()
        hb_dir = runtime / "heartbeats"
        snap_dir = runtime / "snapshots"
        for role in TRADING_ROLES:
            hp = hb_dir / f"{role}.json"
            if hp.exists():
                hp.unlink()
        # Drop leftover snapshots too: a 20-minute-old discovery window would
        # trip missing_window immediately, before the new discovery process publishes.
        if snap_dir.exists():
            for name in ("discovery", "indexes", "books", "engine", "execution"):
                sp = snap_dir / f"{name}.json"
                if sp.exists():
                    sp.unlink()
        for role in TRADING_ROLES:
            if role in procs and procs[role].poll() is None:
                continue
            procs[role] = _spawn(role, cfg_path, runtime, python)
            _log(f"started {role} pid={procs[role].pid}")
            (runtime / f"{role}.pid").write_text(str(procs[role].pid) + "\n")

    def stop_trading(reason: str):
        _log(f"stopping trading processes ({reason})")
        for role, proc in list(procs.items()):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        t0 = time.time()
        while time.time() - t0 < 5:
            if all(proc.poll() is not None for proc in procs.values()):
                break
            time.sleep(0.2)
        for role, proc in list(procs.items()):
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    _log(f"{role} did not reap after SIGKILL pid={proc.pid}")
        procs.clear()

    # If we come up already halted, do not start trading.
    if latch.is_halted():
        h = latch.read()
        _log(f"latch already set ({h.reason} from {h.component}) — not starting trading")
        bus.event("supervisor", "supervisor", f"start while halted: {h.reason}")
    else:
        start_trading()
        bus.event("supervisor", "supervisor", "trading processes started")

    # After a manual reset, we can start trading if we were sitting idle.
    was_halted = latch.is_halted()

    while not stop["flag"]:
        halted = latch.is_halted()
        # Crash of a trading process while not halted → trip and do not restart.
        for role, proc in list(procs.items()):
            code = proc.poll()
            if code is not None:
                _log(f"{role} exited {code}")
                if not latch.is_halted():
                    latch.trip("process_exit", role, f"exit code {code}")
                    bus.event("halt", role, f"process exited {code}")
                # Do not auto-restart.

        # Manual reset first: start trading (which clears leftover heartbeats)
        # before the watchdog inspects, or a 20-minute-old beat from the previous
        # generation would instantly re-trip the latch.
        if was_halted and not latch.is_halted():
            _log("halt latch cleared — starting trading processes")
            start_trading()
            bus.event("supervisor", "supervisor", "trading resumed after manual reset")
        was_halted = latch.is_halted()

        # If *we* were not scheduled, every in-process keeper also froze.
        # Do not blame a worker on the resumed tick (2:02 ET stale_heartbeat).
        stall = wd.observe_scheduler()
        if stall:
            _log(f"host stall detected ({stall}) — skipping this inspect")
            bus.event("supervisor", "supervisor", f"host_stall: {stall}")

        if not stall and not latch.is_halted() and procs:
            fault = wd.check_and_trip()
            if fault:
                _log(f"watchdog halt {fault[0]} from {fault[1]}: {fault[2]}")
                stop_trading(fault[0])

        if not stall and not latch.is_halted() and procs:
            bad = monitor.critical_fault()
            if bad:
                latch.trip("health_critical", bad.name, f"{bad.value} — rule: {bad.rule}")
                bus.event("halt", bad.name, f"critical health check FAILED: {bad.value}")
                _log(f"health halt from {bad.name}: {bad.value}")
                stop_trading("health_critical")

        if latch.is_halted() and procs:
            # Once halted, park trading processes (no silent continue).
            stop_trading("halt latched")

        manage_aux()

        bus.beat("watchdog", ok=True, status="halted" if latch.is_halted() else "watching")
        bus.publish("watchdog", {
            "health": [
                {
                    "component": i.component, "present": i.present, "age_seconds": i.age_seconds,
                    "stale": i.stale, "ok": i.ok, "status": i.status, "threshold": i.threshold, "seq": i.seq,
                }
                for i in wd.health()
            ],
            "halt": latch.read().as_dict(),
        })
        try:
            items_now = health_items()
            bus.publish("health", {"items": items_now, "halted": latch.is_halted(), "ts_unix": time.time()})
            from .. import alerts
            for it in items_now:
                prev = last_status.get(it["name"])
                if prev is not None and prev != it["status"] and it["status"] != OK:
                    alerts.notify(data_dir, f"{it['name']}:{it['status']}",
                                  f"{it['name']} {prev} → {it['status']} ({'CRITICAL' if it['critical'] else 'non-critical'}): {it['value']} {it.get('detail') or ''}")
                last_status[it["name"]] = it["status"]
            if latch.is_halted() and not last_status.get("__halt"):
                h = latch.read()
                alerts.notify(data_dir, "halt", f"HALT latched: {h.reason} from {h.component}: {h.details}")
            last_status["__halt"] = latch.is_halted()
        except Exception as e:  # health reporting must never stop supervision
            _log(f"health publish error: {e}")
        time.sleep(1)

    stop_trading("supervisor exiting")
    monitor.stop()
    for role, proc in list(aux.items()):
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    _log("exit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
