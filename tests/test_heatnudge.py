"""Heat nudge shadow mode: the COP proxy, the shadow schedule, the scorecard, the
derived hourly energy, the stored state, the options, the published answer, and
that the HA layer is wired (no Home Assistant in the pure tier, so by reading source)."""
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

H = load("insights.heatnudge")
TZ = ZoneInfo("Europe/Ljubljana")
BASE = Path(__file__).resolve().parents[1] / "custom_components" / "load_insights"
DAY = date(2026, 10, 20)
KEYS = H.day_keys(DAY, TZ)


def warm_afternoon():
    """A day from 2 C at night to 14 C at 15:00 (a triangle)."""
    return [2 + 12 * max(0.0, 1 - abs(h - 15) / 12) for h in range(24)]


def test_cop_rises_with_outdoor_temperature_and_falls_with_target():
    cops = [H.cop(t, 35) for t in range(-15, 20)]
    assert all(b > a for a, b in zip(cops, cops[1:]))
    targets = [H.cop(5, tg) for tg in range(25, 60, 5)]
    assert all(b < a for a, b in zip(targets, targets[1:]))
    assert H.COP_MIN <= min(cops) and max(cops) <= H.COP_MAX


def test_cop_proxy_slope_is_two_to_three_percent_a_degree_at_35c():
    per_degree = (H.cop(8, 35) / H.cop(2, 35)) ** (1 / 6) - 1
    assert 0.018 < per_degree < 0.03, per_degree


def test_frost_is_a_flag_only_in_the_cold_damp_band():
    assert H.frost_flag(2, 90) and not H.frost_flag(2, 50) and not H.frost_flag(8, 95) and not H.frost_flag(2, None)


def test_the_shadow_keeps_the_heat_and_never_uses_more_energy():
    t = warm_afternoon()
    cops = [H.cop(x, 35) for x in t]
    e = [0.5] * 24
    s = H.shadow_schedule(e, cops, 4, 1.5)
    heat = sum(a * c for a, c in zip(e, cops))
    heat_s = sum(a * c for a, c in zip(s, cops))
    assert abs(heat - heat_s) < 1e-6
    assert sum(s) < sum(e)
    assert max(s) <= 1.5 + 1e-9


def test_the_shift_limit_binds():
    t = [0.0] * 12 + [10.0] + [0.0] * 11           # one warm hour at noon
    cops = [H.cop(x, 35) for x in t]
    e = [0.0] * 24
    e[6] = 1.0
    assert H.shadow_schedule(e, cops, 2, 5.0)[6] > 0.99        # noon is 6 h away: stays
    moved = H.shadow_schedule(e, cops, 6, 5.0)
    assert moved[12] > 0 and moved[6] == 0


def test_a_flat_day_has_nothing_to_nudge():
    cops = [H.cop(5, 35)] * 24
    e = [0.3] * 24
    assert H.shadow_schedule(e, cops, 4, 1.0) == e


def test_scorecard_gap_is_shadow_minus_actual_weighted_temperature():
    t = warm_afternoon()
    e = [1.0 if h in (3, 4, 5, 6) else 0.0 for h in range(24)]     # heat made at night
    c = H.scorecard_day(e, t, 35, "space_heating", 8, 2.0, "measured")
    assert abs(c["actual_t"] - sum(t[3:7]) / 4) < 0.01
    assert c["gap"] > 0 and c["shadow_t"] > c["actual_t"]
    assert abs(c["gap"] - (c["shadow_t"] - c["actual_t"])) < 0.01
    assert c["saving_pct"] > 0
    assert isinstance(c["extra_peak_kw"], float)      # may be negative: the warm hours are cheaper per heat
    assert c["gap_forecast"] is None


def test_scorecard_with_the_forecast_judges_the_picked_hours_at_real_temperatures():
    t = warm_afternoon()
    wrong = list(reversed(t))                                        # a forecast that is backwards
    e = [1.0 if h in (3, 4, 5, 6) else 0.0 for h in range(24)]
    good = H.scorecard_day(e, t, 35, "space_heating", 8, 2.0, "measured", t_forecast=t)
    bad = H.scorecard_day(e, t, 35, "space_heating", 8, 2.0, "measured", t_forecast=wrong)
    assert abs(good["gap_forecast"] - good["gap"]) < 0.01
    assert bad["gap_forecast"] < good["gap_forecast"]


def test_scorecard_skips_a_day_without_heat():
    assert H.scorecard_day([0.0] * 24, warm_afternoon(), 35, None, 4, None, "measured") is None


def test_summary_weights_by_energy_and_keeps_modelled_days_apart():
    days = {"a": {"energy_kwh": 10, "gap": 2.0, "gap_forecast": 1.0, "saving_pct": 4, "extra_peak_kw": 0.5, "basis": "measured", "t_basis": "corrected"},
            "b": {"energy_kwh": 30, "gap": 1.0, "gap_forecast": None, "saving_pct": 2, "extra_peak_kw": 0.1, "basis": "measured", "t_basis": "corrected"},
            "c": {"energy_kwh": 5, "gap": 9.0, "gap_forecast": None, "saving_pct": 9, "extra_peak_kw": 1, "basis": "modelled"}}
    s = H.summarize(days)
    assert s["days_measured"] == 2 and s["days_modelled"] == 1
    assert s["mean_gap"] == 1.25 and s["mean_gap_forecast"] == 1.0


def test_hourly_energy_from_a_daily_counter():
    rows = []
    for d, kwh in ((18, 15.25), (19, 12.0), (20, 8.5), (21, 0.0)):
        # published at 00:30 the NEXT day: yesterday's total
        rows.append((datetime(2026, 10, d + 1, 0, 30, tzinfo=TZ).timestamp() // 3600 * 3600, kwh))
    assert H.classify_energy(rows, TZ) == "daily"
    daily = H.daily_from_lumps(rows, TZ)
    assert daily == {date(2026, 10, 18): 15.25, date(2026, 10, 19): 12.0, date(2026, 10, 20): 8.5}
    # a counter that moves all day is hourly
    hourly = [(datetime(2026, 10, 18, h, tzinfo=TZ).timestamp(), 0.4) for h in range(24)]
    assert H.classify_energy(hourly, TZ) == "hourly"


def test_a_daily_total_spread_by_demand_or_by_activity_adds_up():
    t = warm_afternoon()
    by_demand = H.spread_daily(12.0, H.demand_weights(t))
    assert abs(sum(by_demand) - 12.0) < 1e-9 and by_demand[3] > by_demand[15]      # colder hours get more
    keys = [0.0, 3600.0, 7200.0, 10800.0]
    frac = H.active_fractions([(1800.0, True), (5400.0, False), (11700.0, True)], keys, 12600.0)
    assert [round(x, 3) for x in frac] == [0.5, 0.5, 0.0, 0.25]
    assert abs(sum(H.spread_daily(6.0, frac)) - 6.0) < 1e-9
    assert H.spread_daily(6.0, [0, 0, 0]) == [2.0, 2.0, 2.0]


def test_power_sensor_hourly_mean_is_kwh():
    assert H.hourly_from_power([(0.0, 1500.0), (3600.0, 0.0)]) == {0.0: 1.5}


def test_day_need_fits_a_line_or_falls_back_to_the_median():
    cold = [(20 - 1.5 * t, float(t)) for t in range(0, 10)]
    need, how = H.day_need(cold, 2.0)
    assert how == "fit" and abs(need - 17.0) < 1e-6
    noise = [(5.0, 1.0), (9.0, 1.1), (5.0, 1.0), (9.0, 1.1), (5.0, 1.0), (9.0, 1.1), (7.0, 1.0)]
    assert H.day_need(noise, 3.0) == (7.0, "median")
    assert H.day_need([], 3.0) == (None, "none")


def test_hour_shape_follows_the_similar_days_and_is_flat_without_history():
    mornings = [([1.0 if h in (6, 7) else 0.0 for h in range(24)], 3.0)] * 3
    shape, how = H.hour_shape(mornings, 3.0, list(range(24)))
    assert how == "history" and abs(sum(shape) - 1) < 1e-9 and shape[6] > shape[12]
    assert H.hour_shape([], 3.0, list(range(24)))[1] == "flat"


def test_forecast_day_ranks_hours_and_gains_against_the_day_average():
    t = warm_afternoon()
    shape = [1.0 if h in (3, 4, 5, 6) else 0.0 for h in range(24)]
    shape = [x / 4 for x in shape]
    fc = H.forecast_day(KEYS, t, 35, "space_heating", 10.0, shape, 8, 2.5, humidity=[90] * 24)
    ranks = {r["rank"]: r["temp"] for r in fc["hours"]}
    assert ranks[1] == max(t) and ranks[24] == min(t)
    assert fc["nudge"] and fc["gain_vs_day_average_pct"] > 0 and fc["saving_pct"] > 0
    assert any(r["frost"] for r in fc["hours"])            # 2 C at 90 % in the band
    assert isinstance(fc["extra_peak_kw"], float)


def test_a_forecast_with_holes_fills_them_and_with_none_is_none():
    t = warm_afternoon()
    t[5] = None
    fc = H.forecast_day(KEYS, t, 35, None, 8.0, [1 / 24] * 24, 4, None)
    assert fc["temps_filled"] == 1
    assert H.forecast_day(KEYS, [None] * 24, 35, None, 8.0, [1 / 24] * 24, 4, None) is None


def test_no_history_means_no_schedule_but_still_a_cop_index():
    fc = H.forecast_day(KEYS, warm_afternoon(), 35, None, None, [1 / 24] * 24, 4, None)
    assert fc["nudge"] == [] and fc["hours"][15]["index"] > 1 > fc["hours"][0]["index"]


def test_publish_says_nudge_now_only_in_a_shadow_hour():
    t = warm_afternoon()
    shape = [0.25 if h in (3, 4, 5, 6) else 0.0 for h in range(24)]
    fc = H.forecast_day(KEYS, t, 35, "space_heating", 10.0, shape, 8, 2.5)
    on_hour = datetime.fromtimestamp(fc["nudge"][0], TZ)
    off = datetime(2026, 10, 20, 3, 30, tzinfo=TZ)
    assert H.publish(on_hour.replace(minute=30), fc, None, {}, "hourly", 35, "space_heating")["would_nudge_now"]
    assert not H.publish(off, fc, None, {}, "hourly", 35, "space_heating")["would_nudge_now"] or datetime(2026, 10, 20, 3, tzinfo=TZ).timestamp() in fc["nudge"]
    assert not H.publish(off, None, None, {}, "none", 35, "room", why="no temperature")["would_nudge_now"]
    assert "not a measured COP" in H.publish(off, fc, None, {}, "hourly", 35, "room")["proxy"]


def test_stored_state_migrates_and_unknown_generations_start_empty():
    assert H.migrate(None)["days"] == {}
    kept = H.migrate({"generation": H.HEAT_GENERATION, "days": {"x": {"2026-10-19": {"gap": 1}}}, "forecasts": {}})
    assert kept["days"]["x"]["2026-10-19"]["gap"] == 1
    assert H.migrate({"generation": 99, "days": {"x": {}}})["days"] == {}
    many = {f"2026-01-{d:02d}": {} for d in range(1, 29)}
    assert len(H.prune(many, 10)) == 10 and "2026-01-28" in H.prune(many, 10)


def test_options_rows_get_ids_kind_defaults_and_bad_rows_are_dropped():
    rows = H.parse_devices([
        {"name": "Heat pump", "source": "sensor.hp", "kind": "hot_water"},
        {"name": "Heat pump", "source": "sensor.hp2", "kind": "space_heating", "target": "32.5"},
        {"name": "Attic AC", "source": "sensor.ac", "kind": "bogus", "target_entity": "number.t"},
        {"name": "", "source": "sensor.x"}, {"name": "No source"}])
    assert [r["id"] for r in rows] == ["heat_pump", "heat_pump_2", "attic_ac"]
    assert [r["target"] for r in rows] == [50.0, 32.5, 35.0]
    assert rows[2]["kind"] == "space_heating" and rows[2]["target_entity"] == "number.t"
    assert H.kind_defaults("room")[0] == 21.0 and H.parse_devices(None) == []


def test_the_options_flow_has_the_heat_page_and_its_texts():
    src = (BASE / "config_flow.py").read_text(encoding="utf-8")
    assert "async_step_heat" in src and '"heat"' in src and "CONF_HEAT_DEVICES" in src
    for lang in ("en", "sl"):
        t = json.loads((BASE / "translations" / f"{lang}.json").read_text(encoding="utf-8"))
        assert "heat" in t["options"]["step"]["init"]["menu_options"]
        step = t["options"]["step"]["heat"]
        for k in ("name", "source", "kind", "target", "target_entity", "activity_entity", "remove"):
            assert step["data"][k], (lang, k)
        assert set(t["selector"]["heat_kind"]["options"]) == set(H.KINDS), lang


def test_the_heat_sensors_exist_in_code_and_translations_and_keep_series_out_of_the_recorder():
    src = (BASE / "sensor.py").read_text(encoding="utf-8")
    assert "class HeatIndexSensor" in src and "class HeatScorecardSensor" in src
    assert "HeatIndexSensor(coordinator, entry" in src
    binary = (BASE / "binary_sensor.py").read_text(encoding="utf-8")
    assert "class HeatNudgeNow" in binary
    for lang in ("en", "sl"):
        e = json.loads((BASE / "translations" / f"{lang}.json").read_text(encoding="utf-8"))["entity"]
        assert {"heat_index", "heat_scorecard"} <= set(e["sensor"]) and "heat_nudge_now" in e["binary_sensor"], lang


# --- the corrected outdoor temperature ------------------------------------------
NOW = 28 * 86400.0 + 5 * 3600           # 28 days of hourly pairs, then "now" is 05:00 UTC


NIGHT = set(range(0, 7)) | set(range(18, 24))      # the sun of `sun()` below: UTC 07-17


def pairs_for(sensor_of, ref_of, days=28):
    keys = [d * 86400.0 + h * 3600.0 for d in range(days) for h in range(24)]
    return {k: sensor_of(k) for k in keys}, {k: ref_of(k) for k in keys}


def ref_wave(k):                       # a reference that wanders by day and by hour, 4 to 12 C
    return 8 + 3 * math.sin(k / 86400.0 * 2.3) + 2 * math.sin((k % 86400.0 / 3600.0 - 9) / 24 * 2 * math.pi)


def sun(k):                            # sun heating on the unit, UTC 07-17, up to +9 K at noon
    h = k % 86400.0 / 3600.0
    return max(0.0, 9 * math.sin((h - 7) / 10 * math.pi)) if 7 <= h <= 17 else 0.0


def test_a_good_sensor_is_read_through_unchanged_and_the_forecast_is_not_moved_by_noise():
    sensor, ref = pairs_for(lambda k: ref_wave(k) + 1.3 + 0.2 * math.sin(k), ref_wave)    # 1.3 K off, no sun
    bias = H.learn_bias(sensor, ref, NOW, NIGHT)
    assert bias["learned"] and len(bias["trusted"]) == 24 and abs(bias["offset"] - 1.3) < 0.3
    fc = {NOW - NOW % 3600 + 3600 * i: 9.0 for i in range(6)}
    temps, basis = H.correct_temperatures(sensor, ref, fc, NOW, bias)
    past = [k for k in sensor if k < NOW - NOW % 3600]
    assert all(temps[k] == sensor[k] and basis[k] == "sensor" for k in past)        # correction is zero, hour for hour
    assert all(temps[k] == v for k, v in fc.items())                                 # nothing to correct: the forecast is not shifted
    assert H.day_basis([basis.get(k, "unknown") for k in past[:24]]) == "corrected"


def test_a_sun_heated_sensor_is_replaced_by_the_reference_in_the_sun_hours_only():
    sensor, ref = pairs_for(lambda k: ref_wave(k) - 2.0 + sun(k), ref_wave)
    bias = H.learn_bias(sensor, ref, NOW, NIGHT)
    assert bias["learned"] and abs(bias["offset"] + 2.0) < 0.3
    assert all(h not in bias["trusted"] for h in range(8, 17)) and all(h in bias["trusted"] for h in (22, 23, 0, 1, 2, 3, 4))
    temps, basis = H.correct_temperatures(sensor, ref, {}, NOW, bias)
    noon = 20 * 86400.0 + 12 * 3600
    assert basis[noon] == "reference" and abs(temps[noon] - (ref[noon] + bias["offset"])) < 1e-9
    assert basis[20 * 86400.0 + 2 * 3600] == "sensor"
    errs = [abs(temps[k] - (ref_wave(k) - 2.0)) for k in temps if basis[k] == "reference"]
    assert max(errs) < 0.5, max(errs)                                         # what the sensor would read without sun
    raw = max(abs(sensor[k] - (ref_wave(k) - 2.0)) for k in temps)
    assert raw > 8                                                           # and the raw sensor was 9 K out


def test_no_learnable_bias_means_the_reference_alone_and_without_one_the_sensor_as_it_is():
    sensor, ref = pairs_for(lambda k: ref_wave(k) + sun(k), ref_wave, days=3)         # three days: not enough pairs
    bias = H.learn_bias(sensor, ref, 3 * 86400.0 + 5 * 3600, NIGHT)
    assert not bias["learned"] and bias["offset"] == 0.0 and bias["trusted"] == []
    temps, basis = H.correct_temperatures(sensor, ref, {}, 3 * 86400.0 + 5 * 3600, bias)
    assert all(temps[k] == ref[k] and basis[k] == "reference" for k in sensor)
    temps, basis = H.correct_temperatures(sensor, {}, {}, 3 * 86400.0 + 5 * 3600, bias)
    assert all(temps[k] == sensor[k] and basis[k] == "raw" for k in sensor)
    assert H.day_basis(["raw"] * 24) == "uncorrected"


def test_days_without_a_reference_know_only_the_trusted_hours_and_are_marked_so():
    sensor, ref = pairs_for(lambda k: ref_wave(k) - 2.0 + sun(k), ref_wave)
    old_days = {k for k in sensor if k < 10 * 86400.0}
    bias = H.learn_bias(sensor, {k: v for k, v in ref.items() if k >= 10 * 86400.0}, NOW, NIGHT)      # the reference starts on day 10
    temps, basis = H.correct_temperatures(sensor, {k: v for k, v in ref.items() if k >= 10 * 86400.0}, {}, NOW, bias)
    day = [basis.get(k, "unknown") for k in sorted(old_days)[48:72]]
    assert H.day_basis(day) == "sensor_night" and day.count("sensor") == len(bias["trusted"]) and "sensor" in day
    assert all(k not in temps for k in old_days if basis[k] == "unknown")             # nothing invented for the sun hours


def test_the_headline_counts_only_corrected_days_and_says_how_many_are_left_out():
    base = {"energy_kwh": 5.0, "gap": 2.0, "saving_pct": 6.0, "extra_peak_kw": 0.1, "basis": "activity", "gap_forecast": None}
    days = {"a": {**base, "t_basis": "corrected"}, "b": {**base, "gap": 9.0, "t_basis": "sensor_night"},
            "c": {**base, "gap": 9.0},                                                       # stored before the correction
            "d": {**base, "gap": 9.0, "basis": "modelled", "t_basis": "corrected"}}
    sm = H.summarize(days)
    assert sm["days_measured"] == 1 and sm["mean_gap"] == 2.0 and sm["days_modelled"] == 1
    assert sm["days_uncorrected_temperature"] == 2


def test_hourly_points_are_the_value_in_force_at_half_past_and_stale_ones_are_dropped():
    keys = [0.0, 3600.0, 7200.0, 7200.0 + 5 * 3600]
    out = H.hourly_from_points([(100.0, 5.0), (5500.0, 7.0)], keys)
    assert out[0.0] == 5.0 and out[3600.0] == 5.0 and out[7200.0] == 7.0 and keys[3] not in out
    assert H.hourly_from_points([(4000.0, 1.0)], [0.0]) == {}                               # nothing yet at half past
    assert H.migrate({"generation": H.HEAT_GENERATION, "obs": {"3600": 4.5}})["obs"] == {3600.0: 4.5}


def test_the_published_answer_carries_the_temperature_basis_and_the_sensors_show_it():
    bias = H.learn_bias(*pairs_for(lambda k: ref_wave(k) - 2.0 + sun(k), ref_wave), NOW, NIGHT)
    tb = H.temperature_basis(bias, {"corrected": 10, "sensor_night": 13})
    assert tb["learned"] and tb["days"]["sensor_night"] == 13 and tb["trusted_hours_utc"] and "offset_k" in tb
    out = H.publish(datetime(2026, 10, 20, 3, 30, tzinfo=TZ), None, None, {}, "daily", 35, "room", temperature=tb)
    assert out["temperature_basis"] is tb
    src = (BASE / "sensor.py").read_text(encoding="utf-8")
    assert src.count('"temperature_basis"') >= 3 and "days_uncorrected_temperature" in src
    assert "_weather_reference" in (BASE / "coordinator.py").read_text(encoding="utf-8")


def test_corrected_series_is_what_the_temperature_evidence_reads_INS20():
    """The split series INS-15 is fed: a good sensor's history comes back untouched, a sun-heated one loses
    its sun hours to the reference, nothing but the forecast sits at or after the hour under way."""
    cur = NOW - NOW % 3600
    fc = {cur + 3600 * i: 9.0 for i in range(6)}
    sensor, ref = pairs_for(lambda k: ref_wave(k) + 1.3, ref_wave)
    hist, fcc, basis, bias = H.corrected_series(sensor, ref, fc, NOW, NIGHT)
    assert hist == {k: v for k, v in sensor.items() if k < cur} and min(fcc) >= cur and max(hist) < cur
    assert H.series_basis(bias, basis)["hours"].get("sensor", 0) == len(hist) and bias["trusted"] == list(range(24))
    sensor, ref = pairs_for(lambda k: ref_wave(k) - 2.0 + sun(k), ref_wave)
    hist, _, basis, bias = H.corrected_series(sensor, ref, fc, NOW, NIGHT)
    noon = 20 * 86400.0 + 12 * 3600
    assert abs(hist[noon] - (ref[noon] + bias["offset"])) < 1e-9 and max(hist.values()) < max(sensor.values()) - 4      # the sun peak is gone
    assert H.series_basis(bias, basis)["hours"]["reference"] > 0
    assert H.corrected_series(sensor, {}, {}, NOW, NIGHT)[2] and all(b == "raw" for b in H.corrected_series(sensor, {}, {}, NOW, NIGHT)[2].values())


def test_only_a_sensor_above_the_reference_is_distrusted_and_night_is_the_level_INS20():
    """Kozolec's shape: the model reads 3 K under the sensor at night and level with it by day (the sensor is
    never above the model in the sun): every hour trusted. Andrej's: sun heating on top of the night level."""
    night_warm = lambda k: ref_wave(k) + (3.0 if k % 86400 // 3600 in NIGHT else 0.0)
    bias = H.learn_bias(*pairs_for(night_warm, ref_wave), NOW, NIGHT)
    assert bias["learned"] and abs(bias["offset"] - 3.0) < 0.3 and bias["trusted"] == list(range(24))
    bias = H.learn_bias(*pairs_for(lambda k: ref_wave(k) - 2.4 + sun(k), ref_wave), NOW, NIGHT)
    assert abs(bias["offset"] + 2.4) < 0.3 and all(h in bias["trusted"] for h in NIGHT) and not {9, 10, 11, 12, 13, 14, 15} & set(bias["trusted"])
    assert not H.learn_bias(*pairs_for(night_warm, ref_wave), NOW, set())["learned"]          # no night, no level
    n = H.night_hours(46.1, 14.3, 290 * 86400.0)      # Ljubljana basin, a day in October
    assert {0, 1, 2, 3, 20, 21, 22, 23} <= n and not {8, 9, 10, 11, 12, 13, 14} & n


def test_the_forecast_reads_the_corrected_pair_INS20():
    src = (BASE / "coordinator.py").read_text(encoding="utf-8")
    assert src.index("heatnudge.corrected_series") < src.index("fit = lambda rows, signals")


if __name__ == "__main__":
    run_main(globals())
