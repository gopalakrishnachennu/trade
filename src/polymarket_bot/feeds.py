from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Optional

from .client import GatewayError, PublicClient
from .config import Config
from .models import Book, BookLevel, PricePoint, money

log = logging.getLogger(__name__)


class PriceFeed:
    """Official CF Benchmarks index values via the public Asset Prices API."""

    def __init__(self, client: PublicClient, cfg: Config):
        self.client = client
        self.cfg = cfg
        self.series: dict[str, list[PricePoint]] = defaultdict(list)
        self.last_fetch_ok: dict[str, bool] = {}

    def poll(self, assets: list[str]) -> None:
        now = int(time.time())
        start = now - self.cfg.price_lookback_seconds
        keys = [f"crypto:{a}" for a in assets]
        data = self.client.asset_price_history(keys, start, now, self.cfg.price_bucket)
        for series in data.get("series") or []:
            symbol = ((series.get("asset") or {}).get("symbol") or "").lower()
            if not symbol:
                continue
            points = []
            for c in series.get("candles") or []:
                try:
                    points.append(
                        PricePoint(
                            ts=int(c["timestamp"]),
                            close=float(c["close"]),
                            tick_count=int(c.get("tickCount") or 0),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            points.sort(key=lambda p: p.ts)
            self.series[symbol] = points
            self.last_fetch_ok[symbol] = True

    def points(self, asset: str) -> list[PricePoint]:
        return self.series.get(asset, [])

    def latest(self, asset: str) -> Optional[PricePoint]:
        pts = self.points(asset)
        return pts[-1] if pts else None


def _levels(rows) -> list[BookLevel]:
    out = []
    for row in rows or []:
        px = money(row.get("px") or row.get("price"))
        qty = money(row.get("qty") or row.get("quantity") or row.get("size"))
        if px is None or qty is None:
            continue
        out.append(BookLevel(price=px, qty=qty))
    return out


def parse_book(slug: str, bbo_payload: dict, book_payload: Optional[dict] = None) -> Book:
    md = (bbo_payload or {}).get("marketData") or bbo_payload or {}
    book_md = ((book_payload or {}).get("marketData") or book_payload or {}) if book_payload else {}
    # When only the book endpoint is available, quotes live in the bid/offer arrays.
    if not book_md and (md.get("bids") or md.get("offers") or md.get("asks")):
        book_md = md
    bids = _levels(book_md.get("bids"))
    offers = _levels(book_md.get("offers") or book_md.get("asks"))
    best_bid = money(md.get("bestBid"))
    best_ask = money(md.get("bestAsk"))
    if best_bid is None and bids:
        best_bid = max(lvl.price for lvl in bids)
    if best_ask is None and offers:
        best_ask = min(lvl.price for lvl in offers)
    bid_shares = money(md.get("bidShares"))
    ask_shares = money(md.get("askShares"))
    if bid_shares is None and bids:
        bid_shares = sum(lvl.qty for lvl in bids)
    if ask_shares is None and offers:
        ask_shares = sum(lvl.qty for lvl in offers)
    return Book(
        slug=slug,
        best_bid=best_bid,
        best_ask=best_ask,
        long_quote=money(md.get("longQuote")),
        short_quote=money(md.get("shortQuote")),
        bid_shares=float(bid_shares or 0.0),
        ask_shares=float(ask_shares or 0.0),
        last_trade=money(md.get("lastTradePx")),
        bids=bids,
        offers=offers,
        raw_ts=(md.get("lastPriceSample") or {}).get("ts") if isinstance(md.get("lastPriceSample"), dict) else None,
    )


def _has_quotes(book: Optional[Book]) -> bool:
    if book is None:
        return False
    return any(v is not None for v in (book.best_bid, book.best_ask, book.long_quote, book.short_quote, book.up_ask, book.down_ask))


class BookFeed:
    def __init__(self, client: PublicClient):
        self.client = client
        self.books: dict[str, Book] = {}
        self.window_start_unix: dict[str, float] = {}
        self.first_quote_unix: dict[str, float] = {}
        self.quote_source: dict[str, str] = {}

    def note_window(self, slug: str, window_start) -> None:
        if not slug:
            return
        ts = window_start.timestamp() if hasattr(window_start, "timestamp") else float(window_start)
        # Keep the earliest start we were told (don't reset lag if a later object arrives)
        prev = self.window_start_unix.get(slug)
        if prev is None or ts < prev:
            self.window_start_unix[slug] = ts

    def _record_quote(self, slug: str, book: Book, source: str) -> None:
        if slug in self.first_quote_unix or not _has_quotes(book):
            return
        now = time.time()
        self.first_quote_unix[slug] = now
        self.quote_source[slug] = source
        start = self.window_start_unix.get(slug)
        lag = None if start is None else now - start
        log.info(
            "first quotes for %s via %s, %.1fs after window open",
            slug, source, -1.0 if lag is None else lag,
        )

    def poll_bbo(self, slug: str) -> Optional[Book]:
        """Poll BBO, then the book endpoint if BBO 404s or has no quotes."""
        bbo_err = None
        book = None
        source = None
        try:
            payload = self.client.bbo(slug)
            book = parse_book(slug, payload)
            source = "bbo"
        except GatewayError as e:
            bbo_err = e
            if e.status != 404:
                log.warning("bbo failed for %s: %s", slug, e)
        except Exception:
            log.exception("bbo failed for %s", slug)

        if not _has_quotes(book):
            try:
                payload = self.client.book(slug)
                book2 = parse_book(slug, {}, payload)
                if _has_quotes(book2) or book2.bids or book2.offers:
                    book = book2
                    source = "book"
                    if bbo_err and bbo_err.status == 404:
                        log.info("book endpoint used for %s after bbo 404", slug)
            except GatewayError as e:
                if e.status != 404:
                    log.warning("book failed for %s: %s", slug, e)
                elif bbo_err and bbo_err.status == 404:
                    log.warning("bbo and book both 404 for %s", slug)
            except Exception:
                log.exception("book failed for %s", slug)

        if book is None:
            return None
        self.books[slug] = book
        if source:
            self._record_quote(slug, book, source)
        return book

    def get(self, slug: str) -> Optional[Book]:
        return self.books.get(slug)

    def lag_seconds(self, slug: str) -> Optional[float]:
        start = self.window_start_unix.get(slug)
        first = self.first_quote_unix.get(slug)
        if start is None or first is None:
            return None
        return round(first - start, 2)

    def lag_report(self) -> list[dict]:
        rows = []
        for slug, first in self.first_quote_unix.items():
            start = self.window_start_unix.get(slug)
            rows.append({
                "slug": slug,
                "source": self.quote_source.get(slug),
                "lag_seconds": None if start is None else round(first - start, 2),
            })
        return rows
