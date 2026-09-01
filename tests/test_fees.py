"""Costs: parsing them, storing them, and screening on them.

The subtlety worth testing is that a published NAV is already net of the
management fee, so the fee is not something to subtract from a past return --
it is something to add back if you want to compare managers rather than price
lists. A test that got that backwards would look perfectly reasonable.
"""

import pytest

from tefas.client import Tefas
from tefas.exceptions import EmptyFeeData
from tefas.fields import FEE_FIELDS, parse_decimal
from tefas.store import Store


@pytest.fixture()
def store(tmp_path):
    with Store(str(tmp_path / "fees.db")) as handle:
        yield handle


class NoThrottle:
    """The real limiter sleeps for a whole minute. Tests must not."""

    rate = 10 ** 6
    window = 1.0

    def acquire(self):
        pass

    def penalise(self):
        pass


class StubTefas(Tefas):
    def __init__(self, payload):
        super().__init__()
        self.limiter = NoThrottle()
        self.payload = payload
        self.calls = []

    def _post(self, endpoint, body):
        self.calls.append((endpoint, body))
        # Copied, because a real response is freshly parsed JSON every time and
        # the client is free to annotate it. Handing back the same dicts would
        # let one call's bookkeeping leak into the next.
        return [dict(row) for row in self.payload]


#: One row exactly as TEFAS sends it: fees are Turkish-formatted strings.
LIVE_ROW = {
    "fonKodu": "HAI",
    "fonUnvan": "AHLATCI PORTFOY ALTIN FONU",
    "fonTurAciklama": "Kiymetli Madenler Semsiye Fonu",
    "tefasDurum": True,
    "fonTurKod": 37,
    "kurucuKod": "LAT",
    "altbaslik1": "uygulananYonetimUcretiOranYillikYuzde",
    "uygulananYu1Y": "2,04",
    "altbaslik2": "ictuzukteYerAlanYonetimUcretiOraniYillikYuzde",
    "fonIcTuzukYu1G": "2,04",
    "yillikGetiri": 77.4,
    "altbaslik3": "azamitopgidoran",
    "fonTopGiderKesoran": "2,19",
}


# -- parsing -----------------------------------------------------------------


def test_turkish_decimals_become_floats():
    assert parse_decimal("2,04") == 2.04
    assert parse_decimal("1.234,5") == 1234.5


def test_a_dash_is_missing_not_zero():
    """Exchange traded funds send "-". Reading that as 0 would call the most
    expensive funds free."""
    assert parse_decimal("-") is None
    assert parse_decimal("") is None


def test_zero_stays_zero():
    assert parse_decimal("0") == 0.0


def test_numbers_pass_through_untouched():
    assert parse_decimal(3.5) == 3.5
    assert parse_decimal(None) is None


def test_unparseable_text_does_not_raise():
    assert parse_decimal("bilinmiyor") is None


# -- the client --------------------------------------------------------------


def test_fees_are_parsed_into_numbers():
    row = StubTefas([LIVE_ROW]).fees()[0]
    assert row["management_fee"] == 2.04
    assert row["management_fee_stated"] == 2.04
    assert row["total_expense_ratio"] == 2.19


def test_fees_are_renamed_to_english():
    row = StubTefas([LIVE_ROW]).fees()[0]
    assert row["code"] == "HAI"
    assert row["category"] == "Kiymetli Madenler Semsiye Fonu"


def test_raw_mode_returns_the_untouched_payload():
    row = StubTefas([LIVE_ROW]).fees(raw=True)[0]
    assert row["uygulananYu1Y"] == "2,04", "raw must stay raw, strings and all"


def test_one_request_covers_every_fund():
    client = StubTefas([LIVE_ROW])
    client.fees()
    assert len(client.calls) == 1
    endpoint, body = client.calls[0]
    assert endpoint.endswith("fonYonetimBazliBilgiGetir")
    assert body["fonTipi"] == "YAT"


def test_several_kinds_cost_one_request_each_and_are_labelled():
    client = StubTefas([LIVE_ROW])
    rows = client.fees(kind=["YAT", "EMK"])
    assert len(client.calls) == 2
    assert {row["fonTipi"] for row in rows} == {"YAT", "EMK"}


def test_every_live_field_is_mapped():
    """A key TEFAS sends that nothing maps would reach callers under its
    Turkish name; the canary reports it, and this catches it at build time."""
    assert set(LIVE_ROW) <= set(FEE_FIELDS)


# -- the store ---------------------------------------------------------------


def seed_prices(store, code, first, last, kind="YAT"):
    store.upsert_prices(
        [
            {"code": code, "date": "2026-08-01", "price": first, "title": code},
            {"code": code, "date": "2026-08-31", "price": last, "title": code},
        ],
        kind=kind,
    )


def test_a_fee_snapshot_is_stamped_and_re_runnable(store):
    rows = [{"code": "AAA", "management_fee": 2.0}]
    assert store.upsert_fees(rows, kind="YAT", fetched_on="2026-08-31") == 1
    assert store.upsert_fees(rows, kind="YAT", fetched_on="2026-08-31") == 1
    assert store.stats()["fees"] == 1, "same day overwrites, never duplicates"


def test_fees_keep_their_history(store):
    store.upsert_fees([{"code": "AAA", "management_fee": 2.0}], fetched_on="2026-07-01")
    store.upsert_fees([{"code": "AAA", "management_fee": 2.5}], fetched_on="2026-08-31")
    assert store.stats()["fees"] == 2, "a fee change must not erase what it was"


def test_screening_reports_the_latest_fee(store):
    seed_prices(store, "AAA", 100.0, 110.0)
    store.upsert_fees([{"code": "AAA", "management_fee": 1.0}], fetched_on="2026-07-01")
    store.upsert_fees([{"code": "AAA", "management_fee": 3.0}], fetched_on="2026-08-31")

    row = store.performance("2026-08-01", "2026-08-31")[0]
    assert row["management_fee"] == 3.0


def test_gross_return_adds_the_fee_back_over_the_window(store):
    """NAVs are net of the fee, so gross is net *plus* the fee, pro-rated."""
    seed_prices(store, "AAA", 100.0, 110.0)
    store.upsert_fees([{"code": "AAA", "management_fee": 2.4}], fetched_on="2026-08-31")

    row = store.performance("2026-08-01", "2026-08-31")[0]
    assert row["return_pct"] == pytest.approx(10.0)
    assert row["fee_drag_pct"] == pytest.approx(2.4 * 30 / 365.25)
    assert row["gross_return_pct"] == pytest.approx(10.0 + 2.4 * 30 / 365.25)
    assert row["gross_return_pct"] > row["return_pct"]


def test_a_longer_window_carries_a_bigger_drag(store):
    store.upsert_prices(
        [
            {"code": "AAA", "date": "2025-08-31", "price": 100.0},
            {"code": "AAA", "date": "2026-08-31", "price": 110.0},
        ],
        kind="YAT",
    )
    store.upsert_fees([{"code": "AAA", "management_fee": 2.4}], fetched_on="2026-08-31")

    row = store.performance("2025-08-31", "2026-08-31")[0]
    assert row["fee_drag_pct"] == pytest.approx(2.4, abs=0.02), "about a full year"


def test_funds_without_a_published_fee_report_blank_not_zero(store):
    seed_prices(store, "AAA", 100.0, 110.0)
    store.upsert_fees([{"code": "AAA", "management_fee": None}], fetched_on="2026-08-31")

    row = store.performance("2026-08-01", "2026-08-31")[0]
    assert row["management_fee"] is None
    assert row["gross_return_pct"] is None, "an unknown fee gives an unknown gross"


def test_a_fund_with_no_fee_row_still_appears_in_screening(store):
    """Fees are a late addition; a cache without them must not lose funds."""
    seed_prices(store, "AAA", 100.0, 110.0)
    rows = store.performance("2026-08-01", "2026-08-31")
    assert [row["code"] for row in rows] == ["AAA"]
    assert rows[0]["management_fee"] is None


def test_max_fee_keeps_only_the_cheap_ones(store):
    seed_prices(store, "CHEAP", 100.0, 110.0)
    seed_prices(store, "DEAR", 100.0, 120.0)
    store.upsert_fees(
        [
            {"code": "CHEAP", "management_fee": 0.5},
            {"code": "DEAR", "management_fee": 3.0},
        ],
        fetched_on="2026-08-31",
    )

    rows = store.performance("2026-08-01", "2026-08-31", max_fee=1.0)
    assert [row["code"] for row in rows] == ["CHEAP"]


def test_max_fee_excludes_funds_that_publish_nothing(store):
    """"At most 1%" cannot honestly include a fund whose fee is unknown."""
    seed_prices(store, "AAA", 100.0, 110.0)
    store.upsert_fees([{"code": "AAA", "management_fee": None}], fetched_on="2026-08-31")
    assert store.performance("2026-08-01", "2026-08-31", max_fee=1.0) == []


def test_filtering_by_fee_with_no_snapshot_fails_loudly(store):
    """The same trap as categories: empty must not read as "none qualify"."""
    seed_prices(store, "AAA", 100.0, 110.0)
    with pytest.raises(EmptyFeeData):
        store.performance("2026-08-01", "2026-08-31", max_fee=1.0)


def test_the_error_names_the_command_that_fixes_it(store):
    seed_prices(store, "AAA", 100.0, 110.0)
    with pytest.raises(EmptyFeeData) as caught:
        store.performance("2026-08-01", "2026-08-31", max_fee=1.0)
    assert "sync --fees" in str(caught.value)


# -- category rollup ---------------------------------------------------------


def test_categories_report_their_average_fee(store):
    for code, fee in (("AAA", 2.0), ("BBB", 3.0)):
        seed_prices(store, code, 100.0, 110.0)
        store.upsert_returns(
            [{"code": code, "category": "Hisse Senedi"}], fetched_on="2026-08-31"
        )
        store.upsert_fees([{"code": code, "management_fee": fee}], fetched_on="2026-08-31")

    row = store.category_performance("2026-08-01", "2026-08-31")[0]
    assert row["avg_fee"] == pytest.approx(2.5)


def test_average_fee_ignores_funds_that_publish_none(store):
    for code, fee in (("AAA", 2.0), ("BBB", None)):
        seed_prices(store, code, 100.0, 110.0)
        store.upsert_returns(
            [{"code": code, "category": "Hisse Senedi"}], fetched_on="2026-08-31"
        )
        store.upsert_fees([{"code": code, "management_fee": fee}], fetched_on="2026-08-31")

    row = store.category_performance("2026-08-01", "2026-08-31")[0]
    assert row["funds"] == 2
    assert row["avg_fee"] == pytest.approx(2.0), "a blank is not a zero in the mean"


def test_average_fee_is_none_before_any_snapshot(store):
    seed_prices(store, "AAA", 100.0, 110.0)
    store.upsert_returns(
        [{"code": "AAA", "category": "Hisse Senedi"}], fetched_on="2026-08-31"
    )
    assert store.category_performance("2026-08-01", "2026-08-31")[0]["avg_fee"] is None
