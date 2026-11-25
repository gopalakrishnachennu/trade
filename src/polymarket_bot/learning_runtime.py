"""Glue for Phase 6 learning layers used by engine, execution, and the dashboard.

Keeps workers thin. Nothing here can change mode, raise caps, clear a HALT, or
bypass the session loss limit / rolling guard.
"""

from __future__ import annotations

import csv
import json
import os
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
    strategy = (feat or {}).get("strategy")
    p_mode = cfg.pattern_mode_for(strategy) if hasattr(cfg, "pattern_mode_for") else _mode(cfg, "pattern_mode")
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
    if isinstance(pdec, dict) and "mode" not in pdec:
        pdec = {**pdec, "mode": p_mode}
    if isinstance(bdec, dict) and "mode" not in bdec:
        bdec = {**bdec, "mode": b_mode}
    if isinstance(rule_dec, dict) and "mode" not in rule_dec:
        rule_dec = {**rule_dec, "mode": r_mode}
    return {"pattern": pdec, "bandit": bdec, "rule": rule_dec, "composed": composed, "feat": feat,
            "pattern_mode": p_mode, "rules_mode": r_mode, "bandit_mode": b_mode}


def on_settlement(order, feat: dict, layer_dec: dict, cfg, store, pstore, bandit, data_dir: Path,
                  paths: Optional[dict] = None) -> dict[str, Any]:
    """Attribution, pattern update (caller may already do it), bandit + rules shadow books."""
    events: list[dict] = []
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
        events.extend(rl.tick_lifecycle(data_dir, {
            "min_trades": int(getattr(cfg, "rules_lab_min_trades", 30)),
            "auto_promote": bool(getattr(cfg, "rules_lab_auto_promote", False)),
            "auto_demote": bool(getattr(cfg, "rules_lab_auto_demote", True)),
            "demote_min_trades": int(getattr(cfg, "rules_lab_demote_min_trades", 20)),
            "demote_margin_usd": float(getattr(cfg, "rules_lab_demote_margin_usd", 20.0)),
        }) or [])
    # Pattern / combined-layer book vs the shadow champion (pattern off, same fill).
    events.extend(record_pattern_layer_fill(data_dir, cfg, feat, layer_dec, pnl, cost) or [])
    events.extend(maybe_demote_pattern(data_dir, cfg) or [])
    tagged = tagged or {}
    tagged["_demote_events"] = events
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


# ----- automatic safety demotion (active rules / pattern vs shadow champion) -----


def _layer_dir(data_dir: Path) -> Path:
    d = Path(data_dir) / "patterns"
    d.mkdir(parents=True, exist_ok=True)
    return d


def layer_book_path(data_dir: Path) -> Path:
    return _layer_dir(data_dir) / "layer_book.json"


def pending_skips_path(data_dir: Path) -> Path:
    return _layer_dir(data_dir) / "pending_skips.jsonl"


def load_layer_book(data_dir: Path) -> dict:
    p = layer_book_path(data_dir)
    try:
        raw = json.loads(p.read_text())
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {
        "pattern": {"trades": 0, "pnls": [], "champ_pnls": []},
        "rules": {"trades": 0, "pnls": [], "champ_pnls": []},
    }


def save_layer_book(data_dir: Path, doc: dict) -> None:
    p = layer_book_path(data_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, separators=(",", ":")))
    os.replace(tmp, p)


def _summarize_book(book: dict) -> dict:
    pnls = [float(x) for x in (book.get("pnls") or [])]
    champs = [float(x) for x in (book.get("champ_pnls") or [])]
    book["trades"] = len(pnls)
    book["pnl"] = round(sum(pnls), 2)
    book["champ_pnl"] = round(sum(champs), 2)
    book["delta_pnl"] = round(book["pnl"] - book["champ_pnl"], 2)
    return book


def record_pattern_layer_fill(data_dir: Path, cfg, feat: dict, layer_dec: dict,
                              actual_pnl: float, actual_cost: float) -> list[dict]:
    """On a live fill, credit the pattern layer vs a 1.0× (pattern-off) champion.

    Skip rules never change the P&L of a *taken* fill (they only drop trades);
    those avoided trades are recorded via note_skip / settle_pending_skips.
    """
    p_mode = cfg.pattern_mode_for((feat or {}).get("strategy")) if hasattr(cfg, "pattern_mode_for") else _mode(cfg, "pattern_mode")
    if p_mode == "off" or not bool(getattr(cfg, "pattern_auto_demote", True)):
        return []
    composed = (layer_dec or {}).get("composed") or {}
    pdec = (layer_dec or {}).get("pattern") or {}
    # If pattern was not active, champion == actual (no residual to track).
    if p_mode != "active":
        return []
    m = float(composed.get("mult") if composed.get("mult") is not None else (pdec.get("would_mult") or 1.0) or 1.0)
    if m <= 0:
        m = 1.0
    # Linear scale: the fill already includes fees at the applied size.
    champ = float(actual_pnl) / m
    doc = load_layer_book(data_dir)
    b = doc.setdefault("pattern", {"trades": 0, "pnls": [], "champ_pnls": []})
    b["pnls"] = list(b.get("pnls") or []) + [round(float(actual_pnl), 4)]
    b["champ_pnls"] = list(b.get("champ_pnls") or []) + [round(champ, 4)]
    b["pnls"] = b["pnls"][-500:]
    b["champ_pnls"] = b["champ_pnls"][-500:]
    _summarize_book(b)
    doc["pattern"] = b
    save_layer_book(data_dir, doc)
    return []


def skip_layer_hits(layer_dec: dict) -> tuple[bool, bool]:
    """Which active layer(s) actually caused a composed skip.

    A shadow/off layer must not be charged. String-matching rule names for the
    word "pattern" is not used — that previously tagged rule-only skips as
    pattern skips too.
    """
    pdec = (layer_dec or {}).get("pattern") or {}
    rdec = (layer_dec or {}).get("rule") or {}
    p_mode = str(pdec.get("mode") or (layer_dec or {}).get("pattern_mode") or "off")
    r_mode = str(rdec.get("mode") or (layer_dec or {}).get("rules_mode") or "off")
    hit_pattern = p_mode == "active" and not pdec.get("passed", True)
    hit_rule = r_mode == "active" and not rdec.get("passed", True)
    return hit_pattern, hit_rule


def note_skip(data_dir: Path, feat: dict, layer_dec: dict, *,
              window_end: Any = None, ask: Any = None, side: Any = None,
              slug: Any = None, strike: Any = None, asset: Any = None,
              contracts: float = 25.0, theta: float = 0.095) -> None:
    """Record a would-have-been trade that an active layer skipped, to settle later."""
    composed = (layer_dec or {}).get("composed") or {}
    if composed.get("passed", True):
        return
    hit_pattern, hit_rule = skip_layer_hits(layer_dec)
    if not hit_pattern and not hit_rule:
        return
    detail = str(composed.get("detail") or "")
    rec = {
        "ts": time.time(),
        "market_slug": slug or (feat or {}).get("market_slug"),
        "side": side or (feat or {}).get("side"),
        "asset": asset or (feat or {}).get("asset"),
        "ask": _f(ask if ask is not None else (feat or {}).get("ask")),
        "strike": _f(strike if strike is not None else (feat or {}).get("price_to_beat")),
        "window_end": window_end,
        "contracts": float(contracts or 25.0),
        "theta": float(theta or 0.095),
        "hit_pattern": bool(hit_pattern),
        "hit_rule": bool(hit_rule),
        "detail": detail[:200],
        "strategy": (feat or {}).get("strategy"),
    }
    p = pending_skips_path(data_dir)
    with p.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def settle_pending_skips(data_dir: Path, cfg, *, now: float, markets: dict, prices: dict) -> list[dict]:
    """Settle skipped would-trades whose window has ended; credit champion with avoided P&L."""
    p = pending_skips_path(data_dir)
    if not p.exists():
        return []
    try:
        lines = p.read_text().splitlines()
    except Exception:
        return []
    from .executor import settlement_average, round_settlement
    from .fees import taker_fee_usd
    still, settled = [], []
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        slug = rec.get("market_slug")
        we = rec.get("window_end")
        end_ts = None
        if we is not None:
            try:
                end_ts = float(we) if isinstance(we, (int, float)) else None
            except (TypeError, ValueError):
                end_ts = None
            if end_ts is None:
                try:
                    end_ts = datetime.fromisoformat(str(we).replace("Z", "+00:00")).timestamp()
                except Exception:
                    end_ts = None
        market = (markets or {}).get(slug)
        if end_ts is None and market is not None and getattr(market, "window_end", None) is not None:
            try:
                end_ts = market.window_end.timestamp()
            except Exception:
                end_ts = None
        if end_ts is None or now < end_ts + 1:
            still.append(rec)
            continue
        asset = rec.get("asset") or (getattr(market, "asset", None) if market else None)
        pts = (prices or {}).get(asset) or []
        avg = settlement_average(pts, int(end_ts)) if pts else None
        if avg is None:
            still.append(rec)
            continue
        decimals = getattr(market, "price_decimals", 2) if market else 2
        settle_px = round_settlement(avg, decimals)
        strike = rec.get("strike")
        if strike is None and market is not None:
            strike = getattr(market, "price_to_beat", None)
        strike = _f(strike)
        ask = _f(rec.get("ask"))
        side = rec.get("side")
        if strike is None or ask is None or ask <= 0 or not side:
            continue
        went_up = settle_px >= strike
        won = (side == "up" and went_up) or (side == "down" and not went_up)
        contracts = float(rec.get("contracts") or 25.0)
        theta = float(rec.get("theta") or getattr(cfg, "fee_theta", 0.095) or 0.095)
        fee = taker_fee_usd(theta, contracts, ask)
        cost = contracts * ask + fee
        payout = contracts * (1.0 if won else 0.0)
        champ_pnl = payout - cost
        # candidate skipped: 0
        settled.append((rec, 0.0, champ_pnl))
    # rewrite remaining
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, default=str) + "\n" for r in still))
    os.replace(tmp, p)
    if not settled:
        return []
    doc = load_layer_book(data_dir)
    for rec, cand, champ in settled:
        for key, hit in (("pattern", rec.get("hit_pattern")), ("rules", rec.get("hit_rule"))):
            if not hit:
                continue
            b = doc.setdefault(key, {"trades": 0, "pnls": [], "champ_pnls": []})
            b["pnls"] = list(b.get("pnls") or []) + [round(cand, 4)]
            b["champ_pnls"] = list(b.get("champ_pnls") or []) + [round(champ, 4)]
            b["pnls"] = b["pnls"][-500:]
            b["champ_pnls"] = b["champ_pnls"][-500:]
            _summarize_book(b)
            doc[key] = b
    save_layer_book(data_dir, doc)
    events = maybe_demote_pattern(data_dir, cfg)
    events.extend(maybe_demote_rules_layer(data_dir, cfg) or [])
    return events


def maybe_demote_pattern(data_dir: Path, cfg) -> list[dict]:
    if not bool(getattr(cfg, "pattern_auto_demote", True)):
        return []
    doc = load_layer_book(data_dir)
    book = doc.get("pattern") or {}
    _summarize_book(book)
    trades = int(book.get("trades") or 0)
    min_t = int(getattr(cfg, "pattern_demote_min_trades", 20))
    margin = float(getattr(cfg, "pattern_demote_margin_usd", 20.0))
    delta = float(book.get("delta_pnl") or 0.0)
    if trades < min_t or delta >= -abs(margin):
        return []
    # Revert every per-strategy override and the global mode to shadow via Controls.
    reason = (f"pattern layer underperformed shadow champion by {-delta:.2f} "
              f"over {trades} trades (margin {margin:g}); reverted to shadow")
    _persist_pattern_shadow(data_dir, cfg, reason)
    append = Path(data_dir) / "patterns" / "demote_history.jsonl"
    with append.open("a") as f:
        f.write(json.dumps({"ts": time.time(), "event": "auto_demoted_pattern", "delta_pnl": delta,
                            "trades": trades, "reason": reason}) + "\n")
    return [{"event": "auto_demoted_pattern", "delta_pnl": delta, "trades": trades,
             "reason": reason, "message": reason}]


def maybe_demote_rules_layer(data_dir: Path, cfg) -> list[dict]:
    """Demote *all* active rules if the combined skip layer underperforms the champion."""
    if not bool(getattr(cfg, "rules_lab_auto_demote", True)):
        return []
    doc = load_layer_book(data_dir)
    book = doc.get("rules") or {}
    _summarize_book(book)
    trades = int(book.get("trades") or 0)
    min_t = int(getattr(cfg, "rules_lab_demote_min_trades", 20))
    margin = float(getattr(cfg, "rules_lab_demote_margin_usd", 20.0))
    delta = float(book.get("delta_pnl") or 0.0)
    if trades < min_t or delta >= -abs(margin):
        return []
    events = []
    for p in rl.active_rules(data_dir):
        pid = p.get("id") or p.get("name")
        if not pid:
            continue
        rec = rl.set_status(data_dir, pid, "demoted", actor="auto_demote",
                            note=f"layer underperformed champion by {-delta:.2f} over {trades} trades")
        if rec:
            events.append({"event": "auto_demoted", "id": pid, "delta_pnl": delta,
                           "reason": "layer_underperform",
                           "message": f"rule {pid} auto-demoted: layer Δ {delta:+.2f} over {trades} trades"})
    _persist_rules_shadow(data_dir, cfg, f"rules layer Δ {delta:+.2f} over {trades} trades")
    return events


def _persist_pattern_shadow(data_dir: Path, cfg, reason: str) -> None:
    try:
        from .runtime.controls import apply as controls_apply
        runtime = Path(data_dir) / "runtime"
        changes = {
            "pattern_mode": "shadow",
            "pattern_mode_baseline": "shadow",
            "pattern_mode_high_conviction": "shadow",
            "pattern_mode_late_window": "shadow",
            "pattern_mode_favorite": "shadow",
        }
        controls_apply(runtime, changes, actor="auto_demote", note=reason[:200])
    except Exception:
        # Fall back to a sidecar so the next process can still see the intent.
        p = _layer_dir(data_dir) / "force_pattern_shadow.json"
        p.write_text(json.dumps({"force": True, "reason": reason, "ts": time.time()}))


def _persist_rules_shadow(data_dir: Path, cfg, reason: str) -> None:
    try:
        from .runtime.controls import apply as controls_apply
        runtime = Path(data_dir) / "runtime"
        controls_apply(runtime, {"rules_lab_mode": "shadow"}, actor="auto_demote", note=reason[:200])
    except Exception:
        pass


def on_floor_breach(order, feat: dict, layer_dec: dict, cfg, data_dir: Path) -> list[dict]:
    """If a floor breach was made worse by an active layer, revert that layer to shadow."""
    events: list[dict] = []
    actual = float(getattr(order, "pnl_usd", 0) or 0)
    if not bool(getattr(cfg, "rules_lab_demote_on_floor_breach", True)):
        return events
    composed = (layer_dec or {}).get("composed") or {}
    pdec = (layer_dec or {}).get("pattern") or {}
    strategy = (feat or {}).get("strategy")
    p_mode = cfg.pattern_mode_for(strategy) if hasattr(cfg, "pattern_mode_for") else _mode(cfg, "pattern_mode")
    m = float(composed.get("mult") if composed.get("mult") is not None else (pdec.get("would_mult") or 1.0) or 1.0)
    if p_mode == "active" and m > 1.0 + 1e-9 and actual < 0:
        champ = actual / m
        extra = champ - actual  # positive = extra loss from the boost
        if extra > 1e-9:
            reason = (f"floor breach: pattern sized this loser {m:g}×, extra loss ${extra:.2f}; "
                      "reverted pattern to shadow")
            _persist_pattern_shadow(data_dir, cfg, reason)
            events.append({"event": "auto_demoted_pattern", "reason": "floor_breach",
                           "message": reason, "delta_pnl": round(-extra, 2)})
            with (_layer_dir(data_dir) / "demote_history.jsonl").open("a") as f:
                f.write(json.dumps({"ts": time.time(), "event": "auto_demoted_pattern",
                                    "reason": "floor_breach", "extra_loss": extra}) + "\n")
    # Skip rules cannot cause a taken-trade floor breach (they only drop entries).
    return events
