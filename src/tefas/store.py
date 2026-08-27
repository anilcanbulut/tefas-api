"""Local SQLite cache for TEFAS data.

TEFAS is an ingest source, not a query layer. Anything that reads fund data
repeatedly -- a dashboard, a screener, a backtest -- should read from here, and
let this module be the only thing that ever talks to the network. That keeps
queries at millisecond latency, removes the rate limit from the read path, and
preserves history if TEFAS changes or withdraws an endpoint.

Windows are aligned to fixed 28-day buckets counted from a fixed epoch rather
than from whatever date the caller happened to pass. That makes them identical
across runs, so a completed fetch can be recorded once and skipped forever
after -- which is what makes repeated backfills cheap instead of rude.
"""

import datetime as _dt
import json
import os
import sqlite3
from typing import Iterable, List, Optional, Sequence

from .exceptions import EmptyCategoryData
from .fields import BREAKDOWN_FIELDS, INFO_FIELDS, RETURN_FIELDS

#: Arbitrary but fixed anchor for window alignment. Never change it: doing so
#: invalidates every previously recorded fetch.
EPOCH = _dt.date(2000, 1, 1)

BUCKET_DAYS = 28

_PRICE_COLUMNS = [
    name for name in INFO_FIELDS.values() if name not in ("code", "date")
]
_BREAKDOWN_COLUMNS = [
    name for name in BREAKDOWN_FIELDS.values() if name not in ("code", "date", "title")
]
_RETURN_COLUMNS = [name for name in RETURN_FIELDS.values() if name != "code"]


def aligned_windows(start: _dt.date, end: _dt.date):
    """Yield ``(bucket_index, window_start, window_end)`` covering the range.

    Buckets are the same for every caller, so two runs asking for overlapping
    ranges produce the same windows and the cache actually hits.
    """
    first = (start - EPOCH).days // BUCKET_DAYS
    last = (end - EPOCH).days // BUCKET_DAYS
    for index in range(first, last + 1):
        win_start = EPOCH + _dt.timedelta(days=index * BUCKET_DAYS)
        win_end = win_start + _dt.timedelta(days=BUCKET_DAYS - 1)
        yield index, max(win_start, start), min(win_end, end)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class Store:
    """SQLite-backed cache. Safe to use from one process at a time."""

    def __init__(self, path: str = "tefas.db"):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self._create_schema()

    # -- schema -------------------------------------------------------------

    def _create_schema(self):
        price_cols = ",\n  ".join(_quote(c) + " REAL" for c in _PRICE_COLUMNS if c != "title")
        breakdown_cols = ",\n  ".join(_quote(c) + " REAL" for c in _BREAKDOWN_COLUMNS)
        # Declared without a type on purpose. Return columns mix floats
        # (return_1y), strings (risk_value arrives as "6") and booleans, and a
        # column with no declared type keeps whatever it is given instead of
        # coercing numbers to text the way TEXT affinity would.
        return_cols = ",\n  ".join(_quote(c) for c in _RETURN_COLUMNS)

        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS prices (
              code TEXT NOT NULL,
              date TEXT NOT NULL,
              kind TEXT,
              title TEXT,
              {price_cols},
              PRIMARY KEY (code, date)
            );

            CREATE TABLE IF NOT EXISTS breakdown (
              code TEXT NOT NULL,
              date TEXT NOT NULL,
              kind TEXT,
              title TEXT,
              {breakdown_cols},
              extra TEXT,
              PRIMARY KEY (code, date)
            );

            CREATE TABLE IF NOT EXISTS returns (
              code TEXT NOT NULL,
              fetched_on TEXT NOT NULL,
              kind TEXT,
              {return_cols},
              PRIMARY KEY (code, fetched_on)
            );

            CREATE TABLE IF NOT EXISTS fetch_log (
              dataset TEXT NOT NULL,
              kind TEXT NOT NULL,
              bucket INTEGER NOT NULL,
              rows INTEGER,
              fetched_at TEXT,
              PRIMARY KEY (dataset, kind, bucket)
            );

            CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);
            CREATE INDEX IF NOT EXISTS idx_prices_code ON prices(code);
            CREATE INDEX IF NOT EXISTS idx_breakdown_date ON breakdown(date);
            """.format(
                price_cols=price_cols,
                breakdown_cols=breakdown_cols,
                return_cols=return_cols,
            )
        )
        self.conn.commit()

    # -- writes -------------------------------------------------------------

    def _upsert(self, table: str, known: Sequence[str], rows: Iterable[dict],
                kind: Optional[str], collect_extra: bool):
        rows = list(rows)
        if not rows:
            return 0

        columns = ["code", "date", "kind"] + list(known)
        if collect_extra:
            columns.append("extra")

        placeholders = ", ".join("?" for _ in columns)
        assignments = ", ".join(
            "{0}=excluded.{0}".format(_quote(c)) for c in columns if c not in ("code", "date")
        )
        sql = "INSERT INTO {table} ({cols}) VALUES ({vals}) " \
              "ON CONFLICT(code, date) DO UPDATE SET {upd}".format(
                  table=table,
                  cols=", ".join(_quote(c) for c in columns),
                  vals=placeholders,
                  upd=assignments,
              )

        # fonTipi is stored in its own `kind` column, so keep it out of `extra`.
        known_set = set(known) | {"code", "date", "fonTipi"}
        payload = []
        for row in rows:
            values = [row.get("code"), row.get("date"), kind or row.get("fonTipi")]
            values.extend(row.get(name) for name in known)
            if collect_extra:
                leftovers = {k: v for k, v in row.items() if k not in known_set}
                values.append(json.dumps(leftovers, ensure_ascii=False) if leftovers else None)
            payload.append(values)

        self.conn.executemany(sql, payload)
        self.conn.commit()
        return len(payload)

    def upsert_prices(self, rows, kind=None):
        return self._upsert("prices", ["title"] + [c for c in _PRICE_COLUMNS if c != "title"],
                            rows, kind, collect_extra=False)

    def upsert_breakdown(self, rows, kind=None):
        return self._upsert("breakdown", ["title"] + _BREAKDOWN_COLUMNS,
                            rows, kind, collect_extra=True)

    def upsert_returns(self, rows, kind=None, fetched_on=None):
        rows = list(rows)
        if not rows:
            return 0
        stamp = fetched_on or _dt.date.today().isoformat()
        columns = ["code", "fetched_on", "kind"] + _RETURN_COLUMNS
        assignments = ", ".join(
            "{0}=excluded.{0}".format(_quote(c))
            for c in columns
            if c not in ("code", "fetched_on")
        )
        sql = "INSERT INTO returns ({cols}) VALUES ({vals}) " \
              "ON CONFLICT(code, fetched_on) DO UPDATE SET {upd}".format(
                  cols=", ".join(_quote(c) for c in columns),
                  vals=", ".join("?" for _ in columns),
                  upd=assignments,
              )
        payload = [
            [row.get("code"), stamp, kind or row.get("fonTipi")]
            + [row.get(name) for name in _RETURN_COLUMNS]
            for row in rows
        ]
        self.conn.executemany(sql, payload)
        self.conn.commit()
        return len(payload)

    # -- fetch bookkeeping --------------------------------------------------

    def mark_fetched(self, dataset: str, kind: str, bucket: int, rows: int):
        self.conn.execute(
            "INSERT INTO fetch_log (dataset, kind, bucket, rows, fetched_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(dataset, kind, bucket) DO UPDATE SET "
            "rows=excluded.rows, fetched_at=excluded.fetched_at",
            (dataset, kind, bucket, rows, _dt.datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def fetched_buckets(self, dataset: str, kind: str) -> set:
        cursor = self.conn.execute(
            "SELECT bucket FROM fetch_log WHERE dataset = ? AND kind = ?",
            (dataset, kind),
        )
        return {row[0] for row in cursor}

    def pending_windows(self, dataset: str, kind: str, start: _dt.date, end: _dt.date,
                        refetch_last: bool = True):
        """Windows still needing a fetch.

        The bucket containing ``end`` is always re-fetched by default: it is
        usually still filling up, so a completed marker on it would freeze
        today's data out of the cache.
        """
        done = self.fetched_buckets(dataset, kind)
        last_bucket = (end - EPOCH).days // BUCKET_DAYS
        for bucket, win_start, win_end in aligned_windows(start, end):
            if bucket in done and not (refetch_last and bucket == last_bucket):
                continue
            yield bucket, win_start, win_end

    # -- reads --------------------------------------------------------------

    def latest_date(self, table: str = "prices") -> Optional[str]:
        row = self.conn.execute(
            "SELECT MAX(date) FROM {}".format(table)
        ).fetchone()
        return row[0] if row else None

    def query(self, sql: str, params: Sequence = ()) -> List[dict]:
        cursor = self.conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    def prices(self, code: Optional[str] = None, start: Optional[str] = None,
               end: Optional[str] = None, limit: Optional[int] = None) -> List[dict]:
        clauses, params = [], []
        if code:
            clauses.append("code = ?")
            params.append(code.upper())
        if start:
            clauses.append("date >= ?")
            params.append(start)
        if end:
            clauses.append("date <= ?")
            params.append(end)
        sql = "SELECT * FROM prices"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY date DESC, code"
        if limit:
            sql += " LIMIT {}".format(int(limit))
        return self.query(sql, params)

    def stats(self) -> dict:
        out = {}
        for table in ("prices", "breakdown", "returns"):
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM {}".format(table)
            ).fetchone()
            out[table] = row["n"] if row else 0
        span = self.conn.execute(
            "SELECT MIN(date) AS lo, MAX(date) AS hi FROM prices"
        ).fetchone()
        out["first_date"] = span["lo"] if span else None
        out["last_date"] = span["hi"] if span else None
        out["funds"] = self.conn.execute(
            "SELECT COUNT(DISTINCT code) AS n FROM prices"
        ).fetchone()["n"]
        out["path"] = os.path.abspath(self.path)
        return out

    def performance(self, start: str, end: str, min_return: Optional[float] = None,
                    max_return: Optional[float] = None, category: Optional[str] = None,
                    kind: Optional[str] = None, limit: Optional[int] = None) -> List[dict]:
        """Screen funds by actual return between two dates, computed from prices.

        This is the query the returns endpoint cannot answer: it uses whatever
        window you ask for rather than TEFAS's fixed 1m/3m/6m/1y periods, and it
        costs no requests because it reads cached prices.

        Each fund is measured between its own first and last quote inside the
        window, so a fund that started trading mid-window is still reported
        honestly rather than being compared against a price it never had.

        ``category`` matches the most recent stored returns snapshot and is a
        substring match, so "Hisse Senedi" catches the umbrella category.
        """
        if category and not self.conn.execute(
            "SELECT 1 FROM returns LIMIT 1"
        ).fetchone():
            raise EmptyCategoryData(
                "Cannot filter by category: no returns snapshot has been stored, "
                "and category names come from it. Run `tefas sync --returns` "
                "(or refresh_returns()) first."
            )

        sql = """
            WITH bounds AS (
              SELECT code, MIN(date) AS d0, MAX(date) AS d1
              FROM prices
              WHERE date BETWEEN ? AND ?
              GROUP BY code
            ),
            latest_returns AS (
              SELECT r.code, r.category, r.risk_value
              FROM returns r
              JOIN (
                SELECT code, MAX(fetched_on) AS fetched_on
                FROM returns GROUP BY code
              ) newest
                ON newest.code = r.code AND newest.fetched_on = r.fetched_on
            )
            SELECT
              b.code,
              p1.title,
              b.d0 AS start_date,
              b.d1 AS end_date,
              p0.price AS start_price,
              p1.price AS end_price,
              (p1.price / p0.price - 1.0) * 100.0 AS return_pct,
              p1.market_cap,
              p1.number_of_investors,
              p1.kind,
              lr.category,
              lr.risk_value
            FROM bounds b
            JOIN prices p0 ON p0.code = b.code AND p0.date = b.d0
            JOIN prices p1 ON p1.code = b.code AND p1.date = b.d1
            LEFT JOIN latest_returns lr ON lr.code = b.code
            WHERE p0.price > 0 AND b.d0 <> b.d1
        """
        params: List = [start, end]

        if min_return is not None:
            sql += " AND (p1.price / p0.price - 1.0) * 100.0 >= ?"
            params.append(min_return)
        if max_return is not None:
            sql += " AND (p1.price / p0.price - 1.0) * 100.0 <= ?"
            params.append(max_return)
        if category:
            sql += " AND lr.category LIKE ?"
            params.append("%{}%".format(category))
        if kind:
            sql += " AND p1.kind = ?"
            params.append(kind)

        sql += " ORDER BY return_pct DESC"
        if limit:
            sql += " LIMIT {}".format(int(limit))
        return self.query(sql, params)

    def category_performance(self, start: str, end: str, kind: Optional[str] = None,
                             min_funds: int = 1) -> List[dict]:
        """How each fund category performed over a window.

        Built on top of :meth:`performance`, deliberately, so the group view and
        the per-fund view can never disagree about what a return is or which
        funds count.

        Dispersion is usually the more useful half of the answer: money market
        funds land in a fraction of a point of each other while serbest funds
        span tens of points, and an average alone hides that. So ``spread`` and
        ``positive_share`` are reported alongside the mean, and the median is
        given because a single extreme fund can drag a category's mean well
        away from its typical member.
        """
        rows = self.performance(start, end, kind=kind)

        grouped = {}
        for row in rows:
            category = row.get("category")
            if not category:
                continue
            grouped.setdefault(category, []).append(row["return_pct"])

        if not grouped:
            raise EmptyCategoryData(
                "No categories are stored, so returns cannot be grouped. Run "
                "`tefas sync --returns` (or refresh_returns()) first."
            )

        summary = []
        for category, values in grouped.items():
            if len(values) < min_funds:
                continue
            values.sort()
            count = len(values)
            middle = count // 2
            median = (
                values[middle]
                if count % 2
                else (values[middle - 1] + values[middle]) / 2.0
            )
            summary.append(
                {
                    "category": category,
                    "funds": count,
                    "avg_return": sum(values) / count,
                    "median_return": median,
                    "worst": values[0],
                    "best": values[-1],
                    "spread": values[-1] - values[0],
                    "positive_share": sum(1 for v in values if v > 0) * 100.0 / count,
                }
            )

        summary.sort(key=lambda item: item["avg_return"], reverse=True)
        return summary

    def categories(self) -> List[dict]:
        """Categories present in the most recent stored returns snapshot."""
        return self.query(
            "SELECT category, COUNT(*) AS funds FROM returns "
            "WHERE fetched_on = (SELECT MAX(fetched_on) FROM returns) "
            "AND category IS NOT NULL GROUP BY category ORDER BY funds DESC"
        )

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
