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


def _hours(*sums, start=NOW - 6 * H):
    """Recorded long-term rows, one an hour, with these sums (state = sum)."""
    return [{"start": start + i * H, "state": v, "sum": v} for i, v in enumerate(sums)]


def test_a_reset_counted_again_is_written_over_with_what_detection_saw():
    # the kiln on 22.09: its reading dropped at a reset and came back with ten
    # kWh in it, which Home Assistant counted as that hour's energy
    hours = _hours(10.0, 10.0, 20.0, 20.0, 20.5, 20.5)          # NOW-6h .. NOW-1h
    hourly = {NOW - 2 * H: 500.0}                               # detection: it ran once, 0.5 kWh
    rows, fives, shift = named.plan_rewrite(hourly, NOW - 5 * H, hours, [])
    assert [(r["start"], r["sum"]) for r in rows] == [
        (NOW - 5 * H, 10.0), (NOW - 4 * H, 10.0), (NOW - 3 * H, 10.0), (NOW - 2 * H, 10.5), (NOW - H, 10.5)]
    assert [r["state"] for r in rows] == [10.0, 20.0, 20.0, 20.5, 20.5]   # the readings stay
    assert shift == -10.0                                       # the live hours carry on from 10.5


def test_a_new_meter_gets_the_hours_before_it_from_zero():
    hours = _hours(0.0, 0.1, start=NOW - 2 * H)
    hourly = {NOW - 5 * H: 500.0, NOW - 4 * H: 250.0, NOW - H: 100.0, NOW: 40.0}
    rows, _, shift = named.plan_rewrite(hourly, NOW - 6 * H, hours, [])
    assert [(r["start"], r["state"], r["sum"]) for r in rows] == [
        (NOW - 5 * H, 0.5, 0.5), (NOW - 4 * H, 0.75, 0.75), (NOW - 3 * H, 0.75, 0.75),
        (NOW - 2 * H, 0.0, 0.75), (NOW - H, 0.1, 0.85)]
    assert abs(shift - 0.75) < 1e-9                             # the hour still being recorded ends past NOW


def test_five_minute_rows_take_their_hours_share_and_meet_its_sum():
    hours = _hours(1.0, 1.0, start=NOW - 2 * H)
    fives = [{"start": NOW - H + i * 300, "state": 7.0, "sum": 99.0} for i in range(12)]
    rows, out, _ = named.plan_rewrite({NOW - H: 1200.0}, NOW - H, hours, fives)
    assert [round(r["sum"], 3) for r in out[:3]] == [1.1, 1.2, 1.3]
    assert out[-1]["sum"] == rows[-1]["sum"] == 2.2             # what the hour is compiled from
    assert {r["state"] for r in out} == {7.0}


def test_rewriting_twice_writes_the_same():
    hours = _hours(10.0, 10.0, 20.0, 20.0, 20.5, 20.5)
    hourly = {NOW - 4 * H: 300.0, NOW - 2 * H: 500.0}
    rows, _, shift = named.plan_rewrite(hourly, NOW - 5 * H, hours, [])
    fixed = hours[:1] + [dict(r) for r in rows]
    again, _, shift2 = named.plan_rewrite(hourly, NOW - 5 * H, fixed, [])
    assert again == rows and shift2 == 0.0


def test_nothing_is_written_before_the_meter_has_an_hour_or_with_no_window():
    assert named.plan_rewrite({NOW - H: 5.0}, NOW - 3 * H, [], []) == ([], [], 0.0)
    assert named.plan_rewrite({}, NOW, _hours(1.0, 1.0, start=NOW - 2 * H), []) == ([], [], 0.0)


def test_a_half_hour_zone_is_put_onto_whole_hours():
    rows, _, _ = named.plan_rewrite({NOW - H - 1800: 100.0, NOW - 1800: 100.0}, NOW - 3 * H,
                                    _hours(0.0, start=NOW - H), [])
    assert [(r["start"], r["sum"]) for r in rows] == [(NOW - 2 * H, 0.1), (NOW - H, 0.2)]


def test_a_shared_name_is_the_sum_of_its_signatures():
    det = D.Detector()
    for i, hourly in enumerate(({NOW - 2 * H: 100.0, NOW - H: 50.0}, {NOW - H: 25.0}), 1):
        det.signatures.append(D.Signature(id=i, phases="a", power={"a": 100.0}, duration_s=60.0, pf=None,
                                          count=1, first_seen=0, last_seen=0, name="Fridge", hourly=hourly))
    rows, _, _ = named.plan_rewrite(det.hourly_by_name("Fridge"), NOW - 3 * H, _hours(0.0, start=NOW - H), [])
    assert [(r["start"], r["sum"]) for r in rows] == [(NOW - 2 * H, 0.1), (NOW - H, 0.175)]


def test_the_reading_counts_only_what_detection_gains():
    step = named.carry_reading
    r, seen = step(None, None, 25.0, True)                     # new: starts where detection is
    assert (r, seen) == (25.0, 25.0)
    r, seen = step(r, seen, 26.0, True)
    assert r == 26.0
    r, seen = step(r, seen, 0.0, False)                        # a reset: detection starts again
    r, seen = step(r, seen, 9.0, False)                        # ...and re-reads ten days
    assert r == 26.0
    r, seen = step(r, seen, 9.5, True)                         # caught up: new energy counts
    assert r == 26.5
    r, seen = step(r, seen, 9.4999, True)                      # a rounding hair down
    assert r == 26.5
    assert step(26.5, None, 9.6, True) == (26.5, 9.6)          # a restart: nothing to count from yet


if __name__ == "__main__":
    run_main(globals())
