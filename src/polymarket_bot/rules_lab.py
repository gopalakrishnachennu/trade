"""Analyst rule proposals: a small DSL, a versioned queue, walk-forward backtests,
shadow observation, and a deterministic promotion/demotion gate.

This is separate from the strategy-lab reviewer (which promotes among named
strategies). The same queue is the hook the LLM reviewer should use later:
`enqueue_proposal(...)` is the single write path.

Learning layers can never loosen hard caps, the session loss limit, the rolling
HALT, paper-only mode, or the fail-closed latch. A rule may only skip or shrink
/ modestly scale size inside those caps.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .bandit import ACTION_MULT, compose_learning, edge_bucket_for, enrich_buckets, minutes_bucket_for
from .patterns import _f

ET = ZoneInfo("America/New_York")

ALLOWED_FIELDS = frozenset({
    "asset", "horizon", "side", "hour_et", "hour_bucket", "weekday",
    "minutes_left", "minutes_bucket", "distance_sigma", "distance_bucket",
    "vol_regime", "annualized_vol", "model_prob", "ask", "edge", "spread",
    "depth_contracts", "depth_usd", "strategy", "edge_bucket",
    # Entry-time path features (computed only from ticks at or before the decision).
    "mom_1m", "mom_3m", "mom_window", "mom_1m_bucket", "mom_3m_bucket", "mom_window_bucket",
    "strike_cross_count", "crosses_bucket", "drift_dir", "signed_drift",
})
ALLOWED_OPS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in"})
ALLOWED_ACTIONS = frozenset({"skip", "size_mult"})
# Size multipliers a rule may request — still clamped later by compose_learning / size_order.
MULT_LO, MULT_HI = 0.25, 1.50
MIN_SHADOW_TRADES = 30

DEFAULT_GATE = {
    "min_trades": MIN_SHADOW_TRADES,
    "min_margin_usd": 0.0,          # must be strictly better net P&L
    "max_dd_ratio": 1.00,           # not worse than champion (+ $1 slack)
    "dd_slack_usd": 1.0,
    "auto_promote": False,
    "auto_demote": True,
    "demote_min_trades": 20,
    "demote_margin_usd": 20.0,
    "demote_on_floor_breach": True,
}


def lab_dir(data_dir: Path) -> Path:
    d = Path(data_dir) / "rules_lab"
    d.mkdir(parents=True, exist_ok=True)
    return d


def queue_path(data_dir: Path) -> Path:
    return lab_dir(data_dir) / "queue.json"


def history_path(data_dir: Path) -> Path:
    return lab_dir(data_dir) / "history.jsonl"


def shadow_path(data_dir: Path) -> Path:
    return lab_dir(data_dir) / "shadow.json"


def _atomic_write(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    os.replace(tmp, path)


def load_queue(data_dir: Path) -> list[dict]:
    p = queue_path(data_dir)
    try:
        raw = json.loads(p.read_text())
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict) and isinstance(raw.get("proposals"), list):
            return raw["proposals"]
    except Exception:
        pass
    return []


def save_queue(data_dir: Path, rows: list[dict]) -> None:
    _atomic_write(queue_path(data_dir), {"version": 1, "proposals": rows})


def append_history(data_dir: Path, rec: dict) -> None:
    rec = dict(rec)
    rec.setdefault("ts_unix", time.time())
    rec.setdefault("ts_et", datetime.now(ET).strftime("%Y-%m-%d %I:%M:%S %p %Z"))
    with history_path(data_dir).open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def read_history(data_dir: Path, n: int = 200) -> list[dict]:
    p = history_path(data_dir)
    if not p.exists():
        return []
    lines = p.read_text().splitlines()
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def load_shadow(data_dir: Path) -> dict:
    p = shadow_path(data_dir)
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def save_shadow(data_dir: Path, doc: dict) -> None:
    _atomic_write(shadow_path(data_dir), doc)


# ----- DSL -----

def _cmp(op: str, left, right) -> bool:
    if op in ("in", "not_in"):
        seq = list(right) if isinstance(right, (list, tuple, set)) else [right]
        seq = [str(x).lower() if isinstance(x, str) else x for x in seq]
        lv = str(left).lower() if isinstance(left, str) else left
        hit = lv in seq
        return hit if op == "in" else (not hit)
    # numeric when both look numeric
    lf, rf = _f(left), _f(right)
    if lf is not None and rf is not None and op in ("lt", "lte", "gt", "gte", "eq", "ne"):
        if op == "lt":
            return lf < rf
        if op == "lte":
            return lf <= rf
        if op == "gt":
            return lf > rf
        if op == "gte":
            return lf >= rf
        if op == "eq":
            return abs(lf - rf) <= 1e-12
        return abs(lf - rf) > 1e-12
    ls, rs = ("" if left is None else str(left).lower()), ("" if right is None else str(right).lower())
    if op == "eq":
        return ls == rs
    if op == "ne":
        return ls != rs
    return False


def validate_rule(raw: Any) -> dict[str, Any]:
    """Return a cleaned rule or raise ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("rule must be an object")
    action = str(raw.get("action") or "").strip()
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"action must be one of {sorted(ALLOWED_ACTIONS)}")
    size_mult = None
    if action == "size_mult":
        try:
            size_mult = float(raw.get("size_mult"))
        except (TypeError, ValueError):
            raise ValueError("size_mult is required when action is size_mult")
        if not (MULT_LO - 1e-12 <= size_mult <= MULT_HI + 1e-12):
            raise ValueError(f"size_mult must be in [{MULT_LO}, {MULT_HI}]")
        size_mult = round(max(MULT_LO, min(MULT_HI, size_mult)), 4)
    conds = raw.get("when") or raw.get("conditions")
    if not isinstance(conds, dict) and not isinstance(conds, list):
        raise ValueError("when: must be a list of conditions or {all|any: [...]}")
    if isinstance(conds, list):
        conds = {"all": conds}
    kind = "all" if "all" in conds else ("any" if "any" in conds else None)
    if kind is None or not isinstance(conds[kind], list) or not conds[kind]:
        raise ValueError("when must have a non-empty all: or any: list")
    cleaned = []
    for c in conds[kind]:
        if not isinstance(c, dict):
            raise ValueError("each condition must be {field, op, value}")
        field = str(c.get("field") or "").strip()
        op = str(c.get("op") or "").strip()
        if field not in ALLOWED_FIELDS:
            raise ValueError(f"field {field!r} is not allowed (cannot touch caps, mode, or risk limits)")
        if op not in ALLOWED_OPS:
            raise ValueError(f"op {op!r} is not allowed")
        if "value" not in c:
            raise ValueError("condition needs a value")
        cleaned.append({"field": field, "op": op, "value": c["value"]})
    # Forbidden keys that would smuggle a live-mode or cap change
    for banned in ("mode", "daily_loss_limit_pct", "max_per_bet_usd", "rolling_guard", "halt"):
        if banned in raw:
            raise ValueError(f"rule cannot set {banned}")
    return {
        "action": action,
        "size_mult": size_mult,
        "when": {kind: cleaned},
        "rationale": str(raw.get("rationale") or "")[:2000],
        "name": str(raw.get("name") or raw.get("id") or "")[:80],
        "applies_to": parse_applies_to(raw.get("applies_to")),
    }


def parse_applies_to(raw) -> list[str] | None:
    """None means the rule applies to every strategy. Otherwise a list of names."""
    if raw is None or raw == "all" or raw == "*" or raw == []:
        return None
    if isinstance(raw, str):
        parts = [s.strip() for s in raw.split(",") if s.strip()]
    elif isinstance(raw, (list, tuple)):
        parts = [str(s).strip() for s in raw if str(s).strip()]
    else:
        raise ValueError("applies_to must be a list of strategy names (or omitted for all)")
    if not parts or parts == ["all"] or parts == ["*"]:
        return None
    from .strategies import STRATEGIES
    known = set(STRATEGIES)
    out: list[str] = []
    for s in parts:
        if s not in known:
            raise ValueError(f"unknown strategy in applies_to: {s!r}")
        if s not in out:
            out.append(s)
    return out or None


def rule_applies_to_strategy(rule: dict, strategy: str | None) -> bool:
    applies = rule.get("applies_to")
    if not applies:
        return True
    return (strategy or "") in applies


def set_applies_to(data_dir: Path, pid: str, applies_to, *, actor: str) -> dict | None:
    """Update a queued rule's strategy scope without changing its status."""
    rec = get_proposal(data_dir, pid)
    if rec is None:
        raise ValueError(f"unknown rule {pid}")
    rule = dict(rec.get("rule") or {})
    rule["applies_to"] = parse_applies_to(applies_to)
    cleaned = validate_rule(rule)
    updated = update_proposal(data_dir, pid, rule=cleaned)
    append_history(data_dir, {"event": "set_applies_to", "id": pid, "actor": actor,
                              "applies_to": cleaned.get("applies_to")})
    return updated


def rule_matches(rule: dict, feat: dict) -> bool:
    f = enrich_buckets(feat)
    if "edge_bucket" not in f:
        f["edge_bucket"] = edge_bucket_for(_f(f.get("edge")))
    if "minutes_bucket" not in f:
        f["minutes_bucket"] = minutes_bucket_for(_f(f.get("minutes_left")))
    if not rule_applies_to_strategy(rule, f.get("strategy") or feat.get("strategy")):
        return False
    when = rule.get("when") or {}
    kind = "all" if "all" in when else "any"
    conds = when.get(kind) or []
    hits = []
    for c in conds:
        hits.append(_cmp(c["op"], f.get(c["field"]), c["value"]))
    return all(hits) if kind == "all" else any(hits)


def apply_rule(rule: dict, feat: dict) -> dict[str, Any]:
    """If the rule matches, return a decision dict compatible with compose_learning."""
    if not rule_matches(rule, feat):
        return {"passed": True, "mult": 1.0, "would_action": "noop", "detail": None, "matched": False}
    if rule["action"] == "skip":
        return {"passed": False, "mult": 0.0, "would_action": "skip", "matched": True,
                "detail": f"rule {rule.get('name') or rule.get('id')}: skip"}
    m = float(rule.get("size_mult") or 1.0)
    return {"passed": True, "mult": m, "would_action": f"size_{m:g}", "matched": True,
            "detail": f"rule {rule.get('name') or rule.get('id')}: {m:g}×"}


def apply_active_rules(rules: list[dict], feat: dict) -> dict[str, Any]:
    """Compose all *active* rules conservatively (any skip wins; min multiplier)."""
    passed, mult, details = True, 1.0, []
    matched = 0
    for r in rules:
        d = apply_rule(r, feat)
        if not d.get("matched"):
            continue
        matched += 1
        if not d.get("passed", True):
            passed = False
            details.append(d.get("detail"))
        else:
            mult = min(mult, float(d.get("mult") or 1.0))
            details.append(d.get("detail"))
    if not passed:
        return {"passed": False, "mult": 0.0, "would_action": "skip", "matched": matched,
                "detail": " | ".join(x for x in details if x)}
    return {"passed": True, "mult": mult, "would_action": "size" if matched else "noop",
            "matched": matched, "detail": " | ".join(x for x in details if x) or None}


# ----- queue / lifecycle -----

def enqueue_proposal(data_dir: Path, rule: dict, *, actor: str, rationale: str = "",
                     source: str = "analyst") -> dict:
    cleaned = validate_rule({**rule, "rationale": rationale or rule.get("rationale") or ""})
    pid = cleaned.get("name") or ""
    if not pid or pid in {p.get("id") for p in load_queue(data_dir)}:
        pid = (cleaned.get("name") or "rule")[:40] + "-" + uuid.uuid4().hex[:8]
    rec = {
        "id": pid,
        "status": "proposed",
        "source": source,
        "actor": actor,
        "rule": cleaned,
        "rationale": cleaned["rationale"],
        "created_ts": time.time(),
        "created_et": datetime.now(ET).strftime("%Y-%m-%d %I:%M:%S %p %Z"),
        "backtest": None,
        "shadow": None,
        "gate": None,
    }
    q = load_queue(data_dir)
    q.append(rec)
    save_queue(data_dir, q)
    append_history(data_dir, {"event": "proposed", "id": pid, "actor": actor, "source": source})
    return rec


def get_proposal(data_dir: Path, pid: str) -> Optional[dict]:
    for p in load_queue(data_dir):
        if p.get("id") == pid:
            return p
    return None


def update_proposal(data_dir: Path, pid: str, **fields) -> Optional[dict]:
    q = load_queue(data_dir)
    found = None
    for i, p in enumerate(q):
        if p.get("id") == pid:
            p.update(fields)
            q[i] = p
            found = p
            break
    if found is None:
        return None
    save_queue(data_dir, q)
    return found


def active_rules(data_dir: Path) -> list[dict]:
    return [p["rule"] | {"id": p["id"], "name": p["rule"].get("name") or p["id"]}
            for p in load_queue(data_dir) if p.get("status") == "active"]


def shadow_rules(data_dir: Path) -> list[dict]:
    return [p for p in load_queue(data_dir) if p.get("status") in ("shadow", "ready")]


# ----- walk-forward backtest on evaluation examples -----

def _pf_and_dd(pnls: list[float]) -> tuple[Optional[float], float]:
    gw = sum(x for x in pnls if x > 0)
    gl = -sum(x for x in pnls if x <= 0)
    pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else None)
    eq = pk = 0.0
    dd = 0.0
    for x in pnls:
        eq += x
        if eq > pk:
            pk = eq
        if eq - pk < dd:
            dd = eq - pk
    return pf, dd


def backtest_rule(rule: dict, examples: list[dict], *, champion_rules: Optional[list[dict]] = None,
                  stake: float = 10.0) -> dict[str, Any]:
    """Walk-forward on already-as-of CF examples (settle_ts ordered). Champion = current active rules or none."""
    cleaned = validate_rule(rule) if "when" in rule and "action" in rule else rule
    champ = champion_rules or []

    def run(extra: Optional[dict]) -> dict:
        pnls: list[float] = []
        wins = 0
        skipped = 0
        applied = 0
        taken = set()
        for e in sorted(examples, key=lambda x: (float(x.get("settle_ts") or 0), float(x.get("ts") or 0))):
            key = (e.get("market_slug"), e.get("side"))
            if key in taken:
                continue
            # Only evaluate sides that would have been eligible (passed rules) for the champion book,
            # but still allow the proposal to skip some of those.
            if not e.get("passed"):
                continue
            taken.add(key)
            feat = e.get("feat") or e
            rules = list(champ) + ([extra] if extra is not None else [])
            dec = apply_active_rules(rules, feat)
            if not dec.get("passed", True):
                skipped += 1
                if extra is not None and apply_rule(extra, feat).get("matched"):
                    applied += 1
                continue
            m = float(dec.get("mult") or 1.0)
            if extra is not None and apply_rule(extra, feat).get("matched"):
                applied += 1
            # examples are $1-stake CF; scale
            p = float(e.get("pnl_usd") or 0.0) * stake * m
            pnls.append(p)
            if p > 0:
                wins += 1
        pf, dd = _pf_and_dd(pnls)
        n = len(pnls)
        tot = sum(pnls)
        return {
            "trades": n, "wins": wins, "win_rate": (wins / n) if n else 0.0,
            "pnl": round(tot, 2), "avg": (tot / n) if n else 0.0,
            "pf": None if pf is None else (round(pf, 3) if math_isfinite(pf) else "inf"),
            "max_dd": round(dd, 2), "skipped": skipped, "applied": applied,
        }

    def math_isfinite(x):
        try:
            return x is not None and x != float("inf") and x == x
        except Exception:
            return False

    base = run(None)
    cand = run(cleaned)
    return {
        "champion": base,
        "candidate": cand,
        "delta_pnl": round(cand["pnl"] - base["pnl"], 2),
        "delta_dd": round(cand["max_dd"] - base["max_dd"], 2),
        "beats_pnl": cand["pnl"] > base["pnl"],
        "dd_not_worse": cand["max_dd"] >= base["max_dd"] - 1e-9,  # dd is negative; greater is better
        "stake": stake,
    }


def attach_backtest(data_dir: Path, pid: str, result: dict) -> Optional[dict]:
    rec = update_proposal(data_dir, pid, backtest=result, status="shadow" if result else "proposed")
    append_history(data_dir, {"event": "backtested", "id": pid, "delta_pnl": (result or {}).get("delta_pnl")})
    return rec


# ----- live shadow book + gate -----

def record_live_shadow(data_dir: Path, feat: dict, actual_pnl: float, actual_cost: float,
                       actual_passed: bool = True) -> None:
    """For every settled live trade, update each shadow/ready/active proposal's CF book."""
    doc = load_shadow(data_dir)
    q = load_queue(data_dir)
    changed = False
    for p in q:
        if p.get("status") not in ("shadow", "ready", "active"):
            continue
        pid = p["id"]
        book = doc.setdefault(pid, {"trades": 0, "pnls": [], "champ_pnls": [], "applied": 0})
        # Champion: current *other* active rules (exclude this one if it is active, so demote compares off).
        champ = [r for r in active_rules(data_dir) if r.get("id") != pid]
        ch_dec = apply_active_rules(champ, feat)
        rw_dec = apply_active_rules(champ + [p["rule"] | {"id": pid, "name": p["rule"].get("name") or pid}], feat)
        # Linear CF from the actual fill (actual is the live size, treated as 1.0× relative).
        def _cf(dec):
            if not dec.get("passed", True):
                return 0.0
            return float(actual_pnl) * float(dec.get("mult") or 1.0)

        book["trades"] = int(book.get("trades") or 0) + 1
        book["pnls"] = list(book.get("pnls") or []) + [_cf(rw_dec)]
        book["champ_pnls"] = list(book.get("champ_pnls") or []) + [_cf(ch_dec)]
        if rw_dec.get("matched"):
            book["applied"] = int(book.get("applied") or 0) + 1
        book["pnls"] = book["pnls"][-500:]
        book["champ_pnls"] = book["champ_pnls"][-500:]
        _, c_dd = _pf_and_dd(book["pnls"])
        _, h_dd = _pf_and_dd(book["champ_pnls"])
        book["pnl"] = round(sum(book["pnls"]), 2)
        book["champ_pnl"] = round(sum(book["champ_pnls"]), 2)
        book["max_dd"] = round(c_dd, 2)
        book["champ_dd"] = round(h_dd, 2)
        doc[pid] = book
        changed = True
        update_proposal(data_dir, pid, shadow={k: v for k, v in book.items() if k not in ("pnls", "champ_pnls")})
    if changed:
        save_shadow(data_dir, doc)


def evaluate_gate(book: dict, settings: Optional[dict] = None) -> dict:
    s = {**DEFAULT_GATE, **(settings or {})}
    trades = int(book.get("trades") or 0)
    checks = []
    checks.append({"rule": f"≥ {s['min_trades']} live shadow trades",
                   "passed": trades >= int(s["min_trades"]), "detail": f"{trades} trades"})
    delta = float(book.get("pnl") or 0) - float(book.get("champ_pnl") or 0)
    checks.append({"rule": "better net P&L than champion",
                   "passed": delta > float(s.get("min_margin_usd") or 0),
                   "detail": f"{book.get('pnl'):+.2f} vs {book.get('champ_pnl'):+.2f} (Δ {delta:+.2f})"})
    c_dd = float(book.get("max_dd") or 0)
    h_dd = float(book.get("champ_dd") or 0)
    # dd is ≤ 0; "not worse" means candidate dd is not more negative than champion * ratio - slack
    lim = h_dd * float(s.get("max_dd_ratio") or 1.0) - float(s.get("dd_slack_usd") or 1.0)
    # If champion dd is 0, lim = -slack. Candidate must be >= lim (e.g. -1).
    checks.append({"rule": "drawdown not worse than champion",
                   "passed": c_dd >= lim, "detail": f"{c_dd:.2f} ≥ {lim:.2f}"})
    return {"passed": all(c["passed"] for c in checks), "checks": checks, "delta_pnl": round(delta, 2)}


def tick_lifecycle(data_dir: Path, settings: Optional[dict] = None) -> list[dict]:
    """Promote ready auto-promotes, mark ready, auto-demote underperformers. Returns events."""
    s = {**DEFAULT_GATE, **(settings or {})}
    events = []
    doc = load_shadow(data_dir)
    for p in list(load_queue(data_dir)):
        pid = p["id"]
        book = doc.get(pid) or p.get("shadow") or {}
        if p.get("status") in ("shadow", "ready"):
            g = evaluate_gate(book, s)
            update_proposal(data_dir, pid, gate=g)
            if g["passed"] and p.get("status") != "ready":
                update_proposal(data_dir, pid, status="ready")
                append_history(data_dir, {"event": "ready", "id": pid, **{k: g[k] for k in ("passed", "delta_pnl")}})
                events.append({"id": pid, "event": "ready"})
            if g["passed"] and s.get("auto_promote"):
                update_proposal(data_dir, pid, status="active")
                append_history(data_dir, {"event": "auto_promoted", "id": pid})
                events.append({"id": pid, "event": "auto_promoted"})
        elif p.get("status") == "active" and s.get("auto_demote"):
            # Demote if enough live trades and the rule underperforms the shadow
            # champion by more than demote_margin_usd (default 20).
            min_t = int(s.get("demote_min_trades") or 20)
            margin = float(s.get("demote_margin_usd") or 0.0)
            if int(book.get("trades") or 0) >= min_t:
                g = evaluate_gate(book, s)
                update_proposal(data_dir, pid, gate=g)
                delta = float(book.get("pnl") or 0) - float(book.get("champ_pnl") or 0)
                if delta < -abs(margin):
                    update_proposal(data_dir, pid, status="demoted")
                    append_history(data_dir, {
                        "event": "auto_demoted", "id": pid, "delta_pnl": round(delta, 2),
                        "reason": f"underperformed champion by {-delta:.2f} over {book.get('trades')} trades (margin {margin:g})",
                    })
                    events.append({"id": pid, "event": "auto_demoted", "delta_pnl": round(delta, 2),
                                   "reason": "underperform"})
    return events


def set_status(data_dir: Path, pid: str, status: str, *, actor: str, note: str = "") -> Optional[dict]:
    allowed = {"proposed", "shadow", "ready", "active", "rejected", "demoted"}
    if status not in allowed:
        raise ValueError(f"status must be one of {sorted(allowed)}")
    rec = update_proposal(data_dir, pid, status=status, reviewed_by=actor, review_note=note[:400])
    if rec:
        append_history(data_dir, {"event": status, "id": pid, "actor": actor, "note": note[:200], "manual": True})
    return rec
