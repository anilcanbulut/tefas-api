"""Command line interface.

    tefas sync                      # bring the local cache up to date
    tefas backfill --years 5        # one-off history load, resumable
    tefas funds                     # list fund codes
    tefas show AFA --days 30        # recent prices for one fund
    tefas top --by return_1y -n 20  # league table from the returns endpoint
    tefas stats                     # what the cache holds
    tefas export prices -o out.csv  # dump a table
"""

import argparse
import csv
import datetime as _dt
import logging
import sys

from . import __version__
from . import canary as _canary
from .client import Tefas, parse_date
from .exceptions import TefasError
from .fields import KIND_LABELS, KINDS
from .ingest import DATASETS, backfill, estimate_requests, refresh_returns, sync
from .ratelimit import (
    DEFAULT_BACKOFF_BASE,
    DEFAULT_BACKOFF_CAP,
    DEFAULT_BACKOFF_JITTER,
    DEFAULT_RATE,
    DEFAULT_WINDOW,
)
from .store import Store, aligned_windows


def _progress(done, total, label):
    bar_width = 24
    filled = int(bar_width * done / total) if total else bar_width
    bar = "#" * filled + "." * (bar_width - filled)
    sys.stderr.write("\r  [{}] {}/{}  {}".format(bar, done, total, label[:46].ljust(46)))
    sys.stderr.flush()
    if done >= total:
        sys.stderr.write("\n")


def _client(args):
    """Build a client from the global flags.

    Kept in one place so every subcommand is tuned the same way, and so the
    flags stay a one-to-one mirror of the ``Tefas(...)`` arguments.
    """
    return Tefas(
        rate=args.rate,
        window=args.window,
        max_retries=args.max_retries,
        timeout=args.timeout,
        backoff_base=args.backoff_base,
        backoff_cap=args.backoff_cap,
        backoff_jitter=args.backoff_jitter,
        progress=None if args.quiet else _progress,
    )


def _kinds(args):
    return list(KINDS) if args.kind == "ALL" else [args.kind]


def _print_table(rows, columns, limit=None):
    if not rows:
        print("No rows.")
        return
    rows = rows[:limit] if limit else rows
    widths = {}
    for column in columns:
        widths[column] = max(
            len(column), max(len(_fmt(row.get(column))) for row in rows)
        )
    header = "  ".join(column.ljust(widths[column]) for column in columns)
    print(header)
    print("-" * len(header))
    for row in rows:
        print("  ".join(_fmt(row.get(c)).ljust(widths[c]) for c in columns))


def _fmt(value):
    if value is None:
        return "-"
    if isinstance(value, float):
        return "{:,.6f}".format(value).rstrip("0").rstrip(".")
    return str(value)


# -- commands ---------------------------------------------------------------


def cmd_sync(args):
    with Store(args.db) as store, _client(args) as api:
        result = sync(
            api,
            store,
            kinds=_kinds(args),
            datasets=args.datasets,
            lookback_days=args.lookback,
        )
        if args.returns:
            count = refresh_returns(api, store, kinds=_kinds(args))
            print("Returns snapshot: {} funds".format(count))
        print(
            "Synced: {requests} requests, {rows} rows, {skipped} windows already "
            "cached.".format(**result)
        )
    return 0


def cmd_backfill(args):
    end = _dt.date.today()
    start = parse_date(args.since) if args.since else end - _dt.timedelta(
        days=int(round(args.years * 365.25))
    )
    kinds = _kinds(args)

    planned = estimate_requests(start, end, kinds, args.datasets)
    minutes = planned / max(args.rate, 1)
    print(
        "Backfill {} .. {} for {} ({} datasets)".format(
            start, end, ", ".join(kinds), len(args.datasets)
        )
    )
    print(
        "  at most {} requests, about {:.0f} min at {}/min".format(
            planned, minutes, args.rate
        )
    )
    if not args.yes:
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1

    with Store(args.db) as store, _client(args) as api:
        result = backfill(
            api, store, start=start, end=end, kinds=kinds, datasets=args.datasets
        )
        print(
            "Done: {requests} requests, {rows} rows, {skipped} windows already "
            "cached.".format(**result)
        )
    return 0


def cmd_funds(args):
    with _client(args) as api:
        rows = api.returns(kind=_kinds(args))
    rows.sort(key=lambda r: (r.get("code") or ""))
    if args.codes_only:
        for row in rows:
            print(row.get("code"))
    else:
        _print_table(rows, ["code", "category", "risk_value", "return_1y", "title"])
        print("\n{} funds.".format(len(rows)))
    return 0


def cmd_show(args):
    code = args.code.upper()
    end = _dt.date.today()
    start = end - _dt.timedelta(days=args.days)

    if args.cached:
        with Store(args.db) as store:
            rows = store.prices(code=code, start=start.isoformat())
    else:
        with _client(args) as api:
            rows = api.prices(start, end, kind=args.kind if args.kind != "ALL" else None,
                              code=code)
    rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    _print_table(
        rows,
        ["date", "code", "price", "market_cap", "number_of_investors"],
        limit=args.limit,
    )
    return 0


def cmd_top(args):
    with _client(args) as api:
        rows = api.returns(kind=_kinds(args))
    key = args.by
    ranked = [r for r in rows if isinstance(r.get(key), (int, float))]
    ranked.sort(key=lambda r: r[key], reverse=not args.ascending)
    _print_table(ranked, ["code", key, "risk_value", "category", "title"], limit=args.n)
    return 0


def cmd_profile(args):
    with _client(args) as api:
        row = api.profile(args.code)
    if not row:
        print("TEFAS does not know fund code {}.".format(args.code.upper()))
        return 1
    print()
    for key, value in row.items():
        print("  {:<22} {}".format(key, _fmt(value)))
    rank, total = row.get("category_rank"), row.get("category_fund_count")
    if rank and total:
        print()
        print("  Ranked {} of {} in {}.".format(rank, total, row.get("category")))
    return 0


def cmd_screen(args):
    end = _dt.date.today() if not args.to else parse_date(args.to)
    start = parse_date(args.since) if args.since else end - _dt.timedelta(days=args.days)

    with Store(args.db) as store:
        rows = store.performance(
            start.isoformat(),
            end.isoformat(),
            min_return=args.min,
            max_return=args.max,
            category=args.category,
            kind=None if args.kind == "ALL" else args.kind,
            limit=args.limit,
        )
    if not rows:
        print(
            "No matches in the cache for {} .. {}. Run `tefas sync` first, or "
            "widen the filters.".format(start, end)
        )
        return 1
    print("{} funds, {} .. {}".format(len(rows), start, end))
    _print_table(
        rows, ["code", "return_pct", "start_price", "end_price", "risk_value",
               "category", "title"]
    )
    return 0


def cmd_categories(args):
    with Store(args.db) as store:
        rows = store.categories()
    if not rows:
        print("No categories cached. Run `tefas sync --returns` first.")
        return 1
    _print_table(rows, ["category", "funds"])
    return 0


def cmd_groups(args):
    end = _dt.date.today() if not args.to else parse_date(args.to)
    start = parse_date(args.since) if args.since else end - _dt.timedelta(days=args.days)

    with Store(args.db) as store:
        rows = store.category_performance(
            start.isoformat(),
            end.isoformat(),
            kind=None if args.kind == "ALL" else args.kind,
            min_funds=args.min_funds,
        )
    print("Category performance, {} .. {}".format(start, end))
    _print_table(
        rows,
        ["category", "funds", "avg_return", "median_return", "worst", "best",
         "spread", "positive_share"],
    )
    return 0


def cmd_canary(args):
    with _client(args) as api:
        results = _canary.run(api, code=args.code, kind=args.kind)
    print()
    print(_canary.report(results))
    return _canary.exit_code(results)


def cmd_stats(args):
    with Store(args.db) as store:
        info = store.stats()
    print("Cache: {}".format(info.pop("path")))
    for key, value in info.items():
        print("  {:<12} {}".format(key, value if value is not None else "-"))
    return 0


def cmd_export(args):
    with Store(args.db) as store:
        rows = store.query("SELECT * FROM {}".format(args.table))
    if not rows:
        print("Nothing to export from {}.".format(args.table))
        return 1
    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print("Wrote {} rows to {}".format(len(rows), args.out))
    return 0


def cmd_kinds(args):
    for kind in KINDS:
        print("  {:<5} {}".format(kind, KIND_LABELS[kind]))
    return 0


# -- parser -----------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="tefas", description="Read fund data directly from TEFAS."
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--db", default="tefas.db", help="SQLite cache path")
    parser.add_argument("--kind", default="YAT", choices=list(KINDS) + ["ALL"])
    parser.add_argument("-q", "--quiet", action="store_true", help="no progress bar")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    # One flag per Tefas(...) argument, so the same tuning is reachable from
    # the shell as from Python.
    throttle = parser.add_argument_group(
        "throttling",
        "TEFAS allows 5 requests/minute per IP and sends no Retry-After.",
    )
    throttle.add_argument(
        "--rate", type=int, default=DEFAULT_RATE,
        help="requests per window (TEFAS refuses at 6; default {})".format(DEFAULT_RATE),
    )
    throttle.add_argument(
        "--window", type=float, default=DEFAULT_WINDOW,
        help="rate-limit window in seconds (default {:.0f})".format(DEFAULT_WINDOW),
    )
    throttle.add_argument(
        "--max-retries", type=int, default=5, dest="max_retries",
        help="retries after a 429 (default 5)",
    )
    throttle.add_argument(
        "--timeout", type=int, default=90,
        help="per-request socket timeout in seconds (default 90)",
    )
    throttle.add_argument(
        "--backoff-base", type=float, default=DEFAULT_BACKOFF_BASE,
        dest="backoff_base",
        help="first backoff after a 429, seconds (default {:.0f})".format(
            DEFAULT_BACKOFF_BASE),
    )
    throttle.add_argument(
        "--backoff-cap", type=float, default=DEFAULT_BACKOFF_CAP, dest="backoff_cap",
        help="maximum backoff, seconds (default {:.0f})".format(DEFAULT_BACKOFF_CAP),
    )
    throttle.add_argument(
        "--backoff-jitter", type=float, default=DEFAULT_BACKOFF_JITTER,
        dest="backoff_jitter",
        help="backoff randomisation, 0-1 (default {})".format(DEFAULT_BACKOFF_JITTER),
    )

    subparsers = parser.add_subparsers(dest="command")

    sync_parser = subparsers.add_parser("sync", help="bring the cache up to date")
    sync_parser.add_argument("--lookback", type=int, default=10,
                             help="days of overlap to re-fetch (default 10)")
    sync_parser.add_argument("--datasets", nargs="+", default=list(DATASETS),
                             choices=list(DATASETS))
    sync_parser.add_argument("--returns", action="store_true",
                             help="also snapshot trailing returns")
    sync_parser.set_defaults(func=cmd_sync)

    back_parser = subparsers.add_parser("backfill", help="load history, resumable")
    back_parser.add_argument("--years", type=float, default=5.0)
    back_parser.add_argument("--since", help="explicit start date (YYYY-MM-DD)")
    back_parser.add_argument("--datasets", nargs="+", default=list(DATASETS),
                             choices=list(DATASETS))
    back_parser.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    back_parser.set_defaults(func=cmd_backfill)

    funds_parser = subparsers.add_parser("funds", help="list funds")
    funds_parser.add_argument("--codes-only", action="store_true")
    funds_parser.set_defaults(func=cmd_funds)

    show_parser = subparsers.add_parser("show", help="recent prices for one fund")
    show_parser.add_argument("code")
    show_parser.add_argument("--days", type=int, default=28)
    show_parser.add_argument("--limit", type=int, default=40)
    show_parser.add_argument("--cached", action="store_true", help="read the cache only")
    show_parser.set_defaults(func=cmd_show)

    top_parser = subparsers.add_parser("top", help="rank funds by a return column")
    top_parser.add_argument("--by", default="return_1y")
    top_parser.add_argument("-n", type=int, default=20)
    top_parser.add_argument("--ascending", action="store_true")
    top_parser.set_defaults(func=cmd_top)

    profile_parser = subparsers.add_parser("profile", help="summary card for one fund")
    profile_parser.add_argument("code")
    profile_parser.set_defaults(func=cmd_profile)

    screen_parser = subparsers.add_parser(
        "screen", help="filter cached funds by return over any window"
    )
    screen_parser.add_argument("--days", type=int, default=30,
                               help="window length ending today (default 30)")
    screen_parser.add_argument("--since", help="explicit start date")
    screen_parser.add_argument("--to", help="explicit end date")
    screen_parser.add_argument("--min", type=float, help="minimum return %%")
    screen_parser.add_argument("--max", type=float, help="maximum return %%")
    screen_parser.add_argument("--category", help='substring, e.g. "Hisse Senedi"')
    screen_parser.add_argument("--limit", type=int, default=30)
    screen_parser.set_defaults(func=cmd_screen)

    categories_parser = subparsers.add_parser(
        "categories", help="categories in the cached returns snapshot"
    )
    categories_parser.set_defaults(func=cmd_categories)

    groups_parser = subparsers.add_parser(
        "groups", help="how each fund category performed over a window"
    )
    groups_parser.add_argument("--days", type=int, default=90)
    groups_parser.add_argument("--since", help="explicit start date")
    groups_parser.add_argument("--to", help="explicit end date")
    groups_parser.add_argument("--min-funds", type=int, default=1,
                               dest="min_funds", help="ignore thin categories")
    groups_parser.set_defaults(func=cmd_groups)

    canary_parser = subparsers.add_parser(
        "canary", help="check the API still works and has not changed shape"
    )
    canary_parser.add_argument("--code", default="AFA",
                               help="fund used for the profile check")
    canary_parser.set_defaults(func=cmd_canary)

    stats_parser = subparsers.add_parser("stats", help="what the cache holds")
    stats_parser.set_defaults(func=cmd_stats)

    export_parser = subparsers.add_parser("export", help="dump a table to CSV")
    export_parser.add_argument("table", choices=["prices", "breakdown", "returns"])
    export_parser.add_argument("-o", "--out", required=True)
    export_parser.set_defaults(func=cmd_export)

    kinds_parser = subparsers.add_parser("kinds", help="list fund types")
    kinds_parser.set_defaults(func=cmd_kinds)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not getattr(args, "func", None):
        parser.print_help()
        return 1

    try:
        return args.func(args)
    except TefasError as exc:
        print("TEFAS error: {}".format(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Progress is saved; re-run to resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
