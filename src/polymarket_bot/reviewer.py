"""Strategy reviewer: LLM (optional) proposes, a deterministic gate decides.

Flow (hourly from strategy_lab, or "Run review now" from the dashboard):
  1. Load shadow metrics (data/strategies/metrics.json).
  2. If a reviewer provider is configured, send metrics + recent trades (no
     secrets, no keys, no tokens) and ask for strict JSON:
       {"ranking": [...], "suggested_active": "...", "param_tweaks": {...}, "rationale": "..."}
     If the call or parsing fails: log it, mark the reviewer DEGRADED, keep the
     current strategy. Never halt.
     With no provider: rank deterministically (works without any key).
  3. Clamp parameter tweaks (bounds + max 25% of range per review) and apply
     them to the shadow books only.
  4. Run the promotion gate. Pass + auto_promote → switch the active strategy.
     Pass + manual mode → proposal stays pending for approve/reject.
  5. Log proposal, gate decision and any switch to decisions.jsonl and events.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Optional

from . import strategies as strat

DEFAULT_SETTINGS = {
    "auto_promote": True,
    "min_trades": 30,          # settled trades the candidate needs
    "min_margin_usd": 10.0,    # candidate P&L must beat active by this much
    "min_margin_pct": 0.01,    # ... and by this fraction of starting equity
    "max_dd_ratio": 1.10,      # candidate drawdown ≤ active drawdown × ratio (+ $5 slack)
    "min_profit_factor": 1.10,
    "max_brier": 0.25,
    "cooldown_hours": 6.0,
    "review_interval_min": 60,
    "max_step_frac": 0.25,     # max tweak per review as a fraction of the bound range
}
SETTINGS_BOUNDS = {
    "min_trades": (5, 1000), "min_margin_usd": (0.0, 1000.0), "min_margin_pct": (0.0, 0.5),
    "max_dd_ratio": (0.5, 3.0), "min_profit_factor": (0.5, 5.0), "max_brier": (0.05, 0.5),
    "cooldown_hours": (0.0, 168.0), "review_interval_min": (10, 1440), "max_step_frac": (0.0, 1.0),
}


def _dir(data_dir: Path) -> Path:
    return strat.strategies_dir(data_dir)


def read_settings(data_dir: Path) -> dict[str, Any]:
    try:
        raw = json.loads((_dir(data_dir) / "settings.json").read_text())
    except Exception:
        raw = {}
    return clamp_settings(raw)


def clamp_settings(raw: dict) -> dict[str, Any]:
    out = dict(DEFAULT_SETTINGS)
    for k, v in (raw or {}).items():
        if k == "auto_promote":
            out[k] = bool(v)
        elif k in SETTINGS_BOUNDS:
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            lo, hi = SETTINGS_BOUNDS[k]
            fv = min(hi, max(lo, fv))
            out[k] = int(round(fv)) if k in ("min_trades", "review_interval_min") else fv
    return out


def write_settings(data_dir: Path, raw: dict) -> dict[str, Any]:
    s = clamp_settings({**read_settings(data_dir), **(raw or {})})
    p = _dir(data_dir) / "settings.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2))
    os.replace(tmp, p)
    return s


def log_decision(data_dir: Path, rec: dict, bus=None) -> dict:
    rec = {"ts": time.time(), **rec}
    with (_dir(data_dir) / "decisions.jsonl").open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    if bus is not None:
        try:
            bus.event("strategy", "reviewer", rec.get("message") or rec.get("kind", ""), kind=rec.get("kind"))
        except Exception:
            pass
    return rec


def read_decisions(data_dir: Path, n: int = 200) -> list[dict]:
    p = _dir(data_dir) / "decisions.jsonl"
    if not p.exists():
        return []
    lines = p.read_text().splitlines()[-n:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


# ------------------------------------------------------------------ gate

def score(m: dict) -> float:
    return float(m.get("pnl") or 0) - 0.5 * float(m.get("max_drawdown") or 0)


def deterministic_ranking(metrics: dict[str, dict]) -> list[str]:
    return sorted(metrics, key=lambda n: score(metrics[n]["metrics"]), reverse=True)


def gate(metrics: dict[str, dict], active: str, candidate: Optional[str], settings: dict,
         starting_equity: float, last_switch_ts: Optional[float], now: Optional[float] = None) -> dict[str, Any]:
    """Deterministic promotion gate. Returns {passed, checks:[{rule, passed, detail}]}."""
    now = now or time.time()
    checks = []

    def add(rule, ok, detail):
        checks.append({"rule": rule, "passed": bool(ok), "detail": detail})

    if candidate is None or candidate not in metrics:
        add("candidate is a known strategy", False, f"{candidate!r}")
        return {"passed": False, "checks": checks}
    if candidate == active:
        add("candidate differs from active", False, f"{candidate} is already active")
        return {"passed": False, "checks": checks}
    c = metrics[candidate]["metrics"]
    a = metrics.get(active, {}).get("metrics") or {}
    add(f"≥ {settings['min_trades']} settled trades", c.get("trades", 0) >= settings["min_trades"],
        f"{c.get('trades', 0)} trades")
    margin = max(settings["min_margin_usd"], settings["min_margin_pct"] * starting_equity)
    diff = float(c.get("pnl") or 0) - float(a.get("pnl") or 0)
    add(f"P&L beats active by ≥ ${margin:.2f}", diff >= margin, f"{c.get('pnl', 0):+.2f} vs {a.get('pnl', 0):+.2f} (Δ {diff:+.2f})")
    a_dd, c_dd = float(a.get("max_drawdown") or 0), float(c.get("max_drawdown") or 0)
    lim = a_dd * settings["max_dd_ratio"] + 5.0
    add("drawdown not worse", c_dd <= lim, f"{c_dd:.2f} ≤ {lim:.2f}")
    pf = c.get("profit_factor")
    add(f"profit factor ≥ {settings['min_profit_factor']}", pf is not None and pf >= settings["min_profit_factor"], f"{pf}")
    br = c.get("brier")
    add(f"Brier ≤ {settings['max_brier']}", br is not None and br <= settings["max_brier"], f"{br}")
    if last_switch_ts:
        hrs = (now - last_switch_ts) / 3600
        add(f"cooldown ≥ {settings['cooldown_hours']}h since last switch", hrs >= settings["cooldown_hours"], f"{hrs:.1f}h")
    return {"passed": all(x["passed"] for x in checks), "checks": checks}


def clamp_tweaks(tweaks: Any, current: dict[str, dict], max_step_frac: float) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not isinstance(tweaks, dict):
        return out
    for name, p in tweaks.items():
        if name not in strat.STRATEGIES or not isinstance(p, dict):
            continue
        cur = current.get(name) or strat.default_params(name)
        new = {}
        for k, v in p.items():
            if k not in strat.PARAM_BOUNDS:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            lo, hi = strat.PARAM_BOUNDS[k]
            step = (hi - lo) * max_step_frac
            base = cur.get(k)
            if base is None:  # baseline uses the configured edge; tweaks to it are not applied
                continue
            fv = min(base + step, max(base - step, fv))
            fv = round(min(hi, max(lo, fv)), 4)
            if abs(fv - base) > 1e-9:
                new[k] = fv
        if new:
            out[name] = new
    return out


# ------------------------------------------------------------------ LLM

SYSTEM = (
    "You review paper-trading strategies for 5m/15m/1h crypto up/down binary markets. "
    "Return ONLY a JSON object with keys: ranking (array of strategy names, best first), "
    "suggested_active (one strategy name), param_tweaks (object: strategy -> {param: number}; "
    "allowed params: min_edge, min_prob, min_ask, max_ask, max_frac_left; may be empty), "
    "rationale (string, <= 400 chars). Prefer robustness: penalize small samples and drawdowns. "
    "Do not invent strategies."
)


def build_payload(metrics_doc: dict, settings: dict, pattern_summary: Optional[dict] = None) -> dict:
    strats = {}
    for name, s in (metrics_doc.get("strategies") or {}).items():
        m = dict(s.get("metrics") or {})
        m.pop("calibration", None)
        strats[name] = {"description": s.get("description"), "params": s.get("params"), "metrics": m,
                        "today": s.get("today"), "recent_trades": (s.get("recent") or [])[-25:]}
    payload = {"active": metrics_doc.get("active"), "strategies": strats,
               "param_bounds": strat.PARAM_BOUNDS, "gate": {k: settings[k] for k in (
                   "min_trades", "min_margin_usd", "max_dd_ratio", "min_profit_factor", "max_brier")}}
    if pattern_summary:
        ctxs = list((pattern_summary.get("tables") or {}).get("context") or [])
        by_ev = sorted([c for c in ctxs if c.get("n", 0) > 0], key=lambda c: c.get("post_ev", 0), reverse=True)
        payload["patterns"] = {
            "global": pattern_summary.get("global"),
            "top_contexts": by_ev[:6],
            "bottom_contexts": by_ev[-6:],
            "vol_regime": (pattern_summary.get("tables") or {}).get("vol_regime"),
            "distance_bucket": (pattern_summary.get("tables") or {}).get("distance_bucket"),
            "shadow_stats": pattern_summary.get("shadow_stats"),
        }
    return payload


def parse_reply(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON object in reply")
    obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("reply is not an object")
    rank = [n for n in (obj.get("ranking") or []) if n in strat.STRATEGIES]
    sug = obj.get("suggested_active")
    if sug not in strat.STRATEGIES:
        raise ValueError(f"suggested_active {sug!r} is not a known strategy")
    return {"ranking": rank, "suggested_active": sug, "param_tweaks": obj.get("param_tweaks") or {},
            "rationale": str(obj.get("rationale") or "")[:600]}


def reviewer_status_path(data_dir: Path) -> Path:
    return _dir(data_dir) / "reviewer_state.json"


def _write_status(data_dir: Path, st: dict) -> None:
    p = reviewer_status_path(data_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, default=str))
    os.replace(tmp, p)


def read_status(data_dir: Path) -> dict:
    try:
        return json.loads(reviewer_status_path(data_dir).read_text())
    except Exception:
        return {}


def last_switch_ts(data_dir: Path) -> Optional[float]:
    rec = strat.read_active(data_dir)
    return rec.get("changed_at")


def run_review(data_dir: Path, trigger: str, bus=None, *, llm_call=None) -> dict:
    """One review. Returns the proposal record. `llm_call(system, user) -> text` is injectable for tests."""
    from .lab_params import apply_tweaks  # local import to avoid cycles
    data_dir = Path(data_dir)
    lock_fd = os.open(_dir(data_dir) / "review.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy", "message": "a review is already running"}
        settings = read_settings(data_dir)
        try:
            doc = json.loads((_dir(data_dir) / "metrics.json").read_text())
        except Exception:
            doc = {}
        metrics = doc.get("strategies") or {}
        active = strat.read_active(data_dir)
        prev = read_status(data_dir)
        status = {"last_run": time.time(), "trigger": trigger, "ok": True, "error": None,
                  "consecutive_errors": 0, "source": None, "provider": None, "model": None,
                  "last_success": prev.get("last_success")}
        rec: dict[str, Any] = {"kind": "proposal", "id": uuid.uuid4().hex[:10], "trigger": trigger,
                               "active_before": active["active"]}
        if not metrics:
            status.update(ok=True, source="none")
            _write_status(data_dir, status)
            rec.update(status="no-data", message=f"review ({trigger}): no shadow metrics yet")
            return log_decision(data_dir, rec, bus)

        provider = model = None
        if llm_call is None:
            from .integrations import KeyStore, chat
            ks = KeyStore(data_dir)
            rv = (ks.meta().get("reviewer") or {})
            provider, model = rv.get("provider"), rv.get("model")
            if provider:
                llm_call = lambda s, u: chat(ks, provider, model, s, u)  # noqa: E731
        else:
            provider, model = "injected", "test"

        if llm_call is not None:
            status.update(source="llm", provider=provider, model=model)
            try:
                from . import patterns as pat
                ps = pat.load_pattern_store(data_dir).summary()
                reply = parse_reply(llm_call(SYSTEM, json.dumps(build_payload(doc, settings, ps), default=str)))
            except Exception as e:
                from .integrations import scrub
                err = scrub(f"{type(e).__name__}: {e}")
                status.update(ok=False, error=err, consecutive_errors=int(prev.get("consecutive_errors") or 0) + 1)
                _write_status(data_dir, status)
                rec.update(kind="error", status="error", source="llm", provider=provider, model=model, error=err,
                           message=f"reviewer error ({provider}/{model}): {err} — keeping {active['active']}")
                return log_decision(data_dir, rec, bus)
            source = "llm"
        else:
            rank = deterministic_ranking(metrics)
            reply = {"ranking": rank, "suggested_active": rank[0], "param_tweaks": {},
                     "rationale": "deterministic ranking by P&L − 0.5 × max drawdown (no LLM configured)"}
            source = "deterministic"
            status.update(source=source)

        from .strategy_lab import read_params
        cur_params = read_params(data_dir)
        tweaks = clamp_tweaks(reply.get("param_tweaks"), cur_params, settings["max_step_frac"])
        if tweaks:
            apply_tweaks(data_dir, tweaks)
        g = gate(metrics, active["active"], reply["suggested_active"], settings,
                 float(doc.get("starting_equity") or 1000.0), last_switch_ts(data_dir))
        rec.update(source=source, provider=provider, model=model, ranking=reply["ranking"],
                   suggested=reply["suggested_active"], rationale=reply["rationale"], tweaks=tweaks, gate=g)
        if reply["suggested_active"] == active["active"]:
            rec.update(status="no-change", message=f"review ({source}): keep {active['active']}")
        elif g["passed"] and settings["auto_promote"]:
            params = read_params(data_dir)[reply["suggested_active"]]
            strat.write_active(data_dir, reply["suggested_active"], params,
                               reason=f"auto-promoted by gate (proposal {rec['id']})", actor=f"gate/{source}")
            rec.update(status="applied", message=f"gate passed: switched {active['active']} → {reply['suggested_active']}")
            log_decision(data_dir, {"kind": "switch", "from": active["active"], "to": reply["suggested_active"],
                                    "proposal": rec["id"], "actor": f"gate/{source}",
                                    "message": f"active strategy {active['active']} → {reply['suggested_active']} (gate)"}, bus)
        elif g["passed"]:
            rec.update(status="pending", message=f"gate passed for {reply['suggested_active']}; awaiting manual approval")
        else:
            failed = [c["rule"] for c in g["checks"] if not c["passed"]]
            rec.update(status="pending",
                       message=f"suggests {reply['suggested_active']}; gate failed ({'; '.join(failed)}); keep {active['active']}")
        status["last_success"] = time.time()
        _write_status(data_dir, status)
        return log_decision(data_dir, rec, bus)
    finally:
        os.close(lock_fd)


def run_review_safe(data_dir: Path, trigger: str, bus=None) -> dict:
    try:
        return run_review(data_dir, trigger, bus)
    except Exception as e:
        from .integrations import scrub
        err = scrub(f"{type(e).__name__}: {e}")
        st = read_status(data_dir)
        st.update(last_run=time.time(), ok=False, error=err, consecutive_errors=int(st.get("consecutive_errors") or 0) + 1)
        _write_status(data_dir, st)
        return log_decision(data_dir, {"kind": "error", "status": "error", "error": err,
                                       "trace": traceback.format_exc()[-800:],
                                       "message": f"reviewer crashed: {err}"}, bus)


def resolve(data_dir: Path, proposal_id: str, action: str, actor: str = "operator", bus=None) -> dict:
    """Manual approve/reject of a pending proposal."""
    from .strategy_lab import read_params
    decisions = read_decisions(data_dir, 2000)
    prop = next((d for d in reversed(decisions) if d.get("kind") == "proposal" and d.get("id") == proposal_id), None)
    if prop is None:
        raise ValueError("unknown proposal")
    done = {d.get("proposal") for d in decisions if d.get("kind") in ("approve", "reject")}
    if proposal_id in done or prop.get("status") not in ("pending",):
        raise ValueError(f"proposal is {prop.get('status')}, not pending" if proposal_id not in done else "already resolved")
    if action == "reject":
        return log_decision(data_dir, {"kind": "reject", "proposal": proposal_id, "actor": actor,
                                       "message": f"operator rejected proposal {proposal_id} ({prop.get('suggested')})"}, bus)
    if action != "approve":
        raise ValueError("action must be approve or reject")
    cur = strat.read_active(data_dir)
    target = prop["suggested"]
    strat.write_active(data_dir, target, read_params(data_dir)[target],
                       reason=f"approved by operator (proposal {proposal_id})", actor=actor)
    log_decision(data_dir, {"kind": "approve", "proposal": proposal_id, "actor": actor,
                            "message": f"operator approved proposal {proposal_id}"}, bus)
    return log_decision(data_dir, {"kind": "switch", "from": cur["active"], "to": target, "proposal": proposal_id,
                                   "actor": actor, "message": f"active strategy {cur['active']} → {target} (operator)"}, bus)
