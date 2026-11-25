from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def _get(d: dict, *keys, default=None):
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


@dataclass(frozen=True)
class Config:
    raw: dict
    root: Path

    @property
    def mode(self) -> str:
        return str(self.raw.get("mode", "paper"))

    @property
    def run_seconds(self) -> int:
        return int(self.raw.get("run_seconds", 0))

    @property
    def stop_at(self) -> str | None:
        v = self.raw.get("stop_at")
        return str(v) if v else None

    @property
    def strategy_name(self) -> str:
        return str(_get(self.raw, "strategy", "name", default="default"))

    @property
    def log_level(self) -> str:
        return str(self.raw.get("log_level", "INFO"))

    @property
    def timezone(self) -> str:
        return str(self.raw.get("timezone", "America/New_York"))

    @property
    def public_base(self) -> str:
        return str(_get(self.raw, "api", "public_base", default="https://gateway.polymarket.us")).rstrip("/")

    @property
    def timeout_seconds(self) -> float:
        return float(_get(self.raw, "api", "timeout_seconds", default=15))

    @property
    def user_agent(self) -> str:
        return str(_get(self.raw, "api", "user_agent", default="polymarket-bot-paper/0.1"))

    @property
    def max_rps(self) -> float:
        return float(_get(self.raw, "api", "max_requests_per_second", default=8))

    @property
    def assets(self) -> tuple[str, ...]:
        return tuple(a.lower() for a in _get(self.raw, "discovery", "assets", default=["btc", "eth", "sol", "xrp"]))

    @property
    def horizons(self) -> tuple[str, ...]:
        return tuple(_get(self.raw, "discovery", "horizons", default=["5m", "15m", "1h"]))

    @property
    def market_type(self) -> str:
        return str(_get(self.raw, "discovery", "market_type", default="ASSET_PRICE_MARKET_TYPE_UP_DOWN"))

    @property
    def discovery_refresh_seconds(self) -> float:
        return float(_get(self.raw, "discovery", "refresh_seconds", default=15))

    @property
    def category(self) -> str:
        return str(_get(self.raw, "discovery", "category", default="crypto"))

    @property
    def price_poll_seconds(self) -> float:
        return float(_get(self.raw, "feeds", "price_poll_seconds", default=1.0))

    @property
    def book_poll_seconds(self) -> float:
        return float(_get(self.raw, "feeds", "book_poll_seconds", default=2.0))

    @property
    def price_lookback_seconds(self) -> int:
        return int(_get(self.raw, "feeds", "price_lookback_seconds", default=900))

    @property
    def price_bucket(self) -> str:
        return str(_get(self.raw, "feeds", "price_bucket", default="ASSET_PRICE_BUCKET_1S"))

    @property
    def starting_cash(self) -> float:
        return float(_get(self.raw, "bankroll", "starting_cash_usd", default=1000.0))

    @property
    def fee_theta(self) -> float:
        return float(_get(self.raw, "fees", "theta", default=0.095))

    @property
    def vol_lookback_seconds(self) -> int:
        return int(_get(self.raw, "volatility", "lookback_seconds", default=900))

    def vol_floor(self, asset: str) -> float:
        d = _get(self.raw, "volatility", "annualized_floor", default={}) or {}
        return float(d.get(asset, 0.5))

    def vol_ceiling(self, asset: str) -> float:
        d = _get(self.raw, "volatility", "annualized_ceiling", default={}) or {}
        return float(d.get(asset, 2.0))

    @property
    def min_edge(self) -> float:
        return float(_get(self.raw, "rules", "min_edge_after_fees", default=0.04))

    @property
    def max_spread(self) -> float:
        return float(_get(self.raw, "rules", "max_spread", default=0.06))

    @property
    def min_liquidity_contracts(self) -> float:
        return float(_get(self.raw, "rules", "min_liquidity_contracts", default=50))

    @property
    def min_liquidity_usd(self) -> float:
        return float(_get(self.raw, "rules", "min_liquidity_usd", default=25))

    @property
    def no_trade_final_seconds(self) -> float:
        return float(_get(self.raw, "rules", "no_trade_final_seconds", default=15))

    @property
    def require_price_to_beat(self) -> bool:
        return bool(_get(self.raw, "rules", "require_price_to_beat", default=True))

    @property
    def min_seconds_into_window(self) -> float:
        return float(_get(self.raw, "rules", "min_seconds_into_window", default=5))

    @property
    def min_ask(self) -> float:
        return float(_get(self.raw, "rules", "min_ask", default=0.05))

    @property
    def max_ask(self) -> float:
        return float(_get(self.raw, "rules", "max_ask", default=0.90))

    @property
    def kelly_fraction(self) -> float:
        return float(_get(self.raw, "sizing", "kelly_fraction", default=0.25))

    @property
    def max_fraction_of_bankroll(self) -> float:
        return float(_get(self.raw, "sizing", "max_fraction_of_bankroll", default=0.05))

    @property
    def max_contracts(self) -> float:
        return float(_get(self.raw, "sizing", "max_contracts", default=25))

    @property
    def max_per_bet_usd(self) -> float:
        return float(_get(self.raw, "risk", "max_per_bet_usd", default=50))

    @property
    def daily_profit_target_pct(self) -> float:
        return float(_get(self.raw, "risk", "daily_profit_target_pct", default=0.05))

    @property
    def daily_loss_limit_pct(self) -> float:
        return float(_get(self.raw, "risk", "daily_loss_limit_pct", default=0.10))

    @property
    def window_mode(self) -> str:
        v = str(_get(self.raw, "risk", "window_mode", default="rolling_24h"))
        return v if v in ("rolling_24h", "calendar_day") else "rolling_24h"

    @property
    def window_hours(self) -> float:
        return max(1.0, float(_get(self.raw, "risk", "window_hours", default=24.0)))

    @property
    def pattern_mode(self) -> str:
        v = str(_get(self.raw, "patterns", "mode", default="shadow"))
        return v if v in ("shadow", "active", "off") else "shadow"

    @property
    def pattern_min_samples(self) -> int:
        return max(5, int(_get(self.raw, "patterns", "min_samples", default=20)))

    @property
    def pattern_prior_strength(self) -> float:
        return max(1.0, float(_get(self.raw, "patterns", "prior_strength", default=10.0)))

    @property
    def pattern_min_mult(self) -> float:
        return max(0.1, min(1.0, float(_get(self.raw, "patterns", "min_mult", default=0.25))))

    @property
    def pattern_max_mult(self) -> float:
        return max(1.0, min(2.0, float(_get(self.raw, "patterns", "max_mult", default=1.50))))

    @property
    def pattern_skip_ev_threshold(self) -> float:
        return float(_get(self.raw, "patterns", "skip_ev_threshold", default=-0.02))

    # ---- dynamic profit target (risk.profit_lock)
    @property
    def target_mode(self) -> str:
        v = str(_get(self.raw, "risk", "profit_lock", "mode", default="profit_lock"))
        return v if v in ("fixed", "profit_lock") else "fixed"

    @property
    def lock_ratio(self) -> float:
        return float(_get(self.raw, "risk", "profit_lock", "lock_ratio", default=0.70))

    @property
    def locked_size_mult(self) -> float:
        return float(_get(self.raw, "risk", "profit_lock", "locked_size_mult", default=0.5))

    @property
    def adaptive_target(self) -> bool:
        return bool(_get(self.raw, "risk", "profit_lock", "adaptive", default=True))

    @property
    def target_min_pct(self) -> float:
        return float(_get(self.raw, "risk", "profit_lock", "target_min_pct", default=0.03))

    @property
    def target_max_pct(self) -> float:
        return float(_get(self.raw, "risk", "profit_lock", "target_max_pct", default=0.10))

    @property
    def adaptive_lookback_days(self) -> int:
        return int(_get(self.raw, "risk", "profit_lock", "lookback_days", default=7))

    @property
    def adaptive_min_days(self) -> int:
        return int(_get(self.raw, "risk", "profit_lock", "min_days", default=3))

    @property
    def starting_day_equity(self) -> float:
        v = _get(self.raw, "risk", "starting_day_equity_usd", default=None)
        if v is None:
            return self.starting_cash
        return float(v)

    @property
    def daily_profit_target_usd(self) -> float:
        return self.starting_day_equity * self.daily_profit_target_pct

    @property
    def daily_loss_limit_usd(self) -> float:
        return self.starting_day_equity * self.daily_loss_limit_pct

    @property
    def one_position_per_market(self) -> bool:
        return bool(_get(self.raw, "risk", "one_position_per_market", default=True))

    @property
    def max_concurrent_positions(self) -> int:
        return int(_get(self.raw, "risk", "max_concurrent_positions", default=4))

    @property
    def max_open_exposure_usd(self) -> float:
        return float(_get(self.raw, "risk", "max_open_exposure_usd", default=200))

    @property
    def kill_switch_file(self) -> Path:
        rel = _get(self.raw, "risk", "kill_switch_file", default="data/KILL")
        p = Path(str(rel))
        return p if p.is_absolute() else self.root / p

    def storage_path(self, key: str, default: str) -> Path:
        rel = _get(self.raw, "storage", key, default=default)
        p = Path(str(rel))
        return p if p.is_absolute() else self.root / p

    @property
    def sqlite_path(self) -> Path:
        return self.storage_path("sqlite_path", "data/paper_trades.sqlite")

    @property
    def trades_csv(self) -> Path:
        return self.storage_path("trades_csv", "data/paper_trades.csv")

    @property
    def evaluations_csv(self) -> Path:
        return self.storage_path("evaluations_csv", "data/evaluations.csv")

    @property
    def equity_csv(self) -> Path:
        return self.storage_path("equity_csv", "data/equity.csv")

    @property
    def live_state_path(self) -> Path:
        return self.storage_path("live_state_path", "data/live_state.json")


def deep_merge(base: dict, overlay: dict) -> dict:
    """Merge overlay onto base. Nested dicts are merged; other values replaced."""
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    with path.open() as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Config must be a mapping: {path}")
    if raw.get("mode", "paper") != "paper":
        raise ValueError("This scaffold only supports mode: paper")
    strat = raw.get("strategy") or {}
    rel = strat.get("file") if isinstance(strat, dict) else None
    if rel:
        sp = Path(str(rel))
        if not sp.is_absolute():
            sp = path.parent / sp
        if not sp.exists():
            raise FileNotFoundError(f"Strategy file not found: {sp}")
        with sp.open() as f:
            strategy = yaml.safe_load(f) or {}
        if not isinstance(strategy, dict):
            raise ValueError(f"Strategy must be a mapping: {sp}")
        # Strategy file is the default for trading knobs; config.yaml wins on conflicts.
        raw = deep_merge(strategy, raw)
    return Config(raw=raw, root=path.parent)
