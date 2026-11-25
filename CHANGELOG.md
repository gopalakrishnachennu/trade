# Changelog

All times ET.

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
