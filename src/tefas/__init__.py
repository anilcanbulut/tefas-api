"""tefas-api: a direct, rate-limit-safe reader for the TEFAS fund platform.

    from tefas import Tefas

    with Tefas() as api:
        rows = api.prices("2026-08-01", "2026-08-26")   # every fund, 1 request

TEFAS offers no official API. This package talks to the undocumented JSON
endpoints behind tefas.gov.tr directly, in bulk, and stays inside the observed
rate limit by default.
"""

from .client import (
    BREAKDOWN_ENDPOINT,
    INFO_ENDPOINT,
    MAX_WINDOW_DAYS,
    PRICE_ENDPOINT,
    PROFILE_ENDPOINT,
    RETURNS_ENDPOINT,
    Tefas,
    date_windows,
    parse_date,
    to_pandas,
)
from .exceptions import (
    EmptyCategoryData,
    TefasAPIError,
    TefasError,
    TefasHTTPError,
    TefasInvalidParameter,
    TefasRateLimitError,
)
from .fields import (
    BREAKDOWN_FIELDS,
    INFO_FIELDS,
    KIND_LABELS,
    KINDS,
    PROFILE_FIELDS,
    RETURN_FIELDS,
    UNMAPPED_BREAKDOWN_KEYS,
)
from .ingest import backfill, estimate_requests, refresh_returns, sync
from .ratelimit import OBSERVED_LIMIT, SlidingWindowLimiter
from .store import Store

__version__ = "0.1.0"

__all__ = [
    "Tefas",
    "Store",
    "to_pandas",
    "parse_date",
    "date_windows",
    "backfill",
    "sync",
    "refresh_returns",
    "estimate_requests",
    "SlidingWindowLimiter",
    "OBSERVED_LIMIT",
    "MAX_WINDOW_DAYS",
    "KINDS",
    "KIND_LABELS",
    "INFO_FIELDS",
    "BREAKDOWN_FIELDS",
    "RETURN_FIELDS",
    "PROFILE_FIELDS",
    "UNMAPPED_BREAKDOWN_KEYS",
    "INFO_ENDPOINT",
    "BREAKDOWN_ENDPOINT",
    "RETURNS_ENDPOINT",
    "PRICE_ENDPOINT",
    "PROFILE_ENDPOINT",
    "TefasError",
    "TefasAPIError",
    "TefasHTTPError",
    "TefasRateLimitError",
    "TefasInvalidParameter",
    "EmptyCategoryData",
    "__version__",
]
