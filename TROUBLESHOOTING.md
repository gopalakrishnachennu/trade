# Troubleshooting

Always start at **Health** (`/health?token=…`), then check events (`data/runtime/events.jsonl`) and `journalctl -u polymarket-bot-supervisor`.

| Symptom | Likely cause | Fix |
|---|---|---|
| Header says **HALTED**, reason `stale_heartbeat` | A worker hung, or the host paused | Check `data/runtime/<role>.log` for the last lines. Fix the cause. If it was a one-off host stall, reset with `scripts/reset_halt.sh --yes` |
| `process_exit` | A worker crashed (exception) | Read `data/runtime/<role>.log`, fix the bug, deploy, reset |
| `missing_window` | Discovery shows an ended window, or none after the grace period | Check the exchange API. Restart only after markets list again |
| `health_critical` / `disk` | Free disk < 1 GB or < 5 % | Free space. Make sure archive parts are acknowledged by the Mac (pruning happens only after an ack). Delete old `/root/backup-code-*.tgz`. Then reset |
| `health_critical` / `memory_swap` | RAM and swap exhausted | `free -m`, `ps aux --sort=-rss \| head`. Restart the dashboard if it is the culprit, then reset |
| `health_critical` / `ntp_clock` | Clock offset > 1 s | `timedatectl timesync-status`; `systemctl restart systemd-timesyncd`. Reset after the offset is back in range |
| `health_critical` / `sqlite_integrity` | Ledger corruption | **Do not reset.** Stop the supervisor, copy `data/paper_trades.sqlite` aside, restore from the latest archive part's `sqlite/paper_trades.snapshot.sqlite` (plus later fills from `paper_trades.csv`), and run `PRAGMA integrity_check` |
| `watchdog_lost` (from execution) | Supervisor stopped beating for > 30 s | `systemctl status polymarket-bot-supervisor`. Find why it froze or died, then reset |
| Risk badge **LOCKED-HALF** | Session target hit; trading continues at 0.5× Kelly above the ratcheting floor | Normal |
| Risk badge **COOLDOWN** | Floor breach; new entries paused for 60 / 120 minutes | Normal; resumes automatically at the time shown, floor re-armed from current session P&L |
| Risk badge **SESSION STOP** | Third floor breach (or session loss limit / fixed target) | Normal; new entries resume at midnight ET |
| Risk badge **HALT**, reason `rolling_drawdown` | Equity is ≥ 10 % below the trailing-24h high-water mark | Fail-closed. Understand the drawdown first. Reset only with `scripts/reset_halt.sh --yes` |
| Strategy lab DEGRADED | It crashed and is restarting (30 s backoff) | `data/runtime/strategy_lab.log`. Never halts trading |
| LLM reviewer DEGRADED | Provider error (bad key, quota, network) | Integrations → **Test**. The current strategy is kept. Fix the key or choose "none" |
| `sync_acks` DEGRADED | The Mac has not acknowledged a part for > 24 h | On the Mac, run `bash ~/PolymarketData/bin/pull_archives.sh` and read `~/PolymarketData/sync.log` |
| `caddy_tls` DEGRADED / FAILED | Certificate renewal failing | `journalctl -u caddy`. Ports 80/443 must be reachable. Check sslip.io resolution |
| Dashboard 401 | Missing or wrong `?token=` | Use the original URL. The token is in `data/dashboard_token.txt` on the droplet |
| Dashboard POST returns 403 | Token only in the URL | Writes need the `X-Dashboard-Token` header (the UI sends it) |
| From the operator box, sslip.io does not resolve | Box DNS sinkhole | `curl --resolve 104-236-203-158.sslip.io:443:104.236.203.158 …` |
| Screenshots hang | SSE keeps the page "loading" | Use `scripts/screenshot_dashboard.py` (CDP), not `chrome --screenshot` |
