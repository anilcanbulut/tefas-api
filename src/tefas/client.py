"""Direct client for the TEFAS JSON API.

TEFAS publishes no official API. These endpoints back the tefas.gov.tr web app,
need no key, cookie or login, and were verified against the live service on
27 Aug 2026. They are undocumented and unversioned: TEFAS changed them once in
2026 already and may do so again, so treat this module as the single place that
knows their shape.

The design rests on one measured fact: sending ``fonKodu: null`` returns *every*
fund for the whole requested window in one response -- 2,044 funds over 20
trading days arrived as 40,637 rows in 2.8 seconds. Fetching fund-by-fund is
therefore never correct here; it multiplies request count by ~2,000 against a
five-per-minute limit for data that one call already contains.
"""

import datetime as _dt
import json
import logging
import time
from typing import Iterable, List, Optional, Sequence, Union

import requests

from .exceptions import (
    TefasAPIError,
    TefasHTTPError,
    TefasInvalidParameter,
    TefasRateLimitError,
)
from .fields import BREAKDOWN_FIELDS, INFO_FIELDS, KINDS, RETURN_FIELDS, rename
from .ratelimit import DEFAULT_RATE, DEFAULT_WINDOW, SlidingWindowLimiter, backoff_delay

log = logging.getLogger("tefas")

BASE_URL = "https://www.tefas.gov.tr"

INFO_ENDPOINT = "/api/funds/fonGnlBlgSiraliGetir"
BREAKDOWN_ENDPOINT = "/api/funds/dagilimSiraliGetirT"
RETURNS_ENDPOINT = "/api/funds/fonGetiriBazliBilgiGetir"
PRICE_ENDPOINT = "/api/funds/fonFiyatBilgiGetir"

#: The API rejects ranges longer than one calendar month with
#: "Tarih araligi 1 ayi asamaz". 28 days is the largest window that always
#: stays inside that limit regardless of which month it starts in.
MAX_WINDOW_DAYS = 28

#: Look-back periods ``fonFiyatBilgiGetir`` accepts, in months.
VALID_PERIODS = (1, 3, 6, 12, 36, 60)

DEFAULT_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/tr/fon-verileri",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
}

DateLike = Union[str, _dt.date, _dt.datetime]


def parse_date(value: DateLike) -> _dt.date:
    """Normalise a date argument to ``datetime.date``."""
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str):
        text = value.strip()
        for fmt in ("%Y-%m-%d", "%Y%m%d", "%d.%m.%Y", "%d/%m/%Y"):
            try:
                return _dt.datetime.strptime(text, fmt).date()
            except ValueError:
                continue
    raise TefasInvalidParameter("Unrecognised date: {!r}".format(value))


def date_windows(start: _dt.date, end: _dt.date, size_days: int = MAX_WINDOW_DAYS):
    """Split an inclusive range into consecutive windows the API will accept."""
    if start > end:
        raise TefasInvalidParameter(
            "start ({}) is after end ({})".format(start, end)
        )
    step = _dt.timedelta(days=size_days - 1)
    cursor = start
    while cursor <= end:
        stop = min(cursor + step, end)
        yield cursor, stop
        cursor = stop + _dt.timedelta(days=1)


def _normalise_kinds(kind) -> List[str]:
    if kind is None:
        return list(KINDS)
    if isinstance(kind, str):
        kinds = [kind]
    elif isinstance(kind, (list, tuple, set, frozenset)):
        kinds = list(kind)
    else:
        raise TefasInvalidParameter("Unrecognised kind: {!r}".format(kind))
    for item in kinds:
        if item not in KINDS:
            raise TefasInvalidParameter(
                "Unknown fund kind {!r}; expected one of {}".format(
                    item, ", ".join(KINDS)
                )
            )
    return kinds


class Tefas:
    """A polite, bulk-first TEFAS reader.

    Parameters
    ----------
    rate:
        Requests allowed per ``window`` seconds. Defaults to 4; the gateway
        starts refusing at 6.
    max_retries:
        How many times to retry a request that comes back 429.
    timeout:
        Per-request socket timeout in seconds. Bulk month queries return
        several megabytes, so this is deliberately generous.
    progress:
        Optional ``callable(done, total, label)`` invoked once per HTTP request,
        for CLI progress reporting.
    """

    def __init__(
        self,
        rate: int = DEFAULT_RATE,
        window: float = DEFAULT_WINDOW,
        max_retries: int = 5,
        timeout: int = 90,
        session: Optional[requests.Session] = None,
        progress=None,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.progress = progress
        self.limiter = SlidingWindowLimiter(rate=rate, window=window)
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    # -- transport ----------------------------------------------------------

    def _post(self, endpoint: str, body: dict) -> list:
        """POST one request, honouring the rate limit, and return ``resultList``.

        Raises ``TefasAPIError`` from the response *body*: TEFAS reports invalid
        queries as ``HTTP 200`` with ``errorMessage`` set and ``resultList``
        null, so checking the status code alone silently yields empty results.
        """
        url = BASE_URL + endpoint
        last_error = None

        for attempt in range(self.max_retries + 1):
            self.limiter.acquire()
            try:
                response = self.session.post(
                    url, data=json.dumps(body), timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = exc
                log.warning("TEFAS request failed (%s), retrying: %s", endpoint, exc)
                self._sleep_for_retry(attempt)
                continue

            if response.status_code == 429:
                self.limiter.penalise()
                last_error = TefasRateLimitError("TEFAS returned 429")
                log.info("Rate limited by TEFAS, backing off (attempt %s)", attempt + 1)
                self._sleep_for_retry(attempt)
                continue

            if response.status_code >= 500:
                last_error = TefasHTTPError(
                    "TEFAS returned {}".format(response.status_code)
                )
                self._sleep_for_retry(attempt)
                continue

            if response.status_code != 200:
                raise TefasHTTPError(
                    "TEFAS returned {} for {}".format(response.status_code, endpoint)
                )

            # Decode as UTF-8 explicitly. Fund titles are full of Turkish
            # characters and a consumer that falls back to a system codepage
            # mangles every one of them.
            try:
                payload = json.loads(response.content.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise TefasHTTPError(
                    "Could not decode TEFAS response: {}".format(exc)
                ) from exc

            message = payload.get("errorMessage")
            if message:
                raise TefasAPIError(message, error_code=payload.get("errorCode"))

            return payload.get("resultList") or []

        if isinstance(last_error, TefasRateLimitError):
            raise TefasRateLimitError(
                "Still rate limited after {} retries. Lower `rate` or spread "
                "the job over more time.".format(self.max_retries)
            )
        raise TefasHTTPError(
            "TEFAS request to {} failed: {}".format(endpoint, last_error)
        )

    def _sleep_for_retry(self, attempt: int):
        time.sleep(backoff_delay(attempt))

    def _report(self, done, total, label):
        if self.progress:
            self.progress(done, total, label)

    # -- request bodies -----------------------------------------------------

    @staticmethod
    def _bulk_body(
        kind: str, start: _dt.date, end: _dt.date, code: Optional[str]
    ) -> dict:
        """Body shared by the info and breakdown endpoints.

        Most keys must be present even when null; omitting them makes the
        backend answer with a NullPointerException.
        """
        return {
            "fonTipi": kind,
            "fonKodu": code,
            "aramaMetni": None,
            "fonTurKod": None,
            "fonGrubu": None,
            "sfonTurKod": None,
            "fonTurAciklama": None,
            "kurucuKod": None,
            "basTarih": start.strftime("%Y%m%d"),
            "bitTarih": end.strftime("%Y%m%d"),
            "basSira": 1,
            "bitSira": 100000,
            "dil": "TR",
            "sFonTurKod": "",
            "fonKod": "",
            "fonGrup": "",
            "fonUnvanTip": "",
        }

    def _fetch_bulk(self, endpoint, field_map, start, end, kind, code, raw, label):
        start_date = parse_date(start)
        end_date = parse_date(end) if end is not None else start_date
        kinds = _normalise_kinds(kind)
        windows = list(date_windows(start_date, end_date))

        total = len(windows) * len(kinds)
        rows = []
        done = 0

        for one_kind in kinds:
            for win_start, win_end in windows:
                body = self._bulk_body(one_kind, win_start, win_end, code)
                for row in self._post(endpoint, body):
                    row.pop("rn", None)
                    if len(kinds) > 1:
                        row["fonTipi"] = one_kind
                    rows.append(row if raw else rename(row, field_map))
                done += 1
                self._report(
                    done,
                    total,
                    "{} {} {}..{}".format(label, one_kind, win_start, win_end),
                )

        return rows

    # -- public API ---------------------------------------------------------

    def prices(
        self,
        start: DateLike,
        end: Optional[DateLike] = None,
        kind: Union[str, Sequence[str], None] = "YAT",
        code: Optional[str] = None,
        raw: bool = False,
    ) -> List[dict]:
        """Daily price, fund size and holder counts.

        Leaving ``code`` as ``None`` returns every fund of the given kind, which
        costs the same single request as asking for one. Ranges longer than a
        month are split automatically.
        """
        return self._fetch_bulk(
            INFO_ENDPOINT, INFO_FIELDS, start, end, kind, code, raw, "prices"
        )

    def breakdown(
        self,
        start: DateLike,
        end: Optional[DateLike] = None,
        kind: Union[str, Sequence[str], None] = "YAT",
        code: Optional[str] = None,
        raw: bool = False,
    ) -> List[dict]:
        """Portfolio allocation percentages, one column per instrument class."""
        return self._fetch_bulk(
            BREAKDOWN_ENDPOINT,
            BREAKDOWN_FIELDS,
            start,
            end,
            kind,
            code,
            raw,
            "breakdown",
        )

    def returns(
        self,
        kind: Union[str, Sequence[str], None] = "YAT",
        raw: bool = False,
    ) -> List[dict]:
        """Trailing returns, official risk score (1-7) and category per fund.

        This is a point-in-time snapshot of the fund-comparison screen; it takes
        no date range.
        """
        rows = []
        kinds = _normalise_kinds(kind)
        for index, one_kind in enumerate(kinds, start=1):
            body = {
                "dil": "TR",
                "fonTipi": one_kind,
                "kurucuKodu": None,
                "sfonTurKod": None,
                "fonTurAciklama": None,
                "islem": 1,
                "fonTurKod": None,
                "fonGrubu": None,
                "donemGetiri1a": "1",
                "donemGetiri3a": "1",
                "donemGetiri6a": "1",
                "donemGetiri1y": "1",
                "donemGetiriyb": "1",
                "donemGetiri3y": "1",
                "donemGetiri5y": "1",
                "basTarih": None,
                "bitTarih": None,
                "calismaTipi": 2,
                "getiriOrani": "1",
            }
            for row in self._post(RETURNS_ENDPOINT, body):
                if len(kinds) > 1:
                    row["fonTipi"] = one_kind
                rows.append(row if raw else rename(row, RETURN_FIELDS))
            self._report(index, len(kinds), "returns {}".format(one_kind))
        return rows

    def fund_codes(self, kind: Union[str, Sequence[str], None] = "YAT") -> List[str]:
        """Every fund code of the given kind, in the cheapest possible call."""
        seen = {row["code"] for row in self.returns(kind=kind) if row.get("code")}
        return sorted(seen)

    def history(self, code: str, period: int = 12, raw: bool = False) -> List[dict]:
        """Price history for a single fund by fixed look-back period.

        ``period`` must be one of 1, 3, 6, 12, 36 or 60 months. This endpoint
        exists mainly for parity with the website; ``prices()`` is better for
        anything systematic because it takes real date ranges.
        """
        if period not in VALID_PERIODS:
            raise TefasInvalidParameter(
                "period must be one of {}, got {!r}".format(VALID_PERIODS, period)
            )
        if not code:
            raise TefasInvalidParameter("history() requires a fund code")
        body = {"fonKodu": code.upper(), "dil": "TR", "periyod": period}
        rows = self._post(PRICE_ENDPOINT, body)
        return rows if raw else [rename(row, INFO_FIELDS) for row in rows]

    def snapshot(
        self,
        date: DateLike,
        kind: Union[str, Sequence[str], None] = "YAT",
    ) -> List[dict]:
        """Prices joined to allocations for one date -- everything, two requests."""
        day = parse_date(date)
        prices = self.prices(day, day, kind=kind)
        allocations = self.breakdown(day, day, kind=kind)
        index = {(row.get("code"), row.get("date")): row for row in allocations}

        merged = []
        for row in prices:
            combined = dict(row)
            other = index.get((row.get("code"), row.get("date")))
            if other:
                for key, value in other.items():
                    if key not in ("code", "title", "date"):
                        combined[key] = value
            merged.append(combined)
        return merged

    # -- convenience --------------------------------------------------------

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def to_pandas(rows: Iterable[dict]):
    """Convert rows to a DataFrame with real dtypes. Requires pandas."""
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "pandas is not installed. Install it with: pip install 'tefas-api[pandas]'"
        ) from exc

    frame = pd.DataFrame(list(rows))
    if "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    return frame
