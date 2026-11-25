# Operations

All times are America/New_York (ET). The droplet runs Ubuntu with Python 3.12 in `/opt/polymarket-bot/.venv`.

## Services

| Unit | What | Notes |
|---|---|---|
| `polymarket-bot-supervisor` | supervisor + watchdog + health monitor + archive job; spawns discovery, index_feed, book_feed, engine, execution, strategy_lab | trading path; a crash of a trading worker latches HALT and is **not** restarted |
| `polymarket-bot-dashboard` | uvicorn `app:app` on `127.0.0.1:8787` | read-mostly UI + token-guarded writes |
| `caddy` | HTTPS on :443 for `<ip-with-dashes>.sslip.io` → 127.0.0.1:8787 | Let's Encrypt, auto-renew (needs :80/:443 open) |

```bash
systemctl status polymarket-bot-supervisor polymarket-bot-dashboard caddy
journalctl -u polymarket-bot-supervisor -n 100 --no-pager
tail -f /opt/polymarket-bot/data/runtime/events.jsonl
```

## Deploy (same procedure every time)

From the operator box (`/workspace/polymarket-bot`):

```bash
SSH="ssh -i deploy/do_ed25519 -o IdentitiesOnly=yes -o UserKnownHostsFile=deploy/known_hosts"
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests          # green first
$SSH root@<droplet> 'tar czf /root/backup-code-$(date +%Y%m%d-%H%M).tgz --exclude=polymarket-bot/data --exclude=polymarket-bot/.venv -C /opt polymarket-bot'
rsync -az -e "$SSH" --exclude data/ --exclude .venv/ --exclude .git/ --exclude __pycache__/ --exclude '*.pyc' \
  --exclude deploy/do_ed25519 --exclude deploy/droplet.env --exclude 'screenshots*' --exclude bin/ ./ root@<droplet>:/opt/polymarket-bot/
$SSH root@<droplet> 'cd /opt/polymarket-bot && .venv/bin/pip install -q -r requirements.txt -r web/requirements.txt && PYTHONPATH=src .venv/bin/python -m unittest discover -s tests'
$SSH root@<droplet> 'systemctl restart polymarket-bot-supervisor && sleep 3 && systemctl restart polymarket-bot-dashboard'
```

Verify on the public URL (`/healthz`, `/api/health?token=…`, the pages). Rollback = untar the backup over `/opt/polymarket-bot` and restart both units.

## HALT

- A HALT is a latch in `data/runtime/HALT.json`. The **first reason wins**. The Health page and the header show the reason and component.
- **Understand the cause before you reset.** The causes and their fixes are in TROUBLESHOOTING.md.
- To reset, run this on the droplet only: `cd /opt/polymarket-bot && scripts/reset_halt.sh --yes`. The supervisor then restarts the trading workers.
- The web UI cannot reset a HALT. `POST /api/halt/reset` returns 405.

## Profit lock settings

These live in the **Controls** panel on the Terminal page (versioned, logged, applied within about 1 s), or under `risk.profit_lock` in `strategies/default.yaml`:

| Control | Default | Bounds | Meaning |
|---|---|---|---|
| target_mode | profit_lock | profit_lock / fixed | `fixed` = old behaviour (stop at target) |
| daily_profit_target_pct | 0.05 | — | initial target (fixed mode, or the base of the adaptive target) |
| lock_ratio | 0.70 | 0.30–0.95 | floor = ratio × peak *session* gain (from the current segment baseline); ratchets up only |
| locked_size_mult | 0.5 | 0.10–1.0 | Kelly multiplier after the lock and after each cooldown resume |
| adaptive_target | true | bool | adapt the target from the last 7 days (needs ≥ 3 days of history) |
| target_min_pct / target_max_pct | 0.03 / 0.10 | 0.005–0.25 | clamp for the adaptive target |
| adaptive_lookback_days | 7 | 1–30 | history window |
| daily_loss_limit_pct | 0.10 | — | hard session stop in every mode; resets at midnight ET |
| window_mode | calendar_day | rolling_24h / calendar_day | session window (default midnight ET reset) vs 24h rolling; limits measured from equity at window start |
| max_breaches | 3 | 1–10 | the Nth floor breach stops the session; breaches 1..N−1 are cooldowns |
| cooldown_1_min / cooldown_2_min / cooldown_3_min | 60 / 120 / 240 | 1–1440 | minutes paused after breaches 1, 2, …; the last slot is used only if max_breaches is raised |
| rolling_guard_enabled | true | bool | fail-closed HALT if equity is `rolling_guard_pct` below the trailing-lookback HWM |
| rolling_guard_pct | 0.10 | 0.02–0.50 | drawdown from the trailing HWM that trips `rolling_drawdown` |
| rolling_guard_hours | 24 | 1–168 | lookback for the HWM (spans midnight); reset is CLI-only |

After the session target is hit the bot is **LOCKED-HALF** (0.5× Kelly, ratcheting floor). A floor breach starts a **COOLDOWN** (60 min, then 120 min); when it expires the floor is re-armed from current session P&L and peak tracking restarts from there. The 3rd breach (default) is a **SESSION STOP** until midnight ET. Open positions always settle. A trailing-24h 10 % drawdown from the high-water mark is a fail-closed **HALT** (`rolling_drawdown`) on top of the session −10 % loss limit; clear it only with `scripts/reset_halt.sh --yes` after you understand the cause.

Risk-phase badges: `TRADING` / `LOCKED-HALF` / `COOLDOWN` / `SESSION STOP` / `HALT`. Verdicts use the same wording, e.g. "cooldown after floor breach 1/3, resumes 4:15 AM ET".
| pattern_mode | shadow | off / shadow / active | Bayesian pattern layer (`shadow` logs only; `active` scales Kelly `[0.25×, 1.50×]` and skips EV ≤ −2 %) |
| pattern_min_samples | 20 | 5–200 | minimum settled trades in a context before pattern scaling/skipping activates |

## Archiving and the Mac sync

- **Trigger.** When "toward next part" on the Archives page passes 300 MB (env `ARCHIVE_THRESHOLD_MB`), the supervisor writes `data/archive/part-NNNN-<start>-<end>.tar.zst` and a `.manifest.json`. **Archive now** forces a part. It deletes nothing.
- **Pull and ack.** The Mac pulls each part, checks the sha256 and POSTs an ack. Only then does the droplet prune old *rejected* orders and equity snapshots from the live sqlite. It deletes the tarball 3 days later (`ARCHIVE_ACK_KEEP_DAYS`).
- **Unacknowledged parts are never deleted.** `sync_acks` turns DEGRADED if a part waits longer than 24 h.

### Mac install (run on the Mac, with the owner's approval)

Copy `scripts/local_sync/pull_archives.sh` and `scripts/local_sync/com.polymarket.pullarchives.plist` from this repo (or the operator box) into `~/PolymarketData/bin/`, then:

```bash
mkdir -p ~/PolymarketData/bin ~/.config/polymarket ~/Library/LaunchAgents
chmod 700 ~/PolymarketData/bin/pull_archives.sh
( umask 077; printf 'PM_URL="https://104-236-203-158.sslip.io"\nPM_TOKEN="%s"\n' '<dashboard token>' > ~/.config/polymarket/sync.env )
bash ~/PolymarketData/bin/pull_archives.sh        # first run by hand; prints "done: N new, M acknowledged"
sed "s#__HOME__#$HOME#g" ~/PolymarketData/bin/com.polymarket.pullarchives.plist > ~/Library/LaunchAgents/com.polymarket.pullarchives.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.polymarket.pullarchives.plist
launchctl kickstart -k gui/$(id -u)/com.polymarket.pullarchives
tail ~/PolymarketData/sync.log
```

- The plist runs the pull hourly and at login.
- **Uninstall:** `launchctl bootout gui/$(id -u)/com.polymarket.pullarchives && rm ~/Library/LaunchAgents/com.polymarket.pullarchives.plist`.
- The script only needs `bash`, `curl`, `shasum` and `awk`, all built into macOS. To open a part, install `zstd` (`brew install zstd`), then run `zstd -dc part-….tar.zst | tar -x`.

## Integrations (LLM reviewer)

1. **Connect.** On **Integrations**, paste a key and press **Connect**. The key is encrypted on the droplet, and the UI only ever shows `••••last4`.
2. **Test.** **Test** lists models (OpenRouter: key status), which bills no tokens.
3. **Choose the reviewer.** Pick the provider and model under **Reviewer model**. With none selected, reviews rank strategies deterministically.
4. **Disconnect.** **Disconnect** deletes the stored key.

## Alerts (disabled until configured)

Create `data/secrets/alerts.json` (mode 0600) on the droplet:

```json
{"telegram": {"bot_token": "123:ABC", "chat_id": "123456"},
 "email": {"smtp_host": "smtp.example.com", "smtp_port": 587, "username": "u", "password": "p", "from": "bot@example.com", "to": "you@example.com"}}
```

- **What triggers an alert:** a HALT, or any component changing to DEGRADED or FAILED.
- **Rate limit:** the same alert at most once per 15 minutes.
- **Never blocks the supervisor:** alerts are sent from a background thread.

## Monitoring endpoints

- `GET /healthz` is public. It returns `{"status": "OK|DEGRADED|FAILED|HALTED", "halted": bool}` with HTTP 200 for OK or DEGRADED and 503 otherwise.
- `GET /metrics` needs the token as a Bearer header or `?token=`. It uses the Prometheus text format: `pmbot_component_status{component,critical}`, `pmbot_halted`, equity/P&L gauges, profit-lock gauges, archive gauges and `pmbot_strategy_pnl{strategy,active}`.

## Refresh routine for the static Page (from the operator box)

```bash
scripts/pull_from_droplet.sh             # copies the droplet ledger + CSVs into data/ (keeps keys/tokens local)
PYTHONPATH=src .venv/bin/python scripts/dashboard_data.py -o data/dashboard.json
```

## Exports and reports

- `/api/export/trades.csv`: main-book fills.
- `/api/export/strategy/<name>.csv`: one shadow book.
- `/api/export/decisions.csv`: proposals, gate decisions and switches.
- `/api/report/daily[?day=YYYY-MM-DD]`: the daily text report.
- `PYTHONPATH=src .venv/bin/python scripts/replay.py --all`: zero-lookahead walk-forward replay comparing Pattern OFF vs Pattern ON across archived and live evaluations.

## Phase 6 learning controls and analyst tools

All learning layers default to **shadow** and can be flipped from Controls (Terminal) without a redeploy: Loss attribution, Counterfactual, Calibration, Contextual bandit, Rules lab mode. `cf_weight` (default 0.25) weights the counterfactual stream below real fills.

- Dashboard: `/attribution` (LOSSES) and `/rules-lab` (proposals, backtests, shadow status, promotion history). Calibration and the bandit shadow book are sections on `/patterns`.
- Analyst snapshot (run on the droplet, from the repo):

      PYTHONPATH=src .venv/bin/python scripts/analyst_snapshot.py --data /opt/polymarket-bot/data

- Propose a rule:

      PYTHONPATH=src .venv/bin/python scripts/propose_rule.py \
        --name skip-high-vol-late-5m --action skip \
        --when 'horizon=eq:5m,vol_regime=eq:high,minutes_left=lt:3' \
        --rationale 'late 5m high-vol entries have been fee-eaten' \
        --data /opt/polymarket-bot/data

- Learning backfill and calibration refresh run in the execution process on startup, **after** `HeartbeatKeeper` has started (the watchdog's first-beat grace is 8s; ingesting ~48k evaluations can take longer). They must not trip HALT; if they fail, check `data/runtime/events.jsonl` and the execution log. Rollback is the same as any other deploy (code-only tarball + the pre-deploy full backup).
