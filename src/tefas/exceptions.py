"""Exception hierarchy for the TEFAS client."""


class TefasError(Exception):
    """Base class for every error raised by this package."""


class TefasHTTPError(TefasError):
    """A transport-level failure: connection reset, timeout, 5xx."""


class TefasRateLimitError(TefasError):
    """The gateway returned HTTP 429 and retries were exhausted.

    TEFAS sends no ``Retry-After`` header, so the client backs off on its own
    clock. Seeing this means the configured retry budget was not enough.
    """


class TefasAPIError(TefasError):
    """TEFAS reported a logical error.

    The API answers malformed queries with ``HTTP 200`` and a populated
    ``errorMessage`` body, so this is raised from the payload, never from the
    status code.
    """

    def __init__(self, message: str, error_code=None):
        super().__init__(message)
        self.error_code = error_code


class TefasInvalidParameter(TefasError, ValueError):
    """A argument was rejected before any request was made."""


class EmptyCategoryData(TefasError):
    """A category filter was used before any returns snapshot was stored.

    Category names live in the returns snapshot, so filtering by one against a
    cache that has never seen it would quietly match nothing -- which reads as
    "no funds qualify" rather than "you have not synced categories yet".
    """
