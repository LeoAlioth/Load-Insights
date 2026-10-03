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


if __name__ == "__main__":
    run_main(globals())
