# Paper-trading data

All runtime data for the bot lives here (this folder on the shared box, path `/workspace/polymarket-bot/data/`). Nothing is written to a personal laptop unless you copy it off.

| File | What it is |
|---|---|
| `paper_trades.sqlite` | Source of truth: paper orders and equity snapshots |
| `paper_trades.csv` | Append-only trade audit log (fills, settlements; also older reject rows) |
| `evaluations.csv` | Every rule evaluation (pass and skip reasons) |
| `equity.csv` | Periodic cash / exposure / realized P&L |
| `bot.log` | Bot process log (America/New_York timestamps) |
| `wrapper.log` | Restart-wrapper log |
| `bot.pid` / `wrapper.pid` | PIDs for the running all-day session |
| `KILL` | Create this file to stop *new* paper entries; open trades still settle |
| `STOP` | Create this file to ask the wrapper not to restart after the bot exits |
| `archive/` | Prior runs, kept out of the way (e.g. the morning smoke test) |
| `live_state.json` | Atomic 1-second snapshot the live dashboard reads (gitignored) |
| `dashboard_token.txt` | Access token for the public dashboard URL (gitignored) |
| `dashboard_url.txt` | Current Cloudflare tunnel URL including `?token=` (gitignored; changes on tunnel restart) |
| `dashboard.log` / `cloudflared.log` | Live dashboard and tunnel logs |
| `DASHBOARD_STOP` | Stops the dashboard wrapper; does not stop the paper bot |

These files are gitignored. Summarize a day with:

```bash
PYTHONPATH=src .venv/bin/python scripts/daily_report.py
```
