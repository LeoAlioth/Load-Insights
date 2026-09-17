"""What the meter will do: consumption less PV, then the battery."""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

G = load("insights.grid")
TZ = ZoneInfo("Europe/Ljubljana")
T0 = datetime(2026, 9, 17, 0, 0, tzinfo=TZ)


def hours(vals, start=T0):
    return [(start + timedelta(hours=i), v) for i, v in enumerate(vals)]


def pv(vals, start=T0):
    return {(start + timedelta(hours=i)).timestamp(): v for i, v in enumerate(vals)}


def test_without_pv_the_meter_is_the_consumption():
    g = G.build(hours([1.0, 2.0, 0.5]))
    assert [round(h.net_kwh, 3) for h in g.hours] == [1.0, 2.0, 0.5]
    assert g.import_kwh == 3.5 and g.export_kwh == 0.0
    assert not g.battery_modelled and g.pv_hours == 0
    assert all(h.soc is None for h in g.hours)


def test_pv_turns_the_meter_around():
    g = G.build(hours([1.0, 1.0, 1.0]), pv([0.0, 3.0, 1.0]))
    assert [round(h.net_kwh, 3) for h in g.hours] == [1.0, -2.0, 0.0]
    assert g.import_kwh == 1.0 and g.export_kwh == 2.0
    assert g.pv_hours == 3


def test_a_missing_pv_hour_counts_as_no_sun_and_is_reported():
    g = G.build(hours([1.0, 1.0]), {T0.timestamp(): 2.0})       # only the first hour forecast
    assert [round(h.net_kwh, 3) for h in g.hours] == [-1.0, 1.0]
    assert g.pv_hours == 1, "the caller can see how far the PV forecast reached"


def test_the_battery_absorbs_the_surplus_then_carries_the_evening():
    # 10 kWh pack at 50 %: 5 kWh in it, 5 kWh of room
    g = G.build(hours([1.0, 1.0, 1.0, 1.0]), pv([4.0, 4.0, 0.0, 0.0]),
                soc=50.0, capacity_kwh=10.0)
    assert g.battery_modelled
    n = [round(h.net_kwh, 3) for h in g.hours]
    b = [round(h.battery_kwh, 3) for h in g.hours]
    s = [round(h.soc, 1) for h in g.hours]
    assert b[0] == -3.0 and b[1] == -2.0, b        # 3 kWh surplus, then only 2 kWh of room left
    assert n[0] == 0.0 and n[1] == -1.0, n         # full: the last 1 kWh exports
    assert s[1] == 100.0
    assert b[2] == 1.0 and n[2] == 0.0, (b, n)     # evening drawn from the pack
    assert s[3] == 80.0


def test_an_empty_pack_imports():
    g = G.build(hours([2.0, 2.0]), soc=5.0, capacity_kwh=10.0)
    assert [round(h.battery_kwh, 3) for h in g.hours] == [0.5, 0.0]
    assert [round(h.net_kwh, 3) for h in g.hours] == [1.5, 2.0]
    assert g.hours[1].soc == 0.0


def test_rated_power_bounds_what_the_pack_can_do():
    g = G.build(hours([0.0, 5.0]), pv([9.0, 0.0]), soc=50.0, capacity_kwh=20.0,
                max_charge_w=3000.0, max_discharge_w=2000.0)
    assert round(g.hours[0].battery_kwh, 3) == -3.0, "charge capped at 3 kW"
    assert round(g.hours[0].net_kwh, 3) == -6.0, "the rest exports"
    assert round(g.hours[1].battery_kwh, 3) == 2.0, "discharge capped at 2 kW"
    assert round(g.hours[1].net_kwh, 3) == 3.0


def test_reserve_limits_are_respected():
    g = G.build(hours([2.0, 2.0]), soc=50.0, capacity_kwh=10.0, soc_min=40.0)
    assert round(g.hours[0].battery_kwh, 3) == 1.0, "only down to the floor"
    assert g.hours[0].soc == 40.0 and g.hours[1].battery_kwh == 0.0


def test_no_soc_means_no_battery_and_the_net_is_reported_before_it():
    g = G.build(hours([1.0]), pv([3.0]), soc=None, capacity_kwh=10.0)
    assert not g.battery_modelled
    assert g.hours[0].net_kwh == g.hours[0].net_before_battery_kwh == -2.0
    assert g.hours[0].soc is None


def test_an_empty_forecast_is_an_empty_answer():
    g = G.build([])
    assert g.hours == () and g.import_kwh == 0.0 and g.export_kwh == 0.0


if __name__ == "__main__":
    run_main(globals())
