"""Turn rule checks + portfolio gates into a one-line verdict reason and a checklist.

Pure functions over the engine's signal dicts so the engine, dashboard and
tests all agree on why a row says NO TRADE.
"""

from __future__ import annotations

import math
from typing import Any, Optional

RULE_LABELS = {
    "price_to_beat_known": "Strike stamped",
    "window_timing": "Timing window",
    "max_spread": "Spread ≤ max",
    "min_liquidity": "Liquidity ≥ min",
    "ask_bounds": "Ask inside band",
    "min_edge_after_fees": "Edge ≥ min",
    "strategy": "Active strategy filter",
    "pattern": "Pattern context",
}


def _num(x) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _pct(x, digits=1) -> str:
    v = _num(x)
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def _cents(x) -> str:
    v = _num(x)
    return "—" if v is None else f"{v * 100:.1f}¢"


def describe_check(chk: dict) -> dict:
    """Human value/threshold strings for one rule check."""
    name = chk.get("name")
    passed = bool(chk.get("passed"))
    out = {"name": name, "label": RULE_LABELS.get(name, name), "passed": passed,
           "value": "—", "threshold": "—", "reason": None, "code": name}
    if name == "price_to_beat_known":
        v = _num(chk.get("value"))
        out["value"] = "—" if v is None else f"${v:,.4f}".rstrip("0").rstrip(".")
        out["threshold"] = "required"
        out["reason"] = None if passed else "strike not stamped yet"
    elif name == "window_timing":
        left = _num(chk.get("seconds_left"))
        into = _num(chk.get("seconds_into"))
        cut = _num(chk.get("final_cutoff"))
        mn = _num(chk.get("min_into"))
        out["value"] = f"{_dur(left)} left · {_dur(into)} in"
        out["threshold"] = f"> {_dur(cut)} left, ≥ {_dur(mn)} in"
        if not passed:
            if left is not None and left <= 0:
                out["reason"], out["code"] = "window ended", "window_ended"
            elif left is not None and cut is not None and left < cut:
                out["reason"], out["code"] = f"too late: {_dur(left)} left < {_dur(cut)} cutoff", "too_late"
            elif into is not None and mn is not None and into < mn:
                out["reason"], out["code"] = f"too early: {_dur(into)} into window < {_dur(mn)}", "too_early"
            else:
                out["reason"] = chk.get("detail") or "timing"
    elif name == "max_spread":
        v, th = _num(chk.get("value")), _num(chk.get("threshold"))
        out["value"], out["threshold"] = _cents(v), f"≤ {_cents(th)}"
        if not passed:
            out["reason"] = "spread unknown (no book)" if v is None else f"spread too wide: {_cents(v)} > {_cents(th)}"
    elif name == "min_liquidity":
        v, th = _num(chk.get("value")), _num(chk.get("threshold"))
        vu, thu = _num(chk.get("value_usd")), _num(chk.get("threshold_usd"))
        out["value"] = f"{0 if v is None else v:,.0f} ct / ${0 if vu is None else vu:,.0f}"
        out["threshold"] = f"≥ {0 if th is None else th:,.0f} ct / ${0 if thu is None else thu:,.0f}"
        if not passed:
            if not v:
                out["reason"], out["code"] = "no liquidity at the ask", "no_liquidity"
            else:
                out["reason"] = f"thin book: {v:,.0f} ct (${vu or 0:,.0f}) < {th:,.0f} ct (${thu or 0:,.0f})"
    elif name == "ask_bounds":
        v, lo, hi = _num(chk.get("value")), _num(chk.get("lo")), _num(chk.get("hi"))
        out["value"], out["threshold"] = _cents(v), f"{_cents(lo)}–{_cents(hi)}"
        if not passed:
            out["reason"] = f"ask {_cents(v)} outside {_cents(lo)}–{_cents(hi)}"
    elif name == "min_edge_after_fees":
        v, th = _num(chk.get("value")), _num(chk.get("threshold"))
        out["value"], out["threshold"] = _pct(v), f"≥ {_pct(th).lstrip('+')}"
        if not passed:
            out["reason"] = f"edge {_pct(v)} < min {_pct(th).lstrip('+')}"
    elif name == "pattern":
        out["value"] = str(chk.get("value") or "—")
        out["threshold"] = str(chk.get("threshold") or "—")
        out["reason"] = None if passed else (chk.get("detail") or "pattern EV skip")
        out["code"] = "pattern_skip" if not passed else "pattern"
    else:
        out["reason"] = None if passed else (chk.get("detail") or name)
    return out


def _dur(s) -> str:
    v = _num(s)
    if v is None:
        return "—"
    v = max(0.0, v)
    if v >= 60:
        m, sec = divmod(int(round(v)), 60)
        return f"{m}m{sec:02d}s"
    return f"{v:.0f}s"


def side_view(sig: Optional[dict]) -> dict:
    """Summary of one side: checks described, first failing reason."""
    if not sig:
        return {"available": False, "reason": "no evaluation", "code": "no_eval", "checks": [], "passed": False}
    checks = [describe_check(c) for c in (sig.get("rule_checks") or [])]
    if not checks:
        rs = sig.get("reasons") or []
        txt = "; ".join(rs) or "not evaluated"
        code = "no_quote" if any("ask missing" in r for r in rs) else ("no_model" if any("model" in r for r in rs) else "no_eval")
        nice = {"no_quote": "no quote yet (book not published)", "no_model": "model unavailable (no index history/strike)"}.get(code, txt)
        return {"available": False, "reason": nice, "code": code, "checks": [], "passed": False,
                "edge": sig.get("edge_after_fees"), "ask": sig.get("ask"), "model_prob": sig.get("model_prob")}
    fails = [c for c in checks if not c["passed"]]
    return {
        "available": True,
        "passed": not fails,
        "reason": fails[0]["reason"] if fails else None,
        "code": fails[0]["code"] if fails else "pass",
        "more_fails": max(0, len(fails) - 1),
        "checks": checks,
        "edge": sig.get("edge_after_fees"),
        "ask": sig.get("ask"),
        "model_prob": sig.get("model_prob"),
    }


def decide(row: dict, up: Optional[dict], down: Optional[dict], *, halted: bool = False,
           paused: bool = False, enabled: bool = True, risk_halt: Optional[str] = None,
           holding: bool = False, at_max_positions: bool = False, max_positions: int = 0,
           stake_up: Optional[float] = None, stake_down: Optional[float] = None) -> dict[str, Any]:
    """Return {verdict, side, reason, code, stake, gates, up, down}."""
    uv, dv = side_view(up), side_view(down)
    gates = [
        {"label": "System not halted", "passed": not halted, "value": "HALTED" if halted else "ok"},
        {"label": "Entries not paused", "passed": not paused, "value": "paused" if paused else "ok"},
        {"label": "Asset/horizon enabled", "passed": enabled, "value": "on" if enabled else "off"},
        {"label": "Daily target/limit", "passed": not risk_halt, "value": risk_halt or "ok"},
        {"label": "No open position here", "passed": not holding, "value": "holding" if holding else "ok"},
        {"label": f"Open positions < {max_positions}" if max_positions else "Position cap",
         "passed": not at_max_positions, "value": "at cap" if at_max_positions else "ok"},
    ]

    def edge_of(v):
        e = _num(v.get("edge"))
        return -9.0 if e is None else e

    best_side = "up" if edge_of(uv) >= edge_of(dv) else "down"
    best = uv if best_side == "up" else dv
    stake = stake_up if best_side == "up" else stake_down
    passing = [s for s, v in (("up", uv), ("down", dv)) if v.get("passed")]
    if passing:
        best_side = max(passing, key=lambda s: edge_of(uv if s == "up" else dv))
        best = uv if best_side == "up" else dv
        stake = stake_up if best_side == "up" else stake_down

    def out(verdict, reason, code):
        return {"verdict": verdict, "side": best_side, "reason": reason, "code": code,
                "stake": None if stake is None else round(stake, 2),
                "gates": gates, "up": uv, "down": dv}

    if not row.get("slug"):
        return out("WAIT", "no live window (awaiting next)", "no_window")
    if halted:
        return out("NO TRADE", "system HALTED: entries blocked until manual reset", "halted")
    if not enabled:
        return out("OFF", "disabled by operator", "disabled")
    if not uv["available"] and not dv["available"]:
        return out("WAIT", best["reason"], best["code"])
    if not passing:
        extra = f" (+{best['more_fails']} more)" if best.get("more_fails") else ""
        return out("NO TRADE", f"{best_side.upper()}: {best['reason']}{extra}", best["code"])
    # Rules pass on best_side; portfolio gates decide.
    if paused:
        return out("NO TRADE", f"{best_side.upper()} passes rules, but entries paused by operator", "paused")
    if risk_halt:
        low = risk_halt.lower()
        if "cooldown" in low or "session stop" in low:
            lab = risk_halt  # already "cooldown after floor breach 1/3, resumes 4:15 AM ET"
        elif "target" in low:
            lab = "daily target hit"
        elif "loss" in low:
            lab = "session loss limit hit"
        elif "floor" in low:
            lab = risk_halt
        else:
            lab = risk_halt
        suffix = "" if lab == risk_halt and ("cooldown" in low or "session stop" in low) else ": entries paused"
        return out("NO TRADE", f"{best_side.upper()} passes rules, but {lab}{suffix}", "risk_halt")
    if holding:
        return out("HOLD", "already holding this market", "holding")
    if at_max_positions:
        return out("NO TRADE", f"{best_side.upper()} passes rules, but at max {max_positions} open positions", "max_positions")
    if stake is not None and stake <= 0:
        return out("NO TRADE", f"{best_side.upper()} passes rules, but Kelly stake rounds to $0", "zero_stake")
    return out(f"BUY {best_side.upper()}", f"edge {_pct(best.get('edge'))} passes all rules · stake ${stake or 0:,.2f}", "trade")
