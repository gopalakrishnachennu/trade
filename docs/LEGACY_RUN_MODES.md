# Legacy run modes (single-process bot, tunnels)

These predate the supervised runtime and the DigitalOcean deploy. They still work for local experiments.

## Live dashboard (updates every second)

A small FastAPI app in `web/` streams bot state over Server-Sent Events once per second: heartbeat, index prices, all 12 live windows, fills, open positions with mark-to-market, P&L, and recent log lines. The paper bot writes `data/live_state.json` atomically each loop (at most once per second) so the dashboard never talks to the trading APIs itself.

It binds only to `127.0.0.1:8787` and is published over HTTPS with a token in the URL (`?token=…`). The same token is accepted as HTTP basic-auth password or a `Bearer` / `X-Dashboard-Token` header. The token lives in `data/dashboard_token.txt` (gitignored). On this box, Cloudflare quick tunnels cannot connect (tunnel hostnames are DNS-sinkholed to `198.18.0.1`), so the wrapper uses a [localhost.run](https://localhost.run) SSH tunnel to a real IP looked up via DNS-over-HTTPS. Set `TUNNEL=cloudflared` if you want to try Cloudflare instead.

```bash
setsid scripts/run_live_dashboard.sh </dev/null >/dev/null 2>&1 &
# wait a few seconds, then:
cat data/dashboard_url.txt
```

Stop it (does **not** stop the paper bot):

```bash
scripts/stop_live_dashboard.sh
```

**The public HTTPS URL changes every time the tunnel restarts**, and a free localhost.run domain can also rotate on its own. After a stop/start (or if the page stops loading), read `data/dashboard_url.txt` again. This dashboard is paper-only and cannot place orders.

## Run

Python 3.10+. From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=src python -m polymarket_bot --config config.yaml --duration 180
```

All-day paper session (detached, auto-restart, stops at 11:00 PM ET):

```bash
setsid scripts/run_paper_day.sh </dev/null >/dev/null 2>&1 &
```

Stop the process:

```bash
scripts/stop_paper.sh
```

`--duration 0` runs until `--stop-at` (default `23:00` America/New_York) or SIGTERM. Tests:

```bash
PYTHONPATH=src python -m unittest tests.test_core -v
```

Create `data/KILL` to stop *new* paper trades without killing the process. Open positions still settle.
