"""The scorecard's own arithmetic - capture, impurity, the floor, the
remainder, the invariance diff - on made-up numbers, no history needed.

    python3 tests/test_energy_bench.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import run_main  # noqa: E402
import energy_bench  # noqa: E402


def test_check():
    energy_bench.check()


def test_a_readings_reactive_power_comes_from_its_own_device_first():
    """As production derives it for every meter's channel (the unify audit,
    2026-10-03): a 3EM read as a main meter - Home's Hiša - its apparent
    power beside its power; the house template's own device has none, and
    the grid meter's volts and amps stand in for it."""
    import replay
    rows = [(0.0, 1000.0), (10.0, 1000.0)]
    series = {"sensor.hisa_phase_a_active_power": rows, "sensor.hisa_phase_a_apparent_power": [(0.0, 1250.0)]}
    assert replay.own_reactive(series, "sensor.hisa_phase_a_active_power", "a", rows) == {0.0: 750.0, 10.0: 750.0}
    assert replay.own_reactive({"sensor.se17k_home_power_phase_a": rows}, "sensor.se17k_home_power_phase_a", "a", rows) is None


def test_a_reading_is_scaled_by_the_unit_beside_its_file():
    """The CSVs carry no unit; units.json beside them does, read from the
    file's own folder through a symlink, and a kW meter is replayed in W
    (Home's EVBox, 2026-10-04)."""
    import json
    import os
    import tempfile
    import replay
    with tempfile.TemporaryDirectory() as tmp:
        site, tuning = Path(tmp, "site"), Path(tmp, "tuning")
        site.mkdir()
        tuning.mkdir()
        (site / "day.csv").write_text("entity_id,state,last_changed\nsensor.ev,10.78,2026-09-25T10:07:13+00:00\n"
                                      "sensor.plug,5,2026-09-25T10:07:13+00:00\n", encoding="utf-8")
        (site / "units.json").write_text(json.dumps({"sensor.ev": "kW", "sensor.plug": "W"}), encoding="utf-8")
        os.symlink(site / "day.csv", tuning / "day.csv")
        files = replay.expand([str(tuning)])
        scale = {e: replay.D.unit_scale(u) for e, u in replay.units(files).items()}
        got, _ = replay._series([str(tuning)], False, scale)
        assert got["sensor.ev"][0][1] == 10780.0 and got["sensor.plug"][0][1] == 5.0


if __name__ == "__main__":
    run_main(globals())
