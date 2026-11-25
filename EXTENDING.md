# Extending the bot

Rules for every change: keep it **paper only**, keep it **fail closed**, add tests, run `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`, and commit in small steps. Read INVARIANTS.md first.

## Add a strategy

1. Add an entry to `STRATEGIES` in `src/polymarket_bot/strategies.py` with a description and params, using only keys from `PARAM_BOUNDS`.
2. If you need a new filter, add the param to `PARAM_BOUNDS` and handle it in `evaluate()`. Append a check `{"name": "strategy", "passed": ..., "detail": "<why>"}` so the dashboard can explain a rejection. **Never** skip the base checks.
3. Add tests in `tests/test_extensions.py::StrategyTests`. Each new strategy must fail when data quality fails.
4. Deploy. The lab creates the new shadow book automatically (`data/strategies/shadow/<name>/`). It becomes active only through the gate or a manual approval.

## Add a health probe

1. Write `probe_x(...) -> Item` in `src/polymarket_bot/health.py`. Give it a clear `rule` and `threshold`, and set `critical=True` **only** if trading must stop when it fails.
2. Register it in `HealthMonitor.SCHEDULE` and `_run_probe`.
3. Test the thresholds with injected inputs, as `HealthTests` does. A critical probe needs two consecutive FAILED results before HALT.

## Add an operator control

1. Add a `Field` to `FIELDS` in `src/polymarket_bot/runtime/controls.py`, with bounds and the config `path`. Add a `Config` property if workers need it.
2. Cross-field validation goes in `check_cross`. The UI form is generated from `/api/controls`.
3. Workers pick up the change through `_ControlsWatcher` and report `controls_version`.

## Add an LLM provider

Add it to `PROVIDERS` in `src/polymarket_bot/integrations.py`, using `kind` `openai` (OpenAI-compatible) or `anthropic`. Use **only documented endpoints**. The test call must not bill tokens.

## Add a dashboard page or API

1. Add a route in `web/app.py`. Every route is behind the token middleware. Writes must call `_need_header` (header token, never the query string alone).
2. Pages include `assets/common.css` and `assets/common.js`, which add the nav and the live health pill.
3. Add the path to `NewApiTests.test_public_and_protected` / `test_writes_need_header`.

## Add archived data

Anything written under `data/` should either be safe to grow forever or be covered by `archive.py` (`live_files`, plus a move or copy step that records sha256 and rows in the manifest). CSV writers must use `append_csv_locked`.

## Change the deploy

Keep the same URL and token. Keep Caddy in front of uvicorn on 127.0.0.1. Never put keys in systemd units or in git.

## Propose an analyst rule

1. Inspect `scripts/analyst_snapshot.py` output (attribution, calibration, worst/best contexts, bandit shadow, active rules).
2. Submit with `scripts/propose_rule.py` or POST `/api/rules-lab/propose` (header token). The DSL is validated against `ALLOWED_FIELDS` / `ALLOWED_OPS` in `rules_lab.py` — do not add fields that touch mode, caps, or HALT.
3. The proposal is walk-forward backtested automatically and enters shadow. Promote only through the gate or a manual approve on `/rules-lab`. Auto-promote stays off unless the operator turns it on in Controls.

## Add a learning layer

Compose it in `learning_runtime.decisions_for` / `compose_learning` rather than replacing `PatternStore`. Shadow/off must not change `passed` or the live multiplier. Skip via `passed=False`. Hard caps stay last in `size_order`. Add a Controls `Field` (`off|shadow|active`) and tests that the layer cannot loosen INVARIANTS 1, 2, 3, 7 and 13.

## Add an entry-time feature

Compute it in `patterns.entry_features` from ticks with `ts <= now_ts` only. Add the name to `ENTRY_FEATURE_KEYS` and to `rules_lab.ALLOWED_FIELDS`. Missing values must not match a rule. Backfill via `paths_from_eval_rows` in `iter_counterfactuals`. Do not use the post-entry path (that is attribution, not a live feature).
