# Changelog

All times ET.

## 2026-11-25 (03:20+) — Phase 5: session P&L, escalating cooldowns, rolling 24h HWM HALT

- **Session-based daily P&L and loss limit** (`risk.window_mode: calendar_day` is now the default; `rolling_24h` remains available in Controls):
  - Today's P&L, the session profit target, and the −10 % session loss limit reset at midnight ET.
  - Lifetime equity, cash, win rate, profit factor, drawdown, the equity curve, and the pattern store stay continuous (Phase 4 accounting is unchanged).
- **Profit lock is now an escalating cooldown**, replacing the sticky full-session `STOPPED_FLOOR` / LOCK STOP:
  - Once the session target is hit the bot is **LOCKED-HALF**: floor = 70 % of the session peak gain (ratchets up only), trading at 0.5× Kelly.
  - Floor breach 1 pauses new entries for 60 minutes; breach 2 for 120 minutes. After each cooldown it resumes at 0.5× Kelly with the floor re-armed from current session P&L (peak tracking restarts from that baseline).
  - Breach 3 (configurable `max_breaches`, default 3) is a **SESSION STOP** for the rest of the calendar day. The third cooldown duration (240 min) is reserved if `max_breaches` is raised.
  - Everything (breach count, floor, peak, cooldown) resets at the session boundary.
  - Configurable via versioned config and Controls: `max_breaches`, `cooldown_1_min`, `cooldown_2_min`, `cooldown_3_min`.
  - Lock state is a pure function of the session's settlements plus `now`, so a process restart reproduces it exactly and it cannot go stale. No side file is used.
- **Rolling 24h safety guard** (`risk.rolling_guard`, on by default):
  - If equity is 10 % below its high-water mark at any point in the trailing 24 hours (the window spans midnight), the bot raises a fail-closed `rolling_drawdown` HALT that only `scripts/reset_halt.sh --yes` can clear.
  - This is independent of the per-session −10 % loss limit. A recovered historical dip does not trip; only current equity vs the lookback HWM does.
- **Dashboard** risk phases are now `TRADING` / `LOCKED-HALF` / `COOLDOWN` / `SESSION STOP` / `HALT`, with the reason, a countdown to resume, breach count (n/3), session floor and peak, and distance to the rolling-guard trigger. Verdicts use the same wording (e.g. "cooldown after floor breach 1/3, resumes 4:15 AM ET").
- **Tests & replay**: Phase 5 tests cover cooldown expiry and resume, escalation to session stop, the re-armed floor, midnight breach reset, the rolling guard across midnight, no false trigger on the current ~3.4 % drawdown, and restart persistence. `scripts/replay.py` compares the old sticky full-session floor stop with the new cooldown design on all recorded history.

## 2026-11-25 (00:10+) — Phase 4: continuous accounting, rolling 24h risk window & pattern learning

- **Root-cause fix (midnight reset & stale LOCK STOP)**:
  - Fixed `analytics.ledger_bundle` and `risk.restore_state` so account equity, cash, P&L, fees, staked total, win rate, profit factor, drawdown, and the equity curve are **continuous lifetime metrics** that never reset at midnight ET. Today's P&L and bets are displayed separately (`Today's P&L (ET)`).
  - Fixed `RiskManager.apply_daily_halts` to clear `kill_reason = None` when the active risk window turns over, eliminating the stale `STOPPED_FLOOR` (`LOCK STOP`) carry-over across days.
- **Configurable risk windows (`rolling_24h` default / `calendar_day` option)**:
  - Replaced the hard midnight ET reset with a rolling 24-hour window (`risk.window_mode: rolling_24h`, `risk.window_hours: 24.0`), controllable live via the Controls panel (`window_mode`).
  - Profit-lock target and −10 % loss limit are now measured against **equity at the start of the active window**, not a fixed $1,000.
  - `scripts/daily_report.py` remains reporting-only and now displays continuous equity entering the day alongside that day's activity.
- **Bayesian pattern learning (`src/polymarket_bot/patterns.py`)**:
  - Logs full context features (`asset`, `horizon`, `hour_et`, `hour_bucket`, `weekday`, `minutes_left`, `distance_sigma`, `vol_regime`, `model_prob`, `ask`, `edge`, `spread`, `depth`, `strategy`, `outcome`, `pnl_usd`) to `data/patterns/evaluations.jsonl`, `data/patterns/store.json`, and SQLite `trade_patterns`.
  - Computes hierarchical Beta-Binomial win-rate and shrunk EV posteriors (`asset|horizon|hour_bucket` → `asset|horizon` → global).
  - Runs in `shadow` mode by default (logs counterfactuals and verdict checklist rows without altering live sizing); supports `active` (`[0.25×, 1.50×]` Kelly multiplier within hard caps + skip when `n ≥ min_samples` and `post_ev ≤ -2%` with `ev_ucb < 0`) and `off` via Controls.
  - Added `/patterns` dashboard page and `/api/patterns` endpoint with 24h × asset/timeframe heat map, dimension breakdown tables, and recent trade features.
  - Added pattern summary context to `reviewer.build_payload` (promotion gate remains 100 % rule-based).
- **Walk-forward validation & tests**:
  - Updated `scripts/replay.py` with zero-lookahead walk-forward simulation (`Pattern OFF` vs `Pattern ON`) and `--all` flag.
  - Added Phase 4 unit and midnight-rollover simulation tests (`tests/test_phase4.py`).

## 2026-11-24 (evening) — extension release

- **Profit lock**:
  - After the daily target is hit, the bot locks a floor at 70 % of the peak gain. The floor ratchets up, and trading continues at 0.5× Kelly.
  - It stops only at the floor or at the −10 % loss limit.
  - Optional adaptive target from the 7-day win rate, edge and volatility, clamped to 3–10 %.
  - Configurable in Controls. The KPI strip and the equity chart show the target, peak, floor and mode.
- **Health**:
  - About 20 components, split into critical (latch HALT after 2 consecutive failures) and non-critical (alert only).
  - Disk-full halt, memory/swap, NTP drift, sqlite integrity, ledger freshness, TLS expiry, dashboard, archive, sync acks, LLM reviewer and alerts.
  - New `/health` page, a health pill on every page, `/healthz` and Prometheus `/metrics`.
  - Execution halts itself if the watchdog stops beating.
- **Strategy lab**:
  - `baseline`, `high_conviction`, `late_window` and `favorite`, each in a shadow paper book with P&L, win rate, Brier, calibration, profit factor and drawdown.
  - The active strategy drives the main book, and engine and execution must agree on it before a fill.
- **Reviewer and gate**:
  - Hourly and on-demand reviews, with an LLM (optional) or a deterministic ranking.
  - A deterministic promotion gate, manual approve/reject, and a full decision log with CSV export.
- **Integrations**: OpenAI, Anthropic, Gemini, xAI, OpenRouter and custom OpenAI-compatible providers. Keys are encrypted at rest, mode 0600 and masked. Cursor is listed honestly as unusable.
- **Archiving**:
  - `part-NNNN-<range>.tar.zst` with a sha256 manifest at 300 MB.
  - The ledger stays continuous, with a VACUUM INTO snapshot in each part.
  - Ack-gated pruning, the Archives page, and a Mac pull script with a launchd template.
  - The bus now keeps every rotated event file (previously `.jsonl.1` was overwritten).
- **Extras**:
  - Telegram/email alert hooks (disabled until configured).
  - CSV exports and the daily report in the dashboard.
  - An offline replay runner (`scripts/replay.py`).
- **Docs**: README, ARCHITECTURE, HOW_IT_WAS_BUILT, OPERATIONS, TROUBLESHOOTING, STRATEGIES, EXTENDING, INVARIANTS, SECURITY.

## 2026-11-24 (17:00–17:35) — dashboard redesign

- KPIs from the sqlite ledger, uPlot charts, verdict reasons per market, versioned token-guarded paper controls, flatten with a typed confirmation, and an event stream.

## 2026-11-24 (afternoon) — fail-closed runtime and DigitalOcean deploy

- Separate processes under a supervisor and watchdog, a file bus, and the HALT latch with a CLI-only reset.
- A host-stall guard against false stale-heartbeat halts.
- Droplet deploy with systemd, Caddy (HTTPS) and a permanent sslip.io URL.

## 2026-11-24 (morning) — paper scaffold

- Discovery, official index and BBO polling, the rules engine, fractional Kelly, daily +5 % / −10 % stops, SQLite and CSV ledger, and the daily report.
