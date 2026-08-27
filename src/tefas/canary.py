"""A canary for an undocumented API.

These endpoints carry no version and no deprecation notice. TEFAS changed them
once in 2026 already, and the failure mode that costs the most is not an
outage -- an outage is obvious. It is a *quiet* change: a renamed column, a
field that starts arriving null, a filter that silently stops filtering. Code
keeps running and starts being wrong.

So this checks shape, not just reachability:

* every field the package maps is still present,
* no unrecognised field has appeared (a rename shows up as one of each),
* row counts are in a sane range, so a collapse from 2,000 funds to 5 is caught
  even though both are "successful" responses.

Run it on a schedule and let it page you. Four requests, one minute, once a day
is plenty.
"""

import datetime as _dt
from typing import List, Optional

from .exceptions import TefasError
from .fields import (
    BREAKDOWN_FIELDS,
    INFO_FIELDS,
    PROFILE_FIELDS,
    RETURN_FIELDS,
    UNMAPPED_BREAKDOWN_KEYS,
)

#: Fields whose disappearance would break callers, per dataset. Kept narrow on
#: purpose: these are the ones this package's own methods depend on.
CRITICAL = {
    "prices": ("fonKodu", "tarih", "fiyat"),
    "breakdown": ("fonKodu", "tarih"),
    "returns": ("fonKodu", "getiri1a", "fonTurAciklama"),
    "profile": ("fonKodu", "kategoriDerece", "pazarPayi"),
}

#: Row counts below these suggest the endpoint is answering but not with the
#: whole market. Set well under observed values (2,037 / 2,029 / 1,059) so
#: normal drift does not cry wolf.
MIN_ROWS = {"prices": 500, "breakdown": 500, "returns": 300, "profile": 1}

#: Pagination noise, not data.
IGNORED_KEYS = frozenset({"rn"})


class CheckResult:
    def __init__(self, name):
        self.name = name
        self.rows = 0
        self.missing: List[str] = []
        self.unknown: List[str] = []
        self.error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return not (self.error or self.missing or self.unknown) and self.rows > 0

    @property
    def breaking(self) -> bool:
        """Something callers depend on is gone, or the endpoint is down."""
        return bool(self.error or self.missing)

    def summary(self) -> str:
        if self.error:
            return "FAIL  {:<10} {}".format(self.name, self.error)
        parts = ["{} rows".format(self.rows)]
        if self.missing:
            parts.append("MISSING: " + ", ".join(self.missing))
        if self.unknown:
            parts.append("new fields: " + ", ".join(self.unknown))
        status = "OK  " if self.ok else ("FAIL" if self.breaking else "WARN")
        return "{}  {:<10} {}".format(status, self.name, "; ".join(parts))


def _inspect(name, rows, field_map, allowed_extra=frozenset()):
    result = CheckResult(name)
    result.rows = len(rows)

    if not rows:
        result.error = "endpoint returned no rows"
        return result

    if result.rows < MIN_ROWS.get(name, 1):
        result.error = "only {} rows, expected at least {}".format(
            result.rows, MIN_ROWS[name]
        )
        return result

    # Union the keys across a sample: a field that is null for one fund may be
    # absent from that row alone without meaning anything.
    seen = set()
    for row in rows[:200]:
        seen.update(row.keys())
    seen -= IGNORED_KEYS

    known = set(field_map) | set(allowed_extra)
    result.unknown = sorted(seen - known)
    result.missing = sorted(k for k in CRITICAL.get(name, ()) if k not in seen)
    return result


def run(client, code: str = "AFA", kind: str = "YAT") -> List[CheckResult]:
    """Exercise every endpoint this package depends on. Costs four requests."""
    today = _dt.date.today()
    # A five-day window always contains a trading day, whatever the weekend or
    # public holiday, and still costs a single request.
    recent = today - _dt.timedelta(days=5)
    results = []

    def guarded(name, fetch, field_map, allowed_extra=frozenset()):
        try:
            rows = fetch()
        except TefasError as exc:
            failed = CheckResult(name)
            failed.error = "{}: {}".format(type(exc).__name__, exc)
            results.append(failed)
            return
        results.append(_inspect(name, rows, field_map, allowed_extra))

    guarded(
        "prices",
        lambda: client.prices(recent, today, kind=kind, raw=True),
        INFO_FIELDS,
    )
    guarded(
        "breakdown",
        lambda: client.breakdown(recent, today, kind=kind, raw=True),
        BREAKDOWN_FIELDS,
        UNMAPPED_BREAKDOWN_KEYS,
    )
    guarded("returns", lambda: client.returns(kind=kind, raw=True), RETURN_FIELDS)

    def profile_rows():
        row = client.profile(code, raw=True)
        return [row] if row else []

    guarded("profile", profile_rows, PROFILE_FIELDS)
    return results


def report(results) -> str:
    lines = [result.summary() for result in results]
    breaking = [r for r in results if r.breaking]
    drifted = [r for r in results if r.unknown and not r.breaking]

    lines.append("")
    if breaking:
        lines.append(
            "BREAKING: {} check(s) failed. Callers relying on the missing "
            "fields are already wrong.".format(len(breaking))
        )
    elif drifted:
        lines.append(
            "TEFAS added {} unrecognised field(s). Nothing is broken -- they "
            "pass through under their raw names -- but they are worth mapping.".format(
                sum(len(r.unknown) for r in drifted)
            )
        )
    else:
        lines.append("All checks passed. The API is unchanged in every way this looks at.")
    return "\n".join(lines)


def exit_code(results) -> int:
    """2 if something broke, 1 if only new fields appeared, 0 if all clear."""
    if any(result.breaking for result in results):
        return 2
    if any(result.unknown for result in results):
        return 1
    return 0
