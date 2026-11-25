"""Contextual Thompson-sampling bandit on top of the existing PatternStore.

This does not replace PatternStore.evaluate(). It is a parallel layer with its
own off/shadow/active mode. Contexts are the existing feature buckets
(asset, horizon, hour, vol, distance-σ, edge, minutes-left) with hierarchical
shrinkage toward the asset|horizon and global Beta/EV posteriors.

Actions: skip / 0.5× / 1× / 1.5× Kelly. The objective is fee-aware net P&L
per dollar staked (ROI). Updates happen online after every settlement (and
optionally from weighted counterfactual examples). Hard caps are applied by
the caller via compose_learning() / size_order(); this module never raises them.
"""

from __future__ import annotations

import math
import random
import time
from typing import Any, Optional

from .patterns import (
    BucketStat,
    PatternStore,
    _clamp,
    _f,
    distance_bucket_for,
    edge_bucket_for,
    hour_bucket_for,
    minutes_bucket_for,
    vol_regime_for,
)

ACTIONS = ("skip", "0.5", "1.0", "1.5")
ACTION_MULT = {"skip": 0.0, "0.5": 0.5, "1.0": 1.0, "1.5": 1.5}



def enrich_buckets(feat: dict[str, Any]) -> dict[str, Any]:
    out = dict(feat)
    if not out.get("edge_bucket"):
        out["edge_bucket"] = edge_bucket_for(_f(out.get("edge") if out.get("edge") is not None else out.get("edge_after_fees")))
    if not out.get("minutes_bucket"):
        out["minutes_bucket"] = minutes_bucket_for(_f(out.get("minutes_left")))
    if not out.get("hour_bucket"):
        out["hour_bucket"] = hour_bucket_for(int(out.get("hour_et") or 0))
    if not out.get("distance_bucket"):
        out["distance_bucket"] = distance_bucket_for(_f(out.get("distance_sigma")))
    if not out.get("vol_regime"):
        out["vol_regime"] = vol_regime_for(str(out.get("asset") or ""), _f(out.get("annualized_vol")))
    return out


def context_key(feat: dict[str, Any]) -> str:
    f = enrich_buckets(feat)
    return "|".join([
        str(f.get("asset") or "").lower(),
        str(f.get("horizon") or ""),
        str(f.get("hour_bucket") or "unknown"),
        str(f.get("vol_regime") or "unknown"),
        str(f.get("distance_bucket") or "unknown"),
        str(f.get("edge_bucket") or "unknown"),
        str(f.get("minutes_bucket") or "unknown"),
    ])


def _thompson(alpha: float, beta: float, rng: random.Random) -> float:
    # Gamma(shape, 1) ratio = Beta. Small shapes are ok for random.gammavariate (shape>0).
    a = max(1e-3, float(alpha))
    b = max(1e-3, float(beta))
    x = rng.gammavariate(a, 1.0)
    y = rng.gammavariate(b, 1.0)
    return x / (x + y)


class ContextBandit:
    """Per-context arm stats, shrunk toward the PatternStore's context posterior."""

    def __init__(self, prior_strength: float = 10.0, seed: Optional[int] = None):
        self.prior_strength = max(1.0, float(prior_strength))
        self.arms: dict[str, dict[str, BucketStat]] = {}  # context -> action -> stat
        self.seen_ids: set[str] = set()
        self.shadow: dict[str, Any] = {
            "evaluated": 0, "would_skip": 0, "would_half": 0, "would_full": 0, "would_boost": 0,
            "shadow_pnl": 0.0, "shadow_cost": 0.0, "settled": 0,
        }
        self.recent: list[dict[str, Any]] = []
        self.updated_at: float = 0.0
        self._rng = random.Random(seed)

    def _arm(self, ctx: str, action: str) -> BucketStat:
        d = self.arms.setdefault(ctx, {a: BucketStat() for a in ACTIONS})
        return d[action]

    def priors_from_store(self, store: PatternStore, feat: dict[str, Any]) -> tuple[float, float]:
        """Beta prior (alpha, beta) and EV prior from the hierarchical pattern posterior."""
        f = enrich_buckets(feat)
        child, parent = store.context_posterior(str(f.get("asset") or ""), str(f.get("horizon") or ""), str(f.get("hour_bucket") or "00-03h"))
        chosen = child if child["n"] >= 5 else parent
        wr = float(chosen.get("post_wr") or 0.5)
        ev = float(chosen.get("post_ev") or 0.0)
        return wr, ev

    def decide(
        self,
        feat: dict[str, Any],
        store: PatternStore,
        *,
        mode: str = "shadow",
        min_samples: int = 10,
        skip_ev: float = -0.02,
        record_shadow: bool = False,
    ) -> dict[str, Any]:
        if mode == "off":
            return {"mode": "off", "passed": True, "mult": 1.0, "would_mult": 1.0,
                    "action": "off", "would_action": "1.0", "context": None,
                    "scores": {}, "detail": None}
        f = enrich_buckets(feat)
        ctx = context_key(f)
        wr0, ev0 = self.priors_from_store(store, f)
        m = self.prior_strength
        scores = {}
        samples = {}
        # Thompson sample a fee-aware score for each arm:
        # skip has EV 0 (no stake). Other arms: sampled_wr * typical_win_roi + (1-wr)*(-1)
        # We use the arm's own Beta if it has data, shrunk toward wr0.
        for act in ACTIONS:
            st = self._arm(ctx, act)
            alpha = m * wr0 + st.wins
            beta = m * (1.0 - wr0) + max(0.0, st.n - st.wins)
            wr_s = _thompson(alpha, beta, self._rng)
            if st.n >= 1:
                # posterior mean ROI, shrunk toward ev0
                ev_hat = (m * ev0 + st.sum_roi) / (m + st.n)
            else:
                ev_hat = ev0
            # Combine: sampled win rate applied to a unit-stake payoff approx via ev_hat scale
            if act == "skip":
                score = 0.0
            else:
                # Fee-aware objective is ROI (pnl/cost). Prefer higher sampled EV; penalize skip only if EV>0.
                score = wr_s * max(0.0, ev_hat + 1.0) - (1.0 - wr_s) * max(0.01, 1.0 - max(ev_hat, -0.99))
                # If ev is clearly negative and we have enough samples, push toward skip.
                if st.n + m * 0.25 >= min_samples and ev_hat <= skip_ev:
                    score -= 0.15
            samples[act] = wr_s
            scores[act] = score
        # If every sized arm looks worse than 0 and we have evidence, skip.
        would = max(ACTIONS, key=lambda a: scores[a])
        if would != "skip" and scores[would] <= 0.0 and scores["skip"] >= scores[would] - 1e-12:
            would = "skip"
        would_mult = ACTION_MULT[would]
        tag = " (shadow)" if mode == "shadow" else ""
        detail = f"bandit{tag}: {would} ({would_mult:g}×) ctx={ctx} scores=" + ",".join(f"{a}:{scores[a]:+.3f}" for a in ACTIONS)

        if record_shadow:
            self.shadow["evaluated"] = int(self.shadow.get("evaluated") or 0) + 1
            key = {"skip": "would_skip", "0.5": "would_half", "1.0": "would_full", "1.5": "would_boost"}[would]
            self.shadow[key] = int(self.shadow.get(key) or 0) + 1

        if mode == "shadow":
            passed, mult, action = True, 1.0, f"shadow:{would}"
        else:
            passed = would != "skip"
            mult = would_mult
            action = would
        return {
            "mode": mode, "passed": passed, "mult": mult, "would_mult": would_mult,
            "action": action, "would_action": would, "context": ctx,
            "scores": {k: round(v, 4) for k, v in scores.items()},
            "samples": {k: round(v, 4) for k, v in samples.items()},
            "detail": detail,
        }

    def update(self, feat: dict[str, Any], *, action: str, order_id: str,
               pnl_usd: float, cost_usd: float, outcome: str, weight: float = 1.0) -> bool:
        """Update the arm that was (or would have been) taken. Idempotent on order_id."""
        if action not in ACTION_MULT:
            return False
        oid = str(order_id or "")
        if oid and oid in self.seen_ids:
            return False
        if oid:
            self.seen_ids.add(oid)
        ctx = context_key(feat)
        st = self._arm(ctx, action)
        cost = max(0.01, float(cost_usd or 0.0))
        pnl = float(pnl_usd or 0.0)
        # For skip, reward is 0 and we still record a unit for the arm.
        if action == "skip":
            pnl, cost = 0.0, 1.0
        won = (outcome == "win") or (pnl > 0)
        roi = max(-1.0, min(20.0, pnl / cost))
        w = max(0.0, float(weight))
        # Weighted add via repeated fractional update on BucketStat (n may become float).
        st.n += w
        if won:
            st.wins += w
        st.sum_roi += roi * w
        st.sum_roi_sq += (roi * roi) * w
        st.sum_pnl += pnl * w
        st.sum_cost += cost * w
        self.updated_at = time.time()
        return True

    def record_shadow_settlement(self, would_action: str, actual_pnl: float, actual_cost: float) -> None:
        """Translate the actual fill into the would-have P&L (linear in size; skip → 0)."""
        if would_action not in ACTION_MULT:
            return
        scale = ACTION_MULT[would_action]  # vs the actual 1.0x book by default
        # If the live book already used a different size we still log linear CF vs unit stake.
        cf_pnl = 0.0 if would_action == "skip" else float(actual_pnl) * scale
        cf_cost = 0.0 if would_action == "skip" else float(actual_cost) * scale
        self.shadow["settled"] = int(self.shadow.get("settled") or 0) + 1
        self.shadow["shadow_pnl"] = round(float(self.shadow.get("shadow_pnl") or 0.0) + cf_pnl, 2)
        self.shadow["shadow_cost"] = round(float(self.shadow.get("shadow_cost") or 0.0) + cf_cost, 2)
        self.recent.append({
            "action": would_action, "cf_pnl": round(cf_pnl, 2), "cf_cost": round(cf_cost, 2),
            "actual_pnl": round(float(actual_pnl), 2),
        })
        if len(self.recent) > 80:
            self.recent = self.recent[-80:]

    def summary(self) -> dict[str, Any]:
        rows = []
        for ctx, arms in self.arms.items():
            rec = {"context": ctx, "arms": {}}
            n = 0
            pnl = 0.0
            cost = 0.0
            for a, st in arms.items():
                rec["arms"][a] = {
                    "n": round(st.n, 2), "wins": round(st.wins, 2),
                    "pnl": round(st.sum_pnl, 2), "cost": round(st.sum_cost, 2),
                    "roi": None if st.sum_cost <= 0 else round(st.sum_pnl / st.sum_cost, 4),
                }
                n += st.n
                pnl += st.sum_pnl
                cost += st.sum_cost
            rec["n"] = round(n, 2)
            rec["pnl"] = round(pnl, 2)
            rec["cost"] = round(cost, 2)
            rec["roi"] = None if cost <= 0 else round(pnl / cost, 4)
            if n > 0:
                rows.append(rec)
        rows.sort(key=lambda r: (r["n"], r["pnl"]), reverse=True)
        sp, sc = float(self.shadow.get("shadow_pnl") or 0), float(self.shadow.get("shadow_cost") or 0)
        return {
            "updated_at": self.updated_at,
            "shadow": {**self.shadow, "shadow_roi": None if sc <= 0 else round(sp / sc, 4)},
            "contexts": rows[:80],
            "recent": list(reversed(self.recent[-40:])),
            "n_contexts": len(rows),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "prior_strength": self.prior_strength,
            "updated_at": self.updated_at,
            "arms": {ctx: {a: st.to_dict() for a, st in arms.items()} for ctx, arms in self.arms.items()},
            "seen_ids": sorted(self.seen_ids)[-8000:],
            "shadow": self.shadow,
            "recent": self.recent[-80:],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any], prior_strength: float = 10.0) -> "ContextBandit":
        b = cls(prior_strength=float(raw.get("prior_strength") or prior_strength))
        b.updated_at = float(raw.get("updated_at") or 0.0)
        for ctx, arms in (raw.get("arms") or {}).items():
            b.arms[ctx] = {a: BucketStat.from_dict(d) for a, d in (arms or {}).items() if a in ACTIONS and isinstance(d, dict)}
            for a in ACTIONS:
                b.arms[ctx].setdefault(a, BucketStat())
        b.seen_ids = set(raw.get("seen_ids") or [])
        if isinstance(raw.get("shadow"), dict):
            b.shadow.update(raw["shadow"])
        b.recent = list(raw.get("recent") or [])[-80:]
        return b


def compose_learning(
    pattern_dec: Optional[dict],
    bandit_dec: Optional[dict],
    rule_dec: Optional[dict],
    *,
    pattern_mode: str = "shadow",
    bandit_mode: str = "shadow",
    rules_mode: str = "shadow",
    min_mult: float = 0.25,
    max_mult: float = 1.50,
) -> dict[str, Any]:
    """Combine learning layers. Active skips win; active mults take the min. Caps always apply.

    Shadow / off layers never change passed or mult. This cannot loosen hard caps:
    the returned mult is clamped to [min_mult, max_mult] (or 0 on skip).
    """
    reasons = []
    passed = True
    mult = 1.0

    def _active(dec, mode, name):
        nonlocal passed, mult
        if not dec or mode != "active":
            if dec and dec.get("detail"):
                reasons.append(dec["detail"])
            return
        if not dec.get("passed", True):
            passed = False
            reasons.append(dec.get("detail") or f"{name} skip")
            return
        m = float(dec.get("mult") or 1.0)
        if m < mult:
            mult = m
        reasons.append(dec.get("detail") or f"{name} {m:g}×")

    _active(pattern_dec, pattern_mode, "pattern")
    _active(bandit_dec, bandit_mode, "bandit")
    _active(rule_dec, rules_mode, "rule")
    if not passed:
        return {"passed": False, "mult": 0.0, "reasons": reasons, "detail": " | ".join(reasons)}
    capped = _clamp(mult, min_mult, max_mult) if mult > 0 else 0.0
    # If a layer asked for a half-Kelly (0.5) that is below min_mult (0.25 is lower, so 0.5 stays).
    # Never exceed max_mult even if a rule said 2.0.
    return {"passed": True, "mult": capped, "reasons": reasons, "detail": " | ".join(reasons)}


def bandit_path(data_dir):
    from pathlib import Path
    import os, json
    d = Path(data_dir) / "patterns"
    d.mkdir(parents=True, exist_ok=True)
    return d / "bandit.json"


def load_bandit(data_dir, prior_strength: float = 10.0) -> ContextBandit:
    from pathlib import Path
    import json
    p = bandit_path(data_dir)
    try:
        raw = json.loads(Path(p).read_text())
        if isinstance(raw, dict):
            return ContextBandit.from_dict(raw, prior_strength=prior_strength)
    except Exception:
        pass
    return ContextBandit(prior_strength=prior_strength)


def save_bandit(data_dir, bandit: ContextBandit) -> None:
    from pathlib import Path
    import json, os
    p = bandit_path(data_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(bandit.to_dict(), separators=(",", ":")))
    os.replace(tmp, p)
