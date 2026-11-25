# Polymarket US: what the exchange offers (research notes)

Checked live against the official public API on 24 Nov 2026 (America/New_York). Official docs are slightly behind production on a few points; where they disagree, this README states both.

### Markets (production, public API)

`GET https://gateway.polymarket.us/v1/markets?categories=crypto&closed=false` returned **1,468** open crypto markets, including:

| Family (`assetPriceTerms.marketType`) | Assets | Horizons seen live |
|---|---|---|
| **Up/Down** (`ASSET_PRICE_MARKET_TYPE_UP_DOWN`) | BTC, ETH, SOL, XRP | **5m, 15m, 1h, and 1d** |
| Above/Below (`…_ABOVE_BELOW`) | BTC, ETH, SOL, XRP | hourly, daily, weekly |
| Price Range (`…_RANGE`) | BTC, ETH, SOL, XRP | hourly, daily, weekly, yearly (BTC) |
| One-Touch (`…_ONE_TOUCH`) | BTC, ETH, SOL, XRP | weekly, monthly, yearly (BTC) |
| Hand-listed (`marketType: "futures"`) | mostly BTC | e.g. “When will Bitcoin hit $150k?” |

The bot only trades the **Up/Down 5m / 15m / 1h** set. At any moment the public list typically shows **the currently live window** for each asset/horizon (12 markets), not the full 12-hour/24-hour pipeline described in the docs.

Written market rules (example, BTC 5m) say: the window settles **Up** if the official index at the end is **greater than or equal to** the official index at the open. A flat print settles Up.

Docs still emphasize Bitcoin 15-minute and 60-minute windows and say automated families “roll out to production as announced”:

- [Crypto FAQs](https://docs.polymarket.us/faqs/crypto-faqs)
- [Crypto Schema](https://docs.polymarket.us/trader-guide/crypto-schema)

The live venue and API already list **5m and 1d** windows and **ETH, SOL, XRP** as well. Do not treat the FAQ cadence table as exhaustive.

### Settlement price source

These crypto markets settle on **CF Benchmarks real-time indices**, not Binance/Coinbase spot and **not Chainlink**. (Chainlink streams are used for Polymarket US **metals** and **oil/gas** markets, which is probably why some secondary articles mix the sources.)

| Asset | `assetPriceTerms.indexSymbol` | Official index page |
|---|---|---|
| BTC | `BRTI` | https://www.cfbenchmarks.com/data/indices/BRTI |
| ETH | `ETHUSD_RTI` | https://www.cfbenchmarks.com/data/indices/ETHUSD_RTI |
| SOL | `SOLUSD_RTI` | https://www.cfbenchmarks.com/data/indices/SOLUSD_RTI |
| XRP | `XRPUSD_RTI` | https://www.cfbenchmarks.com/data/indices/XRPUSD_RTI |

Statistic (from official docs and from the market `description` text): the **simple average of the 60 one-second index prints in `[T − 60s, T)`**, rounded to the market’s `priceDecimals`. Up/Down computes that twice (window open = `priceToBeat`, window close = settlement). Incomplete data delays settlement.

CF Benchmarks’ own API is **licensed, not free**: https://docs.cfbenchmarks.com/api/

**Free official feed we use:** Polymarket US’s public Asset Prices API, which serves the same reference series the markets settle on:

- Docs: https://docs.polymarket.us/api-reference/asset-prices/overview
- `GET https://gateway.polymarket.us/v1/asset-prices/history?assets=crypto:btc&assets=crypto:eth&assets=crypto:sol&assets=crypto:xrp&from=<unix>&to=<unix>&bucket=ASSET_PRICE_BUCKET_1S`
- No authentication. Confirmed working from this environment for all four assets.

A `1S` crypto candle currently comes back with identical OHLC and `tickCount: 60`. The bot still follows the documented rule (average of 60 `close` values in `[T-60, T)`). Treat those candles as the official series, not as an exchange spot price. A vol **floor** is applied so a smooth/rolling series cannot make the probability model overconfident.

### Retail API (the one a Polymarket US account uses)

Documented split: https://docs.polymarket.us/api-reference/introduction

| Role | Base URL | Auth |
|---|---|---|
| Public market data | `https://gateway.polymarket.us` | none |
| Trading, portfolio, account | `https://api.polymarket.us` | API key (Ed25519) |
| Market-data WebSocket | `wss://api.polymarket.us/v1/ws/markets` | **API key required** |
| Private WebSocket | `wss://api.polymarket.us/v1/ws/private` | **API key required** |

Useful public REST (no key), all under `/v1`:

- `GET /v1/markets` — list/filter; `?categories=crypto&closed=false`
- `GET /v1/market/slug/{slug}`
- `GET /v1/markets/{slug}/bbo` and `.../book`
- `GET /v1/markets/{slug}/settlement`
- `GET /v1/asset-prices/history`
- `GET /v1/events`, `GET /v1/search`, `GET /v1/price-history`

Some of those also answer on `https://api.polymarket.us` without a key (BBO did). `GET /v1/asset-prices/history` on `api.polymarket.us` returned **401 Missing required API key headers**. The scaffold uses **gateway** only.

**Auth (trading):** not OAuth. Create a Key ID + Secret Key at [polymarket.us/developer](https://polymarket.us/developer) after creating an account in the **iOS app** and finishing identity verification. Docs: https://docs.polymarket.us/api-reference/authentication

Raw request headers:

- `X-PM-Access-Key` — Key ID
- `X-PM-Timestamp` — milliseconds
- `X-PM-Signature` — base64 Ed25519 signature of `timestamp + method + path` using the secret (first 32 bytes of the base64-decoded secret). Timestamps must be within 30 seconds.

**Yes, a US account can trade via the API** once KYC is approved and keys are generated. Official SDKs:

- Python: `pip install polymarket-us` — https://docs.polymarket.us/api-reference/sdks/introduction — https://github.com/Polymarket/polymarket-us-python — https://pypi.org/project/polymarket-us/
- TypeScript: `npm install polymarket-us`

**Order types** (https://docs.polymarket.us/api-reference/sdks/typescript/orders): `ORDER_TYPE_LIMIT` or `ORDER_TYPE_MARKET`. Intents: `BUY_LONG` (Up/Yes), `SELL_LONG`, `BUY_SHORT` (Down/No), `SELL_SHORT`. Time in force: GTC, GTD, IOC, FOK. Also: preview, modify, cancel, cancel-all, close-position.

**Rate limits** (https://docs.polymarket.us/api-reference/rate-limits): general retail cap **25 requests/s** per source IP / Cloudflare location (keys on the same IP share the bucket). HTTP 429 on exceed. Some RFQ routes are 1/s or 400/s. Docs tell you to use WebSockets instead of polling for production. WebSockets require keys, so paper mode polls REST slowly (default 8 req/s).

**Fees** (https://docs.polymarket.us/fees):

```
Fee = Θ × contracts × price × (1 − price)
```

Banker's rounding to the nearest cent. Retail taker Θ by 30-day volume: 0.095 / 0.085 / 0.075 / 0.0695. Markets currently advertise `feeCoefficient: 0.095` (Tier 1). From 21 Nov 2026 12:01 AM ET through 31 Dec 2026, **API taker** trades use Θ = 0.0695 and get a **70% rebate** on those fees. This paper config uses **0.095** on purpose so edge is not overstated.

There is a separate **Institutional / Exchange Gateway** API (`https://api.prod.polymarketexchange.com`, Bearer tokens, gRPC/FIX) documented at https://docs.polymarket.us/institutional/introduction. That is for firms, not a normal Polymarket US app account.

### Contrast: polymarket.com (international)

Different venue, different APIs, not usable by a Polymarket US account.

- Docs: https://docs.polymarket.com/getting-started/api
- Gamma (discovery, no auth): `https://gamma-api.polymarket.com`
- CLOB (books + trading): `https://clob.polymarket.com`
- Data API: `https://data-api.polymarket.com`
- Market WebSocket (public): `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- User WebSocket (auth): `wss://ws-subscriptions-clob.polymarket.com/ws/user`
- CLOB trading auth is wallet-derived (EIP-712 L1 + HMAC L2), not Ed25519 API keys
- Python trading client: [`py-clob-client`](https://github.com/Polymarket/py-clob-client)

Do not point this bot at those hosts.
