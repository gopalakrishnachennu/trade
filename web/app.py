"""Paper dashboard: status from data age + HALT latch, never from PID alone.

One background StateHub composes state once per second (in a thread), keeps
chart history ring buffers and the ledger analytics, and every SSE client
reads the hub's latest payload. Clients get a snapshot on connect, then a
compact tick every second; a ledger message whenever the sqlite ledger
changes; and new events only.

Controls (paper only) write a versioned data/runtime/controls.json that the
engine and execution workers apply and acknowledge. Nothing here can enable
live trading or clear a HALT.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
DATA = Path(os.environ.get("DASHBOARD_DATA", str(ROOT / "data")))
RUNTIME = DATA / "runtime"
TOKEN_PATH = DATA / "dashboard_token.txt"
WEB = Path(__file__).resolve().parent
INDEX_HTML = WEB / "index.html"
RULES_HTML = WEB / "rules.html"
TZ = os.environ.get("DASHBOARD_TZ", "America/New_York")
ET = ZoneInfo(TZ)
CONFIG_PATH = Path(os.environ.get("DASHBOARD_CONFIG", str(ROOT / "config.yaml")))

from polymarket_bot import analytics  # noqa: E402
from polymarket_bot.runtime import controls as ctl  # noqa: E402

app = FastAPI(title="Polymarket paper terminal", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=600)

GRID_KEYS = (
    "asset", "horizon", "slug", "window_start", "window_end", "seconds_remaining",
    "price_to_beat", "index", "distance", "up_ask", "down_ask", "up_bid", "down_bid", "spread",
    "model_prob_up", "model_prob_down", "edge_up", "edge_down",
    "rules_pass_up", "rules_pass_down", "stale", "status",
    "verdict", "verdict_side", "reason", "reason_code", "stake_preview", "enabled", "checklist",
)
INDEX_KEEP_S = 2 * 3600 + 300
MARKET_KEEP_S = 2 * 3600
EVENTS_SNAPSHOT = 200
HISTORY_FILE = RUNTIME / "dash_history.json"


# ---------------------------------------------------------------- auth

def load_token() -> str:
    env = os.environ.get("DASHBOARD_TOKEN", "").strip()
    if env:
        return env
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text().strip()
    raise RuntimeError("No dashboard token")


def _const_eq(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    ok = 0
    for x, y in zip(a.encode(), b.encode()):
        ok |= x ^ y
    return ok == 0


def _header_tokens(request: Request) -> list[str]:
    out = []
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        out.append(auth[7:].strip())
    header = request.headers.get("x-dashboard-token")
    if header:
        out.append(header.strip())
    if auth.lower().startswith("basic "):
        import base64
        try:
            decoded = base64.b64decode(auth[6:].strip()).decode()
            out.append(decoded.partition(":")[2])
        except Exception:
            pass
    return out


def token_ok(request: Request, token: Optional[str]) -> bool:
    expected = load_token()
    if not expected:
        return False
    candidates = list(_header_tokens(request))
    if token:
        candidates.append(token)
    q = request.query_params.get("token")
    if q:
        candidates.append(q)
    return any(_const_eq(c, expected) for c in candidates if c)


def header_token_ok(request: Request) -> bool:
    """Writes need the token in a header (not just the URL) so a link cannot trigger them."""
    expected = load_token()
    return bool(expected) and any(_const_eq(c, expected) for c in _header_tokens(request) if c)


# ---------------------------------------------------------------- state hub

def _load_cfg():
    from polymarket_bot.config import load_config
    return load_config(CONFIG_PATH)


def _bus_latch():
    from polymarket_bot.runtime.bus import Bus
    from polymarket_bot.runtime.interlock import HaltLatch
    return Bus(RUNTIME), HaltLatch(RUNTIME / "HALT.json")


def risk_state(system: str, system_reason: str, halt: dict, pnl: dict, controls: dict) -> dict:
    if halt.get("halted"):
        return {"state": "HALTED", "reason": system_reason or halt.get("reason") or "halted"}
    if system == "STALE":
        return {"state": "STALE", "reason": f"data stale: {system_reason}"}
    rh = pnl.get("risk_halt")
    lock = pnl.get("lock") or {}
    if rh and "floor" in rh:
        return {"state": "LOCK STOP", "reason": f"{rh}; new entries stopped for the day, opens settle normally"}
    if rh and "target" in rh:
        return {"state": "TARGET HIT", "reason": f"{rh}; new entries blocked, opens settle normally"}
    if rh and "loss" in rh:
        return {"state": "LOSS LIMIT", "reason": f"{rh}; new entries blocked, opens settle normally"}
    if rh:
        return {"state": "BLOCKED", "reason": rh}
    if ctl.paused(controls):
        return {"state": "PAUSED", "reason": "new entries paused by operator"}
    if lock.get("phase") == "LOCKED":
        return {"state": "PROFIT LOCK", "reason": (f"target +${lock.get('target_usd', 0):.2f} hit; floor +${lock.get('floor_usd') or 0:.2f} "
                f"locked ({lock.get('peak_usd', 0) and round(100 * (lock.get('floor_usd') or 0) / lock['peak_usd'])}% of peak +${lock.get('peak_usd', 0):.2f}); "
                f"trading at {lock.get('size_mult', 1):g}x size")}
    return {"state": "TRADING", "reason": "entries allowed when a market passes every rule"}


class EdgeTail:
    """Incremental reader of evaluations.csv for today's edge distribution."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.day = None
        self.edges: list[float] = []
        self.passed = 0
        self.header: Optional[list[str]] = None

    def update(self) -> None:
        today = datetime.now(ET).date().isoformat()
        if today != self.day:
            self.day, self.edges, self.passed, self.offset = today, [], 0, 0
        if not self.path.exists():
            return
        size = self.path.stat().st_size
        if size < self.offset:  # rotated / truncated
            self.offset, self.edges, self.passed = 0, [], 0
        with self.path.open("rb") as f:
            if self.header is None:
                self.header = f.readline().decode(errors="replace").strip().split(",")
            if self.offset == 0:
                self.offset = max(len(",".join(self.header)) + 1, size - 8_000_000)
                f.seek(self.offset)
                if self.offset > len(",".join(self.header)) + 1:
                    f.readline()  # partial line
            else:
                f.seek(self.offset)
            chunk = f.read()
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            return
        body = chunk[: last_nl + 1]
        self.offset = self.offset + len(body) if self.offset else len(body)
        for row in csv.DictReader(io.StringIO(body.decode(errors="replace")), fieldnames=self.header):
            ts = str(row.get("ts") or "")
            d = analytics.parse_ts(ts, TZ)
            if d is None or d.astimezone(ET).date().isoformat() != today:
                continue
            try:
                e = float(row.get("edge_after_fees") or "nan")
            except ValueError:
                continue
            if e == e:
                self.edges.append(e)
                if str(row.get("passed")) in ("1", "True", "true"):
                    self.passed += 1
        self.edges = self.edges[-200_000:]

    def histogram(self, min_edge: float) -> dict:
        h = analytics.edge_histogram(self.edges)
        h["passed"] = self.passed
        h["above_min"] = sum(1 for e in self.edges if e >= min_edge)
        h["min_edge"] = min_edge
        h["source"] = str(self.path.name)
        return h


class StateHub:
    def __init__(self):
        self.lock = threading.Lock()
        self.index: dict[str, deque] = {}
        self.markets: dict[str, dict] = {}
        self.full: dict = {}
        self.tick: dict = {}
        self.ledger: dict = {}
        self.edges: dict = {}
        self.events: list[dict] = []
        self.controls: dict = {}
        self.cfg = None
        self.eff = None
        self.started = False
        self._last_persist = 0.0
        self._last_edges = 0.0
        self._edge_tail: Optional[EdgeTail] = None
        self._ticks = 0
        self.error: Optional[str] = None

    # -- persistence
    def load_history(self) -> None:
        try:
            raw = json.loads(HISTORY_FILE.read_text())
        except Exception:
            return
        now = time.time()
        for a, pts in (raw.get("index") or {}).items():
            self.index[a] = deque((p for p in pts if now - p[0] < INDEX_KEEP_S), maxlen=9000)
        for slug, m in (raw.get("markets") or {}).items():
            if m.get("end") and now - m["end"] > MARKET_KEEP_S:
                continue
            m["pts"] = deque(m.get("pts") or [], maxlen=4000)
            self.markets[slug] = m

    def persist(self) -> None:
        from polymarket_bot.runtime.bus import atomic_write_json
        with self.lock:
            out = {"index": {a: list(d) for a, d in self.index.items()},
                   "markets": {s: {**m, "pts": list(m["pts"])} for s, m in self.markets.items()}}
        try:
            atomic_write_json(HISTORY_FILE, out, fsync=False)
        except Exception:
            pass

    # -- one step
    def step(self) -> None:
        from polymarket_bot.runtime.view import compose
        bus, latch = _bus_latch()
        if self.cfg is None:
            self.cfg = _load_cfg()
        controls = ctl.load(RUNTIME)
        if controls.get("version") != (self.controls or {}).get("version") or self.eff is None:
            self.eff = ctl.effective_config(self.cfg, controls)
        self.controls = controls
        cfg, eff = self.cfg, self.eff
        full = compose(bus, latch, cfg)
        now = full["ts_unix"]

        # index ring buffers from the feed's per-second history
        with self.lock:
            for a, pts in (full.get("history") or {}).items():
                d = self.index.setdefault(a, deque(maxlen=9000))
                last = d[-1][0] if d else 0
                for p in pts or []:
                    t = p.get("ts")
                    if t and t > last and p.get("close") is not None:
                        d.append([int(t), float(p["close"])])
                        last = t
                while d and now - d[0][0] > INDEX_KEEP_S:
                    d.popleft()
            # per-market quote/model history
            for r in full.get("grid") or []:
                slug = r.get("slug")
                if not slug:
                    continue
                m = self.markets.get(slug)
                if m is None:
                    m = self.markets[slug] = {
                        "asset": r.get("asset"), "horizon": r.get("horizon"),
                        "start": analytics.ts_unix(r.get("window_start")), "end": analytics.ts_unix(r.get("window_end")),
                        "strike": r.get("price_to_beat"), "pts": deque(maxlen=4000),
                    }
                if r.get("price_to_beat") is not None:
                    m["strike"] = r.get("price_to_beat")
                if r.get("up_ask") is None and r.get("model_prob_up") is None:
                    continue
                if m["pts"] and now - m["pts"][-1][0] < 0.9:
                    continue
                m["pts"].append([round(now, 1), r.get("up_ask"), r.get("down_ask"), _r4(r.get("model_prob_up")),
                                 _r4(r.get("edge_up")), _r4(r.get("edge_down"))])
            for slug in [s for s, m in self.markets.items() if m.get("end") and now - m["end"] > MARKET_KEEP_S]:
                self.markets.pop(slug, None)

        # ledger (cached on sqlite mtime)
        rows = analytics.load_order_rows(cfg.sqlite_path)
        from polymarket_bot import profit_lock as pl
        ledger = analytics.ledger_bundle(rows, cfg.starting_cash, eff.daily_profit_target_pct,
                                         eff.daily_loss_limit_pct, cfg.assets, cfg.horizons, TZ, now=now,
                                         lock_params=pl.params_from_cfg(eff))
        if time.monotonic() - self._last_edges > 30 or not self.edges:
            if self._edge_tail is None:
                self._edge_tail = EdgeTail(cfg.evaluations_csv)
            try:
                self._edge_tail.update()
            except Exception as e:
                self.error = f"edges: {e}"
            self.edges = self._edge_tail.histogram(eff.min_edge)
            self._last_edges = time.monotonic()
        self.edges["min_edge"] = eff.min_edge

        pnl = full.get("pnl") or {}
        rs = risk_state(full["system"], full.get("system_reason") or "", full.get("halt") or {}, pnl, controls)
        hb = {h["component"]: h for h in full.get("health") or []}
        applied = {
            "engine": (hb.get("engine") or {}).get("seq") and _hb_extra(bus, "engine").get("controls_version"),
            "execution": _hb_extra(bus, "execution").get("controls_version"),
        }
        events = list(reversed(full.get("events") or []))[:EVENTS_SNAPSHOT]
        tick = {
            "kind": "tick",
            "ts_unix": now,
            "as_of_et": full.get("as_of_et"),
            "data_age_seconds": full.get("data_age_seconds"),
            "paper_only": True,
            "system": full.get("system"),
            "system_reason": full.get("system_reason"),
            "halt": full.get("halt"),
            "risk": rs,
            "health": full.get("health") or [],
            "snapshot_ages": full.get("snapshot_ages") or {},
            "indexes": {a: {"price": (v or {}).get("price"), "ts": (v or {}).get("ts")} for a, v in (full.get("indexes") or {}).items()},
            "grid": [{k: r.get(k) for k in GRID_KEYS} for r in full.get("grid") or []],
            "pnl": pnl,
            "kpis": ledger["kpis"],
            "ledger_version": ledger["version"],
            "controls": {"version": controls.get("version"), "values": controls.get("values"),
                         "effective": ctl.defaults_from(eff) | (controls.get("values") or {}),
                         "applied": applied, "updated_et": controls.get("updated_et")},
            "dashboard_error": self.error,
        }
        with self.lock:
            self.full, self.tick, self.ledger, self.events = full, tick, ledger, events
        self._ticks += 1
        if time.monotonic() - self._last_persist > 30:
            self._last_persist = time.monotonic()
            self.persist()

    def history(self, slug: Optional[str] = None) -> dict:
        with self.lock:
            if slug:
                m = self.markets.get(slug)
                return {"markets": {slug: {**m, "pts": list(m["pts"])}} if m else {}}
            live = {r.get("slug") for r in self.tick.get("grid") or [] if r.get("slug")}
            return {
                "index": {a: list(d) for a, d in self.index.items()},
                "markets": {s: {**m, "pts": list(m["pts"])} for s, m in self.markets.items() if s in live},
            }

    def snapshot(self) -> dict:
        with self.lock:
            snap = dict(self.tick)
            snap["kind"] = "snapshot"
            snap["ledger"] = self.ledger
            snap["edges"] = self.edges
            snap["events"] = self.events
            snap["rules"] = self.full.get("rules") or []
            snap["rules_catalog"] = self.full.get("rules_catalog") or []
            snap["control_fields"] = control_schema()
            snap["defaults"] = ctl.defaults_from(self.cfg) if self.cfg else {}
        return snap


def _r4(x):
    try:
        return None if x is None else round(float(x), 4)
    except (TypeError, ValueError):
        return None


def _hb_extra(bus, name: str) -> dict:
    hb = bus.read_heartbeat(name)
    return dict(hb.extra) if hb else {}


def control_schema() -> list[dict]:
    return [{"key": k, "kind": f.kind, "label": f.label, "lo": f.lo, "hi": f.hi,
             "choices": list(f.choices), "unit": f.unit} for k, f in ctl.FIELDS.items()]


HUB = StateHub()


async def _hub_loop():
    HUB.load_history()
    while True:
        t0 = time.monotonic()
        try:
            await asyncio.to_thread(HUB.step)
            HUB.error = None if HUB.error and HUB.error.startswith("hub") else HUB.error
        except Exception as e:  # keep serving last good state; surface the error
            HUB.error = f"hub: {type(e).__name__}: {e}"
        await asyncio.sleep(max(0.2, 1.0 - (time.monotonic() - t0)))


@app.on_event("startup")
async def _start_hub():
    if not HUB.started:
        HUB.started = True
        asyncio.get_event_loop().create_task(_hub_loop())


async def _ensure_state():
    if not HUB.tick:
        await asyncio.to_thread(HUB.step)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"))


# ---------------------------------------------------------------- routes

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # /vendor/ holds only the vendored open-source chart library (no data), so
    # <script src> works without putting the token in asset URLs.
    if (request.url.path.startswith("/vendor/") or request.url.path.startswith("/assets/")) and request.method == "GET":
        return await call_next(request)
    # /healthz is a public liveness probe: overall status word only, no data.
    if request.url.path == "/healthz" and request.method == "GET":
        return await call_next(request)
    if not token_ok(request, None):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return Response("Unauthorized. Append ?token=… to the URL.", status_code=401, media_type="text/plain")
    return await call_next(request)


app.mount("/vendor", StaticFiles(directory=str(WEB / "vendor")), name="vendor")
app.mount("/assets", StaticFiles(directory=str(WEB / "assets")), name="assets")


@app.get("/")
async def index():
    return FileResponse(INDEX_HTML, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


@app.get("/rules")
async def rules():
    return FileResponse(RULES_HTML, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


@app.get("/api/state")
async def api_state(part: str = Query(default="snapshot")):
    await _ensure_state()
    if part == "tick":
        body = HUB.tick
    elif part == "full":
        body = HUB.full
    elif part == "ledger":
        body = HUB.ledger
    else:
        body = HUB.snapshot()
    return Response(_dumps(body), media_type="application/json", headers={"Cache-Control": "no-store"})


@app.get("/api/history")
async def api_history(slug: Optional[str] = Query(default=None)):
    await _ensure_state()
    return Response(_dumps(HUB.history(slug)), media_type="application/json", headers={"Cache-Control": "no-store"})


@app.get("/api/controls")
async def get_controls():
    await _ensure_state()
    c = ctl.load(RUNTIME)
    return {"controls": c, "defaults": ctl.defaults_from(HUB.cfg), "fields": control_schema(),
            "applied": HUB.tick.get("controls", {}).get("applied"), "paper_only": True,
            "halt_reset": "CLI only: scripts/reset_halt.sh --yes on the droplet, after the cause is understood"}


def _log_control(record: dict, extra_msg: str = "") -> None:
    bus, _ = _bus_latch()
    msg = f"operator v{record['version']}: " + (extra_msg or ctl.describe(record.get("changes")))
    if record.get("note"):
        msg += f" — note: {record['note']}"
    bus.event("control", "dashboard", msg, version=record["version"], changes=record.get("changes"),
              flatten=record.get("flatten"))


@app.post("/api/controls")
async def post_controls(request: Request):
    if not header_token_ok(request):
        return JSONResponse({"error": "writes need the token in the X-Dashboard-Token header"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    changes = (body or {}).get("changes")
    try:
        res = ctl.apply(RUNTIME, changes, actor="dashboard", note=str((body or {}).get("note") or ""),
                        baseline=ctl.defaults_from(HUB.cfg) if HUB.cfg else None)
    except ctl.ControlError as e:
        return JSONResponse({"ok": False, "errors": e.errors}, status_code=400)
    _log_control(res["record"])
    return {"ok": True, "version": res["controls"]["version"], "record": res["record"], "controls": res["controls"]}


@app.post("/api/controls/flatten")
async def post_flatten(request: Request):
    if not header_token_ok(request):
        return JSONResponse({"error": "writes need the token in the X-Dashboard-Token header"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if (body or {}).get("confirm") != ctl.FLATTEN_CONFIRM:
        return JSONResponse({"ok": False, "errors": {"confirm": f"type {ctl.FLATTEN_CONFIRM} to confirm"}}, status_code=400)
    res = ctl.apply(RUNTIME, {}, actor="dashboard", note=str((body or {}).get("note") or ""), flatten=True)
    _log_control(res["record"], "FLATTEN requested: close all paper positions at the bid and pause new entries")
    return {"ok": True, "version": res["controls"]["version"], "record": res["record"]}


@app.post("/api/halt/reset")
async def halt_reset():
    # Deliberately not available from the web UI (fail-safe).
    return JSONResponse({"ok": False, "error": "HALT reset is CLI-only: scripts/reset_halt.sh --yes"}, status_code=405)


@app.get("/api/stream")
async def api_stream(request: Request, token: Optional[str] = Query(default=None)):
    if not token_ok(request, token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    await _ensure_state()

    async def gen():
        yield "retry: 2000\n\n"
        snap = HUB.snapshot()
        yield f"data: {_dumps(snap)}\n\n"
        ledger_v = snap.get("ledger_version")
        last_ev = max((e.get("ts_unix") or 0 for e in snap.get("events") or []), default=0)
        last_tick = snap.get("ts_unix")
        while True:
            await asyncio.sleep(0.5)
            if await request.is_disconnected():
                break
            tick = HUB.tick
            if tick.get("ts_unix") == last_tick:
                continue
            last_tick = tick.get("ts_unix")
            new_events = [e for e in HUB.events if (e.get("ts_unix") or 0) > last_ev]
            if new_events:
                last_ev = max(e.get("ts_unix") or 0 for e in new_events)
            yield f"data: {_dumps({**tick, 'events_new': new_events})}\n\n"
            if tick.get("ledger_version") != ledger_v:
                ledger_v = tick.get("ledger_version")
                yield f"data: {_dumps({'kind': 'ledger', 'ledger': HUB.ledger, 'edges': HUB.edges})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "Connection": "keep-alive", "X-Accel-Buffering": "no"})


# ================================================================ health, metrics

from polymarket_bot import health as hl  # noqa: E402
from polymarket_bot import archive as ar  # noqa: E402
from polymarket_bot import strategies as strat  # noqa: E402
from polymarket_bot import reviewer as rv  # noqa: E402
from polymarket_bot import alerts as al  # noqa: E402
from polymarket_bot.integrations import KeyStore, PROVIDERS, test_connection  # noqa: E402

SUPERVISOR_STALE_S = 10.0


def health_doc() -> dict:
    bus, latch = _bus_latch()
    snap = bus.snapshot("health") or {}
    payload = snap.get("payload") or {}
    items = list(payload.get("items") or [])
    age = time.time() - float(snap.get("ts_unix") or 0) if snap else None
    sup_ok = age is not None and age <= SUPERVISOR_STALE_S
    items.insert(0, hl.Item(
        "supervisor", hl.OK if sup_ok else hl.FAILED, True,
        f"supervisor publishes health every ~1s; FAILED if older than {SUPERVISOR_STALE_S:g}s "
        "(execution also halts itself if the watchdog beat is > 30s old)",
        f"{SUPERVISOR_STALE_S:g}s", "running" if sup_ok else ("not running" if age is None else f"last publish {age:.0f}s ago"),
        last_heartbeat=(time.time() - age) if age is not None else None, group="trading").as_dict())
    if not sup_ok:  # everything the supervisor reported is stale
        for it in items[1:]:
            it["detail"] = ("STALE (supervisor down) " + (it.get("detail") or "")).strip()
    a = al.status(DATA)
    items.append(hl.Item("alerts", hl.OK, False, "Telegram/email hooks; disabled until data/secrets/alerts.json exists",
                         "", "enabled: " + ", ".join(k for k, v in a.items() if v) if any(a.values()) else "disabled (not configured)").as_dict())
    items.append(hl.Item("dashboard_self", hl.OK, False, "this process answered", "", "serving", last_heartbeat=time.time()).as_dict())
    halt = latch.read().as_dict()
    status = hl.overall(items, bool(halt.get("halted")))
    return {"status": status, "halt": halt, "items": items, "ts_unix": time.time(), "supervisor_age_s": age}


@app.get("/healthz")
async def healthz():
    d = await asyncio.to_thread(health_doc)
    code = 200 if d["status"] in ("OK", "DEGRADED") else 503
    return JSONResponse({"status": d["status"], "halted": bool(d["halt"].get("halted"))}, status_code=code,
                        headers={"Cache-Control": "no-store"})


@app.get("/api/health")
async def api_health():
    return JSONResponse(await asyncio.to_thread(health_doc), headers={"Cache-Control": "no-store"})


def _prom_escape(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


@app.get("/metrics")
async def metrics():
    await _ensure_state()
    d = await asyncio.to_thread(health_doc)
    lv = {"OK": 0, "DEGRADED": 1, "FAILED": 2}
    out = ["# HELP pmbot_component_status Component health: 0=OK 1=DEGRADED 2=FAILED",
           "# TYPE pmbot_component_status gauge"]
    for it in d["items"]:
        out.append(f'pmbot_component_status{{component="{_prom_escape(it["name"])}",critical="{str(bool(it.get("critical"))).lower()}"}} {lv.get(it["status"], 2)}')
    out += ["# HELP pmbot_halted 1 if the HALT latch is set", "# TYPE pmbot_halted gauge",
            f"pmbot_halted {1 if d['halt'].get('halted') else 0}"]
    k = ((HUB.ledger or {}).get("kpis") or {})
    for key, help_ in (("equity", "Paper equity USD"), ("net_pnl", "Net realized P&L USD"), ("win_rate", "Win rate 0-1"),
                       ("bets", "Bets today"), ("open_exposure", "Open exposure USD"), ("max_drawdown", "Max drawdown USD")):
        v = k.get(key)
        if isinstance(v, (int, float)):
            out += [f"# HELP pmbot_{key} {help_}", f"# TYPE pmbot_{key} gauge", f"pmbot_{key} {float(v)}"]
    lock = k.get("lock") or {}
    for key in ("target_usd", "peak_usd", "floor_usd", "size_mult"):
        v = lock.get(key)
        if isinstance(v, (int, float)):
            out += [f"# TYPE pmbot_lock_{key} gauge", f"pmbot_lock_{key} {float(v)}"]
    st = ar.read_state(DATA)
    out += ["# TYPE pmbot_archive_trigger_bytes gauge", f"pmbot_archive_trigger_bytes {int(st.get('trigger_bytes') or 0)}",
            "# TYPE pmbot_archive_unacked gauge", f"pmbot_archive_unacked {sum(1 for a in ar.list_archives(DATA) if not a.get('acked_at'))}"]
    try:
        mdoc = json.loads((DATA / "strategies" / "metrics.json").read_text())
        out.append("# TYPE pmbot_strategy_pnl gauge")
        for n, s in (mdoc.get("strategies") or {}).items():
            out.append(f'pmbot_strategy_pnl{{strategy="{_prom_escape(n)}",active="{str(n == mdoc.get("active")).lower()}"}} {float((s.get("metrics") or {}).get("pnl") or 0)}')
    except Exception:
        pass
    return Response("\n".join(out) + "\n", media_type="text/plain; version=0.0.4")


def _page(name: str):
    return FileResponse(WEB / name, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


@app.get("/health")
async def page_health():
    return _page("health.html")


@app.get("/strategies")
async def page_strategies():
    return _page("strategies.html")


@app.get("/integrations")
async def page_integrations():
    return _page("integrations.html")


@app.get("/archives")
async def page_archives():
    return _page("archives.html")


def _need_header(request: Request):
    if not header_token_ok(request):
        return JSONResponse({"error": "writes need the token in the X-Dashboard-Token header"}, status_code=403)
    return None


async def _json_body(request: Request) -> dict:
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


# ================================================================ archives

@app.get("/api/archives")
async def api_archives(format: Optional[str] = Query(default=None)):
    if format == "tsv":  # for the Mac pull script (no JSON parser needed): name, sha256, acked
        lines = [f"{a['name']}\t{a.get('sha256') or ''}\t{1 if a.get('acked_at') else 0}" for a in ar.list_archives(DATA)]
        return Response("\n".join(lines) + ("\n" if lines else ""), media_type="text/plain")
    st = ar.read_state(DATA)
    return {"archives": ar.list_archives(DATA), "state": st, "threshold_bytes": ar.THRESHOLD_BYTES,
            "ack_keep_days": ar.ACK_KEEP_DAYS, "live_bytes": st.get("live_bytes"), "trigger_bytes": st.get("trigger_bytes")}


@app.get("/api/archives/{name}")
async def api_archive_get(name: str):
    p = ar.resolve_name(DATA, name)
    if p is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    mt = "application/json" if name.endswith(".json") else "application/octet-stream"
    return FileResponse(p, media_type=mt, filename=name, headers={"Cache-Control": "no-store"})


@app.post("/api/archives/{name}/ack")
async def api_archive_ack(name: str, request: Request):
    bad = _need_header(request)
    if bad:
        return bad
    body = await _json_body(request)
    try:
        man = await asyncio.to_thread(ar.ack, DATA, name, str(body.get("sha256") or ""), HUB.cfg.sqlite_path if HUB.cfg else None)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    bus, _ = _bus_latch()
    bus.event("archive", "dashboard", f"acknowledged {name} from {str(body.get('client') or 'client')[:40]}; pruned {man.get('pruned')}")
    return {"ok": True, "acked_at": man.get("acked_at"), "pruned": man.get("pruned")}


@app.post("/api/archives/run")
async def api_archive_run(request: Request):
    bad = _need_header(request)
    if bad:
        return bad
    try:
        man = await asyncio.to_thread(ar.run_archive, DATA, TZ, force=True)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)
    if not man:
        return JSONResponse({"ok": False, "error": "archive job busy"}, status_code=409)
    bus, _ = _bus_latch()
    bus.event("archive", "dashboard", f"manual archive {man.get('archive')} sha256={str(man.get('sha256'))[:12]}…")
    return {"ok": True, "manifest": man}


# ================================================================ strategies

@app.get("/api/strategies")
async def api_strategies():
    def build():
        try:
            mdoc = json.loads((DATA / "strategies" / "metrics.json").read_text())
        except Exception:
            mdoc = {}
        dec = rv.read_decisions(DATA, 300)
        resolved = {d.get("proposal") for d in dec if d.get("kind") in ("approve", "reject")}
        for d in dec:
            if d.get("kind") == "proposal" and d.get("id") in resolved:
                d["resolved"] = True
        ks = KeyStore(DATA)
        return {"metrics": mdoc, "active": strat.read_active(DATA), "settings": rv.read_settings(DATA),
                "settings_bounds": rv.SETTINGS_BOUNDS, "param_bounds": strat.PARAM_BOUNDS,
                "decisions": dec, "reviewer": rv.read_status(DATA), "reviewer_config": ks.meta().get("reviewer") or {}}
    return JSONResponse(await asyncio.to_thread(build), headers={"Cache-Control": "no-store"})


@app.post("/api/strategies/review")
async def api_review_now(request: Request):
    bad = _need_header(request)
    if bad:
        return bad
    bus, _ = _bus_latch()
    rec = await asyncio.to_thread(rv.run_review_safe, DATA, "manual", bus)
    return {"ok": rec.get("status") not in ("error", "busy"), "result": rec}


@app.post("/api/strategies/proposals/{pid}/{action}")
async def api_resolve(pid: str, action: str, request: Request):
    bad = _need_header(request)
    if bad:
        return bad
    bus, _ = _bus_latch()
    try:
        rec = await asyncio.to_thread(rv.resolve, DATA, pid, action, "operator", bus)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "result": rec, "active": strat.read_active(DATA)}


@app.post("/api/strategies/settings")
async def api_strat_settings(request: Request):
    bad = _need_header(request)
    if bad:
        return bad
    body = await _json_body(request)
    s = rv.write_settings(DATA, {k: v for k, v in body.items() if k in rv.DEFAULT_SETTINGS})
    bus, _ = _bus_latch()
    bus.event("strategy", "dashboard", f"reviewer/gate settings updated: {json.dumps(s)}")
    return {"ok": True, "settings": s}


# ================================================================ integrations

@app.get("/api/integrations")
async def api_integrations():
    return KeyStore(DATA).public()


@app.post("/api/integrations/reviewer")
async def api_integrations_reviewer(request: Request):
    bad = _need_header(request)
    if bad:
        return bad
    body = await _json_body(request)
    try:
        r = KeyStore(DATA).set_reviewer(body.get("provider") or None, body.get("model"))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    bus, _ = _bus_latch()
    bus.event("integration", "dashboard", f"reviewer set to {r.get('provider') or 'none (deterministic gate only)'} {r.get('model') or ''}")
    return {"ok": True, "reviewer": r}


@app.post("/api/integrations/{provider}")
async def api_integration_set(provider: str, request: Request):
    bad = _need_header(request)
    if bad:
        return bad
    if provider not in PROVIDERS:
        return JSONResponse({"ok": False, "error": "unknown provider"}, status_code=404)
    body = await _json_body(request)
    try:
        pub = await asyncio.to_thread(KeyStore(DATA).set, provider, str(body.get("key") or ""), body.get("base_url"), body.get("model"))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    bus, _ = _bus_latch()
    bus.event("integration", "dashboard", f"{provider} connected ({pub.get('masked')})")
    return {"ok": True, "provider": pub}


@app.post("/api/integrations/{provider}/test")
async def api_integration_test(provider: str, request: Request):
    bad = _need_header(request)
    if bad:
        return bad
    if provider not in PROVIDERS:
        return JSONResponse({"ok": False, "error": "unknown provider"}, status_code=404)
    res = await asyncio.to_thread(test_connection, KeyStore(DATA), provider)
    return {"ok": res["ok"], "result": res}


@app.delete("/api/integrations/{provider}")
async def api_integration_delete(provider: str, request: Request):
    bad = _need_header(request)
    if bad:
        return bad
    if provider not in PROVIDERS:
        return JSONResponse({"ok": False, "error": "unknown provider"}, status_code=404)
    await asyncio.to_thread(KeyStore(DATA).delete, provider)
    bus, _ = _bus_latch()
    bus.event("integration", "dashboard", f"{provider} disconnected (key deleted)")
    return {"ok": True}


# ================================================================ exports and report

def _csv_response(rows: list[dict], fields: list[str], filename: str) -> Response:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


ORDER_FIELDS = ["id", "ts", "market_slug", "asset", "horizon", "side", "contracts", "price", "fee_usd", "cost_usd",
                "model_prob", "edge_after_fees", "price_to_beat", "window_end", "status", "settlement_price",
                "result", "payout_usd", "pnl_usd", "reject_reason"]


def _orders(db: Path) -> list[dict]:
    import sqlite3
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM paper_orders WHERE status IN ('open','settled') ORDER BY ts")]
    finally:
        con.close()


@app.get("/api/export/trades.csv")
async def export_trades():
    await _ensure_state()
    rows = await asyncio.to_thread(_orders, HUB.cfg.sqlite_path)
    return _csv_response(rows, ORDER_FIELDS, "paper_trades.csv")


@app.get("/api/export/strategy/{name}.csv")
async def export_strategy(name: str):
    if name not in strat.STRATEGIES:
        return JSONResponse({"error": "unknown strategy"}, status_code=404)
    rows = await asyncio.to_thread(_orders, DATA / "strategies" / "shadow" / name / "paper_trades.sqlite")
    return _csv_response(rows, ORDER_FIELDS, f"shadow_{name}.csv")


@app.get("/api/export/decisions.csv")
async def export_decisions():
    rows = [{**d, "gate": json.dumps(d.get("gate")) if d.get("gate") else ""} for d in rv.read_decisions(DATA, 100000)]
    return _csv_response(rows, ["ts", "kind", "id", "proposal", "trigger", "source", "provider", "model", "status",
                                "suggested", "from", "to", "actor", "message", "gate"], "strategy_decisions.csv")


@app.get("/api/report/daily")
async def report_daily(day: Optional[str] = Query(default=None)):
    await _ensure_state()

    def build():
        import importlib.util
        from datetime import date as _date
        spec = importlib.util.spec_from_file_location("daily_report", ROOT / "scripts" / "daily_report.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        d = _date.fromisoformat(day) if day else datetime.now(ET).date()
        orders, snaps = mod._load_orders(HUB.cfg.sqlite_path)
        return mod.build_report(HUB.cfg, d, orders, snaps)
    try:
        txt = await asyncio.to_thread(build)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return Response(txt, media_type="text/plain; charset=utf-8", headers={"Cache-Control": "no-store"})
