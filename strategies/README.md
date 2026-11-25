# Strategies

This folder is where trading rules live. The bot does not hard-code edge, size, or daily risk — it loads them from the YAML file named in `config.yaml`:

```yaml
strategy:
  name: default
  file: strategies/default.yaml
```

## Files

| File | Role |
|---|---|
| `strategies/default.yaml` | Active default: assets, horizons, edge/spread/liquidity rules, Kelly caps, daily +5% / −10% stops |
| `config.yaml` (repo root) | Paper mode, bankroll, API hosts, storage paths, and *which* strategy file to use |

## How to change a strategy

1. Edit `strategies/default.yaml`, or copy it to `strategies/my-variant.yaml` and point `strategy.file` at the copy.
2. Git-commit the change so you can compare later (`git log -p -- strategies/`).
3. If the all-day bot is running, stop it and restart so it reloads the file. It does not hot-reload mid-process.

Do not put API keys in strategy files.
