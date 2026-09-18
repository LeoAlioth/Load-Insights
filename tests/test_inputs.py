"""Any sensor as an explanatory input: labelled, projected, fitted."""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

I = load("insights.inputs")
CAL = load("insights.calendars")
P = load("insights.profile")
TZ = ZoneInfo("Europe/Ljubljana")
NOW = datetime(2026, 9, 16, 10, 30, tzinfo=TZ)
OFFSET = 7200.0          # Europe/Ljubljana in September


def keys(hours):
    return [h.timestamp() for h in hours]


def test_a_sensor_with_few_values_is_used_as_it_is():
    raw = {float(i) * 3600: ["2", "3", "4"][i % 3] for i in range(300)}
    labels, kind = I.label_history(raw)
    assert kind == "categorical"
    assert set(labels.values()) == {"2", "3", "4"} and len(labels) == 300


def test_a_numeric_sensor_is_cut_into_bands():
    raw = {float(i) * 3600: float(i) for i in range(400)}
    labels, kind = I.label_history(raw)
    assert kind == "banded"
    assert set(labels.values()) == {"band 1", "band 2", "band 3", "band 4"}
    counts = {b: sum(1 for v in labels.values() if v == b) for b in set(labels.values())}
    assert max(counts.values()) - min(counts.values()) <= 2, counts


def test_free_text_with_many_values_is_unusable_rather_than_guessed_at():
    raw = {float(i) * 3600: "note %d" % i for i in range(300)}
    labels, kind = I.label_history(raw)
    assert kind == "unusable" and labels == {}
    assert I.label_history({}) == ({}, "empty")


def test_a_weekday_and_hour_schedule_projects_perfectly():
    """A tariff is a function of weekday and hour, so its future is knowable
    from its past - the point of projecting by hour-of-week slot."""
    start = P.floor_hour(NOW) - timedelta(weeks=4)
    hist = P.hour_buckets(start, 4 * 168)

    def tariff(t):
        if t.weekday() >= 5:
            return "3"
        return "2" if 7 <= t.hour < 14 or 16 <= t.hour < 20 else "3"

    labels = {h.timestamp(): tariff(h) for h in hist}
    horizon = P.hour_buckets(P.floor_hour(NOW), 168)
    out = I.project(labels, keys(horizon), OFFSET)
    assert len(out) == 168
    wrong = [(h, out[h.timestamp()], tariff(h)) for h in horizon if out[h.timestamp()] != tariff(h)]
    assert not wrong, wrong[:4]


def test_an_unprojectable_input_is_held_briefly_and_then_absent():
    """Alternating weeks are not an hour-of-week schedule, so no slot agrees
    with itself: the current label carries a few hours and then stops."""
    start = P.floor_hour(NOW) - timedelta(weeks=8)
    hist = P.hour_buckets(start, 8 * 168)
    labels = {h.timestamp(): ("high" if h.isocalendar()[1] % 2 == 0 else "low") for h in hist}
    horizon = P.hour_buckets(P.floor_hour(NOW), 168)
    out = I.project(labels, keys(horizon), OFFSET, current="high")
    assert len(out) == I.HOLD_HOURS, len(out)
    assert set(out.values()) == {"high"}
    assert I.project(labels, keys(horizon), OFFSET) == {}, "nothing to hold, nothing projected"


def test_too_little_history_is_refused():
    assert not I.usable({float(i) * 3600: "x" for i in range(200)}), "one label explains nothing"
    assert not I.usable({float(i) * 3600: ["a", "b"][i % 2] for i in range(100)}), "under a week"
    assert I.usable({float(i) * 3600: ["a", "b"][i % 2] for i in range(200)})


def test_an_attached_sensor_is_fitted_exactly_like_a_calendar():
    """The input must explain something the WEEKLY PROFILE cannot see, so it
    alternates by week rather than by weekday - a tariff, which is a function
    of weekday and hour, is already absorbed by the profile (the test below)."""
    start = P.floor_hour(NOW) - timedelta(weeks=10)
    hist = P.hour_buckets(start, 10 * 168)
    horizon = P.hour_buckets(P.floor_hour(NOW), 168)
    level = lambda t: "high" if t.isocalendar()[1] % 2 == 0 else "low"   # noqa: E731

    labels = {h.timestamp(): level(h) for h in hist + horizon}
    sig = CAL.CalendarSignals.from_labels("sensor.thing", labels)
    assert sig.kind == "sensor" and set(sig.titles) == {"high", "low"}

    base = lambda t: 0.8 if 7 <= t.hour < 22 else 0.3          # noqa: E731
    samples = [(t, base(t) * (1.6 if level(t) == "high" else 1.0)) for t in hist]
    fc = P.forecast(samples, NOW, calendars=[sig])
    m = fc.calendars[0]
    assert m.engaged, m
    assert "high" in m.titles and m.titles["high"].on > 1.15, m.titles
    assert m.titles["high"].on > m.titles.get("low", m.existence).on, m.titles


def test_an_input_the_weekly_profile_already_knows_explains_nothing():
    """A tariff is a function of weekday and hour - exactly what the 168-slot
    profile is. Attaching it is harmless and adds nothing, which is the
    honest outcome, not a bug."""
    start = P.floor_hour(NOW) - timedelta(weeks=8)
    hist = P.hour_buckets(start, 8 * 168)
    horizon = P.hour_buckets(P.floor_hour(NOW), 168)
    tariff = lambda t: "2" if (t.weekday() < 5 and 7 <= t.hour < 14) else "3"   # noqa: E731
    labels = {h.timestamp(): tariff(h) for h in hist + horizon}
    sig = CAL.CalendarSignals.from_labels("sensor.tariff", labels)
    base = lambda t: 0.8 if 7 <= t.hour < 22 else 0.3          # noqa: E731
    samples = [(t, base(t) * (1.6 if tariff(t) == "2" else 1.0)) for t in hist]
    fc = P.forecast(samples, NOW, calendars=[sig])
    assert not fc.calendars[0].engaged, fc.calendars[0].titles
    assert [round(v, 6) for _, v in fc.hourly] == [round(v, 6) for _, v in P.forecast(samples, NOW).hourly]


def test_an_input_that_explains_nothing_changes_nothing():
    import random
    rnd = random.Random(5)
    start = P.floor_hour(NOW) - timedelta(weeks=8)
    hist = P.hour_buckets(start, 8 * 168)
    horizon = P.hour_buckets(P.floor_hour(NOW), 168)
    labels = {h.timestamp(): rnd.choice(["x", "y"]) for h in hist + horizon}
    sig = CAL.CalendarSignals.from_labels("sensor.noise", labels)
    base = lambda t: 0.8 if 7 <= t.hour < 22 else 0.3          # noqa: E731
    samples = [(t, base(t)) for t in hist]
    with_input = P.forecast(samples, NOW, calendars=[sig])
    without = P.forecast(samples, NOW)
    assert not with_input.calendars[0].engaged
    assert [round(v, 6) for _, v in with_input.hourly] == [round(v, 6) for _, v in without.hourly]


if __name__ == "__main__":
    run_main(globals())
