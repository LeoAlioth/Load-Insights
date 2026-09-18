"""A device's own state sharpens its next hours, and only those."""
import math
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

N = load("insights.nowcast")
P = load("insights.profile")
TZ = ZoneInfo("Europe/Ljubljana")
NOW = datetime(2026, 9, 16, 10, 30, tzinfo=TZ)


def boiler(t):
    # a base big enough that the temperature effect never has to be clipped at
    # zero - a clipped fixture bends the relationship the estimator is meant
    # to recover (it attenuated the coefficients by 40 % before)
    return 0.9 if 6 <= t.hour < 9 or 18 <= t.hour < 21 else 0.5


def setup(weeks=10, seed=1):
    rnd = random.Random(seed)
    start = P.floor_hour(NOW) - timedelta(weeks=weeks)
    hist = P.hour_buckets(start, weeks * 168)
    # tank temperature 30..60, INDEPENDENT hour to hour - a real tank is
    # autocorrelated, but this tests the estimator, and only independent
    # states make each lead's coefficient separately identifiable. The
    # boiler burns 0.01 kWh more next hour per degree the tank is below its
    # mean, and half that two hours on (max swing +-0.225 on a 0.5 base).
    temps = {t.timestamp(): rnd.uniform(30.0, 60.0) for t in hist}
    mean = sum(temps.values()) / len(temps)
    samples = []
    for i, t in enumerate(hist):
        v = boiler(t)
        for h, c in ((1, 0.01), (2, 0.005)):
            if i - h >= 0:
                v += -c * (temps[hist[i - h].timestamp()] - mean)
        samples.append((t, max(0.0, v)))
    return hist, temps, samples


def test_the_relationship_is_recovered_lead_by_lead_with_its_sign():
    hist, temps, samples = setup()
    fc = P.forecast(samples, NOW, state_history=temps, state_now=30.0)
    nc = fc.nowcast
    assert nc.engaged, nc
    assert math.isclose(nc.coefficients[1], -0.01, rel_tol=0.2), nc.coefficients
    assert math.isclose(nc.coefficients[2], -0.005, rel_tol=0.3), nc.coefficients
    assert all(abs(nc.coefficients[h]) < 0.002 for h in (0, 3, 4, 5)), nc.coefficients
    assert nc.explained[1] > nc.explained[4]


def test_a_cold_tank_lifts_the_next_hours_and_a_hot_one_lowers_them_and_nothing_beyond():
    hist, temps, samples = setup()
    cold = P.forecast(samples, NOW, state_history=temps, state_now=30.0)
    hot = P.forecast(samples, NOW, state_history=temps, state_now=60.0)
    none = P.forecast(samples, NOW, state_history=temps, state_now=None)
    for i in (1, 2):
        assert cold.hourly[i][1] > none.hourly[i][1] > hot.hourly[i][1], i
    for i in range(N.LEADS, N.LEADS + 24):
        assert math.isclose(cold.hourly[i][1], hot.hourly[i][1]), i
    assert len(cold.nowcast_deltas) == N.LEADS
    assert none.nowcast.engaged and all(d == 0.0 for d in none.nowcast_deltas), "engaged but no live value: nothing to shift"


def test_the_band_narrows_where_the_state_explains_the_hour():
    hist, temps, samples = setup()
    fc = P.forecast(samples, NOW, state_history=temps, state_now=45.0)
    plain = P.forecast(samples, NOW)
    for i in (1, 2):
        w_fc = fc.bands[i][1] - fc.bands[i][0]
        w_plain = plain.bands[i][1] - plain.bands[i][0]
        assert w_fc < w_plain, (i, w_fc, w_plain)


def test_noise_does_not_engage():
    rnd = random.Random(7)
    start = P.floor_hour(NOW) - timedelta(weeks=8)
    hist = P.hour_buckets(start, 8 * 168)
    temps = {t.timestamp(): rnd.uniform(30, 60) for t in hist}
    samples = [(t, boiler(t) + rnd.uniform(-0.01, 0.01)) for t in hist]
    assert not P.forecast(samples, NOW, state_history=temps, state_now=40.0).nowcast.engaged


def test_too_little_state_history_means_no_fit():
    hist, temps, samples = setup()
    few = dict(list(temps.items())[-200:])
    assert not P.forecast(samples, NOW, state_history=few, state_now=40.0).nowcast.engaged


if __name__ == "__main__":
    run_main(globals())
