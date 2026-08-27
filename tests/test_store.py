import datetime as dt
import json

import pytest

from tefas.ingest import backfill, estimate_requests, sync
from tefas.store import Store


@pytest.fixture()
def store(tmp_path):
    with Store(str(tmp_path / "test.db")) as handle:
        yield handle


def price_row(code="AFA", date="2026-08-26", price=1.28):
    return {
        "code": code,
        "date": date,
        "title": "AK PORTFÖY AMERİKA",
        "price": price,
        "market_cap": 5430252659.77,
        "number_of_shares": 4235631370,
        "number_of_investors": 46634,
    }


# -- writing and reading -----------------------------------------------------


def test_prices_round_trip_with_turkish_titles(store):
    store.upsert_prices([price_row()], kind="YAT")
    stored = store.prices(code="AFA")[0]
    assert stored["price"] == 1.28
    assert stored["title"] == "AK PORTFÖY AMERİKA"
    assert stored["kind"] == "YAT"


def test_reingesting_a_day_updates_rather_than_duplicates(store):
    """TEFAS occasionally revises a published price, and sync deliberately
    re-fetches an overlap, so writes must be idempotent."""
    store.upsert_prices([price_row(price=1.28)], kind="YAT")
    store.upsert_prices([price_row(price=1.31)], kind="YAT")
    rows = store.prices(code="AFA")
    assert len(rows) == 1
    assert rows[0]["price"] == 1.31


def test_numbers_keep_their_type_through_sqlite(store):
    store.upsert_prices([price_row()], kind="YAT")
    assert isinstance(store.prices(code="AFA")[0]["price"], float)


def test_unmapped_allocation_keys_are_kept_in_extra(store):
    """New upstream columns must survive as data rather than being dropped."""
    store.upsert_breakdown(
        [{"code": "AAL", "date": "2026-08-26", "stock": 12.0, "bpp": 3.5, "vint": 0.2}],
        kind="YAT",
    )
    row = store.query("SELECT stock, extra FROM breakdown")[0]
    assert row["stock"] == 12.0
    assert json.loads(row["extra"]) == {"bpp": 3.5, "vint": 0.2}


def test_kind_is_not_duplicated_into_extra(store):
    store.upsert_breakdown(
        [{"code": "AAL", "date": "2026-08-26", "fonTipi": "YAT", "stock": 1.0}]
    )
    row = store.query("SELECT kind, extra FROM breakdown")[0]
    assert row["kind"] == "YAT"
    assert row["extra"] is None


def test_returns_keep_numeric_types(store):
    store.upsert_returns(
        [{"code": "AU1", "return_1y": 18.9034, "risk_value": "6"}],
        kind="YAT",
        fetched_on="2026-08-26",
    )
    row = store.query("SELECT return_1y, risk_value FROM returns")[0]
    assert row["return_1y"] == 18.9034
    assert row["risk_value"] == "6"


def test_empty_input_is_a_no_op(store):
    assert store.upsert_prices([]) == 0


def test_stats_summarise_the_cache(store):
    store.upsert_prices(
        [price_row(date="2026-08-25"), price_row(code="AAL", date="2026-08-26")],
        kind="YAT",
    )
    stats = store.stats()
    assert stats["prices"] == 2
    assert stats["funds"] == 2
    assert stats["first_date"] == "2026-08-25"
    assert stats["last_date"] == "2026-08-26"


# -- resume logic ------------------------------------------------------------


class StubClient:
    """Counts calls so cache-skipping can be tested without a network."""

    def __init__(self):
        self.calls = []

    def _rows(self, start, end):
        return [price_row(date=start.isoformat())]

    def prices(self, start, end=None, kind="YAT", code=None, raw=False):
        self.calls.append(("prices", kind, start, end))
        return self._rows(start, end)

    def breakdown(self, start, end=None, kind="YAT", code=None, raw=False):
        self.calls.append(("breakdown", kind, start, end))
        return [{"code": "AFA", "date": start.isoformat(), "stock": 90.0}]


def test_completed_windows_are_skipped_on_a_second_run(store):
    """The point of the fetch log: re-running a backfill must not re-download
    history that is already local."""
    client = StubClient()
    start, end = dt.date(2026, 1, 1), dt.date(2026, 6, 30)

    first = backfill(client, store, start=start, end=end, kinds=["YAT"],
                     datasets=["prices"])
    assert first["requests"] > 1
    assert first["skipped"] == 0

    calls_after_first = len(client.calls)
    second = backfill(client, store, start=start, end=end, kinds=["YAT"],
                      datasets=["prices"])

    assert second["skipped"] == first["requests"] - second["requests"]
    assert second["requests"] < first["requests"]
    assert len(client.calls) - calls_after_first == second["requests"]


def test_the_window_containing_today_is_always_refetched(store):
    """Today's bucket is still filling up; marking it done would freeze the
    most recent data out of the cache permanently."""
    client = StubClient()
    today = dt.date.today()
    start = today - dt.timedelta(days=60)

    backfill(client, store, start=start, end=today, kinds=["YAT"], datasets=["prices"])
    second = backfill(client, store, start=start, end=today, kinds=["YAT"],
                      datasets=["prices"])

    assert second["requests"] >= 1


def test_interrupted_backfill_resumes(store):
    """A run that dies partway must not lose the windows it already stored."""
    client = StubClient()
    start, end = dt.date(2026, 1, 1), dt.date(2026, 6, 30)

    backfill(client, store, start=start, end=dt.date(2026, 3, 31), kinds=["YAT"],
             datasets=["prices"])
    done_early = len(client.calls)

    backfill(client, store, start=start, end=end, kinds=["YAT"], datasets=["prices"])
    total = len(client.calls)

    full = estimate_requests(start, end, ["YAT"], ["prices"])
    assert total <= full + 1, "resume should not re-download the early windows"


def test_kinds_are_tracked_separately(store):
    client = StubClient()
    start, end = dt.date(2026, 1, 1), dt.date(2026, 3, 31)

    backfill(client, store, start=start, end=end, kinds=["YAT"], datasets=["prices"])
    result = backfill(client, store, start=start, end=end, kinds=["EMK"],
                      datasets=["prices"])

    assert result["skipped"] == 0, "EMK must not be skipped because YAT was fetched"


def test_sync_on_an_empty_cache_fetches_a_recent_window(store):
    client = StubClient()
    result = sync(client, store, kinds=["YAT"], datasets=["prices"])
    assert result["requests"] >= 1


def test_sync_overlaps_the_stored_edge(store):
    """The overlap catches revised prices and late-reporting funds."""
    client = StubClient()
    store.upsert_prices([price_row(date=dt.date.today().isoformat())], kind="YAT")

    sync(client, store, kinds=["YAT"], datasets=["prices"], lookback_days=10)
    earliest = min(call[2] for call in client.calls)
    assert earliest <= dt.date.today() - dt.timedelta(days=9)


def test_estimate_matches_what_a_run_actually_costs(store):
    client = StubClient()
    start, end = dt.date(2026, 1, 1), dt.date(2026, 6, 30)
    predicted = estimate_requests(start, end, ["YAT"], ["prices"])
    result = backfill(client, store, start=start, end=end, kinds=["YAT"],
                      datasets=["prices"])
    assert result["requests"] == predicted
