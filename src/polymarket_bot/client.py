from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

log = logging.getLogger(__name__)

# Hard caps so a hung gateway cannot occupy a worker longer than the
# discovery heartbeat threshold (25s). 4 attempts with 2**n sleep and a
# 5s timeout was 5+1+5+2+5+4+5 = 27s — just over the line.
DEFAULT_ATTEMPTS = 2
MAX_ATTEMPTS = 3
BACKOFF_CAP_SECONDS = 1.0


class RateLimiter:
    def __init__(self, max_rps: float):
        self.min_interval = 1.0 / max(0.1, max_rps)
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        gap = self._last + self.min_interval - now
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()


class GatewayError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} for {url}: {body[:300]}")


class PublicClient:
    """Read-only client for the official Polymarket US public gateway."""

    def __init__(self, base: str, user_agent: str, timeout: float, max_rps: float):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.limiter = RateLimiter(max_rps)
        self.user_agent = user_agent

    def get(self, path: str, params: Optional[dict] = None, attempts: int | None = None) -> Any:
        url = self.base + path
        if params:
            # drop Nones; support repeated keys via list values
            items = []
            for k, v in params.items():
                if v is None:
                    continue
                if isinstance(v, (list, tuple)):
                    for item in v:
                        items.append((k, str(item)))
                elif isinstance(v, bool):
                    items.append((k, "true" if v else "false"))
                else:
                    items.append((k, str(v)))
            url = url + "?" + urllib.parse.urlencode(items)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            },
        )
        n = DEFAULT_ATTEMPTS if attempts is None else int(attempts)
        n = max(1, min(n, MAX_ATTEMPTS))
        last_err: Optional[Exception] = None
        for attempt in range(n):
            self.limiter.wait()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace") if e.fp else ""
                last_err = GatewayError(e.code, url, body)
                if e.code == 404:
                    raise last_err from e
                if e.code == 429 or e.code >= 500:
                    delay = min(BACKOFF_CAP_SECONDS, 0.25 * (2 ** attempt))
                    log.warning("HTTP %s for %s (attempt %s/%s), retry in %.2fs", e.code, path, attempt + 1, n, delay)
                    if attempt + 1 < n:
                        time.sleep(delay)
                        continue
                raise last_err from e
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
                last_err = e
                delay = min(BACKOFF_CAP_SECONDS, 0.25 * (2 ** attempt))
                log.warning("%s for %s (attempt %s/%s), retry in %.2fs", type(e).__name__, path, attempt + 1, n, delay)
                if attempt + 1 < n:
                    time.sleep(delay)
        raise last_err or RuntimeError(f"request failed: {url}")

    def list_markets(self, **params) -> list[dict]:
        data = self.get("/v1/markets", params)
        return data.get("markets") or []

    def get_market_by_slug(self, slug: str, attempts: int = 1) -> Optional[dict]:
        # Lookahead covers many slugs; one timeout must not multiply into a stall.
        data = self.get(f"/v1/market/slug/{urllib.parse.quote(slug)}", attempts=attempts)
        return data.get("market") or data

    def bbo(self, slug: str) -> dict:
        return self.get(f"/v1/markets/{urllib.parse.quote(slug)}/bbo")

    def book(self, slug: str) -> dict:
        return self.get(f"/v1/markets/{urllib.parse.quote(slug)}/book")

    def asset_price_history(
        self,
        assets: list[str],
        start: int,
        end: int,
        bucket: str,
    ) -> dict:
        return self.get(
            "/v1/asset-prices/history",
            {"assets": assets, "from": start, "to": end, "bucket": bucket},
        )
