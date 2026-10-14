"""Cross-meter edges (insights/edges.py) on made-up meters: the assembly,
the slicing, the properties and the scorecard's arithmetic.

    python3 tests/test_edges.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

E = load("insights.edges")
D = load("insights.detect")


def step(meter, ch, t, dp, phase=None, dq=None, span=2.0, noise=10.0, event=None, gain=1.0):
    return E.Step(meter=meter, ch=ch, t=t, dp=dp, dq=dq, a=t - span, b=t + 1.0,
                  phase=phase if phase is not None or meter else ch, window=5.0, interval=2.0,
                  noise=noise, floor=noise, event=event, gain=gain)


def flat(level=100.0, frm=0.0, to=1000.0, every=2.0):
    return [(frm + k * every, level) for k in range(int((to - frm) / every))]


def assemble(steps, parents, chans, rows=None):
    a = E.Assembler(parents)
    rows = rows or {(m, c): flat() for m, cs in chans.items() for c in cs}
    a.feed(steps, rows=rows, chans=chans, noise={k: 10.0 for k in rows}, latency={m: 6.0 for m in chans})
    a.flush()
    return a


def by_place(a):
    return {e["place"]: e for e in a.edges}


def test_nested_meters_are_one_edge_placed_innermost():
    """The office plug inside Mansarda inside the grid, each stepping +100 W
    together: one edge, at the plug, its entries on all three; both pairs add up."""
    chans = {"": {"a": "a", "b": "b", "c": "c"}, "Mansarda": {"x": "a", "y": "b"}, "NASA": {"a": "a"}}
    a = assemble([step("", "a", 100.0, 100.0, noise=5.0), step("Mansarda", "x", 101.0, 98.0, "a", noise=5.0),
                  step("NASA", "a", 101.5, 97.0, "a", noise=5.0)], {"NASA": "Mansarda"}, chans)
    assert len(a.edges) == 1
    e = a.edges[0]
    assert (e["place"], e["dir"], e["pattern"]) == ("NASA", 1, "a")
    assert e["entries"]["|a"]["dp"] == 100.0 and e["entries"]["Mansarda|x"]["dp"] == 98.0
    assert "Mansarda|y" not in e["entries"] and "|b" not in e["entries"]   # other phases: not this edge's
    assert not e["shared"] and not e["blind"]
    assert E.conservation(a.cons) == {">Mansarda": [1, 1, 0, 0, 0, 0, 0], "Mansarda>NASA": [1, 1, 0, 0, 0, 0, 0]}


def test_separate_branches_are_two_edges_the_parent_shared():
    """The kettle under Mansarda (+1918) and the hidrofor's plug (-810) in the
    grid's one +1108 reading: two edges, the grid's step kept on both, shared;
    the children add up to it."""
    chans = {"": {"a": "a"}, "Mansarda": {"x": "a"}, "Hidrofor": {"a": "a"}}
    a = assemble([step("", "a", 100.0, 1108.0), step("Mansarda", "x", 100.5, 1918.0, "a"),
                  step("Hidrofor", "a", 101.0, -810.0, "a")], {}, chans)
    got = by_place(a)
    assert set(got) == {"Mansarda", "Hidrofor"}
    k, p = got["Mansarda"], got["Hidrofor"]
    assert (k["dir"], p["dir"]) == (1, -1)
    assert k["entries"]["|a"]["dp"] == p["entries"]["|a"]["dp"] == 1108.0
    assert k["shared"] == {"|a": [p["id"]]} and p["shared"] == {"|a": [k["id"]]}
    assert "Hidrofor|a" not in k["entries"] and "Mansarda|x" not in p["entries"]   # another branch
    assert [c["ok"] for c in a.cons] == [True]


def test_a_rest_no_meter_reads_is_an_edge_of_its_own():
    """A 3 kW load no meter reads, in the same reading as the office's +15 W:
    the grid's step is the office's entry and an edge of its own, shared."""
    chans = {"": {"a": "a"}, "NASA": {"a": "a"}}
    a = assemble([step("", "a", 100.0, 3015.0), step("NASA", "a", 100.5, 15.0, "a")], {}, chans)
    got = by_place(a)
    assert set(got) == {"", "NASA"} and got[""]["shared"] == {"|a": [got["NASA"]["id"]]}
    assert [c["ok"] for c in a.cons] == [False]


def test_a_child_reading_high_adds_up_once_its_gain_is_known():
    """Kozolec 10-04 14:57:44: the boiler's plug +1938 W, the Victron +1825
    (the plug reads ~5 % high; tolerance 105). Raw, they fail to add up and
    the grid's step is a rest of its own; in the grid's terms (gain
    1825/1938) they add up, one edge at the plug - its entries still each
    meter's raw step."""
    chans = {"": {"a": "a"}, "Boiler": {"a": "a"}}

    def run(gain):
        return assemble([step("", "a", 100.0, 1825.0), step("Boiler", "a", 100.5, 1938.0, "a", gain=gain)], {}, chans)
    raw = run(1.0)
    assert [c["ok"] for c in raw.cons] == [False] and set(by_place(raw)) == {"", "Boiler"}
    a = run(1825.0 / 1938.0)
    assert [(round(c["p"]), round(c["c"]), c["ok"]) for c in a.cons] == [(1825, 1825, True)]
    assert [e["place"] for e in a.edges] == ["Boiler"]
    e = a.edges[0]
    assert e["entries"]["Boiler|a"]["dp"] == 1938.0 and e["entries"]["|a"]["dp"] == 1825.0 and e["size"] == 1938.0


def test_a_step_only_a_sub_meter_saw_is_an_edge():
    """NASA's 12 W under Mansarda, below the grid's noise: an edge at NASA,
    Mansarda and the grid read 0 (and 12 W is no clear step for them)."""
    chans = {"": {"a": "a"}, "Mansarda": {"x": "a"}, "NASA": {"a": "a"}}
    a = assemble([step("NASA", "a", 200.0, 12.0, "a")], {"NASA": "Mansarda"}, chans)
    e = a.edges[0]
    assert e["place"] == "NASA" and e["entries"]["Mansarda|x"] == 0.0 and e["entries"]["|a"] == 0.0
    assert e["blind"] == []
    # 12 W is under the two meters' idle noises added (10 + 10 W): counted apart, not judged
    assert sorted(E.conservation(a.cons).items()) == [(">NASA", [0, 0, 0, 0, 0, 0, 1]), ("Mansarda>NASA", [0, 0, 0, 0, 0, 0, 1])]
    # 25 W clears them with no factor on top: judged (a miss), and blind above
    a = assemble([step("NASA", "a", 200.0, 25.0, "a")], {"NASA": "Mansarda"}, chans)
    assert sorted(a.edges[0]["blind"]) == ["", "Mansarda"]
    assert sorted(E.conservation(a.cons).items()) == [(">NASA", [1, 0, 1, 0, 0, 0, 0]), ("Mansarda>NASA", [1, 0, 1, 0, 0, 0, 0])]


def test_zero_against_unknown():
    """A meter that has read through the moment and declared nothing is 0; one
    that had not read yet is unknown; a clear step the meter above read
    through and showed nothing of is blind there."""
    chans = {"": {"a": "a"}, "Plug": {"a": "a"}, "Late": {"a": None}}
    rows = {("", "a"): flat(), ("Plug", "a"): flat(), ("Late", "a"): flat(frm=300.0)}
    a = assemble([step("", "a", 200.0, 500.0)], {}, chans, rows)
    e = a.edges[0]
    assert e["entries"]["Plug|a"] == 0.0 and e["entries"]["Late|a"] is None
    a = assemble([step("Plug", "a", 200.0, 500.0, "a")], {}, chans, rows)
    assert a.edges[0]["blind"] == [""] and a.edges[0]["entries"]["|a"] == 0.0


def test_a_level_read_once_is_measured_to_where_the_meter_settles():
    """Kozolec's boiler plug, 10-04 13:54:20: the switch-on read 1902 W, the
    plug's silence confirmed it, and its next report 40 s on read 1875 - the
    element warming - which its detector declared as a -27 W step of its own.
    A level the meter read once and left by its next reading, back toward
    where it came from by no more than half the step, is no level: the step is measured to
    where the meter settled, the first reading its surge. A level read twice,
    or a return of more than half the step (a spike), stays as declared."""
    chans = {"Boiler": {"a": "a"}}

    def run(readings, steps):
        rows = {("Boiler", "a"): flat(0.0, to=100.0) + readings}
        return assemble([step("Boiler", "a", t, dp, "a") for t, dp in steps], {}, chans, rows)
    on = [(100.0, 1902.0), (140.0, 1875.0)] + [(140.0 + 60.0 * k, 1876.0) for k in range(1, 15)]
    a = run(on, [(100.0, 1902.0), (140.0, -27.0)])
    assert [(e["dir"], e["entries"]["Boiler|a"]["dp"]) for e in a.edges] == [(1, 1875.0)]
    assert abs(a.edges[0]["surge"] - 1902.0 / 1875.0) < 1e-9
    twice = [(100.0, 1902.0), (110.0, 1902.0)] + on[1:]
    assert [e["dir"] for e in run(twice, [(100.0, 1902.0), (140.0, -27.0)]).edges] == [1, -1]
    for back in (12.0, 940.0):        # a spike back, or a step back past half: two steps
        spike = [(100.0, 1902.0), (140.0, back)] + [(140.0 + 60.0 * k, back) for k in range(1, 15)]
        assert [e["dir"] for e in run(spike, [(100.0, 1902.0), (140.0, back - 1902.0)]).edges] == [1, -1]
    # a settle is judged on the steps as declared, and a step that is a
    # settle absorbs none: Vtičnice 10-05 20:14, +35.2, -32.2 (no settle of
    # it), +15.3 (the -32.2's) - once the -32.2 had absorbed the +15.3 it
    # passed as the +35.2's settle in one slicing and not in another
    office = [(100.0, 508.0), (130.0, 475.8), (153.0, 488.1)] + [(153.0 + 10.0 * k, 488.0) for k in range(1, 30)]
    rows = {("Boiler", "a"): flat(487.6, to=100.0) + office}
    steps = [step("Boiler", "a", t, dp, "a") for t, dp in ((100.0, 35.2), (130.0, -32.2), (153.0, 15.3))]
    for every in (None, 20.0):
        a = E.Assembler({})
        if every is None:
            a.feed(steps, rows=rows, chans=chans, noise={("Boiler", "a"): 10.0}, latency={"Boiler": 6.0})
        else:
            t = 0.0
            while t < 600.0:
                e = t + every
                a.feed([s for s in steps if t <= s.b + 1.0 < e], rows={k: [r for r in rr if t <= r[0] < e] for k, rr in rows.items()},
                       frontier={"Boiler": e}, reach={"Boiler": 11.0}, now=e, chans=chans,
                       noise={("Boiler", "a"): 10.0}, latency={"Boiler": 6.0})
                t = e
        a.flush()
        assert [round(e["entries"]["Boiler|a"]["dp"], 1) for e in a.edges] == [35.2, -16.9], (every, a.edges)
    # a small step and a large one back: not its settle
    small = [(100.0, 30.0), (140.0, -1000.0)] + [(140.0 + 60.0 * k, -1000.0) for k in range(1, 15)]
    assert [e["dir"] for e in run(small, [(100.0, 30.0), (140.0, -1030.0)]).edges] == [1, -1]


def test_a_three_phase_charger_read_as_one_total_takes_all_three_legs():
    """A meter whose one channel is not mapped may be on any phase, one mapped
    to all three is on all three: the charger's 11 kW total takes the grid's
    three-leg event whole."""
    for mapped in (None, "abc"):                  # not mapped yet, or mapped to all three
        chans = {"": {"a": "a", "b": "b", "c": "c"}, "Charger": {"a": mapped}}
        legs = [step("", p, 100.0 + k, 3700.0, event=7) for k, p in enumerate("abc")]
        a = assemble(legs + [step("Charger", "a", 102.0, 11050.0, mapped)], {}, chans)
        assert [e["place"] for e in a.edges] == ["Charger"]
        assert {k for k, v in a.edges[0]["entries"].items() if isinstance(v, dict)} == {"Charger|a", "|a", "|b", "|c"}
        assert a.cons[0]["ok"]


def test_a_two_phase_step_takes_each_phase_above_on_its_own():
    """The kiln's A+C rise, one event on Hiša's 3EM, the grid's two legs two
    events of its own (one leg's reading late): each leg takes the grid's
    leg on its phase, and it all adds up at Hiša."""
    chans = {"": {"a": "a", "b": "b", "c": "c"}, "Hiša": {"a": "a", "b": "b", "c": "c"}}
    a = assemble([step("", "a", 100.0, 3400.0, event=1), step("", "c", 104.0, 3390.0, event=2),
                  step("Hiša", "a", 100.5, 3380.0, "a", event=3), step("Hiša", "c", 101.0, 3385.0, "c", event=3)],
                 {}, chans)
    assert [(e["place"], e["pattern"]) for e in a.edges] == [("Hiša", "ac")]
    assert {k for k, v in a.edges[0]["entries"].items() if isinstance(v, dict)} == {"Hiša|a", "Hiša|c", "|a", "|c"}
    assert [c["ok"] for c in a.cons] == [True, True]


def test_falls_on_several_phases_are_one_event():
    """Falls within the meter's event window, balanced, are one edge with the
    phase pattern - as the detector forms rises."""
    chans = {"": {"a": "a", "b": "b", "c": "c"}}
    a = assemble([step("", "a", 100.0, -800.0), step("", "c", 102.0, -790.0), step("", "b", 103.0, -20.0)], {}, chans)
    assert sorted((e["pattern"], round(e["size"])) for e in a.edges) == [("ac", 1590), ("b", 20)]


def test_slicing_never_changes_an_edge():
    """The same steps fed in one call, and pass by pass as each is declared
    (a second after its span), every meter's frontier the pass's end: the
    same edges."""
    chans = {"": {"a": "a", "b": "b"}, "Hisa": {"a": "a", "b": "b"}, "Blaz": {"a": "b"}, "Pump": {"a": "a"}}
    parents = {"Blaz": "Hisa"}
    steps = []
    for k in range(40):
        t = 50.0 + 23.0 * k
        steps.append(step("", "a" if k % 2 else "b", t, 300.0 + 10 * k))
        if k % 3 == 0:
            steps.append(step("Hisa", "a" if k % 2 else "b", t + 0.7, 295.0 + 10 * k, "a" if k % 2 else "b"))
        if k % 6 == 0 and k % 2 == 0:
            steps.append(step("Blaz", "a", t + 1.2, 290.0 + 10 * k, "b"))
        if k % 5 == 0:
            steps.append(step("Pump", "a", t + 9.0, -400.0, "a"))
    rows = {(m, c): flat(to=1200.0) for m, cs in chans.items() for c in cs}
    noise, lat = {k: 10.0 for k in rows}, {m: 6.0 for m in chans}
    one = E.Assembler(parents)
    one.feed(steps, rows=rows, chans=chans, noise=noise, latency=lat)
    one.flush()
    for every in (7.0, 60.0):
        sliced = E.Assembler(parents)
        t = 0.0
        while t < 1200.0:
            e = t + every
            sliced.feed([s for s in steps if t <= s.b + 1.0 < e],
                        rows={k: [r for r in rr if t <= r[0] < e] for k, rr in rows.items()},
                        frontier={m: e for m in chans}, reach={m: 6.0 + 5.0 for m in chans}, now=e,
                        chans=chans, noise=noise, latency=lat)
            t = e
        sliced.flush()
        assert sliced.late == 0
        assert E.compare([E.brief(x) for x in one.edges], [E.brief(x) for x in sliced.edges]) == []
        assert len(sliced.edges) == len(one.edges) > 40


def test_properties_of_a_motor_start():
    """A 1 kW rise whose first reading towers at 2 kW, over a 100 W floor
    read every 2 s: surge 2, no ramp, flat while on."""
    rows = [(float(t), 100.0) for t in range(0, 100, 2)] + [(100.0, 2100.0)] + \
           [(float(t), 1100.0) for t in range(102, 300, 2)]
    p = E.properties(rows, 100.0, 1000.0, [(0.0, 231.0), (100.5, 229.0)])
    assert p["surge"] == 2.0 and p["ramp_n"] == 0 and p["ramp_s"] == 2.0
    assert p["settle"] == 0.0 and p["wobble"] == 0.0 and (p["v0"], p["v1"]) == (231.0, 229.0)
    assert E.angle(1000.0, 500.0) == E.angle(-1000.0, -500.0) > 26.5


def test_scorecard_arithmetic():
    def g(key, sizes, unit=10.0, angle=0.0, surge=None, t0=0.0, day=86400.0):
        return {"key": key, "unit": unit, "items": [
            {"size": s, "angle": angle, "surge": (surge or [1.0] * len(sizes))[k], "t": t0 + k * day}
            for k, s in enumerate(sizes)]}
    a = g(("P", 1, "a"), [1000.0] * 5, surge=[1.0, 1.0, 1.0, 3.0, 3.0])      # mixed by surge, 5 days
    b = g(("P", 1, "a"), [1001.0, 1001.0, 1002.0])                             # a's duplicate
    c = g(("P", -1, "a"), [1000.0] * 4)                                        # a's and b's mirror
    d = g(("Q", 1, "b"), [50.0, 60.0, 70.0], angle=30.0)
    sc = E.scorecard([a, b, c, d])
    assert sc["edges"] == 15 and sc["groups"] == 4 and sc["groups_min"] == 1 and sc["groups_days"] == 4
    assert sc["small_share"] == 10 / 15
    assert sc["mixed"] == 5 / 15 and sc["mixed_by"]["surge"] == 5 / 15 and sc["mixed_by"]["angle"] == 0
    assert sc["duplicates"] == 8 / 15
    assert sc["mirror"] == 8 / 11
    sp = lambda xs: E.MAD_SD * E._mad([D.edge_scale(x, 10.0) for x in xs])  # noqa: E731
    want = (5 * 0.0 + 3 * sp([1001.0, 1001.0, 1002.0]) + 4 * 0.0 + 3 * sp([50.0, 60.0, 70.0])) / 15
    assert abs(sc["tightness"] - want) < 1e-9
    # the conservation's per-pair count, and the comparison of two replays
    cons = [{"parent": "", "children": ["Hiša", "Hidrofor"], "ok": True, "p": 900.0, "c": 905.0},
            {"parent": "", "children": ["Hiša"], "ok": False, "p": 0.0, "c": 500.0},
            {"parent": "", "children": ["Hiša"], "ok": False, "p": 3000.0, "c": 40.0},
            {"parent": "", "children": ["Hiša"], "ok": False, "p": 0.0, "c": 40.0, "clear": False}]
    assert E.conservation(cons) == {">Hiša": [3, 1, 1, 0, 1, 0, 1], ">Hidrofor": [1, 1, 0, 0, 0, 0, 0]}
    x = (10.0, "P", 1, "a", {"|a": 100.0})
    assert E.compare([x], [(11.5, "P", 1, "a", {"|a": 100.2})]) == []
    assert [d["kind"] for d in E.compare([x], [(13.0, "P", 1, "a", {"|a": 100.0})])] == ["only A", "only B"]
    assert [d["kind"] for d in E.compare([x], [(10.0, "P", 1, "a", {"|a": 100.0, "Q|a": 90.0})])] == ["entries"]


def test_grouping_cuts_size_then_angle():
    """Per key, the size density's valleys and then the angle's: a heater and
    a pump of one size are two groups, a second size a third."""
    def edge(i, size, ang):
        return {"id": i, "t": float(i), "place": "", "dir": 1, "pattern": "a", "size": size, "angle": ang,
                "noise": 10.0, "legs": ["|a"], "entries": {}}
    es = [edge(i, 1000.0 + (i % 3), 0.0) for i in range(10)] + [edge(10 + i, 1000.0 + (i % 3), 35.0) for i in range(10)]
    es += [edge(30 + i, 2000.0, None) for i in range(4)]
    got = sorted((len(g["items"]), round(E._median([e["size"] for e in g["items"]]))) for g in E.group(es))
    assert got == [(4, 2000), (10, 1001), (10, 1001)]


if __name__ == "__main__":
    run_main(globals())
