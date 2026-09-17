"""What kind of load a signature might be, and how sure that is."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

C = load("insights.classify")


def test_a_resistive_load_reads_as_a_heating_element():
    g = C.classify(2100, pf=1.0, levels=1.0, duration_s=1200)
    assert g.kind == C.HEATER, g
    assert g.confidence > 0.6 and g.alternative is None, g
    assert "power factor 1.00" in g.words and "2.1 kW" in g.words, g.words


def test_a_lagging_load_reads_as_a_motor():
    g = C.classify(900, pf=0.72, levels=1.0, duration_s=600)
    assert g.kind == C.MOTOR and g.confidence > 0.6, g


def test_a_small_poor_factor_load_reads_as_electronics():
    g = C.classify(90, pf=0.52, levels=1.0, duration_s=36000)
    assert C.SUPPLY in (g.kind, g.alternative), g


def test_a_modulating_load_reads_as_inverter_driven():
    g = C.classify(2000, pf=0.96, levels=3.0, duration_s=5400)
    assert g.kind == C.INVERTER, g


def test_two_families_that_fit_equally_well_are_both_named():
    """A 2 kW hour-long run at unity really is either a water heater or a
    car. Picking one would be inventing certainty."""
    g = C.classify(2000, pf=0.99, levels=1.0, duration_s=3600)
    assert {g.kind, g.alternative} == {C.HEATER, C.CAR}, g
    assert "or" in g.words, g.words


def test_without_a_power_factor_it_says_so_instead_of_guessing():
    g = C.classify(2100, pf=None, levels=1.0, duration_s=1200)
    assert g.kind is None and g.confidence == 0.0, g
    assert "power factor" in g.because[0] and "voltage and current" in g.because[0], g.because


def test_a_programme_is_recognised_by_its_steps_even_without_a_factor():
    g = C.classify(1800, pf=None, levels=4.0, duration_s=5400)
    assert g.kind == C.PROGRAMME, g
    assert 0 < g.confidence <= C.NO_PF_CAP, g.confidence


def test_no_guess_is_ever_certain():
    """Shape cannot identify a device - a hair dryer and a fan heater are
    the same reading - so nothing may claim it."""
    cases = [(2100, 1.0, 1.0, 600), (900, 0.7, 1.0, 300), (7000, 0.99, 1.0, 9000),
             (150, 0.5, 1.0, 40000), (3000, 0.95, 4.0, 7200)]
    for watts, pf, levels, dur in cases:
        g = C.classify(watts, pf, levels, dur)
        assert g.confidence <= C.MAX_CONFIDENCE, (g, watts, pf)


def test_a_reading_outside_every_family_says_nothing():
    g = C.classify(5.0, pf=0.95, levels=1.0, duration_s=10)
    assert g.kind is None, g


if __name__ == "__main__":
    run_main(dict(globals()))
