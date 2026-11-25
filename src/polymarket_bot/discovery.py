from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from .client import GatewayError, PublicClient
from .config import Config
from .models import Market, money, parse_iso

log = logging.getLogger(__name__)

DEFAULT_SLUG_PREFIX = {
    "btc": "cpc-btc-updown",
    "eth": "cpceth-eth-updown",
    "sol": "cpcsol-sol-updown",
    "xrp": "cpcxrp-xrp-updown",
}

# Rediscover this many seconds before a window ends so the next slug is in hand.
ROLLOVER_LEAD_SECONDS = 10.0


def horizon_minutes(horizon: str) -> int:
    h = str(horizon).strip().lower()
    if h.endswith("m") and h[:-1].isdigit():
        return int(h[:-1])
    if h.endswith("h") and h[:-1].isdigit():
        return int(h[:-1]) * 60
    if h.endswith("d") and h[:-1].isdigit():
        return int(h[:-1]) * 1440
    raise ValueError(f"unsupported horizon {horizon!r}")


def window_start_utc(now: datetime, horizon: str, offset: int = 0) -> datetime:
    """Floor `now` to the horizon boundary in UTC, then add `offset` windows."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    step = horizon_minutes(horizon) * 60
    epoch = int(now.timestamp())
    start = (epoch // step + offset) * step
    return datetime.fromtimestamp(start, timezone.utc)


def expected_slug(asset: str, horizon: str, start: datetime, prefixes: Optional[dict[str, str]] = None) -> Optional[str]:
    asset = asset.lower()
    prefix = (prefixes or {}).get(asset) or DEFAULT_SLUG_PREFIX.get(asset)
    if not prefix:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start = start.astimezone(timezone.utc)
    return f"{prefix}-{horizon}-{start.strftime('%Y-%m-%d-%H%M')}z"


def learn_prefixes(markets: Iterable[Market]) -> dict[str, str]:
    prefixes = dict(DEFAULT_SLUG_PREFIX)
    for m in markets:
        marker = f"-{m.horizon}-"
        if m.slug and marker in m.slug:
            prefixes[m.asset] = m.slug.split(marker)[0]
    return prefixes


def _parse_market(raw: dict, cfg: Config) -> Optional[Market]:
    apt = raw.get("assetPriceTerms") or {}
    if apt.get("marketType") != cfg.market_type:
        return None
    horizon = apt.get("horizon")
    asset = ((apt.get("asset") or {}).get("symbol") or "").lower()
    if asset not in cfg.assets or horizon not in cfg.horizons:
        return None
    start = parse_iso(apt.get("windowStart"))
    end = parse_iso(apt.get("windowEnd"))
    if start is None or end is None:
        return None
    try:
        decimals = int(apt.get("priceDecimals") or 2)
    except (TypeError, ValueError):
        decimals = 2
    try:
        min_qty = float(raw.get("minimumTradeQty") or 0.01)
    except (TypeError, ValueError):
        min_qty = 0.01
    try:
        tick = float(raw.get("orderPriceMinTickSize") or 0.01)
    except (TypeError, ValueError):
        tick = 0.01
    return Market(
        id=str(raw.get("id") or ""),
        slug=str(raw.get("slug") or ""),
        question=str(raw.get("question") or raw.get("title") or ""),
        asset=asset,
        horizon=str(horizon),
        index_symbol=str(apt.get("indexSymbol") or ""),
        window_start=start,
        window_end=end,
        price_to_beat=money(apt.get("priceToBeat")),
        price_decimals=decimals,
        fee_coefficient=float(raw["feeCoefficient"]) if raw.get("feeCoefficient") is not None else None,
        min_trade_qty=min_qty,
        tick_size=tick,
        status=str(raw.get("status") or ""),
        active=bool(raw.get("active")),
        closed=bool(raw.get("closed")),
        description=str(raw.get("description") or ""),
    )


def _placeholder(asset: str, horizon: str, start: datetime, slug: Optional[str]) -> Market:
    minutes = horizon_minutes(horizon)
    end = start + timedelta(minutes=minutes)
    return Market(
        id="",
        slug=slug or "",
        question="",
        asset=asset,
        horizon=horizon,
        index_symbol="",
        window_start=start,
        window_end=end,
        price_to_beat=None,
        price_decimals=2,
        fee_coefficient=None,
        min_trade_qty=0.01,
        tick_size=0.01,
        status="expected",
        active=False,
        closed=False,
    )


def _from_markets_list(client: PublicClient, cfg: Config) -> list[Market]:
    found: list[Market] = []
    offset = 0
    limit = 100
    while True:
        page = client.list_markets(
            categories=cfg.category,
            closed=False,
            limit=limit,
            offset=offset,
        )
        for raw in page:
            m = _parse_market(raw, cfg)
            if m and m.slug:
                found.append(m)
        if len(page) < limit:
            break
        offset += limit
        if offset > 5000:
            log.warning("discovery pagination hit safety cap at offset %s", offset)
            break
    return found


def _from_events(client: PublicClient, cfg: Config) -> list[Market]:
    found: list[Market] = []
    try:
        data = client.get("/v1/events", {"categories": cfg.category, "closed": False, "limit": 200})
    except Exception as e:
        log.debug("events list failed: %s", e)
        return found
    rows = data if isinstance(data, list) else (data.get("events") or data.get("data") or [])
    for ev in rows or []:
        if not isinstance(ev, dict):
            continue
        for raw in ev.get("markets") or ev.get("eventMarkets") or ev.get("market") or []:
            if isinstance(raw, dict):
                m = _parse_market(raw, cfg)
                if m and m.slug:
                    found.append(m)
    if found:
        log.info("events endpoint added %d up/down markets", len(found))
    return found


def _fetch_slug(client: PublicClient, cfg: Config, slug: str) -> Optional[Market]:
    try:
        raw = client.get_market_by_slug(slug)
    except GatewayError as e:
        if getattr(e, "status", None) == 404:
            log.debug("market slug %s not created yet", slug)
            return None
        log.warning("market slug %s failed: %s", slug, e)
        return None
    except Exception:
        log.exception("market slug %s failed", slug)
        return None
    if not raw:
        return None
    m = _parse_market(raw, cfg)
    if m and m.slug:
        return m
    # Market exists but didn't parse (type mismatch etc.)
    return None


def discover_open_markets(client: PublicClient, cfg: Config, now: Optional[datetime] = None) -> list[Market]:
    """Find current and next Up/Down windows, including ones the list API has not flipped yet."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)

    by_slug: dict[str, Market] = {}
    for m in _from_markets_list(client, cfg) + _from_events(client, cfg):
        by_slug[m.slug] = m

    prefixes = learn_prefixes(by_slug.values())
    # Current and next expected slugs — next is fetched early so rollover is instant.
    for asset in cfg.assets:
        for horizon in cfg.horizons:
            for offset in (0, 1):
                start = window_start_utc(now, horizon, offset)
                slug = expected_slug(asset, horizon, start, prefixes)
                if not slug or slug in by_slug:
                    continue
                m = _fetch_slug(client, cfg, slug)
                if m:
                    by_slug[m.slug] = m
                    log.info("slug lookahead %s status=%s ptb=%s closed=%s", m.slug, m.status, m.price_to_beat, m.closed)
                elif offset == 0:
                    # Keep a placeholder so the dashboard can show the live window, not the expired one.
                    by_slug[slug] = _placeholder(asset, horizon, start, slug)
                    log.info("expected current window %s not in API yet — placeholder", slug)

    # Drop windows that have already ended. Never keep an expired 5m as "the" market.
    live: list[Market] = []
    for m in by_slug.values():
        if m.window_end <= now:
            continue
        live.append(m)

    markets = sorted(live, key=lambda m: (m.asset, m.horizon, m.window_start))
    log.info(
        "discovered %d current/next up/down markets: %s",
        len(markets),
        ", ".join(f"{m.asset}:{m.horizon}:{m.slug.split('-')[-1]}" for m in markets) or "(none)",
    )
    return markets


def current_market(markets: Iterable[Market], asset: str, horizon: str, now: datetime) -> Optional[Market]:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    candidates = [
        m for m in markets
        if m.asset == asset and m.horizon == horizon and m.window_start <= now < m.window_end
    ]
    if candidates:
        # Prefer one that already has a strike / is marked open
        candidates.sort(key=lambda m: (m.price_to_beat is None, m.closed, m.slug))
        return candidates[0]
    return None


def next_market(markets: Iterable[Market], asset: str, horizon: str, now: datetime) -> Optional[Market]:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    future = [
        m for m in markets
        if m.asset == asset and m.horizon == horizon and m.window_start > now
    ]
    future.sort(key=lambda m: m.window_start)
    return future[0] if future else None


def should_rediscover(markets: list[Market], cfg: Config, now: Optional[datetime] = None, lead: float = ROLLOVER_LEAD_SECONDS) -> bool:
    """True if any live window is missing or within `lead` seconds of ending."""
    if not markets:
        return True
    now = now or datetime.now(timezone.utc)
    for asset in cfg.assets:
        for horizon in cfg.horizons:
            cur = current_market(markets, asset, horizon, now)
            if cur is None:
                return True
            if cur.seconds_remaining(now) <= lead:
                return True
            if cur.price_to_beat is None and cur.seconds_remaining(now) < horizon_minutes(horizon) * 60:
                # Window already started but strike not filled in — keep hitting slug/list.
                return True
    return False
