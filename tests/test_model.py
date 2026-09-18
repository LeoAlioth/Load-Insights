"""The site model reads every shape the Energy dashboard has had."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

model = load("insights.model")
SiteModel, PowerSpec = model.SiteModel, model.PowerSpec

LEGACY_2025_9 = {
    "energy_sources": [
        {"type": "grid",
         "flow_from": [{"stat_energy_from": "sensor.grid_import", "stat_cost": None, "entity_energy_price": None, "number_energy_price": 0.2}],
         "flow_to": [{"stat_energy_to": "sensor.grid_export", "stat_compensation": None, "entity_energy_price": None, "number_energy_price": 0.05}],
         "cost_adjustment_day": 0.0},
        {"type": "solar", "stat_energy_from": "sensor.pv_energy", "config_entry_solar_forecast": ["abc123"]},
        {"type": "battery", "stat_energy_from": "sensor.batt_out", "stat_energy_to": "sensor.batt_in"},
        {"type": "gas", "stat_energy_from": "sensor.gas", "stat_cost": None, "entity_energy_price": None, "number_energy_price": None},
    ],
    "device_consumption": [
        {"stat_consumption": "sensor.workshop_energy", "name": "Workshop"},
        {"stat_consumption": "sensor.boiler_energy", "name": "Workshop boiler", "included_in_stat": "sensor.workshop_energy"},
        {"stat_consumption": "sensor.evse_energy"},
    ],
}

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


def test_legacy_grid_shape_is_read():
    s = SiteModel.from_prefs(LEGACY_2025_9)
    assert s.grid_import == ("sensor.grid_import",) and s.grid_export == ("sensor.grid_export",)
    assert s.solar == ("sensor.pv_energy",) and s.solar_forecast_entries == ("abc123",)
    assert s.battery_out == ("sensor.batt_out",) and s.battery_in == ("sensor.batt_in",)
    assert s.grid_power == () and s.battery_power == () and s.battery_soc == ()
    assert s.has_sources


def test_gas_and_water_are_not_electrical_consumption():
    s = SiteModel.from_prefs(LEGACY_2025_9)
    assert "sensor.gas" not in s.all_statistic_ids()


def test_2026_6_shape_reads_power_soc_and_capacity():
    s = SiteModel.from_prefs(NEW_2026_6)
    assert s.grid_power == (PowerSpec(rate_from="sensor.grid_import_w", rate_to="sensor.grid_export_w"),)
    assert s.solar_power == ("sensor.pv_power",)
    assert s.battery_power == (PowerSpec(rate="sensor.batt_power"),)
    assert s.battery_soc == ("sensor.batt_soc",) and s.battery_capacity_kwh == 10.24
    assert s.solar_forecast_entries == ("abc123", "def456")
    assert s.devices[0].power == "sensor.evse_power" and s.devices[0].label == "Elvi"


def test_bare_stat_rate_on_a_source_reads_as_a_single_signed_sensor():
    src = {"type": "grid", "stat_energy_from": "sensor.g", "stat_rate": "sensor.g_w"}
    s = SiteModel.from_prefs({"energy_sources": [src], "device_consumption": []})
    assert s.grid_power == (PowerSpec(rate="sensor.g_w"),)


def test_consumption_terms_carry_the_dashboard_signs():
    s = SiteModel.from_prefs(NEW_2026_6)
    assert dict(s.consumption_terms()) == {
        "sensor.grid_import": 1.0, "sensor.grid_export": -1.0, "sensor.pv_energy": 1.0,
        "sensor.batt_out": 1.0, "sensor.batt_in": -1.0,
    }


def test_a_device_inside_another_listed_device_is_not_subtracted_twice():
    s = SiteModel.from_prefs(LEGACY_2025_9)
    assert [d.energy for d in s.remainder_devices()] == ["sensor.workshop_energy", "sensor.evse_energy"]


def test_included_in_an_unlisted_device_still_counts():
    prefs = {"energy_sources": [{"type": "grid", "stat_energy_from": "sensor.g"}],
             "device_consumption": [{"stat_consumption": "sensor.a", "included_in_stat": "sensor.not_listed"}]}
    s = SiteModel.from_prefs(prefs)
    assert [d.energy for d in s.remainder_devices()] == ["sensor.a"]


def test_no_dashboard_means_no_sources():
    assert not SiteModel.from_prefs(None).has_sources
    assert not SiteModel.from_prefs({}).has_sources
    assert not SiteModel.from_prefs({"energy_sources": [{"type": "solar", "stat_energy_from": "sensor.pv"}]}).has_sources


def test_summary_counts_for_the_form():
    s = SiteModel.from_prefs(LEGACY_2025_9).summary()
    assert (s["grid"], s["solar"], s["battery"], s["devices"], s["forecasts"]) == (2, 1, 2, 3, 1)
    assert s["device_names"] == ["Workshop", "Workshop boiler", "sensor.evse_energy"]


def test_the_dashboard_already_states_which_way_round_the_meter_is():
    """None / normal / inverted / a pair of sensors - the grid source asks
    exactly this, so a meter that reports export positive is DECLARED and
    never has to be inferred from how it behaves."""
    def polarity(power_config):
        src = {"type": "grid", "stat_energy_from": "sensor.imported"}
        if power_config is not None:
            src["power_config"] = power_config
        site = SiteModel.from_prefs({"energy_sources": [src]})
        return site.grid_power[0].polarity if site.grid_power else None

    assert polarity({"stat_rate": "sensor.m1_power"}) == 1
    assert polarity({"stat_rate_inverted": "sensor.m1_power"}) == -1
    # a pair names its own directions, so there is no polarity to state
    assert polarity({"stat_rate_from": "sensor.imp", "stat_rate_to": "sensor.exp"}) == 1
    assert polarity(None) is None
    # the 2025.12 shape, a bare stat_rate outside any power_config
    site = SiteModel.from_prefs({"energy_sources": [
        {"type": "grid", "stat_energy_from": "sensor.i", "stat_rate": "sensor.p"}]})
    assert site.grid_power[0].polarity == 1
    assert site.grid_power[0].rate_entity == "sensor.p"


if __name__ == "__main__":
    run_main(globals())
