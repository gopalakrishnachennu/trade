# Security

## What is public

- This repository is public. It contains code, docs, systemd units and a Caddyfile template, and **no secrets and no data**.
- The droplet's IP and its sslip.io hostname appear in the docs. Every data route still requires the dashboard token.
- `/healthz` is public and returns only a status word.

## Authentication

- **The token.** One long random dashboard token, stored in `data/dashboard_token.txt` on the droplet (gitignored).
- **Reads.** Accepted as `?token=`, `Authorization: Bearer`, a basic-auth password or `X-Dashboard-Token`.
- **Writes.** Controls, flatten, integrations, archive ack/run, reviews and approvals **require the header** (`X-Dashboard-Token`). A leaked link alone cannot change anything.
- **Comparison.** Constant time.
- **Transport.** TLS terminates at Caddy (Let's Encrypt), and uvicorn listens on 127.0.0.1 only.

## Secrets

| Secret | Where it lives | Never |
|---|---|---|
| Dashboard token | `data/dashboard_token.txt` on the droplet | committed, logged, put in docs |
| SSH deploy key | `deploy/do_ed25519` on the operator box only | committed, copied to the droplet |
| LLM API keys | `data/secrets/integrations.enc` (Fernet-encrypted, 0600, dir 0700), key in `data/secrets/master.key` (0600) | returned to the browser (masked `••••last4`), logged, put in events or archives, committed |
| Alert credentials | `data/secrets/alerts.json` (0600) | committed |

- **What the encryption protects.** A copied `integrations.enc` (from a backup or an archive) can't be read on its own. Anyone with root on the droplet can still read both files. The secrets directory is excluded from archives and from git.
- **Error messages.** Provider error messages are scrubbed of key-like strings before they are stored or shown.

## Threat model

| Threat | Mitigation |
|---|---|
| A leaked URL (token in the query string) | Read-only access. No writes without the header. Rotate by replacing `dashboard_token.txt` and restarting the dashboard. The URL then changes, and the owner must be told |
| Prompt injection through the LLM reply | The reply is parsed as strict JSON. The strategy must be a known name, params are clamped and step-limited and apply to shadow books only, and a deterministic gate decides. Nothing in the reply is executed |
| A compromised LLM provider | It only ever sees metrics and recent paper trades |
| Disk filling up | Critical disk probe halts trading before the disk is full |
| Path traversal on archive download | Names must match `part-NNNN-YYYYMMDD-YYYYMMDD.(tar.zst\|tar.gz\|manifest.json)` |
| Secrets in git history | `.gitignore` covers data, secrets, keys, tokens, archives and logs. The public branch was built from a clean tree and scanned with gitleaks |

## Reporting

Contact the repository owner directly. Don't open public issues containing tokens or keys.
