"""Which of a solar device's sensors are the array's (INS-23)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

S = load("insights.solar")
P = load("insights.phases")


def e(entity_id, name):
    return {"entity_id": entity_id, "device_class": "power", "name": name}


# Andrej's SolarEdge Modbus Multi device: the inverter's total and the M1 GRID meter's phases side by side
DEVICE = [e("sensor.solaredge_i2_ac_power", "AC Power")] + [
    e(f"sensor.solaredge_i2_m1_ac_power_{p}", f"M1 AC Power {p.upper()}") for p in "abc"]


def test_the_grid_meter_on_the_inverters_device_is_not_the_array():
    # the bug: all three phases found, every one the grid meter
    assert [P.match_meter_entities(DEVICE).get(f"power_{p}") for p in "abc"] == [f"sensor.solaredge_i2_m1_ac_power_{p}" for p in "abc"]
    got = P.match_meter_entities(S.array_rows(DEVICE))
    assert not any(k.startswith("power_") and v.endswith(tuple("abc")) and "_m1_" in v for k, v in got.items()), got


def test_the_configured_grid_sensors_are_dropped_whatever_they_are_called():
    rows = [e(f"sensor.house_in_{p}", f"Line {p.upper()} power") for p in "abc"] + [e("sensor.pv_total", "PV power")]
    kept = S.array_rows(rows, [f"sensor.house_in_{p}" for p in "abc"])
    assert [r["entity_id"] for r in kept] == ["sensor.pv_total"]


def test_an_inverters_own_phases_are_kept():
    rows = [e(f"sensor.inverter_ac_power_{p}", f"AC power {p.upper()}") for p in "abc"]
    assert S.array_rows(rows) == rows


if __name__ == "__main__":
    run_main(globals())
