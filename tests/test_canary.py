"""Canary shape checks, and the category rollup.

The canary itself is exercised against stub payloads, so this suite stays
offline. Pointing it at the live API is what `tefas canary` is for.
"""

import pytest

from tefas import canary
from tefas.exceptions import EmptyCategoryData, TefasAPIError
from tefas.store import Store


@pytest.fixture()
def store(tmp_path):
    with Store(str(tmp_path / "groups.db")) as handle:
        yield handle


# -- canary shape checks -----------------------------------------------------


def price_rows(n=600, **overrides):
    row = {"fonKodu": "AFA", "tarih": "2026-08-26", "fiyat": 1.28,
           "fonUnvan": "X", "tedPaySayisi": 1, "kisiSayisi": 1,
           "portfoyBuyukluk": 1.0, "borsaBultenFiyat": None, "rn": 1}
    row.update(overrides)
    return [dict(row) for _ in range(n)]


class StubClient:
    def __init__(self, prices=None, breakdown=None, returns=None, profile=None):
        self._prices = price_rows() if prices is None else prices
        self._breakdown = breakdown if breakdown is not None else [
            {"fonKodu": "AFA", "tarih": "2026-08-26", "hs": 1.0} for _ in range(600)
        ]
        self._returns = returns if returns is not None else [
            {"fonKodu": "AFA", "getiri1a": 1.0, "fonTurAciklama": "X"}
            for _ in range(400)
        ]
        self._profile = profile if profile is not None else {
            "fonKodu": "AFA", "kategoriDerece": 1, "pazarPayi": 1.0
        }

    def prices(self, *a, **k):
        return self._prices

    def breakdown(self, *a, **k):
        return self._breakdown

    def returns(self, *a, **k):
        return self._returns

    def profile(self, *a, **k):
        return self._profile


def by_name(results):
    return {result.name: result for result in results}


def test_an_unchanged_api_passes_every_check():
    results = canary.run(StubClient())
    assert all(result.ok for result in results)
    assert canary.exit_code(results) == 0


def test_all_four_endpoints_are_checked():
    assert set(by_name(canary.run(StubClient()))) == {
        "prices", "breakdown", "returns", "profile"
    }


def test_a_renamed_field_is_caught_as_both_missing_and_new():
    """The signature of a rename, and the thing that silently breaks callers."""
    renamed = price_rows(fiyat=None)
    for row in renamed:
        del row["fiyat"]
        row["fiyatYeni"] = 1.28

    result = by_name(canary.run(StubClient(prices=renamed)))["prices"]
    assert "fiyat" in result.missing
    assert "fiyatYeni" in result.unknown
    assert result.breaking


def test_a_purely_additive_change_warns_but_does_not_fail():
    """New columns are not a breakage: they pass through under raw names."""
    results = canary.run(StubClient(prices=price_rows(yeniAlan=1)))
    result = by_name(results)["prices"]
    assert result.unknown == ["yeniAlan"]
    assert not result.breaking
    assert canary.exit_code(results) == 1


def test_a_missing_critical_field_is_breaking():
    stripped = price_rows()
    for row in stripped:
        del row["tarih"]
    results = canary.run(StubClient(prices=stripped))
    assert by_name(results)["prices"].breaking
    assert canary.exit_code(results) == 2


def test_documented_unmapped_breakdown_keys_are_not_reported_as_new():
    """They are known-unknowns, already passed through deliberately."""
    rows = [
        {"fonKodu": "AFA", "tarih": "2026-08-26", "bpp": 1.0, "vint": 2.0}
        for _ in range(600)
    ]
    assert by_name(canary.run(StubClient(breakdown=rows)))["breakdown"].unknown == []


def test_a_collapsed_row_count_fails_even_though_the_call_succeeded():
    """5 funds is a successful response and a broken one."""
    result = by_name(canary.run(StubClient(prices=price_rows(n=5))))["prices"]
    assert result.error is not None
    assert result.breaking


def test_an_empty_response_fails():
    assert by_name(canary.run(StubClient(prices=[])))["prices"].breaking


def test_an_api_error_is_captured_not_raised():
    """One dead endpoint must not stop the others being checked."""

    class Broken(StubClient):
        def prices(self, *a, **k):
            raise TefasAPIError("Tarih araligi 1 ayi asamaz")

    results = canary.run(Broken())
    assert by_name(results)["prices"].breaking
    assert by_name(results)["returns"].ok


def test_a_missing_profile_fails_that_check_only():
    class NoProfile(StubClient):
        def profile(self, *a, **k):
            return None

    results = canary.run(NoProfile())
    assert by_name(results)["profile"].breaking
    assert by_name(results)["prices"].ok


def test_report_names_the_outcome():
    assert "unchanged" in canary.report(canary.run(StubClient()))
    broken = canary.run(StubClient(prices=[]))
    assert "BREAKING" in canary.report(broken)


def test_a_null_field_in_one_row_is_not_a_missing_field():
    """Fields are unioned across rows, so a fund with a null does not look
    like a schema change."""
    rows = price_rows(n=600)
    del rows[0]["fiyat"]
    assert by_name(canary.run(StubClient(prices=rows)))["prices"].ok


# -- category rollup ---------------------------------------------------------


def seed(store, code, first, last, category, kind="YAT"):
    store.upsert_prices(
        [
            {"code": code, "date": "2026-08-01", "price": first, "title": code},
            {"code": code, "date": "2026-08-31", "price": last, "title": code},
        ],
        kind=kind,
    )
    store.upsert_returns(
        [{"code": code, "category": category}], kind=kind, fetched_on="2026-08-31"
    )


def test_categories_are_ranked_by_average_return(store):
    seed(store, "AAA", 100.0, 110.0, "Hisse Senedi")
    seed(store, "BBB", 100.0, 120.0, "Hisse Senedi")
    seed(store, "CCC", 100.0, 101.0, "Para Piyasası")

    rows = store.category_performance("2026-08-01", "2026-08-31")
    assert [row["category"] for row in rows] == ["Hisse Senedi", "Para Piyasası"]
    assert rows[0]["avg_return"] == pytest.approx(15.0)
    assert rows[0]["funds"] == 2


def test_spread_exposes_dispersion_an_average_hides(store):
    """Two categories can share a mean and be nothing alike."""
    seed(store, "AAA", 100.0, 90.0, "Serbest")
    seed(store, "BBB", 100.0, 130.0, "Serbest")
    seed(store, "CCC", 100.0, 110.0, "Para Piyasası")
    seed(store, "DDD", 100.0, 110.0, "Para Piyasası")

    rows = {r["category"]: r for r in store.category_performance("2026-08-01", "2026-08-31")}
    assert rows["Serbest"]["avg_return"] == pytest.approx(10.0)
    assert rows["Para Piyasası"]["avg_return"] == pytest.approx(10.0)
    assert rows["Serbest"]["spread"] == pytest.approx(40.0)
    assert rows["Para Piyasası"]["spread"] == pytest.approx(0.0)


def test_median_resists_a_single_outlier(store):
    seed(store, "AAA", 100.0, 101.0, "Serbest")
    seed(store, "BBB", 100.0, 102.0, "Serbest")
    seed(store, "CCC", 100.0, 200.0, "Serbest")

    row = store.category_performance("2026-08-01", "2026-08-31")[0]
    assert row["median_return"] == pytest.approx(2.0)
    assert row["avg_return"] > 30


def test_median_of_an_even_count_averages_the_middle_pair(store):
    for code, last in (("AAA", 101.0), ("BBB", 103.0)):
        seed(store, code, 100.0, last, "Serbest")
    row = store.category_performance("2026-08-01", "2026-08-31")[0]
    assert row["median_return"] == pytest.approx(2.0)


def test_positive_share_counts_winners(store):
    seed(store, "AAA", 100.0, 110.0, "Serbest")
    seed(store, "BBB", 100.0, 110.0, "Serbest")
    seed(store, "CCC", 100.0, 90.0, "Serbest")

    row = store.category_performance("2026-08-01", "2026-08-31")[0]
    assert row["positive_share"] == pytest.approx(200.0 / 3)


def test_thin_categories_can_be_excluded(store):
    seed(store, "AAA", 100.0, 110.0, "Hisse Senedi")
    seed(store, "BBB", 100.0, 110.0, "Hisse Senedi")
    seed(store, "CCC", 100.0, 110.0, "Karma")

    rows = store.category_performance("2026-08-01", "2026-08-31", min_funds=2)
    assert [row["category"] for row in rows] == ["Hisse Senedi"]


def test_rollup_and_per_fund_view_agree(store):
    """They share an implementation precisely so this stays true."""
    seed(store, "AAA", 100.0, 110.0, "Hisse Senedi")
    seed(store, "BBB", 100.0, 120.0, "Hisse Senedi")

    per_fund = store.performance("2026-08-01", "2026-08-31")
    grouped = store.category_performance("2026-08-01", "2026-08-31")[0]
    assert grouped["funds"] == len(per_fund)
    assert grouped["best"] == pytest.approx(max(r["return_pct"] for r in per_fund))


def test_rollup_without_categories_fails_loudly(store):
    store.upsert_prices(
        [
            {"code": "AAA", "date": "2026-08-01", "price": 100.0},
            {"code": "AAA", "date": "2026-08-31", "price": 110.0},
        ],
        kind="YAT",
    )
    with pytest.raises(EmptyCategoryData):
        store.category_performance("2026-08-01", "2026-08-31")
