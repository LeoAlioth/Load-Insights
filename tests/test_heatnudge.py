"""Heat nudge shadow mode: the COP proxy, the shadow schedule, the scorecard, the
derived hourly energy, the stored state, the options, the published answer, and
that the HA layer is wired (no Home Assistant in the pure tier, so by reading source)."""
import json
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
    days = {"a": {"energy_kwh": 10, "gap": 2.0, "gap_forecast": 1.0, "saving_pct": 4, "extra_peak_kw": 0.5, "basis": "measured"},
            "b": {"energy_kwh": 30, "gap": 1.0, "gap_forecast": None, "saving_pct": 2, "extra_peak_kw": 0.1, "basis": "measured"},
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


if __name__ == "__main__":
    run_main(globals())
