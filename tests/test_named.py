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


def test_a_meter_is_rewritten_from_the_first_hour_it_is_off():
    hours = _hours(10.0, 10.0, 10.2, 10.5, 10.5, 10.9)          # NOW-6h .. NOW-1h
    hourly = {NOW - 4 * H: 200.0, NOW - 3 * H: 300.0, NOW - H: 400.0}
    rows, _, shift = named.plan_rewrite(hourly, NOW - 5 * H, hours, [])
    assert named.first_change(rows, hours) is None and shift == 0.0     # it says what detection saw
    near = _hours(10.0, 10.0, 10.204, 10.508, 10.508, 10.903)          # a restart's rounding: a few Wh
    assert named.first_change(rows, near) is None
    # a run counted in the hour it closed, filed into the hours it ran in
    late = _hours(10.0, 10.0, 10.0, 10.5, 10.5, 10.9)
    assert named.first_change(rows, late) == NOW - 4 * H
    drifted = _hours(10.0, 10.0, 10.2, 10.5, 10.5, 11.0)                # the total alone: the shift
    assert named.first_change(rows, drifted) == NOW - H
    gap = [r for r in hours if r["start"] != NOW - 3 * H]               # Home Assistant was down
    assert named.first_change(rows, gap) == NOW - 3 * H


def _fleet(*sigs):
    """A grid and Hiša's fleet: (meter, id, name[, successor]) each."""
    fleet = D.Fleet()
    fleet.subs["Hiša"] = D.Detector()
    for meter, i, name, *heir in sigs:
        (fleet.subs[meter] if meter else fleet.main).signatures.append(_sig(i, name, successor_id=(heir or [None])[0]))
    return fleet


def _sig(i, name=None, count=12, wh=600.0, **kw):
    """A load seen ``count`` times, ``wh`` in all, repeating exactly: evidence 1.0."""
    return D.Signature(id=i, phases="a", power={"a": 100.0}, duration_s=60.0, pf=None, count=count,
                       first_seen=0, last_seen=0, name=name, hour_wh=[wh / 24] * 24, **kw)


def test_a_name_left_without_its_loads_vanishes_into_the_name_they_wear():
    # 2026-10-04: Kompresor took #63, Inkubator moved to Hiša's #14
    fleet = _fleet(("", 5, "Kompresor"), ("", 63, "Kompresor 2"), ("", 6, "Inkubator"), ("Hiša", 14, None),
                   ("", 7, "Sock Eater"), ("", 8, "Sock Eater"), ("", 9, "Washer", 10), ("", 10, "Spin"),
                   ("", 11, "Dryer"))

    def vanished(change):
        before = fleet.names()
        change()
        return named.vanished_into(before, fleet.names())

    assert vanished(lambda: fleet.rename(("", 63), "Kompresor")) == {"Kompresor": ["Kompresor 2"]}   # joined
    assert vanished(lambda: fleet.rename(("Hiša", 14), "Inkubator")) == {}   # a load more; nothing vanished
    assert vanished(lambda: fleet.rename(("", 6), None)) == {}               # Inkubator lives on Hiša's
    assert vanished(lambda: fleet.rename(("", 8), "Kiln")) == {}             # Sock Eater keeps #7
    assert vanished(lambda: fleet.rename(("", 7), "Kiln")) == {"Kiln": ["Sock Eater"]}   # now its last
    assert vanished(lambda: fleet.rename(("", 11), "Tumble Dryer")) == {"Tumble Dryer": ["Dryer"]}
    assert vanished(lambda: fleet.adopt(("", 10))) == {"Washer": ["Spin"]}   # the heir wore a name of its own
    assert vanished(lambda: fleet.rename(("", 10), None)) == {}              # forgotten: into nothing


def test_an_adoption_brings_the_old_loads_hours():
    det = D.Detector()
    det.signatures += [_sig(9, "Washer", successor_id=10, hourly={NOW - 2 * H: 300.0, NOW - H: 100.0}),
                       _sig(10, hourly={NOW - H: 50.0, NOW: 80.0}, older={NOW - 20 * 86400: 40.0})]
    det.signatures[0].older = {NOW - 30 * 86400: 70.0}         # named since the carry was due
    assert det.adopt(10) == "Washer"
    old, heir = det.signatures
    assert heir.hourly == {NOW - 2 * H: 300.0, NOW - H: 150.0, NOW: 80.0} and old.hourly == {}
    assert heir.older == {NOW - 30 * 86400: 70.0, NOW - 20 * 86400: 40.0} and old.older == {}
    assert det.hourly_by_name("Washer") == heir.hourly


def test_a_vanished_names_history_is_carried_onto_an_existing_meter():
    # Kompresor 2's meter: 0.2, 0.3, 0.1 kWh in the three hours before the window
    covered = NOW - 3 * H
    extra = {NOW - 6 * H: 0.2, NOW - 5 * H: 0.3, NOW - 4 * H: 0.1, NOW - 2 * H: 9.0}   # the window's: not carried
    hours = _hours(1.0, 1.5, 1.5, 2.0, 2.5, 3.0, start=NOW - 7 * H)       # Kompresor's, NOW-7h .. NOW-2h
    fives = [{"start": NOW - 5 * H + i * 300, "state": 1.5, "sum": 1.5} for i in range(12)]
    rows, out, shift = named.plan_carry(extra, covered, hours, fives)
    assert [(r["start"], round(r["sum"], 6)) for r in rows] == [
        (NOW - 6 * H, 1.7), (NOW - 5 * H, 2.0), (NOW - 4 * H, 2.6)]
    assert [r["state"] for r in rows] == [1.5, 1.5, 2.0]               # the readings stay
    assert abs(shift - 0.6) < 1e-9                                     # every row from the window on
    assert abs(out[0]["sum"] - (1.5 + 0.2 + 0.3 / 12)) < 1e-9          # its hour's share, pro rata
    assert abs(out[-1]["sum"] - rows[1]["sum"]) < 1e-9                 # ...meeting the hour's sum


def test_a_vanished_names_history_is_carried_onto_a_new_meter():
    # Dryer renamed Tumble Dryer: the new meter's first hour is in the window
    covered = NOW - 3 * H
    rows, _, shift = named.plan_carry({NOW - 6 * H: 0.2, NOW - 4 * H: 0.1}, covered, _hours(0.0, start=NOW - H), [])
    assert [(r["start"], round(r["state"], 6), round(r["sum"], 6)) for r in rows] == [
        (NOW - 6 * H, 0.2, 0.2), (NOW - 5 * H, 0.2, 0.2), (NOW - 4 * H, 0.3, 0.3)]   # made as a new meter's
    assert abs(shift - 0.3) < 1e-9
    assert named.plan_carry({}, covered, _hours(0.0, start=NOW - H), []) == ([], [], 0.0)
    assert named.plan_carry({NOW - 6 * H: 0.2}, covered, [], []) == ([], [], 0.0)   # no hour of its own yet


def _apply(hours, rows, shift_from, shift):
    """What the recorder holds after importing ``rows`` and adjusting from ``shift_from``."""
    got = {r["start"]: dict(r) for r in hours}
    for r in got.values():
        if r["start"] >= shift_from:
            r["sum"] += shift
    got.update({r["start"]: dict(r) for r in rows})
    return [got[h] for h in sorted(got)]


def test_a_carry_then_the_hourly_check_run_twice_write_the_same():
    covered = NOW - 3 * H
    hours = _hours(0.0, 0.1, start=NOW - 2 * H)                       # a new meter, its first two hours
    rows, _, shift = named.plan_carry({NOW - 5 * H: 0.4}, covered, hours, [])
    hours = _apply(hours, rows, covered, shift)
    hourly = {NOW - 3 * H: 100.0, NOW - H: 100.0}
    rows, _, shift = named.plan_rewrite(hourly, covered, hours, [])
    hours = _apply(hours, rows, rows[-1]["start"] + H, shift)
    assert [(r["start"], round(r["sum"], 6)) for r in hours] == [
        (NOW - 5 * H, 0.4), (NOW - 4 * H, 0.4), (NOW - 3 * H, 0.5), (NOW - 2 * H, 0.5), (NOW - H, 0.6)]
    again, _, shift = named.plan_rewrite(hourly, covered, hours, [])
    assert named.first_change(again, hours) is None and abs(shift) < 1e-9   # nothing to write the hour after


def test_an_hour_ageing_out_is_kept_only_by_a_confident_unnamed_load():
    now = NOW + 30 * 86400
    old, recent = now - 12 * 86400, now - 86400
    for sig, kept in ((_sig(1), True), (_sig(2, name="Kiln"), False), (_sig(3, count=5), False),
                      (_sig(4, wh=400.0), False), (_sig(5, count=12, power_mad=50.0, duration_mad=60.0), False)):
        sig.hourly = {old: 50.0, recent: 20.0}
        sig.age(now)
        assert sig.hourly == {recent: 20.0}
        assert sig.older == ({old: 50.0} if kept else {}), sig.id
    sig = _sig(6, older={now - 366 * 86400: 5.0, now - 364 * 86400: 6.0})
    sig.age(now)
    assert sig.older == {now - 364 * 86400: 6.0}                       # a year, and no more


def test_swallow_and_prune_keep_the_older_hours_right():
    a, b = _sig(1, older={NOW - 20 * 86400: 10.0}), _sig(2, older={NOW - 20 * 86400: 5.0, NOW - 15 * 86400: 1.0})
    a.swallow(b)
    assert a.older == {NOW - 20 * 86400: 15.0, NOW - 15 * 86400: 1.0}
    fleet = D.Fleet()
    fleet.main.signatures += [_sig(1, "Kiln"), _sig(2, count=1, older={NOW - 20 * 86400: 10.0})]
    stored = fleet.older_to_dict()
    assert stored == {"": {"2": {str(NOW - 20 * 86400): 10.0}}}
    cap, D.MAX_SIGNATURES = D.MAX_SIGNATURES, 1
    try:
        fleet.main._prune(NOW)
    finally:
        D.MAX_SIGNATURES = cap
    assert [s.id for s in fleet.main.signatures] == [1] and fleet.older_to_dict() == {}   # gone with it
    fleet.load_older(stored)                                           # a store from before: nothing to attach to
    assert fleet.main.signatures[0].older == {}
    fleet.main._moved[2] = 1                                           # ...or merged into another since
    fleet.load_older(stored)
    assert fleet.main.signatures[0].older == {NOW - 20 * 86400: 10.0}


def test_naming_a_load_carries_its_older_hours_once():
    fleet = _fleet(("Hiša", 14, None))
    fleet.subs["Hiša"].signatures[0].older = {NOW - 20 * 86400: 300.0, NOW - 20 * 86400 + H: 200.0}
    assert fleet.take_older("Heater") == {}
    fleet.rename(("Hiša", 14), "Heater")
    older = fleet.take_older("Heater")
    assert older == {NOW - 20 * 86400: 300.0, NOW - 20 * 86400 + H: 200.0}
    assert fleet.take_older("Heater") == {} and fleet.older_to_dict() == {}   # cleared: carried once
    rows, _, shift = named.plan_carry({h: wh / 1000.0 for h, wh in older.items()}, NOW - 3 * H,
                                      _hours(0.0, start=NOW - H), [])
    assert (rows[0]["sum"], rows[-1]["sum"], abs(shift - 0.5) < 1e-9) == (0.3, 0.5, True)


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
