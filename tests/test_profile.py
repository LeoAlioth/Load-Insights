"""The hour-of-week profile, its fallbacks, the level correction, the outputs."""
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

P = load("insights.profile")
TZ = ZoneInfo("Europe/Ljubljana")


def pattern(t):
    """A household: 0.3 kWh/h at night, 0.8 by day, +0.4 weekend evenings."""
    v = 0.8 if 7 <= t.hour < 22 else 0.3
    if t.weekday() >= 5 and 17 <= t.hour < 22:
        v += 0.4
    return v


def weeks_of(now, weeks, fn=pattern):
    start = P.floor_hour(now) - timedelta(weeks=weeks)
    return [(t, fn(t)) for t in P.hour_buckets(start, weeks * 168)]


NOW = datetime(2026, 9, 16, 10, 30, tzinfo=TZ)   # a Wednesday


def test_a_steady_weekly_pattern_is_reproduced_exactly():
    fc = P.forecast(weeks_of(NOW, 8), NOW)
    assert len(fc.hourly) == 168
    assert all(math.isclose(v, pattern(t), abs_tol=1e-9) for t, v in fc.hourly), fc.hourly[:3]
    assert math.isclose(fc.level, 1.0)
    assert fc.sample_count == 8 * 168 and fc.span_weeks > 7.9


def test_the_current_hour_is_not_fitted():
    """Its statistic is still accumulating: a partial hour must not drag the slot."""
    samples = weeks_of(NOW, 4) + [(P.floor_hour(NOW), 0.01)]
    prof = P.fit_profile(samples, NOW)
    assert math.isclose(prof.slot_kwh(P.floor_hour(NOW)), pattern(NOW), abs_tol=1e-9)


def test_recency_weighting_prefers_the_last_weeks():
    """Six weeks at the old pattern, the last two doubled: the slot mean lands
    between, and nearer the recent level than a flat average would put it."""
    cut = P.floor_hour(NOW) - timedelta(weeks=2)
    samples = [(t, v * (2.0 if t >= cut else 1.0)) for t, v in weeks_of(NOW, 8)]
    prof = P.fit_profile(samples, NOW, half_life_weeks=3.0)
    t = P.floor_hour(NOW) + timedelta(hours=1)
    flat = (6 * 1.0 + 2 * 2.0) / 8 * pattern(t)          # 1.25 x
    assert prof.slot_kwh(t) > flat
    assert prof.slot_kwh(t) < 2.0 * pattern(t)


def test_an_unseen_slot_falls_back_to_the_hour_of_day_then_overall():
    samples = [(t, v) for t, v in weeks_of(NOW, 4) if not (t.weekday() == 0 and t.hour == 3)]
    prof = P.fit_profile(samples, NOW)
    monday_3 = next(t for t in P.hour_buckets(P.floor_hour(NOW), 168) if t.weekday() == 0 and t.hour == 3)
    assert prof.slots[P.slot_of(monday_3)] is None
    assert math.isclose(prof.slot_kwh(monday_3), 0.3, abs_tol=1e-9)   # every 03:00 is 0.3
    empty = P.fit_profile([], NOW)
    assert empty.slot_kwh(monday_3) is None and empty.predict(NOW, 3) == [(t, 0.0) for t in P.hour_buckets(P.floor_hour(NOW), 3)]


def test_level_correction_is_clamped_and_damped():
    base = weeks_of(NOW, 6)
    cut = P.floor_hour(NOW) - timedelta(hours=24)
    prof = P.fit_profile(base, NOW)
    # last 24 h at 1.5x: ratio 1.5 -> damped 1.25
    up = [(t, v * (1.5 if t >= cut else 1.0)) for t, v in base]
    assert math.isclose(P.level_correction(prof, up, NOW), 1.25, rel_tol=1e-3)
    # last 24 h at 10x: clamped to 2.0 -> damped 1.5
    spike = [(t, v * (10.0 if t >= cut else 1.0)) for t, v in base]
    assert math.isclose(P.level_correction(prof, spike, NOW), 1.5, rel_tol=1e-3)
    # last 24 h at 0.1x: clamped to 0.5 -> damped 0.75
    low = [(t, v * (0.1 if t >= cut else 1.0)) for t, v in base]
    assert math.isclose(P.level_correction(prof, low, NOW), 0.75, rel_tol=1e-3)


def test_too_few_recent_hours_means_no_correction():
    base = weeks_of(NOW, 6)
    cut = P.floor_hour(NOW) - timedelta(hours=24)
    sparse = [(t, v * 3.0) for t, v in base if t < cut] + [(t, v * 3.0) for t, v in base if t >= cut][:5]
    prof = P.fit_profile(sparse, NOW)
    assert P.level_correction(prof, sparse, NOW) == 1.0


def test_next_hour_watts_blends_the_two_buckets_by_minutes_elapsed():
    cur = P.floor_hour(NOW)
    nxt = cur + timedelta(hours=1)
    pred = [(cur, 1.0), (nxt, 2.0)]
    assert math.isclose(P.next_hour_watts(pred, cur.replace(minute=30)), 1500.0)
    assert math.isclose(P.next_hour_watts(pred, cur), 1000.0)
    assert math.isclose(P.next_hour_watts(pred, cur.replace(minute=45)), 1750.0)
    assert P.next_hour_watts([], NOW) is None


def test_today_is_actual_so_far_plus_forecast_for_the_rest():
    samples = weeks_of(NOW, 4)
    fc = P.forecast(samples, NOW)
    # the pattern is steady, so today = the pattern's full day and tomorrow likewise
    today = P.floor_hour(NOW).replace(hour=0)
    expect_today = sum(pattern(t) for t in P.hour_buckets(today, 24))
    expect_tomorrow = sum(pattern(t) for t in P.hour_buckets(today + timedelta(days=1), 24))
    assert math.isclose(fc.today_kwh, expect_today, abs_tol=1e-9)
    assert math.isclose(fc.tomorrow_kwh, expect_tomorrow, abs_tol=1e-9)
    # and the actual part really is the actual: double this morning's hours only
    cut = P.floor_hour(NOW)
    doubled = [(t, v * (2.0 if today <= t < cut else 1.0)) for t, v in samples]
    fc2 = P.forecast(doubled, NOW)
    morning = sum(pattern(t) for t in P.hour_buckets(today, 24) if t < cut)
    # the doubled morning also lifts the level correction (10 of 24 h doubled -> ratio ~1.42 -> ~1.21)
    assert fc2.today_kwh > expect_today + morning * 0.9


def test_weighted_quantile_is_the_inverted_cdf():
    pairs = [(1.0, 10.0), (1.0, 20.0), (1.0, 30.0), (1.0, 40.0)]
    assert P.weighted_quantile(pairs, 0.10) == 10.0
    assert P.weighted_quantile(pairs, 0.50) == 20.0
    assert P.weighted_quantile(pairs, 0.90) == 40.0
    # weight moves the quantile: a heavy 40 dominates
    assert P.weighted_quantile([(1.0, 10.0), (9.0, 40.0)], 0.5) == 40.0
    assert P.weighted_quantile([], 0.5) is None


def test_a_steady_slot_has_no_spread_and_a_bimodal_one_shows_it():
    """Weeks where the car charged at 10:00 and weeks where it did not: the
    mean lands in between (a number that never actually happens) and the
    band says so - p10 at the quiet weeks, p90 at the charging weeks."""
    samples = weeks_of(NOW, 8)
    fc = P.forecast(samples, NOW)
    assert all(math.isclose(b[0], v) and math.isclose(b[1], v) for (t, v), b in zip(fc.hourly, fc.bands)), "steady pattern: band collapses onto the value"

    def car(t):
        v = pattern(t)
        # the car: 3 kWh at 10:00 on alternate weeks (isocalendar week parity)
        if t.hour == 10 and t.isocalendar()[1] % 2 == 0:
            v += 3.0
        return v
    fc2 = P.forecast(weeks_of(NOW, 8, car), NOW)
    ten = [(t, v, b) for (t, v), b in zip(fc2.hourly, fc2.bands) if t.hour == 10][0]
    t, v, (lo, hi) = ten
    assert lo < v < hi, (lo, v, hi)
    # the band is level-scaled like the value (yesterday's 10:00 was itself a
    # car week or not, so the level is not 1 here)
    assert math.isclose(lo, pattern(t) * fc2.level, rel_tol=1e-9), (lo, fc2.level)
    assert math.isclose(hi, (pattern(t) + 3.0) * fc2.level, rel_tol=1e-9), (hi, fc2.level)


def test_the_band_scales_with_the_level_correction():
    base = weeks_of(NOW, 6)
    cut = P.floor_hour(NOW) - timedelta(hours=24)
    up = [(t, v * (1.5 if t >= cut else 1.0)) for t, v in base]
    fc = P.forecast(up, NOW)
    # Under 1.25: forecast() fits the profile on these same samples, so the 24
    # lifted hours already raise their own slots and the ratio reads below 1.5
    # (level_correction against an UNSCALED profile gives exactly 1.25 - see
    # test_level_correction_is_clamped_and_damped). Damped twice, by design.
    assert 1.0 < fc.level < 1.25, fc.level
    # future rows of an untouched steady slot: value and band are pattern x level
    far = [(t, v, b) for (t, v), b in zip(fc.hourly, fc.bands) if t > P.floor_hour(NOW) + timedelta(days=2)]
    t, v, (lo, hi) = far[0]
    # an untouched steady slot: value and both band edges are pattern x level
    for x in (lo, v, hi):
        assert math.isclose(x, pattern(t) * fc.level, rel_tol=1e-9), (lo, v, hi, fc.level)


def test_an_unseen_slot_takes_its_band_from_the_fallback_tier():
    samples = [(t, v) for t, v in weeks_of(NOW, 4) if not (t.weekday() == 0 and t.hour == 3)]
    prof = P.fit_profile(samples, NOW)
    monday_3 = next(t for t in P.hour_buckets(P.floor_hour(NOW), 168) if t.weekday() == 0 and t.hour == 3)
    assert prof.slot_bands[P.slot_of(monday_3)] is None
    assert prof.slot_band(monday_3) == prof.hod_bands[3] == (0.3, 0.3)
    assert P.fit_profile([], NOW).slot_band(monday_3) is None


def test_a_holiday_is_filed_under_sunday_in_the_fit_and_the_prediction():
    """A Wednesday holiday in the history must feed the Sunday slots, not the
    Wednesday ones; a holiday in the horizon must be predicted from Sunday."""
    hol_past = (P.floor_hour(NOW) - timedelta(weeks=2)).date()      # a Wednesday, two weeks ago
    hol_future = (P.floor_hour(NOW) + timedelta(days=5)).date()     # next Monday
    hols = {hol_past, hol_future}

    def with_holiday_as_sunday(t):
        # the household actually behaved like a Sunday on the past holiday
        if t.date() == hol_past:
            sunday_like = t + timedelta(days=(6 - t.weekday()))
            return pattern(sunday_like)
        return pattern(t)

    samples = weeks_of(NOW, 8, with_holiday_as_sunday)
    # Without holidays the Wednesday slots are polluted by the holiday's
    # Sunday-shaped evening (+0.4 at 17-21).
    plain = P.fit_profile(samples, NOW)
    wed_18 = next(t for t in P.hour_buckets(P.floor_hour(NOW), 168) if t.weekday() == 2 and t.hour == 18)
    assert plain.slot_kwh(wed_18) > 0.8
    # With holidays that day goes to Sunday, and Wednesday is clean again.
    prof = P.fit_profile(samples, NOW, holidays=hols)
    assert math.isclose(prof.slot_kwh(wed_18), 0.8, abs_tol=1e-9)
    # And the coming Monday holiday is predicted as a Sunday: 1.2 at 18:00.
    fc = P.forecast(samples, NOW, holidays=hols)
    mon_18 = [v for t, v in fc.hourly if t.date() == hol_future and t.hour == 18][0]
    assert math.isclose(mon_18, 1.2 * fc.level, rel_tol=1e-9), (mon_18, fc.level)


def test_history_is_the_last_48_completed_hours_oldest_first():
    samples = weeks_of(NOW, 4) + [(P.floor_hour(NOW), 0.01)]     # plus the hour in progress
    fc = P.forecast(samples, NOW)
    assert len(fc.history) == 48
    assert fc.history[-1][0] == P.floor_hour(NOW) - timedelta(hours=1), "ends with the last COMPLETED hour"
    assert fc.history[0][0] == P.floor_hour(NOW) - timedelta(hours=48)
    assert all(fc.history[i][0] < fc.history[i + 1][0] for i in range(47))
    assert all(math.isclose(v, pattern(t)) for t, v in fc.history)
    assert P.forecast([], NOW).history == ()


def test_horizon_buckets_are_168_real_consecutive_hours_across_dst():
    """Europe/Ljubljana leaves DST on 2026-10-25 03:00 -> 02:00. Wall-clock
    stepping would produce a duplicated hour; UTC stepping must not."""
    start = datetime(2026, 10, 24, 12, 0, tzinfo=TZ)
    b = P.hour_buckets(start, 168)
    # Instants, not datetimes: two aware datetimes on one tzinfo compare by
    # naive wall clock (PEP 495), so the repeated 02:00 would look like one.
    ks = [t.timestamp() for t in b]
    assert len(b) == 168 and len(set(ks)) == 168
    assert {round(ks[i + 1] - ks[i]) for i in range(167)} == {3600}
    assert any(t.utcoffset() != b[0].utcoffset() for t in b), "the horizon must actually cross the change"
    # And the repeated hour is really there twice, with different offsets.
    twos = [t for t in b if t.month == 10 and t.day == 25 and t.hour == 2]
    assert len(twos) == 2 and twos[0].utcoffset() != twos[1].utcoffset(), twos


def test_the_repeated_dst_hour_does_not_collide_in_the_arithmetic():
    """Consumption for both 02:00s must survive combine() and the forecast's
    lookups - the exact case the naive-comparison trap swallows."""
    S = load("insights.series")
    start = datetime(2026, 10, 25, 0, 0, tzinfo=TZ)
    hours = P.hour_buckets(start, 6)           # 00,01,02(CEST),02(CET),03,04
    imp = [(t, 1.0 + i) for i, t in enumerate(hours)]
    pv = [(t, 0.5) for t in hours]
    out = S.combine({"imp": imp, "pv": pv}, [("imp", 1.0), ("pv", 1.0)])
    assert len(out) == 6 and [v for _, v in out] == [1.5, 2.5, 3.5, 4.5, 5.5, 6.5]
    # next-hour blend across the fold: at 02:30 CEST the next bucket is 02:00 CET
    pred = [(t, float(i)) for i, t in enumerate(hours)]
    at = hours[2] + timedelta(minutes=30)
    assert math.isclose(P.next_hour_watts(pred, at), (0.5 * 2.0 + 0.5 * 3.0) * 1000.0)


if __name__ == "__main__":
    run_main(globals())
