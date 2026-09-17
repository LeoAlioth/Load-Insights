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
    short = C.fit_temperature_response([(1.0, 0.5, 10.0)] * 10)
    assert not short.engaged and short.hours == 10 and "336 needed" in short.reason, short

    rnd = random.Random(7)
    # temperature that varies, consumption that ignores it
    rows = [(1.0, rnd.uniform(-0.2, 0.2), rnd.uniform(0.0, 30.0)) for _ in range(C.MIN_HOURS + 50)]
    weak = C.fit_temperature_response(rows)
    assert not weak.engaged, weak
    assert weak.hours == len(rows), weak
    assert "needed" in weak.reason or "does not rise" in weak.reason, weak.reason

    assert C.NONE.reason == "not fitted"


if __name__ == "__main__":
    run_main(globals())
