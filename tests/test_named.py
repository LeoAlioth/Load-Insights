"""A named load that is a metered device, and the history its meter is owed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

named = load("insights.named")
D = load("insights.detect")
H = 3600
NOW = 1_790_000_000 // H * H          # an hour start


def test_a_load_named_after_a_one_device_meter_is_that_device():
    meters = named.one_device_meters({"Hidrofor": True, "Hiša": False, "Water Pump": True})
    assert meters == ["Hidrofor", "Water Pump"]
    assert named.metered_device("Hidrofor", meters) == "Hidrofor"
    assert named.metered_device("  water pump ", meters) == "Water Pump"   # case and spaces
    assert named.metered_device("Hiša", meters) is None                     # holds several
    assert named.metered_device("Water", meters) is None                    # a whole name, not a part
    assert named.metered_device("", ["", "Boiler"]) is None


def test_a_picked_meter_names_the_load_over_what_was_typed():
    assert named.chosen_name("Hidrofr", "Hidrofor") == "Hidrofor"
    assert named.chosen_name("  Kiln ", None) == "Kiln"
    assert named.chosen_name(None, "Boiler") == "Boiler"
    assert named.chosen_name("  ", None) is None and named.chosen_name(None, None) is None


def test_backfill_stops_before_the_first_recorded_hour():
    hourly = {NOW - 5 * H: 500.0, NOW - 4 * H: 250.0, NOW - 2 * H: 1000.0, NOW - H: 100.0, NOW: 40.0}
    rows, total = named.plan_backfill(hourly, NOW - 2 * H, NOW)
    assert [r["start"] for r in rows] == [NOW - 5 * H, NOW - 4 * H]
    assert [(r["state"], r["sum"]) for r in rows] == [(0.5, 0.5), (0.75, 0.75)]   # kWh, from 0
    assert total == 0.75


def test_backfill_with_nothing_recorded_stops_before_the_current_hour():
    rows, total = named.plan_backfill({NOW - H: 200.0, NOW: 40.0, NOW + H: 1.0}, None, NOW)
    assert [(r["start"], r["sum"]) for r in rows] == [(NOW - H, 0.2)] and total == 0.2


def test_backfill_twice_writes_nothing_the_second_time():
    hourly = {NOW - 3 * H: 300.0, NOW - 2 * H: 300.0}
    rows, _ = named.plan_backfill(hourly, NOW - H, NOW)
    # the rows written are now the meter's first: nothing lies before them
    assert named.plan_backfill(hourly, rows[0]["start"], NOW) == ([], 0.0)


def test_backfill_with_no_hourly_data_does_nothing():
    assert named.plan_backfill({}, None, NOW) == ([], 0.0)
    assert named.plan_backfill({NOW - H: 5.0}, NOW - 3 * H, NOW) == ([], 0.0)   # all after the first row


def test_backfill_puts_a_half_hour_zone_onto_whole_hours():
    rows, total = named.plan_backfill({NOW - H - 1800: 100.0, NOW - 1800: 100.0}, None, NOW)
    assert [(r["start"], r["sum"]) for r in rows] == [(NOW - 2 * H, 0.1), (NOW - H, 0.2)] and total == 0.2


def test_backfill_of_a_shared_name_is_the_sum_of_its_signatures():
    det = D.Detector()
    for i, hourly in enumerate(({NOW - 2 * H: 100.0, NOW - H: 50.0}, {NOW - H: 25.0}), 1):
        det.signatures.append(D.Signature(id=i, phases="a", power={"a": 100.0}, duration_s=60.0, pf=None,
                                          count=1, first_seen=0, last_seen=0, name="Fridge", hourly=hourly))
    rows, total = named.plan_backfill(det.hourly_by_name("Fridge"), None, NOW)
    assert [(r["start"], r["sum"]) for r in rows] == [(NOW - 2 * H, 0.1), (NOW - H, 0.175)] and total == 0.175


if __name__ == "__main__":
    run_main(globals())
