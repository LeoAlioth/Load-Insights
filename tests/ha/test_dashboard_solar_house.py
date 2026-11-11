"""INS-23 (the owner, 2026-11-11: "yes add it by default"): with no inverter set up, the Energy dashboard's solar
source is the inverter term of the house. Andrej's site, 2026-10-21: SolarEdge Modbus Multi puts the M1 grid
meter (export positive) on the inverter's device; with no inverter entered the house was the meter alone and read
-221 / -224 / -288 W at noon. The M1's phase powers must never stand in for the array (insights/solar.array_rows).

Needs Home Assistant installed (detection.py imports it); run with Load Juggler's venv:
    ../Dynamic_OCPP_EVSE/.venv/bin/python tests/ha/test_dashboard_solar_house.py
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from _load import run_main  # noqa: E402
import custom_components.load_insights.detection as D  # noqa: E402
from custom_components.load_insights.const import CONF_DETECTION, CONF_INVERTERS, GRID_PREFIX  # noqa: E402

TOTAL = "sensor.solaredge_i2_ac_power"
M1 = {p: f"sensor.solaredge_i2_m1_ac_power_{p}" for p in "abc"}
HOUSE_W = 100.0          # what each phase really draws


def _series():
    """Two hours of a morning at 6 s: the array ramps 0 -> 900 W (a third on each phase), the M1 reads export
    positive, so it imports early and exports later - what tells its sign."""
    t0, out = 1_762_840_800.0, {}
    ts = [t0 + 6.0 * k for k in range(1200)]
    pv = [900.0 * min(1.0, k / 600.0) for k in range(1200)]
    out[TOTAL] = list(zip(ts, pv))
    for p in "abc":
        out[M1[p]] = [(t, w / 3.0 - HOUSE_W) for t, w in zip(ts, pv)]
    return out


def _runner(options):
    rows = _series()
    r = object.__new__(D.DetectionRunner)
    r.hass = None
    r.entry = SimpleNamespace(options={CONF_DETECTION: {f"{GRID_PREFIX}power_{p}": M1[p] for p in "abc"}, **options})
    r._exports_positive = False

    async def read(start, end, cfg):
        return {k.split("_")[-1]: rows[eid] for k, eid in cfg.items() if eid in rows}, {}
    r._read = read
    return r


def _site(device_rows):
    async def manager(hass):
        return SimpleNamespace(data=None)
    D.async_get_manager = manager
    # as Andrej's dashboard has it (snapshot 2026-11-11): the energy statistic, and its power as stat_rate
    D.energy_site = lambda hass, data: SimpleNamespace(solar=["sensor.solaredge_i2_ac_energy"], solar_power=[TOTAL],
                                                      grid_power=[])
    D.er = SimpleNamespace(async_get=lambda hass: SimpleNamespace(
        async_get=lambda eid: SimpleNamespace(device_id="solaredge_i2")))
    D.device_rows = lambda hass, device_id: device_rows


SOLAREDGE = [{"entity_id": TOTAL, "device_class": "power", "name": "AC Power"},
             *({"entity_id": M1[p], "device_class": "power", "name": f"M1 AC Power {p.upper()}"} for p in "abc")]


def test_with_no_inverter_set_up_the_dashboards_solar_keeps_the_house_above_zero():
    _site(SOLAREDGE)
    house = asyncio.run(_runner({})._derive_load(None, None))
    assert set(house) == {"a", "b", "c"}, house.keys()
    for p, rows in house.items():
        ws = [w for _, w in rows]
        assert min(ws) > 0.0, (p, min(ws))
        assert abs(sum(ws) / len(ws) - HOUSE_W) < 5.0, (p, sum(ws) / len(ws))


def test_the_m1_on_the_inverters_device_never_stands_in_for_the_array():
    _site(SOLAREDGE)
    arrays = asyncio.run(_runner({})._dashboard_arrays())
    assert arrays == [{f"power_{p}": TOTAL for p in "abc"}], arrays


def test_with_no_solar_source_the_house_is_the_meter_alone_as_before():
    _site([{"entity_id": M1[p], "device_class": "power", "name": f"M1 AC Power {p.upper()}"} for p in "abc"])
    D.energy_site = lambda hass, data: SimpleNamespace(solar=[], solar_power=[], grid_power=[])
    house = asyncio.run(_runner({})._derive_load(None, None))
    assert min(w for rows in house.values() for _, w in rows) < 0.0      # the repair's case


def test_an_inverter_set_up_wins_and_the_dashboard_is_not_read():
    async def no(*a):
        raise AssertionError("the dashboard's solar was read although an inverter is set up")
    _site(SOLAREDGE)
    r = _runner({CONF_INVERTERS: [{"power": TOTAL}]})
    r._dashboard_arrays = no

    async def no_split(*a):
        return None
    r._inverter_split = no_split
    house = asyncio.run(r._derive_load(None, None))
    assert min(w for rows in house.values() for _, w in rows) > 0.0


if __name__ == "__main__":
    run_main(globals())
