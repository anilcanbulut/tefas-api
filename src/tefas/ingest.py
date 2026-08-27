"""Backfill and daily sync: the two jobs that put TEFAS data in the cache.

Request budget is the thing worth reasoning about here. At the default four
requests a minute:

* a five-year backfill of prices and allocations for one fund kind is about
  ``2 * 65 = 130`` requests, so roughly half an hour, once;
* a daily sync is two requests, so about a second.

Both numbers are small because every call is a bulk call. Anything that made
one request per fund would need ~2,000 times as many and could not finish.
"""

import datetime as _dt
import logging
from typing import Callable, Optional, Sequence

from .fields import KINDS
from .store import Store, aligned_windows

log = logging.getLogger("tefas.ingest")

DATASETS = ("prices", "breakdown")


def _fetcher(client, dataset: str) -> Callable:
    return {"prices": client.prices, "breakdown": client.breakdown}[dataset]


def _upserter(store: Store, dataset: str) -> Callable:
    return {"prices": store.upsert_prices, "breakdown": store.upsert_breakdown}[dataset]


def estimate_requests(start: _dt.date, end: _dt.date, kinds: Sequence[str],
                      datasets: Sequence[str]) -> int:
    """How many HTTP requests a run over this range would cost at most."""
    windows = len(list(aligned_windows(start, end)))
    return windows * len(kinds) * len(datasets)


def backfill(
    client,
    store: Store,
    start: _dt.date,
    end: Optional[_dt.date] = None,
    kinds: Sequence[str] = ("YAT",),
    datasets: Sequence[str] = DATASETS,
    on_window: Optional[Callable] = None,
) -> dict:
    """Fill the cache over a date range, skipping windows already fetched.

    Safe to interrupt and re-run: progress is recorded per window, so a second
    run resumes rather than starting over.
    """
    end = end or _dt.date.today()
    summary = {"requests": 0, "rows": 0, "skipped": 0}

    for dataset in datasets:
        fetch = _fetcher(client, dataset)
        upsert = _upserter(store, dataset)

        for kind in kinds:
            planned = list(aligned_windows(start, end))
            pending = list(store.pending_windows(dataset, kind, start, end))
            summary["skipped"] += len(planned) - len(pending)

            for bucket, win_start, win_end in pending:
                rows = fetch(win_start, win_end, kind=kind)
                written = upsert(rows, kind=kind)
                store.mark_fetched(dataset, kind, bucket, written)

                summary["requests"] += 1
                summary["rows"] += written
                log.info(
                    "%s %s %s..%s -> %s rows", dataset, kind, win_start, win_end, written
                )
                if on_window:
                    on_window(dataset, kind, win_start, win_end, written)

    return summary


def sync(
    client,
    store: Store,
    kinds: Sequence[str] = ("YAT",),
    datasets: Sequence[str] = DATASETS,
    lookback_days: int = 10,
    on_window: Optional[Callable] = None,
) -> dict:
    """Bring the cache up to date.

    Starts from the newest date already stored, minus a small overlap. The
    overlap matters: TEFAS occasionally revises a published price, and a fund
    that was late reporting appears in the data after the fact.
    """
    today = _dt.date.today()
    newest = store.latest_date("prices")

    if newest:
        start = _dt.date.fromisoformat(newest) - _dt.timedelta(days=lookback_days)
    else:
        start = today - _dt.timedelta(days=30)
        log.info("Cache is empty; syncing the last 30 days. Run backfill for history.")

    return backfill(
        client,
        store,
        start=start,
        end=today,
        kinds=kinds,
        datasets=datasets,
        on_window=on_window,
    )


def refresh_returns(client, store: Store, kinds: Sequence[str] = ("YAT",)) -> int:
    """Store today's trailing-return and risk-score snapshot."""
    total = 0
    for kind in kinds:
        rows = client.returns(kind=kind)
        total += store.upsert_returns(rows, kind=kind)
    return total


def all_kinds() -> Sequence[str]:
    return KINDS
