"""Hour-by-hour arithmetic keeps gaps as gaps."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

series = load("insights.series")
T0 = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)
H = lambda i: T0 + timedelta(hours=i)  # noqa: E731


def test_signed_sum_hour_by_hour():
    s = {"imp": [(H(0), 2.0), (H(1), 3.0)], "exp": [(H(0), 0.5), (H(1), 0.0)], "pv": [(H(0), 1.0), (H(1), 4.0)]}
    out = series.combine(s, [("imp", 1.0), ("exp", -1.0), ("pv", 1.0)])
    assert out == [(H(0), 2.5), (H(1), 7.0)]


def test_a_gap_in_any_term_is_a_gap_in_the_result():
    s = {"imp": [(H(0), 2.0), (H(1), 3.0), (H(2), 1.0)], "pv": [(H(0), 1.0), (H(2), 1.0)]}
    out = series.combine(s, [("imp", 1.0), ("pv", 1.0)])
    assert [t for t, _ in out] == [H(0), H(2)]


def test_none_values_are_gaps_too():
    s = {"imp": [(H(0), 2.0), (H(1), None)]}
    assert series.combine(s, [("imp", 1.0)]) == [(H(0), 2.0)]


def test_subtract_devices_never_below_zero_and_keeps_the_gap_rule():
    """'a' is metered from H(0) but has no H(2): a dropout, so H(2) is a gap."""
    base = [(H(0), 5.0), (H(1), 1.0), (H(2), 4.0)]
    parts = {"a": [(H(0), 2.0), (H(1), 3.0)], "b": [(H(0), 1.0), (H(1), 0.5), (H(2), 1.0)]}
    out = series.subtract_all(base, parts, ["a", "b"])
    assert out == [(H(0), 2.0), (H(1), 0.0)]


def test_a_device_counts_as_zero_before_it_was_metered():
    """Before its first row a device's energy was unmetered - i.e. IN the
    remainder - so the remainder keeps those hours instead of losing them.
    This is the Kozolec case: two lamps added three weeks ago must not cut
    ten weeks of history to three."""
    base = [(H(0), 5.0), (H(1), 5.0), (H(2), 5.0)]
    parts = {"old": [(H(0), 1.0), (H(1), 1.0), (H(2), 1.0)], "new": [(H(2), 2.0)]}
    out = series.subtract_all(base, parts, ["old", "new"])
    assert out == [(H(0), 4.0), (H(1), 4.0), (H(2), 2.0)]


def test_a_device_with_no_statistics_at_all_is_zero_throughout():
    base = [(H(0), 5.0), (H(1), 5.0)]
    out = series.subtract_all(base, {"a": [(H(0), 1.0), (H(1), 1.0)], "ghost": []}, ["a", "ghost"])
    assert out == [(H(0), 4.0), (H(1), 4.0)]


def test_coverage_reports_when_the_remainder_became_complete_and_who_is_missing():
    parts = {"old": [(H(0), 1.0), (H(5), 1.0)], "new": [(H(3), 2.0), (H(4), 2.0)], "ghost": []}
    since, missing = series.coverage(parts, ["old", "new", "ghost"])
    assert since == H(3) and missing == ["ghost"]
    assert series.coverage({}, []) == (None, [])


def test_no_devices_returns_the_base():
    base = [(H(0), 5.0)]
    assert series.subtract_all(base, {}, []) == base


if __name__ == "__main__":
    run_main(globals())
