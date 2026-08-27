import datetime as dt

import pytest

from tefas.client import MAX_WINDOW_DAYS, date_windows, parse_date
from tefas.exceptions import TefasInvalidParameter
from tefas.store import BUCKET_DAYS, aligned_windows


@pytest.mark.parametrize(
    "value",
    ["2026-08-26", "20260826", "26.08.2026", "26/08/2026", dt.date(2026, 8, 26)],
)
def test_parse_date_accepts_common_formats(value):
    assert parse_date(value) == dt.date(2026, 8, 26)


def test_parse_date_takes_the_date_from_a_datetime():
    assert parse_date(dt.datetime(2026, 8, 26, 19, 30)) == dt.date(2026, 8, 26)


def test_parse_date_rejects_nonsense():
    with pytest.raises(TefasInvalidParameter):
        parse_date("last tuesday")


def test_windows_never_exceed_the_api_month_cap():
    start, end = dt.date(2024, 1, 1), dt.date(2026, 8, 26)
    for win_start, win_end in date_windows(start, end):
        assert (win_end - win_start).days + 1 <= MAX_WINDOW_DAYS


def test_windows_are_contiguous_and_cover_the_range():
    start, end = dt.date(2026, 1, 1), dt.date(2026, 8, 26)
    windows = list(date_windows(start, end))
    assert windows[0][0] == start
    assert windows[-1][1] == end
    for (_, prev_end), (next_start, _) in zip(windows, windows[1:]):
        assert next_start == prev_end + dt.timedelta(days=1)


def test_single_day_range_is_one_window():
    day = dt.date(2026, 8, 26)
    assert list(date_windows(day, day)) == [(day, day)]


def test_backwards_range_is_rejected():
    with pytest.raises(TefasInvalidParameter):
        list(date_windows(dt.date(2026, 8, 26), dt.date(2026, 8, 1)))


def test_aligned_windows_are_stable_across_different_callers():
    """The whole cache design depends on this: two overlapping requests must
    produce the same bucket boundaries, or nothing ever hits."""
    first = {b for b, _, _ in aligned_windows(dt.date(2026, 3, 5), dt.date(2026, 8, 26))}
    second = {b for b, _, _ in aligned_windows(dt.date(2026, 1, 17), dt.date(2026, 9, 30))}
    assert first <= second


def test_aligned_windows_are_clipped_to_the_requested_range():
    start, end = dt.date(2026, 5, 10), dt.date(2026, 6, 20)
    windows = list(aligned_windows(start, end))
    assert windows[0][1] == start
    assert windows[-1][2] == end
    for _, win_start, win_end in windows:
        assert (win_end - win_start).days + 1 <= BUCKET_DAYS
