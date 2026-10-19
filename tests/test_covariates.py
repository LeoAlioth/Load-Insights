"""The temperature response: fitted on residuals, guarded, centred."""
import math
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

C = load("insights.covariates")
P = load("insights.profile")
TZ = ZoneInfo("Europe/Ljubljana")
NOW = datetime(2026, 9, 16, 10, 30, tzinfo=TZ)


def pattern(t):
    return 0.8 if 7 <= t.hour < 22 else 0.3


def temps_for(hours, fn):
    return {t.timestamp(): fn(t) for t in hours}


def test_a_heating_response_is_recovered():
    """Consumption rises 0.05 kWh/h per degree below 15 C. Temperatures vary
    day to day, so the fit can see it; the coefficient comes back."""
    rnd = random.Random(1)
    start = P.floor_hour(NOW) - timedelta(weeks=8)
    hours = P.hour_buckets(start, 8 * 168)
    temps = temps_for(hours, lambda t: 5.0 + 20.0 * ((t.toordinal() * 7 + t.hour) % 13) / 13.0 + rnd.uniform(-0.5, 0.5))
    samples = [(t, pattern(t) + 0.05 * C.hdh(temps[t.timestamp()])) for t in hours]
    fc = P.forecast(samples, NOW, temps_history=temps, temps_forecast={})
    r = fc.temperature
    assert r.engaged and math.isclose(r.heating_kwh_per_degh, 0.05, rel_tol=0.15), r
    assert r.cooling_kwh_per_degh == 0.0 and r.explained > 0.5, r


def test_noise_does_not_engage_the_response():
    rnd = random.Random(2)
    start = P.floor_hour(NOW) - timedelta(weeks=8)
    hours = P.hour_buckets(start, 8 * 168)
    temps = temps_for(hours, lambda t: rnd.uniform(0.0, 30.0))
    samples = [(t, pattern(t) + rnd.uniform(-0.05, 0.05)) for t in hours]
    fc = P.forecast(samples, NOW, temps_history=temps, temps_forecast={})
    assert not fc.temperature.engaged, fc.temperature


def test_the_horizon_moves_with_the_forecast_temperature_and_not_without_one():
    rnd = random.Random(3)
    start = P.floor_hour(NOW) - timedelta(weeks=8)
    hours = P.hour_buckets(start, 8 * 168)
    temps = temps_for(hours, lambda t: 5.0 + 20.0 * ((t.toordinal() * 7 + t.hour) % 13) / 13.0 + rnd.uniform(-0.5, 0.5))
    samples = [(t, pattern(t) + 0.05 * C.hdh(temps[t.timestamp()])) for t in hours]
    horizon = P.hour_buckets(P.floor_hour(NOW), 168)
    cold = P.forecast(samples, NOW, temps_history=temps, temps_forecast={h.timestamp(): 0.0 for h in horizon})
    warm = P.forecast(samples, NOW, temps_history=temps, temps_forecast={h.timestamp(): 25.0 for h in horizon})
    none = P.forecast(samples, NOW, temps_history=temps, temps_forecast={})
    for (t, c), (_, w), (_, n) in zip(cold.hourly, warm.hourly, none.hourly):
        assert c > n > w or math.isclose(c, n), (t, c, n, w)
    assert cold.hours_with_forecast_temperature == 168 and none.hours_with_forecast_temperature == 0
    # the band shifts with the response too
    (lo_c, hi_c), (lo_w, hi_w) = cold.bands[24], warm.bands[24]
    assert lo_c > lo_w and hi_c > hi_w


def test_too_few_hours_with_temperature_means_no_fit():
    start = P.floor_hour(NOW) - timedelta(weeks=8)
    hours = P.hour_buckets(start, 8 * 168)
    temps = {t.timestamp(): 0.0 for t in hours[-100:]}       # only 100 hours known
    samples = [(t, pattern(t) + 0.05 * C.hdh(temps.get(t.timestamp(), 15.0))) for t in hours]
    assert not P.forecast(samples, NOW, temps_history=temps).temperature.engaged


def test_interpolation_fills_hours_between_points_and_not_beyond():
    pts = [(0.0, 10.0), (3 * 3600.0, 16.0)]
    keys = [k * 3600.0 for k in range(-1, 6)]
    out = C.interpolate_hourly(pts, keys)
    assert math.isclose(out[0.0], 10.0) and math.isclose(out[3600.0], 12.0) and math.isclose(out[2 * 3600.0], 14.0) and math.isclose(out[3 * 3600.0], 16.0)
    assert -3600.0 not in out and 4 * 3600.0 not in out
    assert C.interpolate_hourly([], keys) == {}


def test_a_negative_coefficient_is_not_a_heating_response():
    """Consumption falling as it gets colder is not heating; clipped to zero
    and, alone, not engaged."""
    rnd = random.Random(4)
    start = P.floor_hour(NOW) - timedelta(weeks=8)
    hours = P.hour_buckets(start, 8 * 168)
    temps = temps_for(hours, lambda t: 5.0 + 20.0 * ((t.toordinal() * 7 + t.hour) % 13) / 13.0 + rnd.uniform(-0.5, 0.5))
    samples = [(t, max(0.0, pattern(t) - 0.02 * C.hdh(temps[t.timestamp()]))) for t in hours]
    assert not P.forecast(samples, NOW, temps_history=temps).temperature.engaged


def test_a_response_that_does_nothing_says_why():
    """The reason is the whole point of the dump: at a September site the
    fit runs on plenty of hours and explains nothing, which reads very
    differently from having no history at all."""
    short = C.fit_temperature_response([(float(i) * 3600, 1.0, 0.5, 1.0, 0.0) for i in range(10)])
    assert not short.engaged and short.hours == 10 and "4 needed" in short.reason, short

    rnd = random.Random(7)
    # temperature that varies, consumption that ignores it
    rows = [(float(i) * 3600, 1.0, rnd.uniform(-0.2, 0.2), rnd.uniform(-8.0, 8.0), 0.0) for i in range(24 * 14)]
    weak = C.fit_temperature_response(rows)
    assert not weak.engaged, weak
    assert weak.hours == len(rows) and weak.days == 14, weak
    assert "needed" in weak.reason or "does not rise" in weak.reason, weak.reason

    flat = C.fit_temperature_response([(float(i) * 3600, 1.0, 0.5, 0.2 * (i % 2), 0.0) for i in range(24 * 14)])
    assert not flat.engaged and "varied by" in flat.reason, flat

    assert C.NONE.reason == "not fitted"


def _hours_back(n):
    return P.hour_buckets(P.floor_hour(NOW) - timedelta(hours=n), n)


def _daily_temps(hours, rnd, lo=2.0, hi=12.0, swing=4.0):
    """A daily mean drawn from lo..hi, plus the same diurnal cycle every day."""
    means = {}
    return {t.timestamp(): means.setdefault(t.date(), rnd.uniform(lo, hi)) + swing * math.sin((t.hour - 9) / 24 * 2 * math.pi)
            for t in hours}


def test_a_true_response_engages_within_four_days():
    rnd = random.Random(11)
    hours = _hours_back(5 * 24)
    temps = _daily_temps(hours, rnd)
    samples = [(t, pattern(t) + 0.5 * C.hdh(temps[t.timestamp()]) + rnd.gauss(0.0, 0.3)) for t in hours]
    r = P.forecast(samples, NOW, temps_history=temps).temperature
    assert r.engaged and math.isclose(r.heating_kwh_per_degh, 0.5, rel_tol=0.15), r
    assert r.days >= 4 and r.t_heating >= r.t_needed > 0, r
    # the same series cut to three days has too few days to say anything
    cut = hours[-3 * 24:]
    r3 = P.forecast([(t, v) for t, v in samples if t in set(cut)], NOW, temps_history=temps).temperature
    assert not r3.engaged and "4 needed" in r3.reason, r3


def test_noise_does_not_engage_over_eight_weeks():
    """No response, residuals that stick (hourly AR(1) 0.9 under a day offset
    AR(1) 0.7) and a temperature that drifts over days: the case an iid
    t-test passes falsely. Fit at 4, 5, 7, 14, 28 and 56 days, five seeds."""
    for seed in range(5):
        rnd = random.Random(100 + seed)
        hours = _hours_back(56 * 24)
        day_off, hourly, vals = 0.0, 0.0, []
        drift = rnd.uniform(0, 6.28)
        temps = {}
        for i, t in enumerate(hours):
            if t.hour == 0 or i == 0:
                day_off = 0.7 * day_off + rnd.gauss(0.0, 0.06)
            hourly = 0.9 * hourly + rnd.gauss(0.0, 0.03)
            vals.append(pattern(t) + day_off + hourly)
            temps[t.timestamp()] = 8.0 + 6.0 * math.sin(i / 24 / 9.0 * 2 * math.pi + drift) + 3.0 * math.sin((t.hour - 9) / 24 * 2 * math.pi)
        for days in (4, 5, 7, 14, 28, 56):
            n = days * 24
            fc = P.forecast(list(zip(hours, vals))[-n:], NOW, temps_history=temps)
            assert not fc.temperature.engaged, (seed, days, fc.temperature)


def test_a_flat_temperature_week_does_not_engage():
    rnd = random.Random(12)
    hours = _hours_back(7 * 24)
    temps = {t.timestamp(): 10.0 + rnd.uniform(-0.3, 0.3) for t in hours}
    samples = [(t, pattern(t) + 0.5 * C.hdh(temps[t.timestamp()])) for t in hours]
    r = P.forecast(samples, NOW, temps_history=temps).temperature
    assert not r.engaged and "varied by" in r.reason, r


def test_a_temperature_that_follows_the_clock_is_no_response():
    """The same diurnal cycle every day, consumption that simply follows the
    clock: nothing for a temperature to add. And where a real response IS
    engaged, the forecast at an hour of day's own usual temperature adds
    nothing."""
    hours = _hours_back(6 * 7 * 24)
    clock = {t.timestamp(): 8.0 + 6.0 * math.sin((t.hour - 9) / 24 * 2 * math.pi) for t in hours}
    samples = [(t, 0.3 + 0.1 * C.hdh(clock[t.timestamp()])) for t in hours]
    assert not P.forecast(samples, NOW, temps_history=clock).temperature.engaged
    rnd = random.Random(13)
    temps = _daily_temps(hours, rnd, 0.0, 14.0)
    samples = [(t, pattern(t) + 0.1 * C.hdh(temps[t.timestamp()]) + rnd.gauss(0.0, 0.03)) for t in hours]
    r = P.forecast(samples, NOW, temps_history=temps).temperature
    assert r.engaged, r
    for hour in (3, 12, 20):
        ch, _ = r.centre.at(hour)
        assert abs(r.delta(15.0 - ch, hour)) < 1e-9, (hour, ch)
        assert r.delta(15.0 - ch - 5.0, hour) > 0.1, hour


def test_t_crit_matches_students_t():
    for df, p, want, tol in ((3, 0.005, 5.841, 0.04), (10, 0.005, 3.169, 0.005), (30, 0.005, 2.750, 0.005), (8, 0.025, 2.306, 0.005)):
        assert math.isclose(C.t_crit(df, p), want, rel_tol=tol), (df, p, C.t_crit(df, p), want)


if __name__ == "__main__":
    run_main(globals())
