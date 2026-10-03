"""Calendars are fitted, not declared."""
import math
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

CAL = load("insights.calendars")
P = load("insights.profile")
TZ = ZoneInfo("Europe/Ljubljana")
NOW = datetime(2026, 9, 16, 10, 30, tzinfo=TZ)


def pattern(t):
    return 0.8 if 7 <= t.hour < 22 else 0.3


def setup(weeks=10):
    start = P.floor_hour(NOW) - timedelta(weeks=weeks)
    hist = P.hour_buckets(start, weeks * 168)
    horizon = P.hour_buckets(P.floor_hour(NOW), 168)
    return hist, horizon, [h.timestamp() for h in hist + horizon]


def away_days(hist, every=9, length=3):
    """Every ``every`` days a ``length``-day all-day absence."""
    days = sorted({t.date() for t in hist})
    events = []
    for i in range(0, len(days) - length, every):
        s = datetime.combine(days[i], datetime.min.time(), tzinfo=TZ)
        events.append((s.timestamp(), (s + timedelta(days=length)).timestamp(), "Away"))
    return events


def test_an_away_calendar_is_recognised_as_daytime_reduction():
    hist, horizon, keys = setup()
    events = away_days(hist)
    sig = CAL.CalendarSignals.from_events("calendar.family", events, keys)
    # while away the house sits at the base load all day
    samples = [(t, 0.3 if t.timestamp() in sig.existence else pattern(t)) for t in hist]
    fc = P.forecast(samples, NOW, calendars=[sig])
    m = fc.calendars[0]
    assert m.entity == "calendar.family" and m.existence.engaged, m
    # effects are measured against the BASELINE profile (off-hours only), so
    # the off factor is 1 and the on factor IS the contrast: 0.3/0.8 by day,
    # 1.0 at night
    assert math.isclose(m.existence.at(12, True), 0.375, rel_tol=0.1), m.existence.on_by_hour
    assert math.isclose(m.existence.at(3, True), 1.0, abs_tol=0.1), m.existence.on_by_hour
    assert m.existence.at(12, False) == 1.0
    assert m.titles == {}   # a single title is the calendar itself: nothing left to explain


def test_the_horizon_applies_the_factors_only_where_an_event_falls():
    hist, horizon, keys = setup()
    events = away_days(hist)
    # and an absence in the coming week, days 3-5
    s = P.floor_hour(NOW).replace(hour=0) + timedelta(days=3)
    events.append((s.timestamp(), (s + timedelta(days=2)).timestamp(), "Away"))
    sig = CAL.CalendarSignals.from_events("calendar.family", events, keys)
    samples = [(t, 0.3 if t.timestamp() in sig.existence else pattern(t)) for t in hist]
    fc = P.forecast(samples, NOW, calendars=[sig])
    # The reference is the TRUTH, not the plain forecast: the plain one is
    # inflated by a level correction judged against a mixture profile, and
    # the calendar-aware one is the more accurate of the two.
    for t, v in fc.hourly:
        truth = 0.3 if t.timestamp() in sig.existence else pattern(t)
        assert math.isclose(v, truth, rel_tol=0.12), (t, v, truth)


def test_a_meaningless_calendar_does_not_engage():
    rnd = random.Random(5)
    hist, horizon, keys = setup()
    events = away_days(hist)
    sig = CAL.CalendarSignals.from_events("calendar.birthdays", events, keys)
    samples = [(t, pattern(t) + rnd.uniform(-0.05, 0.05)) for t in hist]
    fc = P.forecast(samples, NOW, calendars=[sig])
    assert not fc.calendars[0].engaged
    assert all(math.isclose(v, pv, rel_tol=1e-9) for (_, v), (_, pv) in zip(fc.hourly, P.forecast(samples, NOW).hourly))


def test_a_title_earns_a_factor_only_for_how_it_differs():
    """One calendar, two kinds of event: 'away' (house empty) and 'guests'
    (evenings up). Existence lands in between; the titles pull apart."""
    hist, horizon, keys = setup(12)
    days = sorted({t.date() for t in hist})
    events = []
    for i in range(0, len(days) - 3, 8):
        s = datetime.combine(days[i], datetime.min.time(), tzinfo=TZ)
        events.append((s.timestamp(), (s + timedelta(days=2)).timestamp(), "Away"))
    for i in range(4, len(days) - 3, 8):
        s = datetime.combine(days[i], datetime.min.time(), tzinfo=TZ)
        events.append((s.timestamp(), (s + timedelta(days=2)).timestamp(), "Guests"))
    sig = CAL.CalendarSignals.from_events("calendar.house", events, keys)
    away, guests = sig.titles["away"], sig.titles["guests"]

    def actual(t):
        k = t.timestamp()
        if k in away:
            return 0.3
        if k in guests:
            return pattern(t) * (1.6 if 17 <= t.hour < 23 else 1.1)
        return pattern(t)
    samples = [(t, actual(t)) for t in hist]
    fc = P.forecast(samples, NOW, calendars=[sig])
    m = fc.calendars[0]
    # Existence itself may well NOT engage here: the two roles pull in
    # opposite directions and cancel in the calendar's average. That is the
    # point of fitting titles regardless - the calendar's meaning lives in
    # them, and the calendar as a whole still engages through them.
    assert m.engaged
    assert "away" in m.titles and "guests" in m.titles, m.titles.keys()
    assert m.titles["away"].on < 1.0 < m.titles["guests"].on, (m.titles["away"].on, m.titles["guests"].on)
    # and the horizon applies them: an away day is down, a guests evening up
    s_away = P.floor_hour(NOW).replace(hour=0) + timedelta(days=2)
    s_guests = P.floor_hour(NOW).replace(hour=0) + timedelta(days=5)
    sig2 = CAL.CalendarSignals.from_events("calendar.house", events + [
        (s_away.timestamp(), (s_away + timedelta(days=1)).timestamp(), "Away"),
        (s_guests.timestamp(), (s_guests + timedelta(days=1)).timestamp(), "Guests"),
    ], keys)
    fc2 = P.forecast(samples, NOW, calendars=[sig2])
    plain = dict(P.forecast(samples, NOW).hourly)
    v_away = [v for t, v in fc2.hourly if t.date() == s_away.date() and t.hour == 12][0]
    v_guests = [v for t, v in fc2.hourly if t.date() == s_guests.date() and t.hour == 19][0]
    t_away = [t for t, _ in fc2.hourly if t.date() == s_away.date() and t.hour == 12][0]
    t_guests = [t for t, _ in fc2.hourly if t.date() == s_guests.date() and t.hour == 19][0]
    assert v_away < plain[t_away] * 0.8 and v_guests > plain[t_guests] * 1.1, (v_away, plain[t_away], v_guests, plain[t_guests])


def test_signals_telling_one_story_count_it_once():
    """Three signals on the same hours - Home's motion, occupancy and dryer
    sensors, whose states the recorder keeps for the same ten days - each
    see one evening doubling. Fitted side by side each learned x2 and the
    forecast took x8 (Blaz's PC, 3 Oct: ten factors, x880, 70.7 kWh for an
    hour it idled at 7 Wh). The second and third are fitted on what the
    first left, which is nothing."""
    hist, horizon, keys = setup()
    events = away_days(hist)
    s = P.floor_hour(NOW).replace(hour=0) + timedelta(days=2)
    events.append((s.timestamp(), (s + timedelta(days=2)).timestamp(), "On"))
    sigs = [CAL.CalendarSignals.from_events(f"calendar.{n}", events, keys) for n in ("a", "b", "c")]

    def actual(t):
        on = t.timestamp() in sigs[0].existence and 17 <= t.hour < 23
        return pattern(t) * (2.0 if on else 1.0)
    samples = [(t, actual(t)) for t in hist]
    one = P.forecast(samples, NOW, calendars=sigs[:1])
    three = P.forecast(samples, NOW, calendars=sigs)
    assert one.calendars[0].engaged and not any(m.engaged for m in three.calendars[1:]), three.calendars
    for (t, v1), (_, v3) in zip(one.hourly, three.hourly):
        assert math.isclose(v3, v1, rel_tol=1e-6), (t, v1, v3)
        assert math.isclose(v1, actual(t), rel_tol=0.12), (t, v1, actual(t))


def test_the_order_signals_are_attached_in_decides_nothing():
    """A signal on the doubled evenings and a wider one on them and as many
    ordinary days: the one that explains the most alone is fitted first,
    whichever came first in the list - so the wider one finds nothing left,
    and an ordinary day it alone covers is forecast ordinary."""
    hist, horizon, keys = setup()
    days = away_days(hist)
    others = [(s + 4 * 86400, e + 4 * 86400, "Other") for s, e, _ in days]
    s = P.floor_hour(NOW).replace(hour=0) + timedelta(days=2)
    on = days + [(s.timestamp(), (s + timedelta(days=1)).timestamp(), "On")]
    extra = others + [((s + timedelta(days=3)).timestamp(), (s + timedelta(days=4)).timestamp(), "Other")]
    narrow = CAL.CalendarSignals.from_events("calendar.narrow", on, keys)
    wide = CAL.CalendarSignals.from_events("calendar.wide", on + extra, keys)

    def actual(t):
        return pattern(t) * (2.0 if t.timestamp() in narrow.existence and 17 <= t.hour < 23 else 1.0)
    samples = [(t, actual(t)) for t in hist]
    a = P.forecast(samples, NOW, calendars=[narrow, wide])
    b = P.forecast(samples, NOW, calendars=[wide, narrow])
    assert [v for _, v in a.hourly] == [v for _, v in b.hourly]
    for t, v in b.hourly:
        assert math.isclose(v, actual(t), rel_tol=0.12), (t, v, actual(t))


def test_too_few_on_hours_means_no_fit():
    hist, horizon, keys = setup()
    s = hist[100]
    sig = CAL.CalendarSignals.from_events("calendar.x", [(s.timestamp(), (s + timedelta(hours=6)).timestamp(), "Once")], keys)
    samples = [(t, 0.0 if t.timestamp() in sig.existence else pattern(t)) for t in hist]
    assert not P.forecast(samples, NOW, calendars=[sig]).calendars[0].engaged


def test_events_map_onto_hours_including_all_day_spans_and_titles_normalise():
    hist, horizon, keys = setup(1)
    s = hist[10]
    sig = CAL.CalendarSignals.from_events("c", [(s.timestamp() + 600, s.timestamp() + 3600 * 2 + 600, "  Big   Trip ")], keys)
    assert sig.existence == {hist[10].timestamp(), hist[11].timestamp(), hist[12].timestamp()}
    assert set(sig.titles) == {"big trip"}


if __name__ == "__main__":
    run_main(globals())
