# Strategies, shadow books, reviewer and promotion gate

## The strategies

Every strategy **must pass all data-quality rules** from the rules engine: strike stamped, timing window, spread, liquidity and ask band. A strategy can only change the edge threshold (never below the 2 % hard floor) and add filters.

| Name | Idea | Default params |
|---|---|---|
| `baseline` | The original rules engine | configured `min_edge_after_fees` (4 %) |
| `high_conviction` | Fewer, stronger bets | min_edge 8 %, min_prob 60 % |
| `late_window` | Only the last 35 % of a window, when the spot/strike gap carries the most information | min_edge 3 %, max_frac_left 0.35 |
| `favorite` | Back the likely side only | min_edge 3 %, min_prob 65 %, min_ask 55¢ |

Parameter bounds (`strategies.PARAM_BOUNDS`) are min_edge 0.02–0.25, min_prob 0–0.95, min_ask 0–0.95, max_ask 0.05–1, max_frac_left 0.05–1. Every value is clamped wherever it comes from: YAML, the operator or the LLM.

## Shadow books

- **What runs.** The `strategy_lab` process runs every strategy against the same live signals. Each one gets a full paper book: a $1,000 start, the same Kelly sizing, the same risk gates and profit lock, and the same settlement from the official 60 s index average. Each book has its own sqlite under `data/strategies/shadow/<name>/`.
- **Metrics.** Written every 10 s to `data/strategies/metrics.json`:
  - trades, open, win rate, P&L, today's P&L and profit-lock phase
  - profit factor (gross wins ÷ gross losses)
  - max drawdown (peak-to-trough of cumulative realized P&L)
  - Brier score (mean (p − outcome)²; 0.25 = coin flip)
  - 10-bin calibration: predicted p vs observed win frequency, plotted per strategy on the Strategies page
- **Shadow vs main.** The `baseline` shadow should track the main book closely. A large gap means something differs, for example a stale shadow state after a restart.

## Active strategy

- **Where it is set.** `data/strategies/active.json` holds `{active, params, version, changed_at, reason, actor}`. A missing or corrupt file means `baseline`.
- **Engine.** The engine applies the active strategy and stamps each published signal with `strategy` and `strategy_version`.
- **Execution.** Execution re-applies it to the base checks and skips the fill if the stamps don't match its own view. Mid-switch, it therefore fails closed: it skips the trade rather than guess.

## Reviewer

- **Schedule.** Hourly (`review_interval_min`) from the lab, plus **Run review now** on the Strategies page.
- **With an LLM configured** (Integrations → Reviewer model):
  - **Sent:** strategy descriptions, params, metrics and the last 25 trades per strategy. **No keys or tokens.**
  - **Expected back:** a JSON object `{ranking, suggested_active, param_tweaks, rationale}`.
  - **If the reply is invalid or the call fails:** the review is logged as an error, the reviewer shows DEGRADED and **the current strategy is kept**. Nothing halts.
- **Without an LLM:** a deterministic ranking by `P&L − 0.5 × max drawdown`.
- **Param tweaks** are clamped to bounds and to `max_step_frac` (25 % of the range per review). They apply to **shadow books only**. A tweaked strategy reaches the main book only when it is promoted.

## Promotion gate (deterministic; works with no LLM)

A suggested strategy is promoted only if **all** of these hold. The defaults are editable on the page.

1. The candidate is known and differs from the active strategy.
2. It has at least `min_trades` (30) settled shadow trades.
3. Its P&L beats the active strategy by at least max(`min_margin_usd` $10, `min_margin_pct` 1 % of bankroll).
4. Its max drawdown is at most the active strategy's drawdown × `max_dd_ratio` (1.10) + $5.
5. Its profit factor is at least `min_profit_factor` (1.10).
6. Its Brier score is at most `max_brier` (0.25).
7. At least `cooldown_hours` (6 h) have passed since the last switch.

If the gate passes and `auto_promote` is on, the switch happens and is logged. Otherwise the proposal stays **pending**, and the operator can **Approve** (switch now) or **Reject** it. Every proposal, gate result, approve/reject and switch is in `data/strategies/decisions.jsonl` and in the event stream, and can be exported at `/api/export/decisions.csv`.

## Profit lock (daily target)

See ARCHITECTURE.md › Profit lock and OPERATIONS.md › Profit lock settings. Shadow books apply the same profit lock, so comparisons are like for like.

## Pattern learning layer (Phase 4)

- **Feature capture.** Every evaluation and settled trade records: `asset`, `horizon`, `side`, `hour_et` (`hour_bucket`), `weekday`, `minutes_left`, `distance_sigma` (in σ), `vol_regime`, `model_prob`, `ask`, `edge`, `spread`, `depth_contracts`/`depth_usd`, `strategy`, `outcome`, and `pnl_usd`.
- **Hierarchical Bayesian posteriors.** `PatternStore` (`src/polymarket_bot/patterns.py`) shrinks each `asset|horizon|hour_bucket` context toward its `asset|horizon` parent and global prior (`prior_strength = 10.0`).
- **Modes (`patterns.mode` / Control `pattern_mode`):**
  - `shadow` (default): evaluates and logs counterfactual actions (`would skip`, `would boost`, `would trim`) and verdict checklist lines without changing live sizing or gating trades.
  - `active`: scales Kelly fraction within `[0.25×, 1.50×]` (never exceeding `max_per_bet_usd`, `max_fraction_of_bankroll`, or liquidity caps) and skips contexts once `n ≥ min_samples` (default 20) with `post_ev ≤ −2 %` and `ev_ucb < 0`.
  - `off`: kill switch disabling pattern evaluation.
- **Reviewer integration.** `reviewer.build_payload` passes compact pattern stats (`global`, top/bottom contexts, `vol_regime`, `distance_bucket`, `shadow_stats`) to the reviewer, while the **promotion gate remains 100 % rule-based**.

## Offline walk-forward replay

`scripts/replay.py --all` replays all recorded `evaluations.csv` files (archived + live) with strict **zero-lookahead** walk-forward settlement:

- Before evaluating a row at time `t`, only simulated trades whose `settle_ts ≤ t` are settled into `PatternStore`.
- Compares **Pattern OFF** vs **Pattern ON** across every strategy (trades, win%, P&L, avg, profit factor, max drawdown, and shadow-mode counterfactuals).
- Outcome is approximated from the last row with ≤ 30 s left; use it to compare filters and adaptive gating on the same tape without lookahead bias.
- Phase 6 adds a fee-aware calibration report (model P vs realized frequency, Brier, log loss, per asset|horizon) and a Thompson bandit walk-forward (shadow vs active) on the same tape.

## Rules lab (Phase 6)

Analyst (human or agent) rule proposals are **not** a new strategy and do not replace the strategy promotion gate.

- DSL: conditions on allowlisted context features (`asset`, `horizon`, `hour_bucket`, `vol_regime`, `distance_bucket`, `edge_bucket`, `minutes_left`, …) → `skip` or `size_mult` in `[0.25, 1.50]`. Forbidden keys: `mode`, `daily_loss_limit_pct`, `max_per_bet_usd`, `rolling_guard`, `halt`.
- Submit: `scripts/propose_rule.py --name … --action skip --when 'horizon=eq:5m,vol_regime=eq:high,minutes_left=lt:3' --rationale '…'` or the Rules Lab form. The rule is walk-forward backtested on stored evaluations, then placed in shadow.
- Promotion: ≥ 30 live shadow trades, better net P&L than the champion, drawdown not worse. Auto-promote is off; auto-demote is on. Manual approve/reject is on `/rules-lab`.
- When an LLM key is added later, `reviewer.py` will enqueue `rule_proposals` onto the same queue.
- Snapshot for the analyst: `PYTHONPATH=src .venv/bin/python scripts/analyst_snapshot.py --data /opt/polymarket-bot/data`.

## Entry-time path features

`mom_1m`, `mom_3m`, `mom_window` (σ-units, signed with the bet side), `strike_cross_count`, and `drift_dir` / `signed_drift` are computed from index ticks at or before the decision only. They can be used in rules-lab proposals. Hindsight labels such as last-minute flip are not in the DSL.

## Per-strategy pattern mode

`patterns.by_strategy` overrides the global `patterns.mode` for one strategy (`shadow` / `active` / `off`). Live book uses the mode for the **active** strategy. Defaults: `favorite` and `high_conviction` active; `baseline` and `late_window` shadow. Bandit and calibration stay shadow. Active skip rules and the pattern layer auto-demote back to shadow if they underperform the shadow champion by more than `$20` over 20+ trades, or if a floor breach is caused by a pattern size-up.
