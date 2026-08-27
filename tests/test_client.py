import json

import pytest
import requests

from tefas.client import INFO_ENDPOINT, Tefas
from tefas.exceptions import (
    TefasAPIError,
    TefasHTTPError,
    TefasInvalidParameter,
    TefasRateLimitError,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=None):
        self.status_code = status_code
        if content is not None:
            self.content = content
        else:
            self.content = json.dumps(payload or {}).encode("utf-8")


class FakeSession:
    """Stands in for requests.Session, recording what the client sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.headers = {}

    def post(self, url, data=None, timeout=None):
        self.requests.append({"url": url, "body": json.loads(data), "timeout": timeout})
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        pass


class NoThrottle:
    """Stand-in limiter, so these tests never touch the wall clock.

    Throttling itself is covered against a virtual clock in test_ratelimit.py;
    here it would only add sixty real seconds per retry.
    """

    def acquire(self):
        return 0.0

    def penalise(self):
        pass


def make_client(responses, **kwargs):
    session = FakeSession(responses)
    client = Tefas(session=session, **kwargs)
    client.limiter = NoThrottle()
    client._sleep_for_retry = lambda attempt: None
    return client, session


def ok(rows):
    return FakeResponse(payload={"errorMessage": None, "resultList": rows})


# -- the HTTP-200 error trap -------------------------------------------------


def test_logical_errors_arrive_as_http_200_and_still_raise():
    """TEFAS answers a bad query with 200 and an errorMessage. Trusting the
    status code alone turns a real error into silently empty results."""
    responses = [
        FakeResponse(
            status_code=200,
            payload={"errorMessage": "Tarih araligi 1 ayi asamaz", "resultList": None},
        )
    ]
    client, _ = make_client(responses)
    with pytest.raises(TefasAPIError, match="1 ayi asamaz"):
        client.prices("2026-08-26")


def test_null_result_list_becomes_an_empty_list():
    client, _ = make_client([FakeResponse(payload={"resultList": None})])
    assert client.prices("2026-08-26") == []


# -- encoding ----------------------------------------------------------------


def test_turkish_titles_survive_decoding():
    title = "ATA PORTFÖY İKİNCİ HİSSE SENEDİ (TL) FONU"
    body = json.dumps(
        {"resultList": [{"fonKodu": "AAV", "fonUnvan": title, "tarih": "2026-08-26"}]}
    ).encode("utf-8")
    client, _ = make_client([FakeResponse(content=body)])
    assert client.prices("2026-08-26")[0]["title"] == title


# -- rate limiting -----------------------------------------------------------


def test_a_429_is_retried_and_then_succeeds():
    responses = [FakeResponse(status_code=429), ok([{"fonKodu": "AFA"}])]
    client, session = make_client(responses)
    assert len(client.prices("2026-08-26")) == 1
    assert len(session.requests) == 2


def test_persistent_429_raises_a_rate_limit_error():
    client, _ = make_client([FakeResponse(status_code=429)] * 4, max_retries=3)
    with pytest.raises(TefasRateLimitError):
        client.prices("2026-08-26")


def test_server_errors_are_retried():
    client, session = make_client([FakeResponse(status_code=503), ok([])])
    client.prices("2026-08-26")
    assert len(session.requests) == 2


def test_transport_errors_are_retried():
    client, session = make_client([requests.ConnectionError("reset"), ok([])])
    client.prices("2026-08-26")
    assert len(session.requests) == 2


def test_client_errors_are_not_retried():
    client, session = make_client([FakeResponse(status_code=404)])
    with pytest.raises(TefasHTTPError):
        client.prices("2026-08-26")
    assert len(session.requests) == 1


# -- request shape -----------------------------------------------------------


def test_bulk_query_asks_for_every_fund_at_once():
    """The core efficiency claim: no fund code means all funds, one request."""
    client, session = make_client([ok([])])
    client.prices("2026-08-26")
    assert len(session.requests) == 1
    assert session.requests[0]["body"]["fonKodu"] is None


def test_long_ranges_are_split_into_windows_the_api_accepts():
    client, session = make_client([ok([]) for _ in range(9)])
    client.prices("2026-01-01", "2026-08-26")
    assert len(session.requests) == 9
    for request in session.requests:
        assert len(request["body"]["basTarih"]) == 8


def test_required_but_null_keys_are_present():
    """Omitting them makes the backend answer with a NullPointerException."""
    client, session = make_client([ok([])])
    client.prices("2026-08-26")
    body = session.requests[0]["body"]
    for key in ("aramaMetni", "fonTurKod", "fonGrubu", "kurucuKod", "sfonTurKod"):
        assert key in body
    for key in ("sFonTurKod", "fonKod", "fonGrup", "fonUnvanTip"):
        assert body[key] == ""


def test_endpoint_is_the_bulk_info_endpoint():
    client, session = make_client([ok([])])
    client.prices("2026-08-26")
    assert session.requests[0]["url"].endswith(INFO_ENDPOINT)


def test_several_kinds_are_tagged_so_rows_stay_distinguishable():
    client, session = make_client([ok([{"fonKodu": "A"}]), ok([{"fonKodu": "B"}])])
    rows = client.prices("2026-08-26", kind=["YAT", "EMK"])
    assert {row["fonTipi"] for row in rows} == {"YAT", "EMK"}


def test_unknown_kind_is_rejected_before_any_request():
    client, session = make_client([])
    with pytest.raises(TefasInvalidParameter):
        client.prices("2026-08-26", kind="NOPE")
    assert session.requests == []


# -- field handling ----------------------------------------------------------


def test_turkish_keys_are_renamed_to_english_columns():
    row = {
        "fonKodu": "AFA",
        "fonUnvan": "AK PORTFOY",
        "tarih": "2026-08-26",
        "fiyat": 1.28,
        "portfoyBuyukluk": 5430252659.77,
        "kisiSayisi": 46634,
        "rn": 1,
    }
    client, _ = make_client([ok([row])])
    result = client.prices("2026-08-26")[0]
    assert result["code"] == "AFA"
    assert result["price"] == 1.28
    assert result["market_cap"] == 5430252659.77
    assert result["number_of_investors"] == 46634
    assert "rn" not in result, "row-number is pagination noise, not data"


def test_raw_mode_preserves_the_original_api_keys():
    client, _ = make_client([ok([{"fonKodu": "AFA", "fiyat": 1.28}])])
    result = client.prices("2026-08-26", raw=True)[0]
    assert result["fonKodu"] == "AFA"


def test_unknown_columns_pass_through_instead_of_being_dropped():
    """A column TEFAS adds upstream should show up as data, not vanish."""
    client, _ = make_client([ok([{"fonKodu": "AFA", "yeniAlan": 42}])])
    assert client.prices("2026-08-26")[0]["yeniAlan"] == 42


# -- other endpoints ---------------------------------------------------------


def test_history_rejects_periods_the_endpoint_does_not_accept():
    client, session = make_client([])
    with pytest.raises(TefasInvalidParameter):
        client.history("AFA", period=7)
    assert session.requests == []


def test_history_uppercases_the_fund_code():
    client, session = make_client([ok([])])
    client.history("afa", period=12)
    assert session.requests[0]["body"]["fonKodu"] == "AFA"


def test_snapshot_joins_prices_to_allocations():
    prices = ok([{"fonKodu": "AFA", "tarih": "2026-08-26", "fiyat": 1.28}])
    allocations = ok([{"fonKodu": "AFA", "tarih": "2026-08-26", "hs": 91.4}])
    client, session = make_client([prices, allocations])
    row = client.snapshot("2026-08-26")[0]
    assert row["price"] == 1.28
    assert row["stock"] == 91.4
    assert len(session.requests) == 2, "a full snapshot should cost two requests"
