"""System health probes, critical vs non-critical, run by the supervisor.

Every component reports status OK / DEGRADED / FAILED with its rule and
threshold. CRITICAL components latch HALT when FAILED (after `confirm`
consecutive failed checks); NON-CRITICAL ones are shown DEGRADED/FAILED and
alerted but never halt trading.

Thresholds can be overridden with environment variables (see OPERATIONS.md).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

OK, DEGRADED, FAILED = "OK", "DEGRADED", "FAILED"


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


DISK_FAIL_FREE_GB = _envf("HEALTH_DISK_FAIL_GB", 1.0)
DISK_FAIL_FREE_PCT = _envf("HEALTH_DISK_FAIL_PCT", 5.0)
DISK_WARN_FREE_GB = _envf("HEALTH_DISK_WARN_GB", 3.0)
DISK_WARN_FREE_PCT = _envf("HEALTH_DISK_WARN_PCT", 15.0)
MEM_FAIL_AVAIL_PCT = _envf("HEALTH_MEM_FAIL_PCT", 3.0)
MEM_WARN_AVAIL_PCT = _envf("HEALTH_MEM_WARN_PCT", 12.0)
CLOCK_FAIL_S = _envf("HEALTH_CLOCK_FAIL_S", 1.0)
CLOCK_WARN_S = _envf("HEALTH_CLOCK_WARN_S", 0.25)
CERT_WARN_DAYS = _envf("HEALTH_CERT_WARN_DAYS", 14)
CERT_FAIL_DAYS = _envf("HEALTH_CERT_FAIL_DAYS", 3)
DASHBOARD_PORT = int(_envf("DASHBOARD_PORT", 8787))


@dataclass
class Item:
    name: str
    status: str
    critical: bool
    rule: str
    threshold: str = ""
    value: str = ""
    detail: str = ""
    last_heartbeat: Optional[float] = None
    checked_at: float = field(default_factory=time.time)
    group: str = "system"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ probes (pure-ish)

def probe_disk(path: Path, usage: Optional[Callable] = None) -> Item:
    u = (usage or shutil.disk_usage)(str(path))
    free_gb = u.free / 1e9
    free_pct = 100.0 * u.free / u.total if u.total else 0.0
    rule = (f"FAILED if free < {DISK_FAIL_FREE_GB:g} GB or < {DISK_FAIL_FREE_PCT:g}% (halts before the disk fills); "
            f"DEGRADED if < {DISK_WARN_FREE_GB:g} GB or < {DISK_WARN_FREE_PCT:g}%")
    if free_gb < DISK_FAIL_FREE_GB or free_pct < DISK_FAIL_FREE_PCT:
        st = FAILED
    elif free_gb < DISK_WARN_FREE_GB or free_pct < DISK_WARN_FREE_PCT:
        st = DEGRADED
    else:
        st = OK
    return Item("disk", st, True, rule, f"{DISK_FAIL_FREE_GB:g} GB / {DISK_FAIL_FREE_PCT:g}%",
                f"{free_gb:.1f} GB free ({free_pct:.0f}%) of {u.total/1e9:.0f} GB")


def read_meminfo(text: Optional[str] = None) -> dict[str, int]:
    if text is None:
        try:
            text = Path("/proc/meminfo").read_text()
        except OSError:
            return {}
    out = {}
    for ln in text.splitlines():
        m = re.match(r"(\w+):\s+(\d+)", ln)
        if m:
            out[m.group(1)] = int(m.group(2)) * 1024
    return out


def probe_memory(mi: Optional[dict] = None) -> Item:
    mi = read_meminfo() if mi is None else mi
    rule = (f"FAILED if MemAvailable < {MEM_FAIL_AVAIL_PCT:g}% and swap > 90% used; "
            f"DEGRADED if MemAvailable < {MEM_WARN_AVAIL_PCT:g}% or swap > 50% used")
    if not mi.get("MemTotal"):
        return Item("memory", DEGRADED, True, rule, detail="/proc/meminfo unavailable")
    avail_pct = 100.0 * mi.get("MemAvailable", 0) / mi["MemTotal"]
    st_total = mi.get("SwapTotal", 0)
    swap_used_pct = 100.0 * (st_total - mi.get("SwapFree", 0)) / st_total if st_total else 100.0
    if avail_pct < MEM_FAIL_AVAIL_PCT and swap_used_pct > 90:
        st = FAILED
    elif avail_pct < MEM_WARN_AVAIL_PCT or (st_total and swap_used_pct > 50):
        st = DEGRADED
    else:
        st = OK
    return Item("memory_swap", st, True, rule, f"{MEM_FAIL_AVAIL_PCT:g}% avail",
                f"{mi.get('MemAvailable',0)/2**20:.0f} MB avail ({avail_pct:.0f}%), swap {swap_used_pct if st_total else 0:.0f}% used")


def parse_timesync(text: str) -> tuple[Optional[float], Optional[bool]]:
    off = None
    m = re.search(r"Offset:\s*([+-]?[\d.]+)\s*(us|µs|ms|s)\b", text)
    if m:
        v = float(m.group(1))
        off = v / 1e6 if m.group(2) in ("us", "µs") else v / 1e3 if m.group(2) == "ms" else v
    return off, None


def probe_clock(run: Optional[Callable] = None) -> Item:
    rule = f"FAILED if |NTP offset| > {CLOCK_FAIL_S:g}s; DEGRADED if > {CLOCK_WARN_S:g}s, unsynchronized or unknown"
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout)
    try:
        show = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]).strip()
        ts = run(["timedatectl", "timesync-status"])
    except Exception as e:
        return Item("ntp_clock", DEGRADED, True, rule, f"{CLOCK_FAIL_S:g}s", "unknown", f"timedatectl unavailable: {type(e).__name__}")
    off, _ = parse_timesync(ts or "")
    synced = show == "yes"
    if off is not None and abs(off) > CLOCK_FAIL_S:
        st = FAILED
    elif off is None or abs(off) > CLOCK_WARN_S or not synced:
        st = DEGRADED
    else:
        st = OK
    val = ("offset " + (f"{off*1000:+.1f} ms" if off is not None else "unknown")) + (" · synced" if synced else " · NOT synced")
    return Item("ntp_clock", st, True, rule, f"{CLOCK_FAIL_S:g}s", val)


def probe_sqlite_integrity(db: Path) -> Item:
    rule = "PRAGMA quick_check on the ledger every 15 min; FAILED unless 'ok'; DEGRADED if locked/unreadable once"
    if not db.exists():
        return Item("sqlite_integrity", DEGRADED, True, rule, "ok", "missing", str(db))
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            res = con.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            con.close()
    except sqlite3.OperationalError as e:  # locked/busy: retry next time
        return Item("sqlite_integrity", DEGRADED, True, rule, "ok", "unreadable", str(e)[:200])
    except sqlite3.DatabaseError as e:     # corrupt / not a database
        return Item("sqlite_integrity", FAILED, True, rule, "ok", "corrupt", str(e)[:200])
    return Item("sqlite_integrity", OK if res == "ok" else FAILED, True, rule, "ok", str(res)[:200])


def probe_ledger(db: Path, execution_trading: bool) -> Item:
    rule = "Ledger readable; equity snapshot written ≤ 30s ago while execution runs. FAILED if unreadable; DEGRADED if stale"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            n = con.execute("SELECT COUNT(*) FROM paper_orders WHERE status IN ('open','settled')").fetchone()[0]
            last = con.execute("SELECT ts FROM equity ORDER BY rowid DESC LIMIT 1").fetchone()
        finally:
            con.close()
    except Exception as e:
        return Item("ledger", FAILED, True, rule, "30s", "unreadable", str(e)[:200])
    age = None
    if last and last[0]:
        try:
            d = datetime.fromisoformat(str(last[0]).replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.astimezone()
            age = time.time() - d.timestamp()
        except ValueError:
            pass
    st = OK
    if execution_trading and (age is None or age > 30):
        st = DEGRADED
    return Item("ledger", st, True, rule, "30s", f"{n} fills · last equity write {('%.0fs ago' % age) if age is not None else '—'}",
                last_heartbeat=(time.time() - age) if age is not None else None)


def cert_days_left(host: str, port: int = 443, connect_host: str = "127.0.0.1", timeout: float = 5.0) -> float:
    ctx = ssl.create_default_context()
    with socket.create_connection((connect_host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ss:
            cert = ss.getpeercert()
    exp = ssl.cert_time_to_seconds(cert["notAfter"])
    return (exp - time.time()) / 86400


def public_host() -> Optional[str]:
    h = os.environ.get("PUBLIC_HOST")
    if h:
        return h
    try:
        for ln in Path("/etc/caddy/Caddyfile").read_text().splitlines():
            m = re.match(r"^\s*([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\s*\{", ln)
            if m:
                return m.group(1)
    except OSError:
        pass
    return None


def probe_tls() -> Item:
    rule = f"Caddy serves a valid cert; DEGRADED if < {CERT_WARN_DAYS:g} days left, FAILED if < {CERT_FAIL_DAYS:g} days or unreachable"
    host = public_host()
    if not host:
        return Item("caddy_tls", DEGRADED, False, rule, f"{CERT_WARN_DAYS:g} d", "no public host configured")
    try:
        days = cert_days_left(host)
    except Exception as e:
        return Item("caddy_tls", FAILED, False, rule, f"{CERT_WARN_DAYS:g} d", "unreachable", f"{type(e).__name__}: {e}"[:200])
    st = FAILED if days < CERT_FAIL_DAYS else DEGRADED if days < CERT_WARN_DAYS else OK
    return Item("caddy_tls", st, False, rule, f"{CERT_WARN_DAYS:g} d", f"{host}: cert expires in {days:.1f} days")


def probe_dashboard(port: int = DASHBOARD_PORT) -> Item:
    rule = "GET /healthz on 127.0.0.1 answers within 3s"
    t0 = time.time()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as r:
            r.read(2000)
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code  # 503 = dashboard alive but system unhealthy
    except Exception as e:
        return Item("dashboard", FAILED, False, rule, "3s", "down", f"{type(e).__name__}: {e}"[:200])
    return Item("dashboard", OK, False, rule, "3s", f"HTTP {code} in {(time.time()-t0)*1000:.0f} ms", last_heartbeat=time.time())


def probe_archive(data_dir: Path) -> list[Item]:
    from . import archive as ar
    st = ar.read_state(data_dir)
    rule = "Archive job runs every 10 min; DEGRADED on error or no check in 30 min"
    last_check = st.get("last_check")
    err = st.get("last_error") if (st.get("last_error_at") or 0) > (st.get("last_run") or 0) else None
    status = OK
    if err or not last_check or time.time() - last_check > 1800:
        status = DEGRADED
    trig = st.get("trigger_bytes", st.get("live_bytes", 0)) or 0
    thr = st.get("threshold_bytes", ar.THRESHOLD_BYTES)
    a = Item("archive_job", status, False, rule, f"{thr/2**20:.0f} MB",
             f"{trig/2**20:.1f} MB toward next part · last part {st.get('last_part') or '—'}",
             err or "", last_heartbeat=last_check)
    age = ar.unacked_age_hours(data_dir)
    rule2 = "Mac pull acknowledges each part (sha256 verified); DEGRADED if a part is unacknowledged > 24h"
    s2 = DEGRADED if (age is not None and age > 24) else OK
    n_un = sum(1 for x in ar.list_archives(data_dir) if not x.get("acked_at"))
    b = Item("sync_acks", s2, False, rule2, "24 h",
             f"{n_un} unacknowledged" + (f" (oldest {age:.1f} h)" if age is not None else ""),
             last_heartbeat=st.get("last_ack"))
    return [a, b]


def probe_reviewer(data_dir: Path) -> Item:
    from . import reviewer
    st = reviewer.read_status(data_dir)
    rule = "Hourly review; DEGRADED if the last LLM call failed (current strategy is kept, never halts)"
    try:
        meta = json.loads((Path(data_dir) / "integrations.json").read_text()).get("reviewer") or {}
    except Exception:
        meta = {}
    if not st:
        return Item("llm_reviewer", OK, False, rule, "1 h", "no review yet" + ("" if meta.get("provider") else " · no LLM configured (deterministic gate)"))
    val = f"last {st.get('trigger')} review via {st.get('source')}" + (f" ({st.get('provider')}/{st.get('model')})" if st.get("provider") else "")
    if not st.get("ok"):
        return Item("llm_reviewer", DEGRADED, False, rule, "1 h", val, st.get("error") or "", last_heartbeat=st.get("last_run"))
    return Item("llm_reviewer", OK, False, rule, "1 h", val, last_heartbeat=st.get("last_run"))


# ------------------------------------------------------------------ monitor

class HealthMonitor:
    """Runs probes on their own schedules in a background thread.

    The supervisor main loop reads `items()` and latches HALT on a critical
    FAILED that persisted for `confirm` consecutive checks.
    """

    SCHEDULE = {  # seconds between runs
        "disk": 10, "memory_swap": 10, "ntp_clock": 60, "sqlite_integrity": 900, "ledger": 15,
        "caddy_tls": 600, "dashboard": 20, "archive": 30, "llm_reviewer": 30,
    }

    def __init__(self, data_dir: Path, db: Path, confirm: int = 2, execution_trading: Callable[[], bool] = lambda: True):
        self.data_dir = Path(data_dir)
        self.db = Path(db)
        self.confirm = confirm
        self.execution_trading = execution_trading
        self._items: dict[str, Item] = {}
        self._fails: dict[str, int] = {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _run_probe(self, key: str) -> list[Item]:
        if key == "disk":
            return [probe_disk(self.data_dir)]
        if key == "memory_swap":
            return [probe_memory()]
        if key == "ntp_clock":
            return [probe_clock()]
        if key == "sqlite_integrity":
            return [probe_sqlite_integrity(self.db)]
        if key == "ledger":
            return [probe_ledger(self.db, self.execution_trading())]
        if key == "caddy_tls":
            return [probe_tls()]
        if key == "dashboard":
            return [probe_dashboard()]
        if key == "archive":
            return probe_archive(self.data_dir)
        if key == "llm_reviewer":
            return [probe_reviewer(self.data_dir)]
        return []

    def tick(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        for key, every in self.SCHEDULE.items():
            if now - self._last.get(key, 0) < every:
                continue
            self._last[key] = now
            try:
                items = self._run_probe(key)
            except Exception as e:
                items = [Item(key, DEGRADED, False, "probe crashed", detail=f"{type(e).__name__}: {e}"[:200])]
            with self._lock:
                for it in items:
                    it.checked_at = now
                    self._fails[it.name] = self._fails.get(it.name, 0) + 1 if it.status == FAILED else 0
                    self._items[it.name] = it

    def items(self) -> list[Item]:
        with self._lock:
            return list(self._items.values())

    def critical_fault(self) -> Optional[Item]:
        with self._lock:
            for it in self._items.values():
                if it.critical and it.status == FAILED and self._fails.get(it.name, 0) >= self.confirm:
                    return it
        return None

    def start(self) -> None:
        def loop():
            while not self._stop.is_set():
                self.tick()
                self._stop.wait(2.0)
        self._thread = threading.Thread(target=loop, name="health", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def overall(items: list[dict], halted: bool) -> str:
    if halted:
        return "HALTED"
    if any(i["status"] == FAILED and i.get("critical") for i in items):
        return "FAILED"
    if any(i["status"] != OK for i in items):
        return "DEGRADED"
    return "OK"
