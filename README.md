# tefas-api

A direct, rate-limit-safe Python client for **TEFAS** (Türkiye Elektronik Fon Alım Satım Platformu).

TEFAS publishes no official API. This package talks to the undocumented JSON endpoints behind
`tefas.gov.tr` — no key, no login, no browser automation, no scraping of HTML — and is built around
one measured fact: **a single request returns every fund for a whole month.**

```python
from tefas import Tefas

with Tefas() as api:
    rows = api.prices("2026-08-01", "2026-08-26")   # 2,044 funds x 20 days, 1 request
```

## Why not one of the existing libraries

TEFAS replaced its site with a Next.js front end in 2026 and retired the old `fundturkey.com.tr`
endpoints, which broke every client built before then. The most popular survivor migrated to a
**per-fund** endpoint on the premise that the API "no longer supports a single bulk-by-date
request". That premise is wrong — the bulk endpoint is alive and is what this package uses — and
the cost of believing it is one HTTP request per fund, a 50-fund cap, and the loss of portfolio
size, share count, investor count and the entire allocation breakdown.

Against a five-requests-per-minute limit, per-fund fetching cannot finish. Bulk fetching costs two
requests a day.

## Install

```bash
pip install -e .              # from a clone
pip install -e '.[pandas]'    # with DataFrame support
```

Requires Python 3.9+. The only hard dependency is `requests`.

## The measured facts this is built on

Verified against the live service on 27 Aug 2026:

| | |
|---|---|
| Bulk query result | 2,044 funds × 20 trading days = **40,637 rows in one request** (9.5 MB, 2.8 s) |
| Rate limit | **5 requests/minute**; the 6th returns `429` instantly |
| `Retry-After` header | Not sent — clients must back off on their own clock |
| Date range cap | **1 month** per query (`"Tarih aralığı 1 ayı aşamaz"`) |
| Errors | Arrive as **HTTP 200** with `errorMessage` set, not as an error status |
| Gateway | Apinizer — a managed API gateway, so 429 is a configured throttle, not a WAF ban |
| Recovery | Immediate once the window rolls; no cooldown penalty or escalation observed |

The client defaults to 4 requests/minute, one below the ceiling, because the window is shared with
anything else calling TEFAS from the same address.

## Usage

### Prices, allocations, returns

```python
from tefas import Tefas

api = Tefas()

# Daily price, fund size, holder counts. code=None means every fund.
api.prices("2026-08-26")
api.prices("2026-01-01", "2026-08-26")          # auto-split into 28-day windows
api.prices("2026-08-01", "2026-08-26", code="AFA")

# Portfolio allocation, ~58 columns, one per instrument class
api.breakdown("2026-08-26")

# Trailing returns, official risk score (1-7), category
api.returns()

# Prices joined to allocations for one date: everything, in two requests
api.snapshot("2026-08-26")

# Every fund code
api.fund_codes()
```

All methods take `kind=` — `"YAT"` (default), `"EMK"`, `"BYF"`, `"GYF"`, `"GSYF"`, a list of them,
or `None` for all five.

Pass `raw=True` to get TEFAS's original Turkish keys instead of the English column names.

### With pandas

```python
from tefas import Tefas, to_pandas

frame = to_pandas(Tefas().prices("2026-08-01", "2026-08-26"))
```

### Cache it locally

Treat TEFAS as an ingest source, not a query layer:

```python
from tefas import Tefas, Store, backfill, sync

store = Store("tefas.db")

with Tefas() as api:
    backfill(api, store, start=date(2021, 1, 1))   # once, resumable
    sync(api, store)                                # daily, ~2 requests
```

`backfill` records each completed window, so interrupting it and re-running resumes rather than
starting over. `sync` re-fetches a short overlap by default because TEFAS occasionally revises a
published price and late-reporting funds appear after the fact.

## CLI

```bash
tefas sync                        # bring the cache up to date
tefas backfill --years 5          # one-off history load, resumable
tefas funds                       # list funds
tefas show AFA --days 30          # recent prices for one fund
tefas top --by return_1y -n 20    # league table
tefas stats                       # what the cache holds
tefas export prices -o out.csv    # dump a table
tefas kinds                       # list fund types
```

Global flags: `--db`, `--kind`, `--rate`, `--quiet`, `--verbose`.

## Request budget

At the default 4 requests/minute:

| Job | Requests | Time |
|---|---|---|
| Daily sync (prices + allocations) | 2 | ~1 s |
| One year of history | ~26 | ~7 min |
| Five years of history | ~130 | ~33 min |

Run `tefas backfill --years 5` once; after that, `sync` is all you need.

## Gotchas this package handles for you

- **Errors come back as HTTP 200.** A naive `raise_for_status()` sails past them and yields silently
  empty results. The client raises `TefasAPIError` from the response body.
- **Turkish characters.** Responses are UTF-8, but any consumer that falls back to a system codepage
  mangles fund titles. The client decodes explicitly as UTF-8 rather than trusting inference.
- **The one-month cap.** Longer ranges are split automatically into 28-day windows.
- **Required-but-null fields.** The request body needs about a dozen keys present even when empty;
  omitting them makes the backend answer with a `NullPointerException`.
- **`db` vs `dt`.** In the allocation data `db` is *fx payable bills* and `dt` is *government bond* —
  easy to transpose, and this package uses TEFAS's own historical mapping.

## Fund types

| Code | Meaning |
|---|---|
| `YAT` | Securities mutual funds (~2,040) |
| `EMK` | Pension funds (~400) |
| `GYF` | Real estate investment funds (~260) |
| `BYF` | Exchange traded funds (~30) |
| `GSYF` | Venture capital investment funds |

## Field names

Turkish API keys are renamed to stable English column names (`fiyat` → `price`,
`portfoyBuyukluk` → `market_cap`, `hs` → `stock`, and so on). The allocation names follow the mapping
TEFAS itself used on the legacy endpoint, so data collected before and after the 2026 migration stays
comparable.

Eleven allocation keys the new API added have no established English name (`bpp`, `btaa`, `btas`,
`dot`, `gsyy`, `gyy`, `kibd`, `oksyd`, `osdb`, `vint`, `bilFiyat`). They are passed through under
their raw codes rather than guessed at, and listed in `tefas.UNMAPPED_BREAKDOWN_KEYS`. Any key TEFAS
adds in future is passed through the same way instead of being dropped.

## Endpoints

| Endpoint | Returns |
|---|---|
| `POST /api/funds/fonGnlBlgSiraliGetir` | Price, fund size, share and investor counts |
| `POST /api/funds/dagilimSiraliGetirT` | Portfolio allocation, ~58 columns |
| `POST /api/funds/fonGetiriBazliBilgiGetir` | Trailing returns, risk score, category |
| `POST /api/funds/fonFiyatBilgiGetir` | Single-fund history by fixed look-back period |

## A caveat worth designing around

These endpoints are undocumented and unversioned. TEFAS changed them once in 2026 and can do so
again without notice. All of that knowledge is confined to `client.py`, so a break has one place to
be fixed. Consider running the test suite on a schedule as a canary, so you learn about a change
from a monitor rather than from a user.

## Licence

MIT. Not affiliated with or endorsed by TEFAS or Takasbank. Fund data is published by TEFAS; verify
anything you make decisions on against the official source.
