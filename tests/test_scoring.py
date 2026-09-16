"""The ledger: record, settle, score, round-trip."""
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

S = load("insights.scoring")
P = load("insights.profile")
TZ = ZoneInfo("Europe/Ljubljana")
T0 = datetime(2026, 9, 10, 0, 0, tzinfo=TZ)


def fake_forecast(now, kwh=1.0, band=(0.8, 1.2)):
    hours = P.hour_buckets(P.floor_hour(now), 168)
    return [(t, kwh) for t in hours], [band] * 168


def run_days(days, actual_fn, kwh_fn, band=(0.8, 1.2), start=T0):
    """Hourly refreshes for ``days`` days: settle then record, as the coordinator does."""
    led = S.Ledger()
    actual = []
    for i in range(days * 24):
        now = start + timedelta(hours=i, minutes=30)
        cur = P.floor_hour(now)
        # the hour that just completed becomes actual
        done = cur - timedelta(hours=1)
        actual.append((done, actual_fn(done)))
        actual = actual[-48:]
        led.settle(now, actual)
        hourly, bands = fake_forecast(now, kwh_fn(now), band)
        led.record(now, hourly, bands, tomorrow_kwh=24 * kwh_fn(now))
    return led, now


def test_each_completed_hour_gets_scored_at_every_lead_once_enough_time_has_passed():
    led, now = run_days(9, lambda t: 1.0, lambda now: 1.0)
    # 9 days of hourly refreshes: the hour and day leads have a week of
    # settled hours; the week lead (167 h, the horizon's last row) only
    # started settling after day 7
    assert led.metrics(now, 1)["n"] == 24 * 7
    assert led.metrics(now, 24)["n"] == 24 * 7
    assert 0 < led.metrics(now, S.LEADS["week_ahead"])["n"] < 24 * 7
    assert S.LEADS["week_ahead"] == 167, "a 168-row horizon ends 167 h ahead"
    assert led.metrics(now, 1)["mae_w"] == 0.0 and led.metrics(now, 24)["bias_w"] == 0.0


def test_mae_and_bias_carry_the_sign_and_the_watts():
    # forecast 1.0 kWh/h, actual 1.2 -> error +200 W, bias +200 (we under-forecast)
    led, now = run_days(9, lambda t: 1.2, lambda now: 1.0)
    m = led.metrics(now, 24)
    assert math.isclose(m["mae_w"], 200.0, abs_tol=1e-6) and math.isclose(m["bias_w"], 200.0, abs_tol=1e-6)
    led2, now2 = run_days(9, lambda t: 0.7, lambda now: 1.0)
    assert math.isclose(led2.metrics(now2, 24)["bias_w"], -300.0, abs_tol=1e-6)


def test_coverage_is_the_share_of_actuals_inside_the_day_ahead_band():
    led, now = run_days(9, lambda t: 1.1 if t.hour % 2 else 1.5, lambda now: 1.0, band=(0.8, 1.2))
    assert math.isclose(led.coverage(now), 0.5, abs_tol=0.05)


def test_the_day_ahead_total_is_taken_at_noon_and_settled_when_the_day_completes():
    led, now = run_days(4, lambda t: 1.0, lambda now: 1.0 if now.hour != 12 else 1.5)
    # at noon the forecast said 36 kWh for tomorrow; the day used 24 - all
    # 24 hours, so the day must not have been scored before it ended
    last = led.last_day()
    assert last is not None
    day, actual, predicted = last
    assert math.isclose(actual, 24.0) and math.isclose(predicted, 36.0), last
    # and every scored day has its full 24
    assert all(math.isclose(a, 24.0) for _, a, _ in led.day_errors), led.day_errors


def test_pending_hours_without_actuals_are_dropped_after_the_grace():
    led = S.Ledger()
    now = T0 + timedelta(hours=100)
    hourly, bands = fake_forecast(now)
    led.record(now, hourly, bands, None)
    assert len(led.pending[1]) == 1
    later = now + timedelta(hours=S.PENDING_GRACE_H + 2)
    led.settle(later, [])     # no actuals ever arrive
    assert led.pending[1] == {} and led.metrics(later, 1)["n"] == 0


def test_the_error_window_is_bounded():
    led, now = run_days(40, lambda t: 1.0, lambda now: 1.0)
    for lead in S.LEADS_H:
        assert all(k >= P.floor_hour(now).timestamp() - S.ERROR_WINDOW_DAYS * 86400 for k, _, _ in led.errors[lead])
    assert len(led.errors[1]) <= S.ERROR_WINDOW_DAYS * 24 + 1


def test_the_ledger_round_trips_through_json():
    import json
    led, now = run_days(9, lambda t: 1.1, lambda now: 1.0)
    copy = S.Ledger.from_dict(json.loads(json.dumps(led.to_dict())))
    assert copy.metrics(now, 24) == led.metrics(now, 24)
    assert copy.coverage(now) == led.coverage(now)
    assert copy.last_day() == led.last_day()
    assert copy.pending == led.pending and copy.pending_band == led.pending_band
    assert S.Ledger.from_dict(None).metrics(now, 1)["n"] == 0


if __name__ == "__main__":
    run_main(globals())
