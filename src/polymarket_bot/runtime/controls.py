"""Operator controls: a versioned, validated overlay on the paper config.

The dashboard writes data/runtime/controls.json (atomic). The engine and
execution workers read it every loop and apply a new version when it changes,
publishing the version they applied so the UI can confirm the round trip.

Safety:
  * Only whitelisted keys, each with hard bounds. Unknown keys are rejected.
  * Nothing here can set mode, API keys, or anything that enables live trading;
    effective_config() re-checks mode == paper after the overlay.
  * Clearing a HALT is not a control. It stays on scripts/reset_halt.sh --yes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ..config import Config, deep_merge
from .bus import atomic_write_json, read_json

ET = ZoneInfo("America/New_York")
ALL_ASSETS = ("btc", "eth", "sol", "xrp")
ALL_HORIZONS = ("5m", "15m", "1h")
FLATTEN_CONFIRM = "FLATTEN"


@dataclass(frozen=True)
class Field:
    kind: str                 # bool | float | subset
    label: str
    lo: Optional[float] = None
    hi: Optional[float] = None
    choices: tuple = ()
    path: tuple = ()          # where it lands in Config.raw (None for filters)
    unit: str = ""


FIELDS: dict[str, Field] = {
    "entries_paused": Field("bool", "Pause new entries"),
    "min_edge_after_fees": Field("float", "Min edge after fees", 0.01, 0.30, path=("rules", "min_edge_after_fees"), unit="fraction"),
    "max_per_bet_usd": Field("float", "Max stake per bet ($)", 1.0, 100.0, path=("risk", "max_per_bet_usd"), unit="usd"),
    "kelly_fraction": Field("float", "Kelly fraction", 0.0, 0.5, path=("sizing", "kelly_fraction"), unit="fraction"),
    "no_trade_final_seconds": Field("float", "No-trade-late window (s)", 5.0, 900.0, path=("rules", "no_trade_final_seconds"), unit="seconds"),
    "daily_profit_target_pct": Field("float", "Daily profit target (paper)", 0.005, 0.25, path=("risk", "daily_profit_target_pct"), unit="fraction"),
    "daily_loss_limit_pct": Field("float", "Daily loss limit (paper)", 0.01, 0.20, path=("risk", "daily_loss_limit_pct"), unit="fraction"),
    "target_mode": Field("choice", "Profit target mode", choices=("profit_lock", "fixed"), path=("risk", "profit_lock", "mode")),
    "lock_ratio": Field("float", "Lock floor (% of peak gain)", 0.30, 0.95, path=("risk", "profit_lock", "lock_ratio"), unit="fraction"),
    "locked_size_mult": Field("float", "Size after target (x Kelly)", 0.10, 1.0, path=("risk", "profit_lock", "locked_size_mult"), unit="mult"),
    "adaptive_target": Field("bool", "Adaptive target (win rate/edge/vol)", path=("risk", "profit_lock", "adaptive")),
    "target_min_pct": Field("float", "Adaptive target min", 0.005, 0.25, path=("risk", "profit_lock", "target_min_pct"), unit="fraction"),
    "target_max_pct": Field("float", "Adaptive target max", 0.005, 0.25, path=("risk", "profit_lock", "target_max_pct"), unit="fraction"),
    "adaptive_lookback_days": Field("float", "Adaptive lookback (days)", 1, 30, path=("risk", "profit_lock", "lookback_days"), unit="days"),
    "window_mode": Field("choice", "Risk window mode", choices=("rolling_24h", "calendar_day"), path=("risk", "window_mode")),
    "max_breaches": Field("float", "Max floor breaches / session", 1, 10, path=("risk", "profit_lock", "max_breaches")),
    "cooldown_1_min": Field("float", "Cooldown after breach 1 (min)", 1, 1440, path=("risk", "profit_lock", "cooldown_1_min"), unit="minutes"),
    "cooldown_2_min": Field("float", "Cooldown after breach 2 (min)", 1, 1440, path=("risk", "profit_lock", "cooldown_2_min"), unit="minutes"),
    "cooldown_3_min": Field("float", "Cooldown after breach 3 (min)", 1, 1440, path=("risk", "profit_lock", "cooldown_3_min"), unit="minutes"),
    "rolling_guard_enabled": Field("bool", "Rolling 24h drawdown HALT", path=("risk", "rolling_guard", "enabled")),
    "rolling_guard_pct": Field("float", "Rolling-guard drawdown %", 0.02, 0.50, path=("risk", "rolling_guard", "drawdown_pct"), unit="fraction"),
    "rolling_guard_hours": Field("float", "Rolling-guard lookback (h)", 1, 168, path=("risk", "rolling_guard", "lookback_hours"), unit="hours"),
    "pattern_mode": Field("choice", "Pattern learning mode", choices=("shadow", "active", "off"), path=("patterns", "mode")),
    "pattern_min_samples": Field("float", "Pattern min samples", 5, 200, path=("patterns", "min_samples"), unit="trades"),
    "assets": Field("subset", "Enabled assets", choices=ALL_ASSETS),
    "horizons": Field("subset", "Enabled horizons", choices=ALL_HORIZONS),
}


class ControlError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def defaults_from(cfg: Config) -> dict[str, Any]:
    return {
        "entries_paused": False,
        "min_edge_after_fees": cfg.min_edge,
        "max_per_bet_usd": cfg.max_per_bet_usd,
        "kelly_fraction": cfg.kelly_fraction,
        "no_trade_final_seconds": cfg.no_trade_final_seconds,
        "daily_profit_target_pct": cfg.daily_profit_target_pct,
        "daily_loss_limit_pct": cfg.daily_loss_limit_pct,
        "target_mode": cfg.target_mode,
        "lock_ratio": cfg.lock_ratio,
        "locked_size_mult": cfg.locked_size_mult,
        "adaptive_target": cfg.adaptive_target,
        "target_min_pct": cfg.target_min_pct,
        "target_max_pct": cfg.target_max_pct,
        "adaptive_lookback_days": cfg.adaptive_lookback_days,
        "window_mode": cfg.window_mode,
        "max_breaches": cfg.max_breaches,
        "cooldown_1_min": cfg.cooldown_minutes[0],
        "cooldown_2_min": cfg.cooldown_minutes[1] if len(cfg.cooldown_minutes) > 1 else 120,
        "cooldown_3_min": cfg.cooldown_minutes[2] if len(cfg.cooldown_minutes) > 2 else 240,
        "rolling_guard_enabled": cfg.rolling_guard_enabled,
        "rolling_guard_pct": cfg.rolling_guard_pct,
        "rolling_guard_hours": cfg.rolling_guard_hours,
        "pattern_mode": cfg.pattern_mode,
        "pattern_min_samples": cfg.pattern_min_samples,
        "assets": list(cfg.assets),
        "horizons": list(cfg.horizons),
    }


def validate(changes: Any) -> dict[str, Any]:
    """Return cleaned values or raise ControlError. Never partially applies."""
    if not isinstance(changes, dict) or not changes:
        raise ControlError({"_": "expected a non-empty object of changes"})
    errors: dict[str, str] = {}
    clean: dict[str, Any] = {}
    for key, raw in changes.items():
        f = FIELDS.get(key)
        if f is None:
            errors[key] = "not a controllable setting (paper controls only)"
            continue
        if f.kind == "bool":
            if isinstance(raw, bool):
                clean[key] = raw
            else:
                errors[key] = "must be true or false"
        elif f.kind == "float":
            if isinstance(raw, bool):
                errors[key] = "must be a number"
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                errors[key] = "must be a number"
                continue
            if v != v or v in (float("inf"), float("-inf")):
                errors[key] = "must be finite"
            elif v < f.lo or v > f.hi:
                errors[key] = f"must be between {f.lo:g} and {f.hi:g}"
            else:
                clean[key] = round(v, 6)
        elif f.kind == "choice":
            if raw in f.choices:
                clean[key] = raw
            else:
                errors[key] = f"must be one of {list(f.choices)}"
        elif f.kind == "subset":
            if not isinstance(raw, (list, tuple)):
                errors[key] = "must be a list"
                continue
            vals = [str(x).lower() for x in raw]
            bad = [x for x in vals if x not in f.choices]
            if bad:
                errors[key] = f"unknown {bad}; allowed {list(f.choices)}"
            else:
                clean[key] = [c for c in f.choices if c in vals]
    if "adaptive_lookback_days" in clean:
        clean["adaptive_lookback_days"] = int(round(clean["adaptive_lookback_days"]))
    if "pattern_min_samples" in clean:
        clean["pattern_min_samples"] = int(round(clean["pattern_min_samples"]))
    if errors:
        raise ControlError(errors)
    return clean


def check_cross(values: dict) -> None:
    """Cross-field rules on the merged values (raise ControlError)."""
    lo, hi = values.get("target_min_pct"), values.get("target_max_pct")
    if lo is not None and hi is not None and lo > hi:
        raise ControlError({"target_min_pct": f"min {lo} must be <= max {hi}"})


def controls_path(runtime: Path) -> Path:
    return Path(runtime) / "controls.json"


def load(runtime: Path) -> dict[str, Any]:
    data = read_json(controls_path(runtime))
    if not isinstance(data, dict):
        return {"version": 0, "values": {}, "flatten_id": 0, "history": []}
    data.setdefault("version", 0)
    data.setdefault("values", {})
    data.setdefault("flatten_id", 0)
    data.setdefault("history", [])
    # Re-validate on read: a hand-edited file cannot smuggle in other keys.
    try:
        data["values"] = validate(data["values"]) if data["values"] else {}
    except ControlError:
        data["values"] = {}
        data["invalid_on_disk"] = True
    return data


def apply(runtime: Path, changes: dict, *, actor: str = "dashboard", note: str = "",
          flatten: bool = False, baseline: Optional[dict] = None) -> dict[str, Any]:
    clean = validate(changes) if changes else {}
    cur = load(runtime)
    before = {**(baseline or {}), **cur["values"]}
    values = {**cur["values"], **clean}
    check_cross({**(baseline or {}), **values})
    now = time.time()
    rec = {
        "version": int(cur["version"]) + 1,
        "ts_unix": now,
        "ts_et": datetime.fromtimestamp(now, ET).strftime("%Y-%m-%d %I:%M:%S %p %Z"),
        "actor": actor,
        "note": str(note or "")[:200],
        "changes": {k: {"from": before.get(k), "to": v} for k, v in clean.items() if before.get(k) != v},
        "flatten": bool(flatten),
    }
    out = {
        "version": rec["version"],
        "values": values,
        "flatten_id": rec["version"] if flatten else int(cur.get("flatten_id") or 0),
        "updated_ts": now,
        "updated_et": rec["ts_et"],
        "history": (list(cur.get("history") or []) + [rec])[-50:],
    }
    if flatten:
        out["values"]["entries_paused"] = True
    atomic_write_json(controls_path(runtime), out)
    return {"controls": out, "record": rec}


def effective_config(cfg: Config, controls: dict) -> Config:
    """Base config with the controls overlay. Always paper."""
    values = (controls or {}).get("values") or {}
    overlay: dict = {}
    for key, v in values.items():
        f = FIELDS.get(key)
        if f is None or not f.path:
            continue
        node = overlay
        for p in f.path[:-1]:
            node = node.setdefault(p, {})
        node[f.path[-1]] = v
    raw = deep_merge(cfg.raw, overlay) if overlay else dict(cfg.raw)
    raw["mode"] = "paper"
    eff = Config(raw=raw, root=cfg.root)
    if eff.mode != "paper":  # belt and braces
        raise RuntimeError("controls produced a non-paper config")
    return eff


def enabled(controls: dict, asset: str, horizon: str) -> bool:
    values = (controls or {}).get("values") or {}
    a = values.get("assets")
    h = values.get("horizons")
    if a is not None and asset.lower() not in a:
        return False
    if h is not None and horizon not in h:
        return False
    return True


def paused(controls: dict) -> bool:
    return bool(((controls or {}).get("values") or {}).get("entries_paused"))


def describe(changes: dict) -> str:
    parts = []
    for k, ch in (changes or {}).items():
        parts.append(f"{k} {json.dumps(ch.get('from'))} → {json.dumps(ch.get('to'))}")
    return ", ".join(parts) or "no value change"
