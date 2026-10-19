"""A renamed entity is followed through the settings and the Energy dashboard."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

R = load("insights.renames")

NEW_2026_6 = {
    "energy_sources": [
        {"type": "grid", "stat_energy_from": "sensor.grid_import", "stat_energy_to": "sensor.grid_export",
         "stat_cost": None, "entity_energy_price": None, "number_energy_price": None,
         "stat_compensation": None, "entity_energy_price_export": None, "number_energy_price_export": None,
         "power_config": {"stat_rate_from": "sensor.grid_import_w", "stat_rate_to": "sensor.grid_export_w"},
         "cost_adjustment_day": 0.0, "name": "Elektro"},
        {"type": "solar", "stat_energy_from": "sensor.pv_energy", "stat_rate": "sensor.pv_power",
         "config_entry_solar_forecast": ["abc123", "def456"]},
        {"type": "battery", "stat_energy_from": "sensor.batt_out", "stat_energy_to": "sensor.batt_in",
         "power_config": {"stat_rate": "sensor.batt_power"}, "stat_soc": "sensor.batt_soc", "capacity": 10.24},
    ],
    "device_consumption": [
        {"stat_consumption": "sensor.evse_energy", "stat_rate": "sensor.evse_power", "name": "Elvi"},
    ],
}


def test_a_renamed_entity_is_followed_through_the_settings():
    renames = {"sensor.boiler_energy": "sensor.bojler_energy", "climate.t": "climate.termostat"}
    options = {"input_entities": ["climate.t", "sensor.room"],
               "input_links": {"climate.t": ["sensor.boiler_energy", "load:Mat"]},
               "detection": {"single_device": ["sensor.boiler_energy"], "interval_minutes": "5"},
               "inverters": [{"power": "sensor.boiler_energy_x"}]}
    out = R.follow_renames(options, renames)
    assert out["input_entities"] == ["climate.termostat", "sensor.room"]
    assert out["input_links"] == {"climate.termostat": ["sensor.bojler_energy", "load:Mat"]}
    assert out["detection"] == {"single_device": ["sensor.bojler_energy"], "interval_minutes": "5"}
    assert out["inverters"] == [{"power": "sensor.boiler_energy_x"}]     # only a whole id is one
    assert R.follow_renames(options, {}) == options


def test_the_dashboard_is_told_what_its_renamed_statistics_became():
    renames = {"sensor.evse_energy": "sensor.elvi_energy", "sensor.grid_import_w": "sensor.m1_import_w",
               "sensor.water": "sensor.voda", "sensor.not_on_it": "sensor.x"}
    prefs = {**NEW_2026_6, "device_consumption_water": [{"stat_consumption": "sensor.water"}],
             "device_consumption": NEW_2026_6["device_consumption"] + [
                 {"stat_consumption": "sensor.pec", "included_in_stat": "sensor.evse_energy"}]}
    named, update = R.dashboard_renames(prefs, renames)
    assert named == {k: v for k, v in renames.items() if k != "sensor.not_on_it"}
    assert set(update) == {"energy_sources", "device_consumption", "device_consumption_water"}
    assert update["energy_sources"][0]["power_config"]["stat_rate_from"] == "sensor.m1_import_w"
    assert update["energy_sources"][1:] == NEW_2026_6["energy_sources"][1:]
    assert [(d["stat_consumption"], d.get("included_in_stat")) for d in update["device_consumption"]] == \
        [("sensor.elvi_energy", None), ("sensor.pec", "sensor.elvi_energy")]
    # only the lists that change, and nothing at all without a dashboard
    assert set(R.dashboard_renames(prefs, {"sensor.water": "sensor.voda"})[1]) == {"device_consumption_water"}
    assert R.dashboard_renames(prefs, {"sensor.not_on_it": "sensor.x"}) == ({}, {})
    assert R.dashboard_renames(None, renames) == ({}, {})


if __name__ == "__main__":
    run_main(globals())
