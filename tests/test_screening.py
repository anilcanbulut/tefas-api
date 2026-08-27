"""Fund metadata and cache-side screening."""

import datetime as dt

import pytest

from tefas.client import PROFILE_ENDPOINT
from tefas.exceptions import TefasInvalidParameter
from tefas.store import Store
from tests.test_client import make_client, ok


@pytest.fixture()
def store(tmp_path):
    with Store(str(tmp_path / "screen.db")) as handle:
        yield handle


# -- profile -----------------------------------------------------------------


PROFILE_ROW = {
    "fonKodu": "AFA",
    "fonUnvan": "AK PORTFÖY AMERİKA YABANCI HİSSE SENEDİ FONU",
    "sonFiyat": 1.284642,
    "gunlukGetiri": 0.2029,
    "portBuyukluk": 5435479653.67,
    "fonKategori": "Hisse Senedi Fonu",
    "kategoriDerece": 34,
    "kategoriFonSay": 199,
    "pazarPayi": 2.05,
}


def test_profile_exposes_rank_and_market_share():
    """These three fields exist on no other endpoint."""
    client, session = make_client([ok([PROFILE_ROW])])
    row = client.profile("AFA")
    assert row["category_rank"] == 34
    assert row["category_fund_count"] == 199
    assert row["market_share"] == 2.05
    assert session.requests[0]["url"].endswith(PROFILE_ENDPOINT)


def test_profile_uppercases_the_code():
    client, session = make_client([ok([PROFILE_ROW])])
    client.profile("afa")
    assert session.requests[0]["body"]["fonKodu"] == "AFA"


def test_profile_returns_none_for_an_unknown_fund():
    client, _ = make_client([ok([])])
    assert client.profile("ZZZ") is None


def test_profile_needs_a_code():
    client, session = make_client([])
    with pytest.raises(TefasInvalidParameter):
        client.profile("")
    assert session.requests == []


# -- screening from the cache ------------------------------------------------


def seed(store, code, series, category="Hisse Senedi Şemsiye Fonu", kind="YAT"):
    """Write a price series, plus a returns row carrying the category."""
    store.upsert_prices(
        [
            {"code": code, "date": date, "price": price, "title": code + " FONU",
             "market_cap": 1000.0}
            for date, price in series
        ],
        kind=kind,
    )
    store.upsert_returns(
        [{"code": code, "category": category, "risk_value": "6"}],
        kind=kind,
        fetched_on="2026-08-27",
    )


def test_return_is_measured_between_the_window_edges(store):
    seed(store, "AAA", [("2026-08-01", 100.0), ("2026-08-15", 105.0),
                        ("2026-08-31", 110.0)])
    row = store.performance("2026-08-01", "2026-08-31")[0]
    assert row["return_pct"] == pytest.approx(10.0)
    assert row["start_price"] == 100.0
    assert row["end_price"] == 110.0


def test_window_bounds_are_respected(store):
    seed(store, "AAA", [("2026-08-01", 100.0), ("2026-08-15", 105.0),
                        ("2026-08-31", 110.0)])
    row = store.performance("2026-08-01", "2026-08-15")[0]
    assert row["return_pct"] == pytest.approx(5.0)


def test_min_return_filters(store):
    seed(store, "AAA", [("2026-08-01", 100.0), ("2026-08-31", 110.0)])
    seed(store, "BBB", [("2026-08-01", 100.0), ("2026-08-31", 102.0)])

    hits = store.performance("2026-08-01", "2026-08-31", min_return=5)
    assert [row["code"] for row in hits] == ["AAA"]


def test_max_return_filters(store):
    seed(store, "AAA", [("2026-08-01", 100.0), ("2026-08-31", 110.0)])
    seed(store, "BBB", [("2026-08-01", 100.0), ("2026-08-31", 102.0)])

    hits = store.performance("2026-08-01", "2026-08-31", max_return=5)
    assert [row["code"] for row in hits] == ["BBB"]


def test_losses_are_reported_as_negative_returns(store):
    seed(store, "AAA", [("2026-08-01", 100.0), ("2026-08-31", 90.0)])
    row = store.performance("2026-08-01", "2026-08-31")[0]
    assert row["return_pct"] == pytest.approx(-10.0)


def test_category_is_a_substring_match(store):
    """So "Hisse Senedi" catches "Hisse Senedi Şemsiye Fonu"."""
    seed(store, "AAA", [("2026-08-01", 100.0), ("2026-08-31", 110.0)],
         category="Hisse Senedi Şemsiye Fonu")
    seed(store, "BBB", [("2026-08-01", 100.0), ("2026-08-31", 120.0)],
         category="Para Piyasası Şemsiye Fonu")

    hits = store.performance("2026-08-01", "2026-08-31", category="Hisse Senedi")
    assert [row["code"] for row in hits] == ["AAA"]


def test_results_are_ranked_best_first(store):
    seed(store, "AAA", [("2026-08-01", 100.0), ("2026-08-31", 105.0)])
    seed(store, "BBB", [("2026-08-01", 100.0), ("2026-08-31", 120.0)])
    seed(store, "CCC", [("2026-08-01", 100.0), ("2026-08-31", 110.0)])

    codes = [row["code"] for row in store.performance("2026-08-01", "2026-08-31")]
    assert codes == ["BBB", "CCC", "AAA"]


def test_a_fund_with_one_quote_is_excluded(store):
    """One price cannot express a return, and 0% would be a lie."""
    seed(store, "AAA", [("2026-08-15", 100.0)])
    assert store.performance("2026-08-01", "2026-08-31") == []


def test_a_fund_that_started_mid_window_uses_its_own_first_quote(store):
    """It is measured from when it began trading, not compared with a price it
    never had."""
    seed(store, "AAA", [("2026-08-20", 100.0), ("2026-08-31", 110.0)])
    row = store.performance("2026-08-01", "2026-08-31")[0]
    assert row["start_date"] == "2026-08-20"
    assert row["return_pct"] == pytest.approx(10.0)


def test_kind_filters(store):
    seed(store, "AAA", [("2026-08-01", 100.0), ("2026-08-31", 110.0)], kind="YAT")
    seed(store, "EEE", [("2026-08-01", 100.0), ("2026-08-31", 120.0)], kind="EMK")

    hits = store.performance("2026-08-01", "2026-08-31", kind="EMK")
    assert [row["code"] for row in hits] == ["EEE"]


def test_screening_needs_no_network(store):
    """The point of the cache: arbitrary windows, zero requests."""
    seed(store, "AAA", [("2026-08-01", 100.0), ("2026-08-31", 110.0)])
    assert store.performance("2026-08-01", "2026-08-31", min_return=5)


def test_categories_come_from_the_newest_snapshot(store):
    store.upsert_returns([{"code": "AAA", "category": "Eski"}], fetched_on="2026-01-01")
    store.upsert_returns([{"code": "AAA", "category": "Yeni"}], fetched_on="2026-08-27")
    assert [row["category"] for row in store.categories()] == ["Yeni"]


def test_categories_are_counted(store):
    for code in ("AAA", "BBB"):
        store.upsert_returns(
            [{"code": code, "category": "Hisse Senedi Şemsiye Fonu"}],
            fetched_on="2026-08-27",
        )
    assert store.categories()[0]["funds"] == 2


def test_category_filter_without_a_snapshot_fails_loudly(store):
    """Silently matching nothing would read as "no funds qualify" rather than
    "you have not synced categories yet"."""
    from tefas.exceptions import EmptyCategoryData

    store.upsert_prices(
        [
            {"code": "AAA", "date": "2026-08-01", "price": 100.0},
            {"code": "AAA", "date": "2026-08-31", "price": 110.0},
        ],
        kind="YAT",
    )
    assert store.performance("2026-08-01", "2026-08-31")  # unfiltered still works
    with pytest.raises(EmptyCategoryData, match="sync --returns"):
        store.performance("2026-08-01", "2026-08-31", category="Hisse Senedi")
