"""Auto-naming (INS-21): the bar on real signatures, hysteresis, markers and tombstones, numbering, the dashboard list.

The Facts below are real signatures from the live exports of 2026-10-20 (Home,
Kozolec, Andrej's site) and the ten-day replays; `tests/autoname_bench.py` runs
the whole table.
"""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

A = load("insights.autoname")
C = load("insights.classify")
BASE = Path(__file__).resolve().parents[1] / "custom_components" / "load_insights"

FLAT = [100.0] * 24
NIGHT = [188, 149, 169, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 24, 113, 160, 132]
WEEK = [171, 124, 86, 96, 124, 151, 184]


def fridge(**kw):
    """Mansarda's fridge at Home (replay #3)."""
    base = dict(ref=("Mansarda", 3), phases="c", watts=66.0, duration_s=1163.0, interval_s=2473.0, count=201,
                span_days=10.7, evidence=0.85, hour_wh=FLAT, day_wh=WEEK, level_count=1.1, interval_mad=1120.0)
    base.update(kw)
    return A.Facts(**base)


def heater(**kw):
    """Home's 1.7 kW, 10-run piece of Le Hottie (export #3961, hand-named)."""
    base = dict(ref=("", 3961), phases="a", watts=1724.0, duration_s=20.0, interval_s=167108.0, count=10,
                span_days=6.5, evidence=0.89, hour_wh=[0, 0, 0, 0, 0, 0, 0, 0, 0, 51, 20, 0, 0, 0, 0, 0, 0, 0, 0, 0, 43, 0, 0, 0],
                day_wh=[51, 0, 0, 0, 0, 0, 43], guess_kind=C.HEATER, guess_confidence=0.85)
    base.update(kw)
    return A.Facts(**base)


# ---------------------------------------------------------------- the recogniser, calibrated cases
def test_the_fridge_at_home_clears():
    assert A.recognise(fridge()).kind == A.FRIDGE_KIND
    # ...and the freezer beside it (replay #4: 47 W, 22 min every 90 min, evidence 0.71)
    assert A.recognise(fridge(ref=("Mansarda", 4), watts=47.0, duration_s=1351.0, interval_s=5389.0,
                              count=153, evidence=0.71, interval_mad=1018.0)).kind == A.FRIDGE_KIND


def test_andrejs_fridge_clears_though_it_runs_only_at_night():
    f = fridge(ref=("", 72), watts=72.0, duration_s=450.0, interval_s=1237.0, count=106, span_days=9.2, evidence=0.81,
               hour_wh=NIGHT, level_count=None, interval_mad=None)
    assert A.recognise(f).kind == A.FRIDGE_KIND


def test_kozolecs_fridges_split_over_many_signatures_do_not():
    # replay #33: 56 W, 43 min, evidence 0.5, an interval of 3 h with a 2 h jitter - one fragment of two fridges
    f = fridge(ref=("", 33), watts=56.0, duration_s=2564.0, interval_s=11332.0, count=81, span_days=15.3,
               evidence=0.5, interval_mad=6911.0)
    v = A.recognise(f)
    assert v.kind is None and "evidence" in v.failed


def test_a_pump_a_charger_and_the_hidrofor_are_not_fridges():
    assert A.recognise(fridge(watts=902.0, duration_s=127.0, interval_s=1600.0)).kind is None      # Home's Hidrofor
    assert A.recognise(fridge(phases="abc")).kind is None
    assert A.recognise(fridge(watts=2855.0)).kind is None
    assert A.recognise(fridge(duration_s=139.0, interval_s=488.0, watts=79.0, evidence=0.5)).kind is None   # Home's 79 W noise


def test_a_fridge_needs_many_runs_a_day_and_hours():
    assert A.recognise(fridge(count=20)).kind is None
    assert A.recognise(fridge(count=60, span_days=30.0)).kind is None                              # two cycles a day
    assert A.recognise(fridge(hour_wh=[0] * 20 + [400, 0, 0, 0])).kind is None                     # one burst


def test_the_heater_bar_takes_le_hotties_best_piece_and_leaves_the_rest():
    assert A.recognise(heater()).kind == A.HEATER_KIND
    v = A.recognise(heater(ref=("", 4578), count=5, span_days=2.8, evidence=0.96))
    assert v.kind is None and "runs" in v.failed and "days seen" in v.failed
    assert A.recognise(heater(ref=("", 9038), count=8, span_days=5.7, evidence=0.53)).kind is None
    # a heater the classifier cannot call (power factor untrusted) is not named: Kozolec's Boiler, hand-named
    assert A.recognise(heater(guess_kind=None, guess_confidence=0.0, watts=1268.0, count=1353, evidence=0.5)).kind is None


def test_a_kettle_or_an_oven_is_not_called_a_heater():
    assert A.recognise(heater(appliance=C.COOKING)).kind is None
    assert A.recognise(heater(watts=300.0)).kind is None


def test_a_metered_device_is_never_touched():
    assert A.recognise(heater(metered=True)).kind is None


# ---------------------------------------------------------------- hysteresis
def run_days(days, facts_by_day, cands=None):
    cands = cands or {}
    for day in days:
        cands = A.tick(cands, {"k": facts_by_day(day)}, day)
    return cands


def test_a_load_must_clear_on_enough_distinct_days():
    one_day = run_days(["2026-10-20"] * 5, lambda d: fridge())
    assert len(one_day["k"].days) == 1 and not A.ready(one_day["k"])
    three = run_days(["2026-10-20", "2026-10-21", "2026-10-22"], lambda d: fridge())
    assert A.ready(three["k"])


def test_a_name_is_not_revoked_by_one_bad_day_but_by_a_week():
    cands = run_days(["2026-10-20", "2026-10-21", "2026-10-22"], lambda d: fridge())
    cands = run_days(["2026-10-23"], lambda d: fridge(evidence=0.3), cands)
    assert not A.lapsed(cands["k"]) and A.ready(cands["k"])
    cands = run_days([f"2026-10-{24 + i}" for i in range(6)], lambda d: fridge(evidence=0.3), cands)
    assert A.lapsed(cands["k"])
    # a clear day ends the streak
    cands = run_days(["2026-10-30"], lambda d: fridge(), cands)
    assert not A.lapsed(cands["k"])


def test_a_load_that_never_clears_is_never_a_candidate():
    assert run_days(["2026-10-20"], lambda d: fridge(count=3)) == {}


# ---------------------------------------------------------------- names, markers, tombstones
PATTERNS = {"fridge": "Fridge {n}", "heater": "Heater {n}"}
SL = {"fridge": "Hladilnik {n}", "heater": "Grelnik {n}"}


def ready_cands(facts):
    out = {}
    for key, f in facts.items():
        out[key] = A.Candidate(A.recognise(f).kind, days=["2026-10-20", "2026-10-21", "2026-10-22"],
                               descriptor=A.descriptor(f))
    return out


def test_numbering_is_per_kind_and_never_takes_a_name_that_is_someones():
    assert A.next_name("Fridge {n}", []) == "Fridge 1"
    assert A.next_name("Fridge {n}", ["fridge 1 ", "Heater 1"]) == "Fridge 2"
    assert A.next_name("Hladilnik {n}", ["Hladilnik 1", "Hladilnik 2"]) == "Hladilnik 3"


def test_two_loads_of_different_size_get_two_numbers_and_look_alikes_share_one():
    f1 = fridge(ref=("", 1))
    f2 = fridge(ref=("", 2), watts=120.0)                 # another size: another fridge
    f3 = fridge(ref=("", 3), watts=62.0)                  # the first, split by the detector
    facts = {A.key_of(f.ref): f for f in (f1, f2, f3)}
    got = A.decide(facts, ready_cands(facts), {}, [], PATTERNS, [])
    assert got == [("#1", "Fridge 1", "fridge"), ("#2", "Fridge 2", "fridge"), ("#3", "Fridge 1", "fridge")]
    heat = {"#9": heater(ref=("", 9))}
    got = A.decide(heat, ready_cands(heat), {"Fridge 1": {"kind": "fridge", "descriptor": {}}}, [], SL, ["Fridge 1"])
    assert got == [("#9", "Grelnik 1", "heater")]


def test_an_existing_name_is_never_overwritten_or_reused():
    f = fridge(ref=("", 1), named="Kitchen fridge")
    assert A.decide({"#1": f}, ready_cands({"#1": f}), {}, [], PATTERNS, []) == []
    g = fridge(ref=("", 2))
    assert A.decide({"#2": g}, ready_cands({"#2": g}), {}, [], PATTERNS, ["Fridge 1"]) == [("#2", "Fridge 2", "fridge")]


def test_a_metered_signature_is_never_named():
    f = fridge(metered=True)
    assert A.decide({"#1": f}, ready_cands({"#1": f}), {}, [], PATTERNS, []) == []


def test_what_the_owner_removed_or_renamed_is_never_made_again():
    marks = {"Fridge 1": {"kind": "fridge", "descriptor": A.descriptor(fridge())},
             "Heater 1": {"kind": "heater", "descriptor": A.descriptor(heater())}}
    kept, tombs = A.reconcile(marks, ["Heater 1", "Kitchen fridge"])     # Fridge 1 was renamed away
    assert list(kept) == ["Heater 1"] and [t.name for t in tombs] == ["Fridge 1"]
    again = {"#1": fridge(ref=("", 7), watts=70.0)}                       # the same fridge, a new signature
    assert A.decide(again, ready_cands(again), kept, tombs, PATTERNS, []) == []
    other = {"#2": fridge(ref=("", 8), watts=250.0)}                      # another size is not buried
    assert [n for _, n, _ in A.decide(other, ready_cands(other), kept, tombs, PATTERNS, [])] == ["Fridge 1"]
    # the case and the spaces round a name do not make it another name
    kept2, tombs2 = A.reconcile(marks, ["fridge 1", " Heater 1"])
    assert not tombs2 and len(kept2) == 2


def test_a_load_the_owner_already_named_claims_its_split_pieces():
    mine = {"#72": fridge(ref=("", 72), watts=72.0)}
    owned = [("Hladilnik A 70 W", A.descriptor(fridge(ref=("", 8), watts=83.0)))]
    assert A.decide(mine, ready_cands(mine), {}, [], PATTERNS, [], owned, lambda n: "hladilnik" in n.lower()) == []
    assert A.decide(mine, ready_cands(mine), {}, [], PATTERNS, [], owned, lambda n: False) != []     # not fridge-worded
    # a heater is claimed by any owner-named load of its phases and size
    h = {"#3": heater()}
    assert A.decide(h, ready_cands(h), {}, [], PATTERNS, [], [("Le Hottie", A.descriptor(heater(watts=1700.0)))]) == []


def test_facts_of_reads_a_signature():
    from types import SimpleNamespace as NS
    g = C.Guess(C.HEATER, 0.85, appliance=C.WATER_TANK)
    sig = NS(phases="a", watts=1800.0, duration_s=120.0, interval_s=None, count=12, first_seen=0.0, last_seen=86400.0 * 6,
             evidence=0.8, hour_wh=[1] * 24, day_wh=[1] * 7, level_count=1.0, interval_mad=None, name=None, guess=lambda: g)
    f = A.facts_of(("", 5), sig)
    assert f.span_days == 6.0 and f.guess_kind == C.HEATER and f.appliance == C.WATER_TANK and f.ref == ("", 5)
    assert A.recognise(f).kind == A.HEATER_KIND


# ---------------------------------------------------------------- the Energy dashboard
class FakeManager:
    def __init__(self, devices):
        self.data = None if devices is None else {"device_consumption": devices, "energy_sources": []}
        self.writes = []

    async def async_update(self, update):
        self.writes.append(update)
        self.data = {**self.data, **update}


OWNER = {"stat_consumption": "sensor.kiln_energy", "name": "Kiln"}


def test_an_auto_named_load_is_added_once_and_the_owners_entries_stay():
    m = FakeManager([dict(OWNER)])
    ok, added, declined = asyncio.run(A.write_dashboard(m, {"sensor.fridge_1_energy": "Fridge 1"}, [], []))
    assert ok and added == ["sensor.fridge_1_energy"] and declined == []
    assert m.data["device_consumption"] == [OWNER, {"stat_consumption": "sensor.fridge_1_energy", "name": "Fridge 1"}]
    ok, added2, _ = asyncio.run(A.write_dashboard(m, {"sensor.fridge_1_energy": "Fridge 1"}, added, []))
    assert not ok and len(m.writes) == 1 and added2 == added                       # no duplicate, no second write


def test_an_entry_the_owner_made_is_never_taken_off_or_doubled():
    m = FakeManager([dict(OWNER)])
    ok, added, _ = asyncio.run(A.write_dashboard(m, {"sensor.kiln_energy": "Heater 1"}, [], []))
    assert not ok and added == [] and m.data["device_consumption"] == [OWNER]
    ok, added, _ = asyncio.run(A.write_dashboard(m, {}, [], []))                     # nothing wanted, nothing ours
    assert not ok and m.data["device_consumption"] == [OWNER]


def test_only_our_entries_go_when_the_name_goes_or_the_switch_is_off():
    m = FakeManager([dict(OWNER), {"stat_consumption": "sensor.fridge_1_energy", "name": "Fridge 1"}])
    ok, added, declined = asyncio.run(A.write_dashboard(m, {}, ["sensor.fridge_1_energy"], []))
    assert ok and added == [] and m.data["device_consumption"] == [OWNER]


def test_an_entry_the_owner_took_out_is_not_put_back():
    m = FakeManager([dict(OWNER)])                                                    # ours is not listed any more
    ok, added, declined = asyncio.run(A.write_dashboard(m, {"sensor.fridge_1_energy": "Fridge 1"},
                                                        ["sensor.fridge_1_energy"], []))
    assert not ok and added == [] and declined == ["sensor.fridge_1_energy"] and not m.writes
    ok, added, declined = asyncio.run(A.write_dashboard(m, {"sensor.fridge_1_energy": "Fridge 1"}, added, declined))
    assert not ok and not m.writes and declined == ["sensor.fridge_1_energy"]


def test_a_dashboard_never_set_up_is_left_alone():
    m = FakeManager(None)
    ok, added, _ = asyncio.run(A.write_dashboard(m, {"sensor.fridge_1_energy": "Fridge 1"}, [], []))
    assert not ok and not m.writes


# ---------------------------------------------------------------- the options and the words
def test_a_hand_named_load_goes_on_the_dashboard_with_its_power_and_a_rename_moves_it():
    wanted = {"sensor.pralni_stroj_energy": "Pralni Stroj?"}
    rates = {"sensor.pralni_stroj_energy": "sensor.pralni_stroj_power"}
    new, added, _ = A.plan_dashboard([], wanted, [], [], rates)
    assert new == [{"stat_consumption": "sensor.pralni_stroj_energy", "name": "Pralni Stroj?",
                    "stat_rate": "sensor.pralni_stroj_power"}] and added == ["sensor.pralni_stroj_energy"]
    # renamed: the old entry (ours) goes, the new one comes
    renamed = {"sensor.washer_energy": "Washer"}
    new2, added2, _ = A.plan_dashboard(new, renamed, added, [], {"sensor.washer_energy": "sensor.washer_power"})
    assert [d["stat_consumption"] for d in new2] == ["sensor.washer_energy"] and added2 == ["sensor.washer_energy"]
    # an entry the owner made (not in added) is never touched, rate or not
    mine = [{"stat_consumption": "sensor.plug_energy", "stat_rate": "sensor.plug_power"}]
    assert A.plan_dashboard(mine, {}, [], [], {})[0] is None


def test_a_meters_base_load_sits_under_its_meter_and_goes_with_the_option():
    meter = [{"stat_consumption": "sensor.hisa_energy", "stat_rate": "sensor.hisa_power", "name": "Hiša"}]
    wanted = {"sensor.hisa_base_load_energy": "Hiša base load"}
    new, added, _ = A.plan_dashboard(meter, wanted, [], [], {"sensor.hisa_base_load_energy": "sensor.hisa_base_load_power"},
                                     {"sensor.hisa_base_load_energy": "sensor.hisa_energy"})
    assert new[1] == {"stat_consumption": "sensor.hisa_base_load_energy", "name": "Hiša base load",
                      "stat_rate": "sensor.hisa_base_load_power", "included_in_stat": "sensor.hisa_energy"}
    # the option off: nothing wanted, our entry goes, the meter's stays
    gone, added2, _ = A.plan_dashboard(new, {}, added, [], {}, {})
    assert gone == meter and added2 == []


def test_both_switches_are_options_on_by_default_and_translated():
    const = (BASE / "const.py").read_text(encoding="utf-8")
    flow = (BASE / "config_flow.py").read_text(encoding="utf-8")
    named = (BASE / "autonaming.py").read_text(encoding="utf-8")
    assert 'CONF_AUTONAME = "autoname"' in const and 'CONF_AUTONAME_DASHBOARD = "autoname_dashboard"' in const
    # default on when never saved, in the page and in the runner; a False is stored, not dropped as empty
    assert "current.get(key, True)" in flow and "bool(user_input.get(key, True))" in flow
    assert "get(CONF_AUTONAME, True)" in named and "get(CONF_AUTONAME_DASHBOARD, True)" in named
    # the dashboard switch covers every named load and does not depend on auto-naming (2026-10-25)
    assert "self.enabled and" not in named.split("def dashboard")[1].split("# ----")[0]
    assert "for name in sorted(r.fleet.names())" in named and "sorted(self.marks)" not in named
    for lang in ("en", "sl"):
        t = json.loads((BASE / "translations" / f"{lang}.json").read_text(encoding="utf-8"))
        step = t["options"]["step"]["detection"]
        words = step["sections"]["naming"]
        for key in ("autoname", "autoname_dashboard"):
            assert words["data"][key] and words["data_description"][key]
        for kind in ("fridge", "heater"):
            assert "{n}" in t["exceptions"][f"autoname_{kind}"]["message"]
    sl = json.loads((BASE / "translations" / "sl.json").read_text(encoding="utf-8"))
    assert sl["exceptions"]["autoname_fridge"]["message"] == "Hladilnik {n}"
    assert sl["exceptions"]["autoname_heater"]["message"] == "Grelnik {n}"


def test_a_base_load_is_its_meters_less_the_meters_inside_it():
    """The owner, 2026-10-31: "the submeters should always be subtracted" - a
    meter's base load counted the always-on draw of the meters inside it a
    second time (Delavnica's held the Server UPS's ~95 W, Mansarda's the
    EcoFlow). Home's Delavnica, grid + PV - Hiša - Mansarda - the EVBox, is not
    read; its base load is the grid's less those three's and less the meters
    inside it (Klima). Two ignored top-level meters: no remainder at all."""
    import ast
    import types
    from typing import List, Optional, Tuple
    src = (BASE / "detection.py").read_text(encoding="utf-8")
    cls = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef) and n.name == "DetectionRunner")
    ns = {"List": List, "Optional": Optional, "Tuple": Tuple}
    for fn in cls.body:
        if isinstance(fn, ast.FunctionDef) and fn.name in ("remainder_meter", "remainder_base_load", "_inside", "base_load",
                                                           "_now_w", "_inner_now", "base_load_cap"):
            exec(compile(ast.Module([fn], []), "detection.py", "exec"), ns)
    bases = {"": (900.0, 9000.0), "Hiša": (300.0, 3000.0), "Mansarda": (150.0, 1500.0),
             "Polnilnica": (5.0, 50.0), "Klima": (20.0, 200.0), "Skrinja": (165.0, 1650.0)}
    fleet = types.SimpleNamespace(base_load=lambda name, phase=None: bases[name], subs={}, phase_map=lambda n: {})
    subs = {"Hiša": {"energy": "h"}, "Mansarda": {"energy": "m"}, "Polnilnica": {"energy": "p"},
            "Klima": {"energy": "k", "parent": "Delavnica"}, "Skrinja": {"energy": "s", "parent": "Hiša"}}
    every = {**subs, "Delavnica": {"energy": "d", "ignored": True}}
    r = types.SimpleNamespace(all_submeters=every, submeters=subs, fleet=fleet)
    for name in ("remainder_meter", "_inside"):
        setattr(r, name, types.MethodType(ns[name], r))
    assert r.remainder_meter() == ("Delavnica", "d", ["Hiša", "Mansarda", "Polnilnica"])
    assert ns["remainder_base_load"](r) == (425.0, 4250.0)            # 900 - 300 - 150 - 5 - Klima's 20
    assert ns["base_load"](r, "Hiša") == (135.0, 1350.0)              # Hiša less the freezers inside it
    assert ns["base_load"](r, "Mansarda") == (150.0, 1500.0)          # nothing metered inside it
    bases["Skrinja"] = (400.0, 4000.0)
    assert ns["base_load"](r, "Hiša")[0] == 0.0                       # never below 0
    # ...and never above what the meter draws now less what the meters inside it draw now (the owner,
    # 2026-10-31): Hiša reading 400 W with its freezers at 330 W leaves 70 W at most
    det = lambda **lv: types.SimpleNamespace(phases={p: types.SimpleNamespace(level=w) for p, w in lv.items()})
    fleet.main = det(a=600.0, b=500.0, c=400.0)
    fleet.subs = {"Hiša": det(a=150.0, c=250.0), "Mansarda": det(a=200.0), "Polnilnica": det(a=0.0),
                  "Klima": det(a=30.0), "Skrinja": det(a=330.0)}
    for name in ("_now_w", "_inner_now"):
        setattr(r, name, types.MethodType(ns[name], r))
    assert ns["base_load_cap"](r, "Hiša") == 70.0
    assert ns["base_load_cap"](r, "Mansarda") == 200.0
    assert ns["base_load_cap"](r, "", None, True) == 1500.0 - 400.0 - 200.0 - 0.0 - 30.0   # the remainder's
    fleet.subs["Skrinja"] = det(a=500.0)
    assert ns["base_load_cap"](r, "Hiša") == 0.0                       # never below 0
    every["Garage"] = {"energy": "g", "ignored": True}
    assert r.remainder_meter() is None and ns["remainder_base_load"](r) == (None, 0.0)

def test_the_runner_asks_the_namer_only_after_history_is_read():
    named = (BASE / "autonaming.py").read_text(encoding="utf-8")
    assert re.search(r"if not r\.enabled or r\.refiling or not r\.caught_up:\s+return", named)


if __name__ == "__main__":
    run_main(globals())
