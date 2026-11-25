# How it was built

The bot was built in one day (24 Nov 2026, ET) in five phases. Each phase kept the previous phase's guarantees.

## 1. Paper scaffold (morning)

- **Research first.** The live Polymarket US public API was checked before any code was written ([POLYMARKET_US.md](POLYMARKET_US.md)): which Up/Down markets exist, how they settle (a 60-second average of the CF Benchmarks index) and the fee formula.
- **The loop.** Discovery → index and BBO polling → a lognormal probability model against `priceToBeat` → rules → fractional Kelly → paper fills → SQLite and CSV.
- **Daily stops.** +5 % / −10 % daily stops block new entries only.

## 2. Fail-closed runtime (afternoon)

- **The 8-minute stale screen.** A single process showed a stale screen for 8 minutes while it looked "up". That led to the rule: **status comes from data age and a latch, not from "is the process running"**.
- **The split.** The bot was split into separate processes talking over an atomic file bus (heartbeats, snapshots, events). A watchdog checks freshness, missing windows, clock skew and sequence numbers. Any fault latches HALT. The latch is reset from the CLI only, and crashed workers are not restarted.
- **The host-stall guard.** A false halt at 2:02 ET (the whole host paused) added a guard: if the supervisor itself was not scheduled, it does not blame a worker.

## 3. DigitalOcean deploy

- **Why.** The operator box cannot host a public URL reliably (tunnel DNS is sinkholed).
- **The setup.** A 1 GB droplet with swap, two systemd units, Caddy with Let's Encrypt on an sslip.io hostname, and the same token. Code is deployed by rsync after a backup tarball. Tests run on the droplet before restart.

## 4. Dashboard redesign (17:00)

- **Built on the sqlite ledger.** The dashboard is a dense terminal-style UI. KPIs come from the sqlite ledger, which is the source of truth, and mixed naive and aware timestamps are parsed carefully.
- **Explanations.** Every NO TRADE comes with its reason (the first failing rule with numbers).
- **Controls.** Operator controls are versioned and bounded. They are applied by workers within about a second and logged. Writes need a header token, so a URL alone can't change anything.

## 5. Extension phase (21:00)

Each step was built as a small, tested unit and committed:

1. **Profit lock.** The daily target became a ratcheting floor instead of a hard stop. It is a pure function of the day's settlements, so a restart reproduces the same state.
2. **Health.** Probes were written as pure functions with injectable inputs for tests. The supervisor runs them in a thread so slow probes never stall the loop. Critical failures latch HALT after two consecutive checks.
3. **Strategies.** A strategy is a filter over the engine's existing signals, so data-quality rules can't be bypassed. Shadow books reuse the real `PaperExecutor`, which keeps comparisons honest. The active strategy is checked twice (engine and execution) so a switch fails closed.
4. **Reviewer and gate.** The LLM is optional and advisory. A deterministic gate decides, and every step is logged.
5. **Integrations.** Keys use Fernet encryption, mode 0600, masking and error scrubbing. Test calls bill no tokens.
6. **Archiving.** The design keeps the sqlite ledger live and continuous, moves only old rows and rotated files, takes a `VACUUM INTO` snapshot, and prunes only after a sha256-verified ack from the Mac. CSV writers and the archiver share a `flock`.
7. **Docs and push.** The docs were written, the history was scanned, and a clean public branch was pushed. An early commit had contained the dashboard URL with its token, so the public history starts from a clean tree.

## Tools

- Python 3.12, FastAPI, uvicorn, uPlot (vendored), sqlite, systemd, Caddy, zstd.
- Headless Chrome over CDP for screenshots.
- `unittest` only (no pytest), so the droplet needs nothing extra.
