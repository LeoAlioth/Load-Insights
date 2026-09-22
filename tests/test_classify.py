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
    assert g.kind == C.VARIABLE, g


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


def test_gliding_is_not_holding_a_level_even_when_no_step_is_taken():
    """Kozolec's Grundfos Scala2 runs 104 to 247 W to hold pressure and never
    takes a step big enough to count as a level, so it read as one flat level
    at a near-unity power factor - which is a small heating element. The
    wander WITHIN a run is what separates them, and it is a measurement."""
    scala = C.classify(206.0, 0.96, 1.05, 240.0, "a", low=104.0, high=247.0)
    heater = C.classify(206.0, 0.96, 1.05, 240.0, "a", low=203.0, high=209.0)
    assert scala.kind == C.VARIABLE, scala
    assert heater.kind == C.HEATER, heater
    assert scala.tag == "pump?" and heater.tag == "heater"
    assert any("varies between 104 W and 247 W" in b for b in scala.because), scala.because
    assert any("steady" in b for b in heater.because), heater.because
    # with nothing measured it falls back to levels alone, as before
    assert C.classify(206.0, 0.96, 1.05, 240.0, "a").kind == C.HEATER


def test_a_pump_is_recognised_whether_or_not_it_has_a_drive():
    """Both of Anze's houses have a pressure pump and they are electrically
    opposite: the Metabo goes straight to the line and reads like the
    induction motor it is, the Scala2 sits behind a drive that corrects its
    own power factor back to unity. A window that fits one excludes the
    other."""
    metabo = C.classify(1000.0, 0.80, 1.0, 180.0, "a", low=960.0, high=1040.0)
    scala = C.classify(206.0, 0.96, 1.05, 240.0, "a", low=104.0, high=247.0)
    assert metabo.kind == C.MOTOR and scala.kind == C.VARIABLE
    assert metabo.appliance == scala.appliance == C.PUMP


def test_a_balanced_three_phase_motor_says_so():
    """Almost nothing in a house is a balanced three-phase motor and a
    workshop is full of them, so it is a family of its own rather than a
    guess about the house."""
    one = C.classify(2491.0, 0.90, 1.1, 64.0, "b")
    three = C.classify(2491.0, 0.90, 1.1, 64.0, "abc")
    assert three.kind == C.MOTOR_3P and three.tag == "workshop?"
    assert one.kind != C.MOTOR_3P


def test_a_meters_name_says_what_is_on_it_where_shape_cannot():
    """The best evidence the integration ever gets, and it went unused for a
    long time: shape can only say a load draws 1.8 kW at a heating element's
    power factor, while whoever wired the site already wrote "Boiler" on the
    meter. Both cases here are real and both defeat the shape - Kozolec's
    boiler cycles 70 s where a hot-water profile wants a quarter of an hour,
    and its pressure pump sits behind a drive that corrects the factor to 0.96
    and reads as a heating element."""
    boiler = dict(watts=1822, pf=0.99, levels=1.0, duration_s=70, phases="a")
    assert C.classify(**boiler).appliance is None
    named = C.classify(where="Boiler", **boiler)
    assert named.appliance == C.WATER_TANK
    assert named.named and named.tag == "hot water"       # not "hot water?"
    assert any("called Boiler" in b for b in named.because)

    pump = dict(watts=236, pf=0.96, levels=1.0, duration_s=66, phases="a")
    assert C.classify(**pump).appliance is None
    assert C.classify(where="Hidrofor", **pump).appliance == C.PUMP


def test_a_room_is_not_a_device():
    """Most meters are named after ROOMS, and a room says nothing about what
    is plugged into it. Whole words only, so "ac" cannot match inside
    "Mansarda"."""
    for room in ("Mansarda", "Hiša", "Blaževa Soba", "Vtičnice - pisarna",
                 "Pond", "Pastir Staja", "Bug Lamp", "main", ""):
        assert C.appliance_from_name(room) is None, room
        assert C.family_from_name(room) is None, room


def test_both_languages_and_the_entity_id_when_that_is_all_there_is():
    """The names are the owner's. Anze's two sites run half in Slovene, and a
    device the Energy dashboard knows only by its statistic arrives as
    sensor.workshop_boiler_energy rather than as a name at all."""
    assert C.appliance_from_name("Water Pump") == C.PUMP
    assert C.appliance_from_name("Hidrofor") == C.PUMP
    assert C.appliance_from_name("sensor.kotlovnica_well_pump_energy") == C.PUMP
    assert C.appliance_from_name("Washing Machine") == C.WASHER
    assert C.appliance_from_name("sensor.dryer_energy") == C.DRYER
    assert C.appliance_from_name("sensor.workshop_boiler_energy") == C.WATER_TANK
    for charger in ("Pond EVSE", "Car charger", "Polnilnica", "wallbox"):
        assert C.family_from_name(charger) == C.CAR, charger


def test_a_named_charger_still_has_to_look_like_one():
    """A name explains what a reading is; it does not excuse one that
    disagrees. A 40 W thing on a meter called EVSE is not a car charging."""
    big = dict(watts=3566, pf=0.99, levels=1.0, duration_s=7200, phases="a")
    assert C.classify(where="Pond EVSE", **big).kind == C.CAR
    tiny = dict(watts=40, pf=0.99, levels=1.0, duration_s=7200, phases="a")
    assert C.classify(where="Pond EVSE", **tiny).kind != C.CAR
    brief = dict(watts=3566, pf=0.99, levels=1.0, duration_s=20, phases="a")
    assert C.classify(where="Pond EVSE", **brief).kind != C.CAR


if __name__ == "__main__":
    run_main(dict(globals()))
