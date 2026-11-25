"""Glue for Phase 6 learning layers used by engine, execution, and the dashboard.

Keeps workers thin. Nothing here can change mode, raise caps, clear a HALT, or
bypass the session loss limit / rolling guard.
"""

from __future__ import annotations

import csv
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from . import attribution as attr
from . import bandit as ban
from . import calibration as cal
from . import patterns as pat
from . import rules_lab as rl
from .bandit import compose_learning
from .patterns import _f

ET = ZoneInfo("America/New_York")


def _mode(cfg, name, default="shadow") -> str:
    return str(getattr(cfg, name, default) or default)


def load_eval_rows(path: Path) -> list[dict]:
    if not path or not Path(path).exists():
        return []
    with Path(path).open(newline="") as f:
        return list(csv.DictReader(f))


def decisions_for(feat: dict, cfg, pstore: pat.PatternStore, bandit: ban.ContextBandit,
                  data_dir: Path, *, record_shadow: bool = False) -> dict[str, Any]:
    """Evaluate pattern, bandit, and active/shadow rules; compose a safe multiplier."""
    p_mode = _mode(cfg, "pattern_mode")
    b_mode = _mode(cfg, "bandit_mode")
    r_mode = _mode(cfg, "rules_lab_mode")
    cf_mode = _mode(cfg, "counterfactual_mode")
    cf_w = float(getattr(cfg, "cf_weight", 0.25) or 0.0) if cf_mode == "active" else 0.0

    pdec = pstore.evaluate(
        feat,
        mode=p_mode,
        min_samples=int(getattr(cfg, "pattern_min_samples", 20)),
        min_mult=float(getattr(cfg, "pattern_min_mult", 0.25)),
        max_mult=float(getattr(cfg, "pattern_max_mult", 1.50)),
        skip_ev_threshold=float(getattr(cfg, "pattern_skip_ev_threshold", -0.02)),
        record_shadow=record_shadow and p_mode == "shadow",
        cf_weight=cf_w,
    )
    bdec = bandit.decide(
        feat, pstore,
        mode=b_mode,
        record_shadow=record_shadow and b_mode == "shadow",
    )
    # Live composition uses only *active* rules. Shadow proposals are scored separately on settle.
    rule_dec = rl.apply_active_rules(rl.active_rules(data_dir), feat) if r_mode != "off" else {
        "passed": True, "mult": 1.0, "detail": None, "matched": 0,
    }
    composed = compose_learning(
        pdec, bdec, rule_dec,
        pattern_mode=p_mode, bandit_mode=b_mode, rules_mode=r_mode,
        min_mult=float(getattr(cfg, "pattern_min_mult", 0.25)),
        max_mult=float(getattr(cfg, "pattern_max_mult", 1.50)),
    )
    return {"pattern": pdec, "bandit": bdec, "rule": rule_dec, "composed": composed, "feat": feat}


def on_settlement(order, feat: dict, layer_dec: dict, cfg, store, pstore, bandit, data_dir: Path,
                  paths: Optional[dict] = None) -> dict[str, Any]:
    """Attribution, pattern update (caller may already do it), bandit + rules shadow books."""
    pnl = float(order.pnl_usd or 0.0)
    cost = float(order.cost_usd or 0.0)
    outcome = order.result or ("win" if pnl > 0 else "loss")
    tagged = {}
    if _mode(cfg, "attribution_mode") != "off":
        path = (paths or {}).get(getattr(order, "market_slug", "") or feat.get("market_slug") or "")
        tagged = attr.tag_trade(
            feat, outcome=outcome, pnl_usd=pnl, cost_usd=cost,
            fee_usd=getattr(order, "fee_usd", None),
            settlement_price=getattr(order, "settlement_price", None),
            price_to_beat=getattr(order, "price_to_beat", None) or feat.get("price_to_beat"),
            path=path,
            theta=float(getattr(cfg, "fee_theta", 0.095)),
        )
        store.write_attribution({
            "order_id": order.id, **tagged,
            "primary_reason": tagged.get("primary"),
            "updated_ts": datetime.now(ET).isoformat(timespec="seconds"),
        })
    bdec = (layer_dec or {}).get("bandit") or {}
    would = bdec.get("would_action") or "1.0"
    if _mode(cfg, "bandit_mode") != "off":
        # Shadow book: would-have P&L. Active: update the arm that was applied if mode active,
        # otherwise the would-have arm (so shadow still learns).
        arm = would if _mode(cfg, "bandit_mode") != "active" else (
            "skip" if not (layer_dec or {}).get("composed", {}).get("passed", True)
            else (would if would in ban.ACTION_MULT else "1.0")
        )
        # For a live fill the actual size is already after caps; record actual pnl on the would/applied arm
        # scaled to unit if it was skip (0).
        bandit.update(feat, action=arm, order_id=f"live:{order.id}", pnl_usd=pnl, cost_usd=cost, outcome=outcome)
        if _mode(cfg, "bandit_mode") == "shadow":
            bandit.record_shadow_settlement(would, pnl, cost)
        ban.save_bandit(data_dir, bandit)
    if _mode(cfg, "rules_lab_mode") != "off":
        rl.record_live_shadow(data_dir, feat, pnl, cost)
        rl.tick_lifecycle(data_dir, {
            "min_trades": int(getattr(cfg, "rules_lab_min_trades", 30)),
            "auto_promote": bool(getattr(cfg, "rules_lab_auto_promote", False)),
            "auto_demote": bool(getattr(cfg, "rules_lab_auto_demote", True)),
        })
    return tagged


def backfill_attribution(store, eval_rows: list[dict], cfg) -> int:
    if _mode(cfg, "attribution_mode") == "off":
        return 0
    paths = attr.build_index_paths(eval_rows)
    patterns = store.all_trade_patterns()
    existing = store.all_attributions()
    n = 0
    for order in store.all_orders():
        if order.status != "settled" or order.pnl_usd is None:
            continue
        if order.id in existing:
            continue
        feat = attr.features_from_order(order, patterns.get(order.id), tz_name=getattr(cfg, "timezone", "America/New_York"), cfg=cfg)
        tagged = attr.tag_trade(
            feat, outcome=order.result or ("win" if order.pnl_usd > 0 else "loss"),
            pnl_usd=order.pnl_usd, cost_usd=order.cost_usd or 0.0, fee_usd=order.fee_usd,
            settlement_price=order.settlement_price, price_to_beat=order.price_to_beat,
            path=paths.get(order.market_slug), theta=float(getattr(cfg, "fee_theta", 0.095)),
        )
        store.write_attribution({
            "order_id": order.id, **tagged,
            "primary_reason": tagged.get("primary"),
            "updated_ts": datetime.now(ET).isoformat(timespec="seconds"),
        })
        n += 1
    return n


def ingest_counterfactuals(pstore: pat.PatternStore, eval_rows: list[dict], cfg, *,
                           now: Optional[float] = None, force: bool = False) -> int:
    mode = _mode(cfg, "counterfactual_mode")
    if mode == "off":
        return 0
    now = time.time() if now is None else float(now)
    outcome, settle_ts = cal.index_outcomes(eval_rows, tz_name=getattr(cfg, "timezone", "America/New_York"))
    examples = cal.iter_counterfactuals(
        eval_rows, outcome, settle_ts,
        tz_name=getattr(cfg, "timezone", "America/New_York"),
        theta=float(getattr(cfg, "fee_theta", 0.095)),
        stake=1.0, include_no_trade=True,
    )
    weight = float(getattr(cfg, "cf_weight", 0.25) or 0.25)
    added = 0
    cursor = 0.0 if force else float(pstore.cf_cursor_ts or 0.0)
    for e in examples:
        if float(e["settle_ts"]) > now + 1e-9:
            continue
        if float(e["settle_ts"]) < cursor - 1e-6:
            continue
        if pstore.update_counterfactual(
            e["feat"], source_id=e["id"], pnl_usd=e["pnl_usd"], cost_usd=e["cost_usd"],
            outcome="win" if e["won"] else "loss", weight=weight,
        ):
            added += 1
            if e["settle_ts"] > pstore.cf_cursor_ts:
                pstore.cf_cursor_ts = e["settle_ts"]
    return added


def refresh_calibration(eval_rows: list[dict], cfg, data_dir: Path, *, now: Optional[float] = None) -> dict:
    now = time.time() if now is None else float(now)
    outcome, settle_ts = cal.index_outcomes(eval_rows, tz_name=getattr(cfg, "timezone", "America/New_York"))
    examples = cal.asof(cal.iter_counterfactuals(
        eval_rows, outcome, settle_ts,
        tz_name=getattr(cfg, "timezone", "America/New_York"),
        theta=float(getattr(cfg, "fee_theta", 0.095)),
    ), now)
    # Dedup to last eval per slug+side so we don't overweight markets with many snapshots
    latest: dict[tuple, dict] = {}
    for e in examples:
        latest[(e["market_slug"], e["side"])] = e
    unique = list(latest.values())
    doc = cal.report(unique)
    method = str(getattr(cfg, "calibration_method", "isotonic") or "isotonic")
    calib = cal.Calibrator(method=method).fit(unique)
    doc["calibrator"] = calib.to_dict()
    doc["method"] = method
    doc["mode"] = _mode(cfg, "calibration_mode")
    doc["built_at"] = now
    doc["unique_sides"] = len(unique)
    cal.save_report(data_dir, doc)
    return doc


def tighten_with_calibration(sig, cfg, calib_doc: Optional[dict]) -> dict:
    """Shadow-safe calibration for a live signal.

    Active mode may only *tighten*: sized_p = min(raw_p, calibrated_p), which
    can only shrink Kelly and can only make edge_after_fees smaller. A raw
    min_edge fail can never be rescued by a higher calibrated p.
    Shadow/off leave the signal unchanged and just report what would happen.
    """
    from .fees import edge_after_fees as _edge
    raw_p = getattr(sig, "model_prob", None) if not isinstance(sig, dict) else sig.get("model_prob")
    ask = getattr(sig, "ask", None) if not isinstance(sig, dict) else sig.get("ask")
    asset = (getattr(sig, "asset", None) if not isinstance(sig, dict) else sig.get("asset")) or ""
    horizon = (getattr(sig, "horizon", None) if not isinstance(sig, dict) else sig.get("horizon")) or ""
    theta = float(getattr(cfg, "fee_theta", 0.095))
    mode = _mode(cfg, "calibration_mode")
    raw_edge = None
    try:
        if raw_p is not None and ask:
            raw_edge = _edge(float(raw_p), float(ask), theta)
    except Exception:
        raw_edge = None
    cal_p = None
    if mode != "off" and calib_doc and raw_p is not None:
        try:
            c = cal.Calibrator.from_dict(calib_doc.get("calibrator") or calib_doc)
            cal_p = c.apply(raw_p, asset, horizon)
        except Exception:
            cal_p = None
    sized_p = raw_p
    applied = False
    fail_edge = False
    cal_edge = raw_edge
    if mode == "active" and cal_p is not None and raw_p is not None:
        sized_p = min(float(raw_p), float(cal_p))
        try:
            cal_edge = _edge(float(sized_p), float(ask), theta) if ask else raw_edge
        except Exception:
            cal_edge = raw_edge
        applied = sized_p < float(raw_p) - 1e-12 or (cal_edge is not None and raw_edge is not None and cal_edge < raw_edge - 1e-12)
        min_edge = float(getattr(cfg, "min_edge", 0.04))
        if cal_edge is not None and cal_edge < min_edge:
            fail_edge = True
    detail = (
        f"calibration ({mode}): raw p={None if raw_p is None else f'{float(raw_p):.3f}'}"
        f" → cal {None if cal_p is None else f'{float(cal_p):.3f}'}"
        f" sized={None if sized_p is None else f'{float(sized_p):.3f}'}"
        + (" tightens" if applied else " no change")
    )
    return {
        "mode": mode, "raw_p": raw_p, "cal_p": cal_p, "sized_p": sized_p,
        "raw_edge": raw_edge, "cal_edge": cal_edge, "applied": applied,
        "fail_edge": fail_edge, "detail": detail, "passed": not fail_edge,
    }


def apply_calibrated_prob(raw_p: Optional[float], asset: str, horizon: str, cfg, calib_doc: Optional[dict]) -> Optional[float]:
    """Return calibrated p when mode is active; otherwise the raw p. Never used to loosen min_edge."""
    if raw_p is None or _mode(cfg, "calibration_mode") != "active" or not calib_doc:
        return raw_p
    c = cal.Calibrator.from_dict(calib_doc.get("calibrator") or calib_doc)
    return c.apply(raw_p, asset, horizon)


def startup(cfg, store, pstore, bandit, data_dir: Path) -> dict[str, int]:
    rows = load_eval_rows(getattr(cfg, "evaluations_csv", None))
    # Also merge archive evals if present next to data/
    data_root = Path(data_dir)
    arch = data_root / "archive"
    if arch.exists():
        import glob
        extra = []
        for p in sorted(glob.glob(str(arch / "*/evaluations.csv"))):
            extra.extend(load_eval_rows(Path(p)))
        if extra:
            rows = extra + rows
    n_attr = backfill_attribution(store, rows, cfg)
    n_cf = ingest_counterfactuals(pstore, rows, cfg)
    if n_cf:
        pat.save_pattern_store(data_dir, pstore)
    if _mode(cfg, "calibration_mode") != "off":
        refresh_calibration(rows, cfg, data_dir)
    return {"attribution": n_attr, "counterfactuals": n_cf, "eval_rows": len(rows)}
