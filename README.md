# Polymarket US crypto Up/Down — fail-closed paper trading bot

Paper-trading system for **Polymarket US** (the CFTC-regulated exchange at [polymarket.us](https://polymarket.us), not polymarket.com). It watches BTC, ETH, SOL and XRP **Up or Down** windows (5 min, 15 min, 1 h), prices them with a model built on the official settlement index, and records **simulated** fills and P&L.

> **Paper only.** Nothing in this repository places real orders, reads exchange API keys or has a live mode. `mode` is forced to `paper` in code and every worker refuses to start otherwise. See [INVARIANTS.md](INVARIANTS.md).

## What it does

- **Supervised, fail-closed runtime.** Separate processes (`discovery`, `index_feed`, `book_feed`, `engine`, `execution`) run under a supervisor and watchdog, and talk through an atomic file bus. Any stale heartbeat, crash, missing window, clock problem or **critical health failure** latches a **HALT**. A HALT stops new entries until an operator clears it from the command line.
- **Rules engine and sizing.** The engine checks strike, timing, spread, liquidity, ask band and edge after fees, then sizes with fractional Kelly. Risk limits cover per bet, concurrency and exposure.
- **Session profit lock with escalating cooldowns.** Today's P&L and the −10% loss limit reset at midnight ET; lifetime equity stays continuous. When the session target is hit, a floor of 70% of the peak session gain is locked and trading continues at half Kelly. A floor breach pauses new entries for 60 minutes (then 120); after the cooldown the floor is re-armed from current session P&L. A third breach, or the session loss limit, stops new entries until midnight. A separate rolling 24h 10% drawdown from the high-water mark is a fail-closed HALT that only the CLI reset can clear.
- **Strategy lab.** Four strategies (`baseline`, `high_conviction`, `late_window`, `favorite`) each trade a virtual paper book in shadow, with P&L, win rate, Brier, calibration, profit factor and drawdown. One **active** strategy drives the main book. A reviewer runs hourly and on demand. It can use an LLM or a deterministic ranking. A **deterministic promotion gate** decides switches, and every proposal can be approved or rejected by hand.
- **Integrations.** OpenAI, Anthropic, Gemini, xAI, OpenRouter, or any OpenAI-compatible server can act as the reviewer. Keys are encrypted at rest, kept in mode 0600, masked in the UI and never logged. Cursor is listed but has no usable public API.
- **Health.** The Health page covers about 20 components, split into critical (latch HALT) and non-critical (alert only). It includes disk-full protection, a TLS expiry warning, NTP drift and a sqlite integrity check. `/healthz` and Prometheus `/metrics` are available.
- **Archiving.** When live data passes 300 MB, the bot writes `tar.zst` parts with sha256 manifests. A Mac pulls them over HTTPS and acknowledges each one, and only acknowledged data is ever pruned.
- **Dashboard.** A dark terminal UI that updates every second. Pages: Terminal, Health, Strategies, Integrations, Archives and Rules. It also offers versioned operator controls, CSV export and a daily report.

## Quick start (local)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r web/requirements.txt
PYTHONPATH=src python -m unittest discover -s tests          # all tests must pass
PYTHONPATH=src python -m polymarket_bot.runtime.supervisor --config config.yaml   # trading runtime (paper)
DASHBOARD_TOKEN=dev uvicorn --app-dir web app:app --port 8787                     # dashboard → http://127.0.0.1:8787/?token=dev
```

Production runs on a DigitalOcean droplet behind Caddy (HTTPS) as two systemd services. See [OPERATIONS.md](OPERATIONS.md).

## Documentation

| Doc | What's in it |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Processes, bus, interlocks, health, strategies, archiving, data flow |
| [docs/HOW_IT_WAS_BUILT.md](docs/HOW_IT_WAS_BUILT.md) | The build history and the reasons behind each design decision |
| [OPERATIONS.md](OPERATIONS.md) | Deploy, restart, HALT reset, archiving and Mac sync, integrations, alerts, refresh routine |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Symptom → cause → fix |
| [STRATEGIES.md](STRATEGIES.md) | Strategies, shadow books, metrics, reviewer, promotion gate, profit lock |
| [EXTENDING.md](EXTENDING.md) | How to add a strategy, a health probe, a control, a provider or a page |
| [INVARIANTS.md](INVARIANTS.md) | What must never change |
| [SECURITY.md](SECURITY.md) | Token auth, secrets handling, threat model, what is public |
| [CHANGELOG.md](CHANGELOG.md) | Release notes |
| [docs/POLYMARKET_US.md](docs/POLYMARKET_US.md) | Research notes on Polymarket US markets, settlement and fees |
| [docs/LEGACY_RUN_MODES.md](docs/LEGACY_RUN_MODES.md) | The older single-process bot and tunnel scripts |

## Layout

```
src/polymarket_bot/       core library
  runtime/                supervisor, watchdog, bus, interlock (HALT latch), workers, controls
  rules.py probability.py sizing.py risk.py profit_lock.py executor.py store.py
  strategies.py strategy_lab.py reviewer.py integrations.py health.py archive.py alerts.py analytics.py verdict.py
web/                      FastAPI dashboard (app.py) + pages (index, health, strategies, integrations, archives, rules)
strategies/default.yaml   strategy + risk parameters (edit, commit)
config.yaml               points at the strategy file; paper mode
scripts/                  reset_halt.sh, pull_from_droplet.sh, dashboard_data.py, daily_report.py, replay.py,
                          screenshot_dashboard.py, local_sync/ (Mac archive pull + launchd template)
deploy/                   systemd units and Caddyfile template (no secrets)
tests/                    unittest suite (no pytest needed)
```

Runtime data (`data/`) lives only on the droplet and on the Mac. It is never committed.
