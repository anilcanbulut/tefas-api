# tefas-api — notes for agents

Python client for TEFAS (Turkish fund platform). Reads the undocumented JSON API behind
`tefas.gov.tr` directly. No key, no login, no scraping, no browser automation.

Installed **editable and global** on this machine, so `import tefas` and the `tefas` CLI work from
any directory without setup. Edits to this repo take effect immediately — never reinstall.

## Three rules that cause real bugs if ignored

**1. Always pass an absolute database path.** The default is `tefas.db` *relative to the current
directory*. Working in a different folder silently creates a second, empty cache — which looks
exactly like "TEFAS has no data" and wastes an afternoon.

```python
Store(r"C:\Users\anilc\tefas\tefas.db")     # not Store()
```

**2. Only one process may call TEFAS at a time.** The rate limiter is per-process but the limit is
per-IP: 5 requests/minute, shared by everything on this machine. Two agents syncing concurrently
will 429 each other. Reading the cache is unlimited and safe in parallel.

**3. Never fetch fund-by-fund.** `fonKodu: null` returns *every* fund for the whole window in one
request — 2,044 funds over 20 days = 40,637 rows in 2.8s. Looping over fund codes needs ~2,000x more
requests for data one call already contains, and cannot finish inside the rate limit. If you find
yourself writing `for code in codes: api.prices(..., code=code)`, stop.

## Reading data (what most tasks need)

Prefer the cache. It has no rate limit, no network, and millisecond queries.

```python
from tefas import Store
store = Store(r"C:\Users\anilc\tefas\tefas.db")

store.prices(code="AFA", start="2026-01-01")
store.performance("2026-07-27", "2026-08-27", category="Hisse Senedi", min_return=5)
store.performance("2026-07-27", "2026-08-27", max_fee=1.5)  # only cheap funds
store.category_performance("2026-06-01", "2026-08-27")   # which categories did well
store.query("SELECT ... FROM prices")                     # ordinary SQL, any query
```

Tables are `prices`, `breakdown` (allocation, ~58 columns), `returns` (trailing returns, risk
score, category), `fees` (what each fund charges), `fetch_log`. Prices and allocations are keyed on
`(code, date)`; returns and fees are snapshots keyed on `(code, fetched_on)`.

**Prices are net of fees.** A fund's `return_pct` already has its own costs deducted, so never
subtract the fee from it. `performance()` goes the other way and reports `gross_return_pct` --
return with the fee added back -- which is what compares managers rather than price lists.

## Fetching from TEFAS (only if the cache lacks it)

```bash
tefas --db <abs-path> sync --returns --fees   # ~4 requests, run daily
tefas --db <abs-path> backfill --years 5      # ~130 requests, ~33 min, resumable
tefas --db <abs-path> fees --category Hisse   # 1 request, what funds charge
```

`backfill` records completed windows, so interrupting and re-running resumes. Only the window
containing today is re-fetched.

## Configuration

Tunable from both surfaces, explicitly — no config file, nothing read from the environment.

```python
Tefas(rate=2, window=60, max_retries=5, timeout=90,
      backoff_base=60, backoff_cap=300, backoff_jitter=0.25)
Store(path=r"C:\Users\anilc\tefas\tefas.db")
```

```bash
tefas --rate 2 --window 60 --max-retries 5 --timeout 90 \
      --backoff-base 60 --backoff-cap 300 --db <abs-path> sync
```

Every `Tefas(...)` argument except `session` and `progress` has a matching CLI flag, enforced by a
parity test in `tests/test_config.py`. **If you add a constructor argument, add the flag too** or
that test fails.

Lower `--rate` if something else on this machine is also calling TEFAS; the 5/min limit is per-IP
and shared.

## Layout

| File | Holds |
|---|---|
| `client.py` | All six endpoints and their request shapes. **The only file that knows the API.** |
| `store.py` | SQLite cache, screening, category rollup |
| `ingest.py` | Resumable backfill and daily sync |
| `ratelimit.py` | Sliding-window throttle, 429 backoff |
| `fields.py` | Turkish→English column maps |
| `canary.py` | Schema-drift checks |
| `cli.py` | `tefas` command |

When TEFAS changes, `client.py` and `fields.py` are where the fix goes.

## Invariants — do not change these casually

- **`EPOCH` and `BUCKET_DAYS` in `store.py`.** Cache windows are aligned to fixed 28-day buckets
  from a fixed anchor so repeated runs produce identical boundaries and `fetch_log` actually hits.
  Changing either invalidates every recorded fetch and forces a full re-download.
- **`MAX_WINDOW_DAYS = 28`.** The API rejects ranges over one month. 28 is the largest window that
  is always safe regardless of which month it starts in.
- **Unknown API fields pass through, never dropped.** Allocation keys with no established English
  name go to `breakdown.extra` as JSON. Keep it that way — it is how a new upstream column survives.

## Traps in the API itself (already handled — do not "simplify" away)

- **Errors arrive as HTTP 200** with `errorMessage` set. `raise_for_status()` sails past them and
  yields silently empty results. `_post` raises from the body.
- **Responses must be decoded as UTF-8 explicitly.** Fund titles are full of İ, Ö, Ş, Ğ and mangle
  under a system codepage fallback.
- **A dozen request keys must be present even when null**, or the backend returns a
  `NullPointerException`.
- **`dt` is government bond, `db` is fx payable bills.** Easy to transpose; they are not the same.
- **Fee columns are Turkish decimal strings**, and `"-"` means the fund publishes nothing. Reading
  that as zero would call the most expensive funds free. `parse_decimal` handles both.

## Tests

```bash
python -m pytest -q          # 131 tests, no network, ~2s
tefas canary                 # 5 live requests, checks the API hasn't changed shape
```

Tests must stay offline. `tests/test_client.py` swaps in a `NoThrottle` limiter — without it the
real throttle adds 60 real seconds per retry.

`canary` exits `0` clear, `1` new fields appeared, `2` something callers depend on is gone. It runs
daily in GitHub Actions.

## Not available from TEFAS

ISIN, minimum investment, inception date, benchmark index, prospectus text, and individual
holdings — allocation is by instrument *class*, not by security. That data lives on KAP. Don't go
looking for an endpoint; there isn't one.

Fees **are** available, via `fonYonetimBazliBilgiGetir` — this file said otherwise until the
endpoint was found by reading the site's own JavaScript bundle. If something else here reads as a
flat "TEFAS doesn't publish X", the bundle at `/_next/static/chunks/common-*.js` lists every
endpoint the site calls, and is worth checking before believing it.

`fonAlSatEkranValorHesapla` (body `{islemTip, fonKodu, paraBirimi}`) looks like settlement days but
answered "güncel kısıt bilgisine ulaşılamadı" for every fund tried outside trading hours. Left
unwrapped on purpose: verify it during market hours before relying on it.
