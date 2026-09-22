"""Which of a meter device's sensors are its per-phase readings.

The fixtures are the two meters actually in use at home: a SolarEdge SE17K
meter 1, read over Modbus, and a Shelly Pro 3EM downstream of it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

D = load("insights.discovery")


def e(entity_id, device_class, name):
    return {"entity_id": entity_id, "device_class": device_class, "name": name}


# a SolarEdge meter names every reading "AC something", gives each phase
# against neutral AND each pair of phases, and carries the lifetime energy
# counters on the same device
SOLAREDGE = [
    e("sensor.solaredge_se17k_m1_ac_current_a", "current", "SE17K M1 AC Current A"),
    e("sensor.solaredge_se17k_m1_ac_current_b", "current", "SE17K M1 AC Current B"),
    e("sensor.solaredge_se17k_m1_ac_current_c", "current", "SE17K M1 AC Current C"),
    e("sensor.solaredge_se17k_m1_ac_current", "current", "SE17K M1 AC Current"),
    e("sensor.solaredge_se17k_m1_ac_voltage_an", "voltage", "SE17K M1 AC Voltage AN"),
    e("sensor.solaredge_se17k_m1_ac_voltage_bn", "voltage", "SE17K M1 AC Voltage BN"),
    e("sensor.solaredge_se17k_m1_ac_voltage_cn", "voltage", "SE17K M1 AC Voltage CN"),
    e("sensor.solaredge_se17k_m1_ac_voltage_ab", "voltage", "SE17K M1 AC Voltage AB"),
    e("sensor.solaredge_se17k_m1_ac_voltage_bc", "voltage", "SE17K M1 AC Voltage BC"),
    e("sensor.solaredge_se17k_m1_ac_voltage_ca", "voltage", "SE17K M1 AC Voltage CA"),
    e("sensor.solaredge_se17k_m1_ac_power_a", "power", "SE17K M1 AC Power A"),
    e("sensor.solaredge_se17k_m1_ac_power_b", "power", "SE17K M1 AC Power B"),
    e("sensor.solaredge_se17k_m1_ac_power_c", "power", "SE17K M1 AC Power C"),
    e("sensor.solaredge_se17k_m1_ac_power", "power", "SE17K M1 AC Power"),
    e("sensor.solaredge_se17k_m1_ac_power_factor_a", "power_factor", "SE17K M1 AC Power Factor A"),
    e("sensor.solaredge_se17k_m1_ac_power_factor_b", "power_factor", "SE17K M1 AC Power Factor B"),
    e("sensor.solaredge_se17k_m1_ac_power_factor_c", "power_factor", "SE17K M1 AC Power Factor C"),
    e("sensor.solaredge_se17k_m1_imported_a", "energy", "SE17K M1 Imported A"),
    e("sensor.solaredge_se17k_m1_exported_a", "energy", "SE17K M1 Exported A"),
    e("sensor.solaredge_se17k_m1_ac_frequency", "frequency", "SE17K M1 AC Frequency"),
]

# a Shelly Pro 3EM spells the phase out and carries the total on the same device
SHELLY = [
    e("sensor.shellypro3em_34987a459ae0_phase_a_active_power", "power", "Hiša Phase A Active Power"),
    e("sensor.shellypro3em_34987a459ae0_phase_b_active_power", "power", "Hiša Phase B Active Power"),
    e("sensor.shellypro3em_34987a459ae0_phase_c_active_power", "power", "Hiša Phase C Active Power"),
    e("sensor.shellypro3em_34987a459ae0_total_active_power", "power", "Hiša Total Active Power"),
    e("sensor.shellypro3em_34987a459ae0_phase_a_voltage", "voltage", "Hiša Phase A Voltage"),
    e("sensor.shellypro3em_34987a459ae0_phase_b_voltage", "voltage", "Hiša Phase B Voltage"),
    e("sensor.shellypro3em_34987a459ae0_phase_c_voltage", "voltage", "Hiša Phase C Voltage"),
    e("sensor.shellypro3em_34987a459ae0_phase_a_current", "current", "Hiša Phase A Current"),
    e("sensor.shellypro3em_34987a459ae0_phase_a_power_factor", "power_factor", "Hiša Phase A Power Factor"),
    e("sensor.shellypro3em_34987a459ae0_phase_a_apparent_power", "power", "Hiša Phase A Apparent Power"),
]


def test_line_to_neutral_is_that_phases_own_voltage():
    """The bug: a SolarEdge meter offers no bare "voltage A", only AN/BN/CN,
    so rejecting anything with two letters left the voltages empty."""
    got = D.match_meter_entities(SOLAREDGE)
    assert got.get("voltage_a", "").endswith("_voltage_an"), got.get("voltage_a")
    assert got.get("voltage_b", "").endswith("_voltage_bn"), got.get("voltage_b")
    assert got.get("voltage_c", "").endswith("_voltage_cn"), got.get("voltage_c")
    assert D.phase_of("ac_voltage_an") == "a"
    assert D.phase_of("ac_voltage_bn") == "b"
    assert D.phase_of("ac_voltage_cn") == "c"
    # the l1n spelling of the same thing
    assert D.phase_of("meter_l1n_voltage") == "a"
    assert D.phase_of("meter_l3n_voltage") == "c"


def test_line_to_line_voltage_belongs_to_no_single_phase():
    """It moves when either of its two phases does, so it is not a reading
    of either. However it is spelled."""
    for name in ("ac_voltage_ab", "ac_voltage_bc", "ac_voltage_ca",
                 "voltage_a_b", "voltage_l1_l2", "voltage_l2_l1", "voltage_ln"):
        assert D.phase_of(name) is None, name
    picked = set(D.match_meter_entities(SOLAREDGE).values())
    for eid in ("sensor.solaredge_se17k_m1_ac_voltage_ab",
                "sensor.solaredge_se17k_m1_ac_voltage_bc",
                "sensor.solaredge_se17k_m1_ac_voltage_ca"):
        assert eid not in picked, eid


def test_ac_means_alternating_current_not_phases_a_to_c():
    """Every SolarEdge reading is prefixed "AC". Reading that as a phase pair
    would reject the whole device."""
    got = D.match_meter_entities(SOLAREDGE)
    assert got["current_a"] == "sensor.solaredge_se17k_m1_ac_current_a"
    assert got["power_c"] == "sensor.solaredge_se17k_m1_ac_power_c"
    assert D.phase_of("ac_power_a") == "a"


def test_all_four_kinds_are_found_on_a_solaredge_meter():
    got = D.match_meter_entities(SOLAREDGE)
    for kind in ("power", "current", "voltage", "pf"):
        for phase in "abc":
            assert f"{kind}_{phase}" in got, (kind, phase, sorted(got))
    assert len(got) == 12, sorted(got)


def test_a_total_is_not_a_phase():
    got = D.match_meter_entities(SHELLY)
    assert "sensor.shellypro3em_34987a459ae0_total_active_power" not in got.values()
    assert got["power_a"].endswith("phase_a_active_power")
    assert got["voltage_b"].endswith("phase_b_voltage")
    assert got["pf_a"].endswith("phase_a_power_factor")


def test_the_plainest_reading_wins_over_a_qualified_one():
    """Apparent power is power, but active power is the one we want."""
    got = D.match_meter_entities(SHELLY)
    assert got["power_a"].endswith("active_power"), got["power_a"]
    rows = [
        e("sensor.m_power_a", "power", "M Power A"),
        e("sensor.m_power_a_import", "power", "M Power A Import"),
        e("sensor.m_reactive_power_a", "power", "M Reactive Power A"),
    ]
    assert D.match_meter_entities(rows)["power_a"] == "sensor.m_power_a"


def test_energy_counters_and_frequency_are_never_phase_readings():
    picked = set(D.match_meter_entities(SOLAREDGE).values())
    assert "sensor.solaredge_se17k_m1_imported_a" not in picked
    assert "sensor.solaredge_se17k_m1_ac_frequency" not in picked


def test_describe_match_says_what_was_recognised():
    got = D.match_meter_entities(SOLAREDGE)
    line = D.describe_match(got)
    assert "voltage A+B+C" in line, line
    assert "power factor A+B+C" in line, line
    assert D.describe_match({}).startswith("no per-phase readings")


def test_an_inverter_publishing_both_sides_is_left_to_the_user():
    """A MultiPlus offers input and output power on one device. Preferring
    the load side looks obvious and is wrong at least once: Kozolec has no
    load-side power at all, and its input is where the grid would connect,
    so that IS its meter (Anze, 2026-09-17). The matcher picks one and the
    form shows it for changing; it does not take sides."""
    rows = [
        e("sensor.multiplus_id_276_input_power_l1", "power", "MultiPlus Input Power L1"),
        e("sensor.multiplus_id_276_output_power_l1", "power", "MultiPlus Output Power L1"),
    ]
    got = D.match_meter_entities(rows)
    assert got["power_a"] in {r["entity_id"] for r in rows}, got
    assert len(got) == 1, got


def test_a_plain_grid_meter_is_unaffected_by_that_preference():
    got = D.match_meter_entities(SOLAREDGE)
    assert got["power_a"] == "sensor.solaredge_se17k_m1_ac_power_a", got["power_a"]


def test_a_power_reading_with_its_own_volts_and_amps_beside_it_wins():
    """Two readings of the same house at Kozolec, one on the GX device and
    one on the MultiPlus, differed by twenty characters of name and by the
    MultiPlus publishing voltage and current for the same phase. The second
    is what makes a power factor possible, so it outranks the shorter name."""
    rows = [
        {"entity_id": "sensor.gx_device_consumption_power_l1", "device_class": "power",
         "name": "", "device_id": "gx"},
        {"entity_id": "sensor.gx_device_consumption_current_l1", "device_class": "current",
         "name": "", "device_id": "gx"},
        {"entity_id": "sensor.multiplus_ii_48_15000_200_100_id_276_output_power_l1",
         "device_class": "power", "name": "", "device_id": "mp"},
        {"entity_id": "sensor.multiplus_ii_48_15000_200_100_id_276_output_current_l1",
         "device_class": "current", "name": "", "device_id": "mp"},
        {"entity_id": "sensor.multiplus_ii_48_15000_200_100_id_276_output_voltage_l1",
         "device_class": "voltage", "name": "", "device_id": "mp"},
    ]
    found = D.match_meter_entities(rows)
    assert found["power_a"] == "sensor.multiplus_ii_48_15000_200_100_id_276_output_power_l1"
    # the GX alone - no voltage - falls back to the plainest name as before
    assert D.match_meter_entities(rows[:2])["power_a"] == "sensor.gx_device_consumption_power_l1"
    # a power factor entity counts as coherence on its own
    pf = [{"entity_id": "sensor.long_name_meter_power_a", "device_class": "power", "name": "", "device_id": "x"},
          {"entity_id": "sensor.long_name_meter_pf_a", "device_class": "power_factor", "name": "", "device_id": "x"},
          {"entity_id": "sensor.short_power_a", "device_class": "power", "name": "", "device_id": "y"}]
    assert D.match_meter_entities(pf)["power_a"] == "sensor.long_name_meter_power_a"

def test_coherence_breaks_ties_but_never_overrules_which_side_we_want():
    """A MultiPlus publishes power, current and voltage on its AC INPUT as
    well as its output, and at Kozolec that input is a generator port sitting
    at zero. Preferring a reading with volts and amps beside it must not talk
    us onto the wrong side of an inverter."""
    rows = [
        {"entity_id": "sensor.mp_input_power_l1", "device_class": "power", "name": "", "device_id": "mp"},
        {"entity_id": "sensor.mp_input_current_l1", "device_class": "current", "name": "", "device_id": "mp"},
        {"entity_id": "sensor.mp_input_voltage_l1", "device_class": "voltage", "name": "", "device_id": "mp"},
        # the output side publishes watts alone - no volts, no amps
        {"entity_id": "sensor.mp_output_power_l1", "device_class": "power", "name": "", "device_id": "mp"},
    ]
    assert D.match_meter_entities(rows, "load")["power_a"] == "sensor.mp_output_power_l1"
    # and for the GRID role the preference flips, as it always did
    assert D.match_meter_entities(rows, "grid")["power_a"] == "sensor.mp_input_power_l1"
    # with no role word in play, coherence still decides
    plain = [
        {"entity_id": "sensor.meter_two_power_a", "device_class": "power", "name": "", "device_id": "b"},
        {"entity_id": "sensor.meter_two_current_a", "device_class": "current", "name": "", "device_id": "b"},
        {"entity_id": "sensor.meter_two_voltage_a", "device_class": "voltage", "name": "", "device_id": "b"},
        {"entity_id": "sensor.one_power_a", "device_class": "power", "name": "", "device_id": "a"},
    ]
    assert D.match_meter_entities(plain)["power_a"] == "sensor.meter_two_power_a"


def test_the_watts_that_go_with_a_meters_amps_are_the_ones_beside_them():
    """A meter publishing both sides of an inverter offers watts for each,
    and the amps we hold belong to exactly one of them - without needing to
    know that "output" is the word that matters."""
    both = ["sensor.mp_input_power_l1", "sensor.mp_output_power_l1"]
    assert D.closest_by_name(both, "sensor.mp_output_current_l1") == "sensor.mp_output_power_l1"
    assert D.closest_by_name(both, "sensor.mp_input_current_l1") == "sensor.mp_input_power_l1"
    # one candidate is the answer whatever it is called
    assert D.closest_by_name(["sensor.m1_ac_power_a"], "sensor.m1_ac_current_a") == "sensor.m1_ac_power_a"
    assert D.closest_by_name([], "sensor.anything") is None
    # a tie is broken the same way every time rather than by registry order
    tie = ["sensor.b_power_a", "sensor.a_power_a"]
    assert D.closest_by_name(tie, "sensor.z_current_a") == D.closest_by_name(list(reversed(tie)), "sensor.z_current_a")


def test_a_three_phase_shelly_is_one_device_per_phase():
    """The commonest three-phase meter there is, and it arrives as FOUR
    devices: a parent carrying the totals, where the Energy dashboard's
    statistic lives, and one child per phase pointing at it with
    via_device_id. Reading only the parent's own entities found a single
    total and nothing else, so a three-phase meter was taken for a one-phase
    one - which is what stopped Anze's attic and grid meters from ever being
    subtracted per phase (2026-09-22)."""
    parent = [
        {"entity_id": "sensor.attic_total_active_power", "device_class": "power",
         "name": "Total active power"},
        {"entity_id": "sensor.attic_total_active_energy", "device_class": "energy",
         "name": "Total active energy"},
    ]
    # a total is not a phase, so the parent alone yields nothing at all
    assert D.match_meter_entities(parent) == {}

    children = [
        {"entity_id": f"sensor.attic_phase_{p}_active_power", "device_class": "power",
         "name": f"Phase {p.upper()} active power"} for p in "abc"
    ]
    got = D.match_meter_entities(parent + children)
    assert got == {f"power_{p}": f"sensor.attic_phase_{p}_active_power" for p in "abc"}, got


if __name__ == "__main__":
    run_main(dict(globals()))
