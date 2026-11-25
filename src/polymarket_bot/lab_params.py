"""Apply clamped reviewer tweaks to shadow strategy params (never to the live risk config)."""

from __future__ import annotations

from pathlib import Path

from . import strategies as strat
from .strategy_lab import read_params, write_params


def apply_tweaks(data_dir: Path, tweaks: dict[str, dict]) -> dict[str, dict]:
    params = read_params(data_dir)
    for name, p in tweaks.items():
        if name in params:
            params[name] = strat.clamp_params(p, params[name])
    write_params(data_dir, params)
    return params
