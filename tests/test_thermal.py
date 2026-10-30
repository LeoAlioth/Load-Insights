"""Energy per degree: heating runs, the learned figure, the room it gives."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

TH = load("insights.thermal")
H = 3600.0
K = 0.2          # kWh per K, the tank's own


def clean_tank(days):
    """Each run: 3 hours at 0.6 kWh/h = 1.8 kWh for 9 K (k = 0.2); cooling 0.5 K an hour."""
    energy, temps, t = [], {}, 40.0
    for d in range(days):
        for h in range(24):
            key = (d * 24 + h) * H
            temps[key] = round(t * 2) / 2
            heating = 6 <= h <= 8
            energy.append((key, 0.6 if heating else 0.0))
            t += (0.6 / K) if heating else -0.5
    return energy, temps


def test_a_clean_tank_gives_its_own_figure():
    energy, temps = clean_tank(6)
    runs = TH.heating_runs(energy, temps)
    assert len(runs) >= 5
    k, spread, n = TH.kwh_per_degree(runs, TH.TANK)
    assert abs(k - K) < 0.02 and spread < 0.1 and n >= 3


def test_too_few_runs_is_unknown():
    energy, temps = clean_tank(3)
    runs = TH.heating_runs(energy, temps)
    assert TH.kwh_per_degree(runs[:2], TH.TANK) is None


def test_a_run_with_a_draw_in_it_is_dropped():
    energy, temps = clean_tank(6)
    keys = sorted(temps)
    base = len(TH.heating_runs(energy, temps))
    temps[keys[2 * 24 + 8]] -= 6      # the hour after the second heating hour is colder than its predecessor
    assert len(TH.heating_runs(energy, temps)) == base - 1


def test_a_room_keeps_a_run_that_dips_inside_it_a_tank_does_not():
    energy, temps = clean_tank(6)
    clean = len(TH.heating_runs(energy, temps))
    temps[sorted(temps)[2 * 24 + 8]] -= 4      # a dip inside the third day's run, its net rise unchanged
    assert len(TH.heating_runs(energy, temps, allow_dips=True)) == clean
    assert len(TH.heating_runs(energy, temps)) == clean - 1


def test_a_rise_inside_the_thermometers_resolution_is_not_a_run():
    energy, _ = clean_tank(6)
    flat = {k: 40.0 + (0.5 if (k // H) % 24 in (7, 8, 9) else 0.0) for k, _ in energy}
    assert TH.heating_runs(energy, flat) == []


def _runs(ks):
    return [TH.Run(i * H, k * 10.0, 10.0) for i, k in enumerate(ks)]


def test_a_spread_is_tolerated_by_a_room_before_a_tank():
    mixed = _runs((0.1, 0.1, 0.1, 0.3, 0.3, 0.3))        # spread 0.5: above a tank's 0.35, below a room's 0.6
    assert TH.kwh_per_degree(mixed, TH.TANK) is None
    assert TH.kwh_per_degree(mixed, TH.ROOM) is not None
    assert TH.kwh_per_degree(_runs((0.1, 0.1, 0.1, 0.5, 0.5, 0.5)), TH.ROOM) is None     # 0.67 is too wild for both


def test_a_room_follows_its_recent_runs_a_tank_its_longer_window():
    runs = _runs([0.2] * 8 + [0.3] * 6)
    assert abs(TH.kwh_per_degree(runs, TH.ROOM)[0] - 0.3) < 1e-9        # the last six
    assert abs(TH.kwh_per_degree(runs, TH.TANK)[0] - 0.25) < 1e-9       # the last twelve: six of each


def test_the_room_is_degrees_below_boost_times_k_and_never_negative():
    assert TH.room_from_thermal(0.2, 42.0, 60.0) == 3.6
    assert TH.room_from_thermal(0.2, 61.0, 60.0) == 0.0
    assert TH.room_from_thermal(0.2, None, 60.0) is None
    assert TH.room_from_thermal(0.2, 42.0, None) is None


if __name__ == "__main__":
    run_main(globals())
