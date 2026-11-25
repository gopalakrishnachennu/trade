# Invariants — what must not change

Breaking any of these needs an explicit decision from the owner, written down here first.

1. **Paper only.**
   - No code path places a real order or loads exchange credentials.
   - `effective_config` forces `mode: paper`, and execution refuses to start otherwise (`mode_not_paper` HALT).
   - Controls reject any key about mode, keys or live trading.
   - LLM integrations can never place orders, and their output only selects among paper strategies.
2. **Fail closed.** When the system is unsure, it stops opening positions. It does not skip a bad tick, retry blindly or keep trading on stale data.
3. **The HALT latch.**
   - The first reason wins.
   - It is cleared only by `scripts/reset_halt.sh --yes` on the droplet, after the cause is understood.
   - There is no web reset (`/api/halt/reset` → 405).
   - The supervisor does not restart a crashed trading worker.
4. **The interlock check comes immediately before every fill.** Settlement of open paper positions may continue while halted.
5. **Critical vs non-critical.**
   - Critical health failures latch HALT.
   - The LLM reviewer, archive job, sync acks, TLS, dashboard, strategy lab, alerts, **loss attribution, calibration refresh, the contextual bandit, and the rules lab** **never** halt trading. Learning failures are logged as events.
   - A reviewer failure keeps the current strategy.
6. **Strategies cannot bypass data quality.** Every strategy needs all non-edge rule checks to pass, plus edge ≥ 2 %. Parameters are always clamped. Execution skips the fill when engine and execution disagree about the active strategy.
7. **The promotion gate is deterministic** and works without any LLM. LLM tweaks touch shadow books only. Adaptive pattern multipliers and bandit/rule size multipliers (`[0.25×, 1.50×]`) are strictly bounded inside existing Kelly, per-bet, exposure, and liquidity hard caps. Analyst and LLM rule proposals share one queue; a rule cannot set mode, caps, the session loss limit, the rolling HALT, or paper-only.
8. **Ledger continuity.**
   - `data/paper_trades.sqlite` is never replaced, and lifetime equity/cash/P&L/stats carry over continuously across midnight (never wiped to starting bankroll).
   - Risk windows (`calendar_day` default, or `rolling_24h`) measure session limits against window-start equity and must clear breach counts, cooldowns and floors when a window expires.
   - A trailing-24h 10 % drawdown from the high-water mark is a fail-closed HALT (`rolling_drawdown`) that only the CLI reset can clear.
   - Open and settled orders are never deleted.
   - Archiving moves only pre-today CSV rows, rotated events and logs.
   - Pruning, which covers rejected orders, equity snapshots and tarballs, happens **only after an ack with a matching sha256**.
   - **Unacknowledged data is never deleted.**
9. **Auth.**
   - Every page and API needs the dashboard token.
   - Writes need it in the `X-Dashboard-Token` header.
   - Only `/healthz` (status word only), `/vendor/*` and `/assets/*` (static JS/CSS, no data) are public.
   - The URL and token stay the same across deploys.
10. **Secrets** (the dashboard token, the SSH key, `droplet.env`, integration keys, alert credentials) are never committed, logged, returned to the browser or included in archives.
11. **Times shown to people are America/New_York.** Timestamps are parsed as mixed naive or aware ISO strings through `analytics.parse_ts`.
12. **Tests stay green** before every deploy (`python -m unittest discover -s tests`).

13. **Learning cannot touch safety limits.**
    - Pattern learning, counterfactual evidence, calibration, the contextual bandit, and analyst rules may only skip or shrink size, or (when explicitly `active`) scale Kelly inside `[0.25×, 1.50×]`.
    - They cannot raise or disable `max_per_bet_usd`, `max_fraction_of_bankroll`, `max_contracts`, the session loss limit, the rolling 24h HWM HALT, paper-only, or the fail-closed latch, and they cannot clear a HALT.
    - Active calibration may only tighten: sized p = min(raw p, calibrated p). A raw min-edge fail can never be rescued by a higher calibrated p.
    - `compose_learning` takes the minimum of the active multipliers; any active skip sets `passed=False` (a zero multiplier is not used, because `size_order` floors `pattern_mult` at 0.25).
    - Every learning layer is switchable `off` / `shadow` / `active` from versioned Controls without a redeploy. Defaults are `shadow`.
    - The rule DSL allowlists context feature fields only.
