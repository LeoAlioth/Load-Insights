"""Sessions, transitions, multi-phase merging, signatures, resumability."""
import json
import bisect
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from datetime import datetime, timezone  # noqa: E402
from _load import load, run_main  # noqa: E402

D = load("insights.detect")
T0 = 1_789_000_000.0     # some epoch, arbitrary
DT = 5.0


def _sig(id, watts, dur=60.0, count=5, phases="a", pf=None, **kw):
    """A signature of ``watts`` split evenly over ``phases``, first seen at 0
    and last at ``id`` unless ``kw`` says otherwise."""
    kw = {"first_seen": 0.0, "last_seen": float(id), **kw}
    return D.Signature(id=id, phases=phases, power={p: watts / len(phases) for p in phases},
                       duration_s=dur, pf=pf, count=count, **kw)


def series(seconds, fn, seed=0, base=300.0, noise=15.0):
    rnd = random.Random(seed)
    out = []
    t = T0
    while t < T0 + seconds:
        out.append((t, base + fn(t - T0) + rnd.uniform(-noise, noise)))
        t += DT
    return out


def kiln(period=300.0, on=80.0, watts=3000.0):
    return lambda s: watts if (s % period) < on else 0.0


def _fridge(period=5400.0, on=1700.0, start_w=58.0, end_w=44.0):
    """A compressor: +58 W at the start, sagging to +44 W by the end of a run."""
    return lambda s: (start_w - (start_w - end_w) * (s % period) / on) if (s % period) < on else 0.0


def test_clusters_from_before_the_angle_keep_their_steps_once_it_is_on():
    """Production's saved clusters were built before the angle was measured
    and have none; a step filtered to clusters WITH one matched none of them
    and started every load again (2026-10-12)."""
    rows = series(12 * 300, kiln(), noise=5.0)
    var = {"a": {t: 0.0 for t, _ in rows}}
    half = T0 + 6 * 300
    det = D.Detector()
    det.process({"a": [r for r in rows if r[0] < half]}, q=var, now_ts=half)
    for c in det.edges:                          # a library saved before the angle
        c.angle, c.angle_n = None, 0.0
    before = len(det.edges)
    det.process({"a": [r for r in rows if r[0] >= half]}, q=var, now_ts=T0 + 12 * 300 + 60)
    assert before >= 2 and len(det.edges) == before, (before, len(det.edges))
    assert all(c.angle is not None for c in det.edges if c.count > 6), [(c.watts, c.angle) for c in det.edges]


def test_a_load_that_sags_while_it_runs_still_closes_when_it_stops():
    """Kozolec's fridge stops 14 W short of the step it started with, past the
    ~10 W tolerance, and read as stepping down to 14 W and running on: its
    runs came out 1.8 h long, three merged into one (2026-09-28)."""
    det = D.Detector()
    a = series(4 * 5400, _fridge(), base=60.0, noise=3.0)
    closed = det.process({"a": a}, now_ts=T0 + 4 * 5400 + 60)
    right = [x for x in closed if abs(x.duration_s - 1700) <= 60]
    assert len(right) >= 3, [round(x.duration_s) for x in closed]

def test_a_load_the_reading_cannot_be_carrying_is_closed():
    """Home's floor mat stopped in the same reading as the hob's 2 kW pulse,
    and stayed open nine hours. The phase reading less than the loads
    believed running add up to is what gives it away."""
    def phase(floor_zero):
        st = D.PhaseState(floor_zero=floor_zero)
        st.baseline, st.level, st.noise = 300.0, 1300.0, 20.0
        st.open_edges = [D._Open(T0, 600.0, None, [(T0, 600.0)]),          # the mat
                         D._Open(T0 + 60, 400.0, None, [(T0 + 60, 400.0)])]
        return st
    st = phase(True)
    closed = st._unseen_stop(T0 + 300, 450.0)                            # 1000 believed on, 450 read
    assert [round(x.duration_s) for x in closed] == [300], closed        # the one that fits 550: the mat
    assert [o.watts for o in st.open_edges] == [400.0]
    assert st._unseen_stop(T0 + 310, 450.0) == []                        # 400 fits in 450: nothing more
    assert phase(True)._unseen_stop(T0 + 300, 950.0) == []               # it all fits
    low = phase(True)
    closed = low._unseen_stop(T0 + 300, 100.0)                           # 900 short: no one load fits it,
    assert len(closed) == 2 and low.open_edges == [], closed             # but neither fits in 100 W at all
    big = phase(True)
    big._unseen_stop(T0 + 300, 500.0)                                    # 500 short of 1000: nothing fits, but
    assert [o.watts for o in big.open_edges] == [400.0], big.open_edges  # 500 less the 400 W run is under half
    big._unseen_stop(T0 + 300, 250.0)                                    # the 600 W (run lifecycle, the owner
    assert [o.watts for o in big.open_edges] == [400.0], big.open_edges  # 2026-10-28); 250 still carries half of 400
    assert phase(False)._unseen_stop(T0 + 300, 450.0) == []              # a reading with solar in it


def test_a_two_phase_pulser_becomes_one_signature_on_a_plus_c():
    det = D.Detector()
    hours = 2
    a = series(hours * 3600, kiln())
    c = series(hours * 3600, kiln(), seed=1)
    b = series(hours * 3600, lambda s: 0.0, seed=2)
    closed = det.process({"a": a, "b": b, "c": c}, now_ts=T0 + hours * 3600 + 120)
    assert len(det.signatures) == 1, [s.describe(None) for s in det.signatures]
    sig = det.signatures[0]
    assert sig.phases == "ac" and sig.count >= 20, (sig.phases, sig.count)
    assert abs(sig.power["a"] - 3000) < 150 and abs(sig.power["c"] - 3000) < 150, sig.power
    assert 60 < sig.duration_s < 100 and 250 < sig.interval_s < 350, (sig.duration_s, sig.interval_s)
    assert any(f"{w} kW on phases A and C" in sig.describe(None) for w in ("5.9", "6.0", "6.1")), sig.describe(None)
    assert all(s.phases == "ac" for s in closed)


def test_a_three_phase_fall_one_reading_apart_is_one_event():
    """A fall is an all-phase event as a rise is (run lifecycle, 2026-10-28: "why
    on earth are fall steps per phase"): a three-phase load's legs falling a
    reading apart are one fall, of one step group on all three phases."""
    det = D.Detector()
    on = lambda s: 2000.0 if any(k * 1800 + 300 <= s < k * 1800 + 900 for k in range(4)) else 0.0
    late = lambda s: 2000.0 if any(k * 1800 + 300 <= s < k * 1800 + 905 for k in range(4)) else 0.0
    rows = {"a": series(4 * 1800, on), "b": series(4 * 1800, on, seed=1), "c": series(4 * 1800, late, seed=2)}
    closed = det.process(rows, now_ts=T0 + 4 * 1800 + 120)
    falls = [g for g in det.edges if not g.up]
    assert [g.phase for g in falls] == ["abc"], [(g.phase, g.watts, g.count) for g in falls]
    assert falls[0].count == 4 and abs(falls[0].watts - 6000.0) < 300.0, (falls[0].count, falls[0].watts)
    assert closed and all(s.phases == "abc" for s in closed), [(s.phases, s.duration_s) for s in closed]
    assert any(k.endswith(f">{falls[0].id}") for k in det.order), det.order       # counted on the pattern


def test_a_fall_waits_for_its_event_and_closes_only_what_was_open_before_it():
    """The fall's event forms a few readings after it; a start declared in
    between is not what it closes."""
    det = D.Detector()
    a = series(2400, lambda s: 1000.0 if 300 <= s < 900 or 920 <= s < 1520 else 0.0)
    det.process({"a": a}, now_ts=T0 + 2500)
    runs = sorted(det.recent, key=lambda r: r["start"])
    assert len(runs) == 2 and all(0.15 < r["kwh"] < 0.185 for r in runs), runs     # 1 kW x 600 s each


def test_order_counts_a_fall_against_every_group_running_then():
    """Partner evidence is order: a fall counts against every rise group
    running on its phases before it, whatever it then closes - so a wrong
    close teaches no pair (run lifecycle, 2026-10-28)."""
    det = D.Detector()
    det.phases["a"].open_edges = [D._Open(T0, 500.0, None, [(T0, 500.0)], cluster=1),
                                  D._Open(T0 + 60, 200.0, None, [(T0 + 60, 200.0)], cluster=2),
                                  D._Open(T0 + 200, 300.0, None, [(T0 + 200, 300.0)], cluster=4)]   # after the fall
    det.phases["b"].open_edges = [D._Open(T0, 900.0, None, [(T0, 900.0)], cluster=3)]
    det.note_order(9, "a", T0 + 120)
    assert sorted(det.order) == ["1>9", "2>9"], det.order
    assert det.order["2>9"][0] == 1.0 and abs(det.order["2>9"][1] - math.log(60.0)) < 1e-9
    assert det.fall_n == {9: 1.0}


def _order_cycles(fall_at):
    """Group 1 runs 100 s of every 1000 s for ten cycles; a fall of group 9 at
    ``fall_at`` seconds into each cycle."""
    det = D.Detector()
    for k in range(10):
        t0 = T0 + k * 1000.0
        det._order_tick(t0)
        det.phases["a"].open_edges = [D._Open(t0, 500.0, None, [(t0, 500.0)], cluster=1)]
        if fall_at < 100.0:
            det.note_order(9, "a", t0 + fall_at)
        det._order_tick(t0 + 100.0)
        det.phases["a"].open_edges = []
        if fall_at >= 100.0:
            det.note_order(9, "a", t0 + fall_at)
    return det


def test_partner_evidence_is_zero_until_above_chance():
    while_on = _order_cycles(90.0)
    assert while_on.partner_evidence(1, 9) > 0.0, (while_on.order, while_on.run_time, while_on.order_span)
    mean, spread = while_on.pair_length(1, 9)
    assert abs(math.exp(mean) - 90.0) < 1.0 and spread < 1e-6
    assert _order_cycles(500.0).partner_evidence(1, 9) == 0.0      # never while it ran
    few = D.Detector()
    few.order, few.fall_n, few.run_time, few.order_span = {"1>9": [3.0, 0.0, 0.0]}, {9: 3.0}, {1: 10.0}, 1e6
    assert few.partner_evidence(1, 9) == 0.0                         # under PAIR_MIN_RUNS


def test_order_counts_survive_a_restart():
    det = _order_cycles(90.0)
    back = D.Detector.from_dict(json.loads(json.dumps(det.to_dict())))
    near = lambda a, b: abs(a - b) <= 1e-3 * max(1.0, abs(b))      # stored at the precision they carry (_trim)
    assert back.order.keys() == det.order.keys() and all(
        near(x, y) for k in det.order for x, y in zip(back.order[k], det.order[k]))
    assert all(near(back.fall_n[k], v) for k, v in det.fall_n.items())
    assert all(near(back.run_time[k], v) for k, v in det.run_time.items())
    assert near(back.order_span, det.order_span) and near(back.partner_evidence(1, 9), det.partner_evidence(1, 9))


def _scoring_phase(*opens):
    """A phase at 300 W idle, its library a fresh detector, ``opens`` running:
    (since offset s, watts, group)."""
    det = D.Detector()
    st = det.phases["a"]
    st.lib, st.name, st.baseline, st.noise, st.floor_zero = det, "a", 300.0, 10.0, True
    st.open_edges = [D._Open(T0 + a, w, None, [(T0 + a, w)], cluster=c) for a, w, c in opens]
    st.level = 300.0 + sum(w for _, w, _ in opens)
    return det, st


def _evidence(det, rise, fall, secs, ratio=1.0, n=20):
    det.order[f"{rise}>{fall}"] = [n, n * math.log(secs), n * math.log(secs) ** 2,
                                   n * math.log(ratio), n * math.log(ratio) ** 2]
    det.fall_n[fall], det.run_time[rise], det.order_span = n, 0.1 * 86400.0, 86400.0


def test_a_fall_closes_its_partner_not_the_newest_of_its_size():
    """Susilna's compressor runs two hours; an unrelated 190 W load started
    three minutes ago. The compressor's partner fall closes the compressor's
    run, not the newest start of its size (run lifecycle, 2026-10-28)."""
    det, st = _scoring_phase((0.0, 190.0, 1), (7020.0, 190.0, 2))
    _evidence(det, 1, 9, 7200.0)
    st.stop_cluster = 9
    closed = st._pair(T0 + 7200.0, 190.0, None, 490.0)
    assert [x.start for x in closed] == [T0], closed
    assert [o.since for o in st.open_edges] == [T0 + 7020.0]


def test_a_drifting_load_is_closed_by_a_fall_a_fifth_under_its_rise():
    """Rise 390 W, fall 300 W: the 2026-10-28 score's tight test held it open
    until the pair had evidence; the probability score (2026-10-29) takes a
    fall a fifth under its rise at once - a load settles after its start."""
    det, st = _scoring_phase((0.0, 390.0, 1))
    st.stop_cluster = 9
    assert len(st._pair(T0 + 1800.0, 300.0, None, 600.0)) == 1 and st.open_edges == []


def test_a_step_down_beside_another_run_is_a_level_not_a_stop():
    """The owner's sequence (2026-10-29): 0, 2000, 2700, 1100, 400, 0. The
    -1600 is a step down of the 2 kW run (the phase still carries its 400 W
    rest beside the 700 W run), the -700 ends the 700 W run, the -400 the
    2 kW one: two runs, the first of two levels."""
    lv = [0.0, 2000.0, 2700.0, 1100.0, 400.0, 0.0]
    det = D.Detector()
    det.process({"a": series(4500, lambda s: lv[int((s - 300) // 600) + 1] if 300 <= s < 3300 else 0.0)},
                now_ts=T0 + 5000)
    runs = sorted((round(r["start"] - T0), round(r["end"] - T0), r["levels"], r["kwh"]) for r in det.recent)
    assert [x[:3] for x in runs] == [(300, 2700, 2), (900, 2100, 1)], runs
    assert abs(runs[0][3] - 0.8) < 0.02 and abs(runs[1][3] - 0.233) < 0.01, runs


def test_fall_similarity_follows_the_owners_curve():
    """1 within the band; 0.5 at three quarters; a far miss low, not 0; a fall
    above its rise counts its miss 2.5 times (the owner, 2026-10-29). The old
    upright freezer (141 -> 120 W) and the chest (82 -> 61 W) on one plug, a
    10 W band: each fall alike its own rise, not the other's."""
    sim = D.fall_similarity
    assert sim(100.0, 95.0, 10.0) == 1.0 or abs(sim(100.0, 95.0, 10.0) - 1.0) < 1e-3
    assert abs(sim(100.0, 75.0, 0.0) - 0.5) < 1e-9
    assert 0.1 <= sim(100.0, 40.0, 0.0) <= 0.2
    assert sim(141.0, 120.0, 10.0) > 0.9 and sim(82.0, 61.0, 10.0) > 0.9
    assert sim(82.0, 120.0, 10.0) < 0.25 and sim(141.0, 61.0, 10.0) < 0.25
    assert sim(100.0, 115.0, 0.0) < sim(100.0, 85.0, 0.0)                  # above its rise: less alike


def test_a_fall_ends_the_run_at_its_usual_length_not_the_one_far_younger():
    """Two 100 W runs, a 100 W fall of a group neither has a pair with: the
    one three hours into its usual three ends, not the one half an hour into
    its usual eight - however young or old (the owner, 2026-10-29: the chest
    freezer two hours into five took the upright's fall at its usual 58 min
    when scored by its chance of still running)."""
    det, st = _scoring_phase((0.0, 100.0, 1), (9000.0, 100.0, 2))
    _evidence(det, 1, 7, 3 * 3600.0)
    _evidence(det, 2, 8, 8 * 3600.0)
    st.stop_cluster = 9
    closed = st._pair(T0 + 3 * 3600.0, 100.0, None, 300.0)
    assert [x.start for x in closed] == [T0], closed
    assert abs(st._age_fit(st.open_edges[0], T0 + 3 * 3600.0) - (1.0 - D.FALL_AGE_SHORT_W)) < 0.01


def test_a_run_far_younger_than_usual_still_takes_its_own_fall():
    """Kozolec 14 Oct 18:17: a 537 W load ran 21 s against a usual 20 min. Its
    -528 W is its stop - the age votes, never vetoes (the owner, 2026-10-29) -
    and the EVSE's 3,604 W charge beside it keeps its size."""
    det, st = _scoring_phase((0.0, 3604.0, 1), (55.0, 537.0, 2))
    _evidence(det, 2, 8, 1200.0)
    st.stop_cluster = 9
    closed = st._pair(T0 + 76.0, 528.0, None, 300.0 + 3604.0)
    assert [x.start for x in closed] == [T0 + 55.0], closed
    assert [round(o.watts) for o in st.open_edges] == [3604]


def test_a_fall_is_scored_against_what_the_run_draws_after_its_held_drops():
    """Kozolec 4 Oct: the EVSE rose 3,568 W, lowered its charge mid-run (-802 W,
    matching nothing, held) and stopped -2,604 W. Scored against its rise less
    the held drop, it closes in two levels, not booked at 2,604 W for 3.4 h
    (the owner, 2026-10-29)."""
    det, st = _scoring_phase((0.0, 3568.0, 1))
    st.held_drops = [(T0 + 10000.0, 802.0, None)]
    st.stop_cluster = 9
    (run,) = st._pair(T0 + 12300.0, 2604.0, None, 300.0)
    assert [round(w) for _, w in run.levels[""]] == [3568, 2766], run.levels
    assert st.held_drops == [] and st.open_edges == []


def test_a_single_phase_fall_never_ends_a_leg_of_a_three_phase_run():
    """Home's EVBox (11 Oct 08:12): its A leg of 1,671 W open, a 1,751 W load
    on A for 29 s - the fall on A alone ends that load, never the charger's leg;
    a fall on all three phases ends it (the owner, 2026-10-29)."""
    det, st = _scoring_phase((0.0, 1671.0, 5), (630.0, 1782.0, 6))
    det.edges = [D.StepGroup(id=5, phase="abc", up=True, watts=5300.0), D.StepGroup(id=6, phase="a", up=True, watts=1780.0),
                 D.StepGroup(id=9, phase="a", up=False, watts=1750.0), D.StepGroup(id=10, phase="abc", up=False, watts=5300.0)]
    _evidence(det, 5, 9, 600.0)                       # a partner by order, as the charger's long runs made it
    st.stop_cluster = 9
    closed = st._pair(T0 + 660.0, 1751.0, None, 300.0 + 1671.0)
    assert [x.start for x in closed] == [T0 + 630.0], closed
    det, st = _scoring_phase((0.0, 1671.0, 5))
    det.edges = [D.StepGroup(id=5, phase="abc", up=True, watts=5300.0), D.StepGroup(id=9, phase="a", up=False, watts=1750.0),
                 D.StepGroup(id=10, phase="abc", up=False, watts=5300.0)]
    st.stop_cluster = 9
    assert st._pair(T0 + 3000.0, 1671.0, None, 300.0) == [] and len(st.open_edges) == 1
    st.held_drops, st.stop_cluster = [], 10
    assert len(st._pair(T0 + 3000.0, 1671.0, None, 300.0)) == 1 and st.open_edges == []


def _second_rise_phase(declared):
    det, st = _scoring_phase((0.0, 3400.0, 1))
    det.edges = [D.StepGroup(id=1, phase="a", up=True, watts=4000.0)]
    keep = D._Open(T0 + 70.0, 5700.0, None, [(T0 + 70.0, 5700.0)], cluster=1)
    st.declared = declared
    st.declared_t = [d[0] for d in declared]
    st.open_edges.append(keep)
    return det, st, keep


def test_a_second_rise_never_ends_the_first_without_a_fall_or_the_readings():
    """The EVBox ramping 3.4 then 5.7 kW in one group (Home, 7 Oct): the reading
    still carries the first - both stay on, with no history of two units. Not
    carried: closed at the fall that brought the reading down, never at its
    usual length (the owner, 2026-10-29)."""
    det, st, keep = _second_rise_phase([(T0 + 70.0, 5700.0, None, T0 + 65.0, T0 + 70.0, T0 + 75.0, 300.0 + 3400.0 + 5700.0)])
    assert st.second_rise(1, keep) == [] and len(st.open_edges) == 2
    assert len(det.edges[0].cannot) == 1                                   # carried in full: two units' overlap
    det, st, keep = _second_rise_phase([(T0 + 40.0, -900.0, None, T0 + 35.0, T0 + 40.0, T0 + 45.0, 300.0 + 2500.0),
                                        (T0 + 70.0, 5700.0, None, T0 + 65.0, T0 + 70.0, T0 + 75.0, 300.0 + 2500.0 + 5700.0)])
    assert st.second_rise(1, keep) == [] and len(st.open_edges) == 2       # 2,500 of 3,400 left: still on
    det, st, keep = _second_rise_phase([(T0 + 40.0, -3000.0, None, T0 + 35.0, T0 + 40.0, T0 + 45.0, 700.0),
                                        (T0 + 70.0, 5700.0, None, T0 + 65.0, T0 + 70.0, T0 + 75.0, 700.0 + 5700.0)])
    assert [(x.start, x.end) for x in st.second_rise(1, keep)] == [(T0, T0 + 40.0)]   # fell unseen at -3,000
    det, st, keep = _second_rise_phase([(T0 + 70.0, 5700.0, None, T0 + 65.0, T0 + 70.0, T0 + 75.0, 300.0 + 5700.0)])
    assert [(x.start, x.end) for x in st.second_rise(1, keep)] == [(T0, T0 + 70.0)]   # no fall seen: the new rise


def test_a_second_rise_ends_the_first_at_a_fall_of_its_size_no_run_of_its_group_took():
    """Kozolec 3 Oct 08:40: a boiler pulse kept 72 min because an unmetered
    charge in the reading looked like it still carried it, its fall taken by
    its own next pulse filed in another group (the owner, 2026-10-29). A fall
    that ended a run of the same group is that run's: two units, the reading
    decides."""
    rows = [(T0 + 40.0, -3390.0, None, T0 + 35.0, T0 + 40.0, T0 + 45.0, 300.0 + 5000.0),
            (T0 + 70.0, 5700.0, None, T0 + 65.0, T0 + 70.0, T0 + 75.0, 300.0 + 5000.0 + 5700.0)]
    det, st, keep = _second_rise_phase(rows)
    st.closed_groups = [(T0 + 40.0, 2)]
    assert [(x.start, x.end) for x in st.second_rise(1, keep)] == [(T0, T0 + 40.0)]
    det, st, keep = _second_rise_phase(rows)
    st.closed_groups = [(T0 + 40.0, 1)]
    assert st.second_rise(1, keep) == [] and len(st.open_edges) == 2


def test_a_fall_read_two_ways_is_decided_by_the_next_step():
    """Look back (_step_down): the 1,437 W fall beside the boiler and the
    EVSE is the EVSE stepping down while the boiler stays on, until a later step
    decides: the boiler's own fall (it was on), or the EVSE stopping from its
    full 3,498 W (it never stepped: the boiler stopped back at the fall)."""
    for second in ("boiler", "evse"):
        det, st = _scoring_phase((0.0, 1836.0, 12), (60.0, 3498.0, 50))
        st.baseline = 54.0
        _evidence(det, 12, 13, 1400.0)
        _evidence(det, 77, 14, 600.0)
        st.stop_cluster = 14
        at, level = T0 + 1400.0, 54.0 + 1836.0 + 3498.0 - 1437.0
        assert st._pair(at, 1437.0, None, level) + st._unseen_stop(at, level) == []
        assert st.step_pending is not None and [round(o.now or o.watts) for o in st.open_edges] == [1836, 2061]
        if second == "boiler":
            st.stop_cluster = 13
            closed = st._pair(at + 49.0, 1828.0, None, level - 1828.0)
            assert [x.start for x in closed] == [T0] and st.step_pending is None
            assert [round(o.now or o.watts) for o in st.open_edges] == [2061]
        else:
            st.stop_cluster = 51
            evse = st.open_edges[1]
            st._end(evse, at + 600.0, 3498.0, None, direct=True)
            assert st.step_pending is None and [(r.start, r.end) for r in st._resolved] == [(T0, at)]


def test_a_fall_group_nobody_is_partnered_with_is_no_evidence_against_a_run():
    """A fall in a group no start has as a partner scores a run whose group has
    partners as one with none (FALL_NO_PAIR): Kozolec's boiler lost its falls,
    each in a new group, at FALL_OTHER_PAIR (2026-10-29)."""
    det, st = _scoring_phase((0.0, 1800.0, 1))
    _evidence(det, 1, 7, 600.0)
    st.stop_cluster = 9
    fresh = st._score_held(st.open_edges[0], 1800.0, T0 + 600.0)[0]
    _evidence(det, 2, 9, 600.0)
    taken = st._score_held(st.open_edges[0], 1800.0, T0 + 600.0)[0]
    assert abs(fresh / taken - D.FALL_NO_PAIR / D.FALL_OTHER_PAIR) < 1e-6, (fresh, taken)


def test_two_runs_stopping_in_one_reading_are_one_candidate():
    """Kozolec 14 Oct 19:15: the boiler (1,829 W) and a 1 kW load fell together,
    2,827 W, beside a 3,604 W charge: the two close, the charge goes on (the
    owner, 2026-10-29). A run the sum does not need stays: 1,000 + 20 W is not
    a 1,000 W fall's pair."""
    det, st = _scoring_phase((0.0, 3604.0, 1), (60.0, 1829.0, 2), (120.0, 1005.0, 3))
    _evidence(det, 1, 7, 4 * 3600.0)
    st.stop_cluster = 9
    closed = st._pair(T0 + 3600.0, 2827.0, None, 300.0 + 3604.0)
    assert [x.start for x in closed] == [T0 + 60.0, T0 + 120.0], closed
    assert [round(o.watts) for o in st.open_edges] == [3604]
    det, st = _scoring_phase((0.0, 1000.0, 1), (60.0, 20.0, 2))
    assert st._combination(T0 + 600.0, 1000.0) is None


def test_the_reading_short_of_its_runs_ends_the_least_likely_still_running():
    """The reading carries one of two 500 W runs: the one past its usual ten
    minutes ends, not the older one an hour into its usual eight (the owner,
    2026-10-29: Susilna's compressor was closed oldest first)."""
    det, st = _scoring_phase((0.0, 500.0, 1), (3000.0, 500.0, 2))
    _evidence(det, 1, 7, 8 * 3600.0)
    _evidence(det, 2, 8, 600.0)
    closed = st._unseen_stop(T0 + 3600.0, 600.0)
    assert [x.start for x in closed] == [T0 + 3000.0], closed
    assert [o.since for o in st.open_edges] == [T0]


def test_a_join_keeps_what_the_joined_group_learned_of_order():
    """Group 5 joins group 4 in the daily pass: its pairs, running time, falls
    and co-runs add to 4's, and a run still open in 5 is scored as 4's (the
    hidrofor's young rise groups, 2026-10-29)."""
    det = D.Detector()
    det.edges = [D.StepGroup(id=4, phase="a", up=True, watts=900.0), D.StepGroup(id=5, phase="a", up=True, watts=950.0)]
    _evidence(det, 5, 9, 60.0, ratio=0.86)
    det.run_time[4], det.co_run = 100.0, {"4,5": 2, "5,6": 3}
    assert det.partner_evidence(4, 9) == 0.0
    det.edges[1].joined = 4
    det._fold_order(5, 4)
    assert "5>9" not in det.order and det.order["4>9"][0] == 20
    assert det.run_time == {4: 100.0 + 0.1 * 86400.0} and det.co_run == {"4,6": 3}
    assert det.partner_evidence(4, 9) > 0.0 and det.partner_evidence(5, 9) == det.partner_evidence(4, 9)
    assert abs(det.pair_ratio(5, 9)[0] - math.log(0.86)) < 1e-9


def test_a_fall_far_from_every_run_closes_nothing():
    det, st = _scoring_phase((0.0, 1000.0, 1))
    st.stop_cluster = 9
    assert st._pair(T0 + 600.0, 400.0, None, 900.0) == [] and len(st.open_edges) == 1
    assert st.held_drops and st.held_drops[-1][:2] == (T0 + 600.0, 400.0)       # held, as a drop


def test_a_dip_and_its_return_open_no_run():
    """Home's EVBox dipped by 2.1 kW for a reading or two 22 times in a charge,
    each return a run of its own (2026-10-04): a dip is two levels of the run
    that carries it (run lifecycle, 2026-10-28)."""
    det = D.Detector()
    a = series(4000, lambda s: (8500.0 if 1500 <= s < 1520 else 10600.0) if 300 <= s < 3000 else 0.0)
    det.process({"a": a}, now_ts=T0 + 3400)
    assert len(det.recent) == 1, [(r["start"] - T0, r["kwh"], r["levels"]) for r in det.recent]
    assert det.recent[0]["levels"] >= 3 and det.recent[0]["max_w"] > 10000, det.recent


def test_a_late_return_is_a_new_run():
    det = D.Detector()
    a = series(4000, lambda s: 10600.0 if 300 <= s < 1500 or 1560 <= s < 3000 else 0.0)
    det.process({"a": a}, now_ts=T0 + 3400)
    assert len(det.recent) == 2, [(r["start"] - T0, r["kwh"]) for r in det.recent]


def test_two_fridges_overlapping_count_two():
    """Kozolec's fridges, 83 W and 75 W, in one group: the second rises while
    the first still runs - a second unit, both open, a cannot-link pair; each
    ends by its own fall (run lifecycle, 2026-10-28). With no overlap seen
    before too: the reading carries the first, so it is no missed fall (the
    owner, 2026-10-29)."""
    det, st = _scoring_phase((0.0, 83.0, 1))
    det.edges = [D.StepGroup(id=1, phase="a", up=True, watts=80.0)]
    det.edges[0].cannot = [(84.0, None, 74.0, None)] * D.PAIR_MIN_RUNS     # overlaps seen before
    st.noise = 5.0
    keep = D._Open(T0 + 600.0, 75.0, None, [(T0 + 600.0, 75.0)], cluster=1)
    st.declared = [(T0 + 600.0, 75.0, None, T0 + 595.0, T0 + 600.0, T0 + 605.0, 300.0 + 83.0 + 75.0)]
    st.declared_t = [T0 + 600.0]
    st.open_edges.append(keep)
    assert st.second_rise(1, keep) == [] and len(st.open_edges) == 2
    assert det.edges[0].cannot[-1] == (83.0, None, 75.0, None)
    fresh, st2 = _scoring_phase((0.0, 83.0, 1))                           # no overlaps before: still two units
    fresh.edges = [D.StepGroup(id=1, phase="a", up=True, watts=80.0)]
    st2.noise, st2.declared, st2.declared_t = 5.0, list(st.declared), list(st.declared_t)
    keep2 = D._Open(T0 + 600.0, 75.0, None, [(T0 + 600.0, 75.0)], cluster=1)
    st2.open_edges.append(keep2)
    assert st2.second_rise(1, keep2) == [] and len(st2.open_edges) == 2 and len(fresh.edges[0].cannot) == 1


def test_a_second_rise_without_the_first_carried_ends_the_first_at_its_missed_fall():
    det, st = _scoring_phase((0.0, 83.0, 1))
    det.edges = [D.StepGroup(id=1, phase="a", up=True, watts=80.0)]
    st.noise = 5.0
    keep = D._Open(T0 + 600.0, 80.0, None, [(T0 + 600.0, 80.0)], cluster=1)
    st.declared = [(T0 + 400.0, -82.0, None, T0 + 395.0, T0 + 400.0, T0 + 405.0, 300.0),     # its fall, seen
                   (T0 + 600.0, 80.0, None, T0 + 595.0, T0 + 600.0, T0 + 605.0, 380.0)]      # from 300: not carried
    st.declared_t = [T0 + 400.0, T0 + 600.0]
    st.open_edges.append(keep)
    closed = st.second_rise(1, keep)
    assert [(x.start, x.end) for x in closed] == [(T0, T0 + 400.0)] and st.open_edges == [keep]
    assert det.edges[0].cannot == []


def test_overlapping_similar_fridges_split_into_two_groups():
    """Kozolec's two fridges, overlapping now and then: sizes the meter can
    resolve (86 and 72 W at 3 W of noise) are two groups from their first
    steps (the mixture); sizes it cannot (83 and 75 W, under the step error)
    stay one group however often they overlap - which of the two a step was
    would be a guess - and the overlaps count it as two units (second_rise).
    The size density's split (split_groups) is gone with it (2026-10-29)."""
    for (hi, lo), groups in (((86.0, 72.0), 2), ((83.0, 75.0), 1)):
        det = D.Detector()
        det.phases["a"].noise = 3.0
        for k in range(6):
            for w in (hi, lo):
                det._classify_step("a", T0 + 60.0 * (2 * k + (w < 80)), w, None, 0.0)
        ups = [c for c in det.edges if c.up]
        ups[0].cannot = [(hi, None, lo, None)] * 10
        if len(ups) == 2:                  # one rose while the other ran, as _form_first counts it live
            det.co_run = {f"{ups[0].id},{ups[1].id}": 10}
            det.run_time, det.order_span = {ups[0].id: 600.0, ups[1].id: 600.0}, 86400.0
        det.mix_daily()
        assert len([c for c in det.edges if c.up and c.joined is None]) == groups, ((hi, lo), [(c.watts, c.joined) for c in det.edges])

def _mix_groups(offsets, noise=2.83, base=1000.0, angles=None):
    det = D.Detector()
    det.phases["a"].noise = noise                     # a step error of ~4 W (sqrt(2) x noise)
    ids = []
    for k, o in enumerate(offsets):
        var = None if angles is None else (base + o) * math.tan(math.radians(angles[k]))
        ids.append(det._classify_step("a", T0 + 600.0 * k, base + o, var, 0.0).id)
    return ids


def test_mixture_groups_the_owners_two_examples_as_he_would():
    """The owner's examples, offsets from 1 kW, a ~4 W step error: two tight
    triplets are two groups, an even spread is one (2026-10-28)."""
    a = _mix_groups([-12, -10, -8, 8, 10, 12])
    assert len(set(a)) == 2 and a[0] == a[1] == a[2] and a[3] == a[4] == a[5], a
    det = D.Detector()
    det.phases["a"].noise = 2.83
    for k, o in enumerate([-12, -7, -2, 2, 7, 12]):
        det._classify_step("a", T0 + 600.0 * k, 1000.0 + o, None, 0.0)
    det.mix_daily()                                    # an even spread founded a second group live; the daily join
    assert sum(c.joined is None for c in det.edges if c.up) == 1, [(c.watts, c.recent, c.joined) for c in det.edges]


def test_mixture_tells_a_pump_from_a_heater_of_one_size_by_angle():
    ids = _mix_groups([0, 2, -1, 1, -2, 0], angles=[0, 35, 0, 35, 0, 35])
    assert len(set(ids)) == 2 and ids[0] == ids[2] == ids[4] and ids[1] == ids[3] == ids[5], ids


def test_mixture_daily_splits_two_piles_and_joins_one_device():
    """The daily one-or-two test: a group holding two piles 60 W apart at
    1 kW is split; two groups holding one device's spread are joined; one
    tight pile stays one group."""
    rnd = random.Random(5)
    det = D.Detector()
    det.phases["a"].noise = 2.83
    g = det._classify_step("a", T0, 1000.0, None, 0.0)
    g.recent = [(rnd.gauss(970.0, 3.0), None, None, None) for _ in range(20)] + [(rnd.gauss(1030.0, 3.0), None, None, None) for _ in range(20)]
    g.watts, g.watts_mad, g.count = 1000.0, 30.0, 40.0
    assert det.mix_daily() == (1, 0)
    centres = sorted(round(c.watts) for c in det.edges if c.joined is None)
    assert len(centres) == 2 and abs(centres[0] - 970) < 5 and abs(centres[1] - 1030) < 5, centres
    one = D.Detector()
    one.phases["a"].noise = 2.83
    a = one._classify_step("a", T0, 990.0, None, 0.0)
    b = D.StepGroup(id=one.next_edge_id, phase="a", up=True, watts=1006.0, owner="")
    one.next_edge_id += 1
    one.edges.append(b)
    a.recent = [(rnd.gauss(998.0, 8.0), None, None, None) for _ in range(15)]
    b.recent = [(rnd.gauss(998.0, 8.0), None, None, None) for _ in range(15)]
    a.watts_mad = b.watts_mad = 6.0
    a.count, b.count = 15.0, 15.0
    assert one.mix_daily()[1] == 1 and sum(c.joined is None for c in one.edges) == 1


def test_a_step_is_kept_in_its_group_with_its_on_side_voltage():
    """Each grouped step is kept in its group (StepGroup.recent, the one store of
    them, 2026-10-30) with its time and the voltage on its on side - a 2 kW
    heater pulling its phase from 240 to 236 V: 236 V, after its rise. Every
    step of the last two days stays, beyond them the newest STEP_MAX, none
    older than a month; it travels with the library."""
    f = D.Fleet()
    f.volts = {"": {"a": [(T0 + k, 240.0 if k < 0 else 236.0) for k in range(-60, 61, 5)]}}
    f.main.volts_at = lambda ph, t, before: f._volts_at("", ph, t, before)
    g = f.main._classify_step("a", T0, 2000.0, None, 0.0)
    assert g.recent[-1][0] == 2000.0 and g.recent[-1][2:] == (T0, 236.0), g.recent
    back = D.Fleet.from_dict(json.loads(json.dumps(f.to_dict())))
    assert [c.recent for c in back.main.edges] == [c.recent for c in f.main.edges]
    rows = [(1000.0, None, T0 - 40 * 86400.0, None)] + [(1000.0, None, T0 - 5 * 86400.0 + k, None) for k in range(150)] \
        + [(1000.0, None, T0 - 3600.0 + k, None) for k in range(150)]
    kept = D._kept_steps(rows)
    assert all(x[2] >= T0 - D.STEP_AGE_S for x in kept)                         # a month at most
    assert sum(1 for x in kept if x[2] >= T0 - 3600.0) == 150                     # all of the last two days
    assert len(kept) == 150, len(kept)                    # over 100 in two days: none older
    few = rows[:151] + [(1000.0, None, T0 - 3600.0 + k, None) for k in range(30)]
    kept = D._kept_steps(few)
    assert len(kept) == D.STEP_MAX and sum(1 for x in kept if x[2] >= T0 - 3600.0) == 30   # 30 recent, 70 older to 100

def test_a_heaters_voltage_exponent_is_fitted_and_its_sizes_compared_at_230_volts():
    """At Home the grid voltage swings with the PV: a 2 kW element reads ~1.91
    kW at 225 V and ~2.27 kW at 245 V. Its group's k (~2) is fitted from the
    steps store, and a new step is compared at 230 V; at Kozolec's steady
    inverter voltage no k is believed."""
    rnd = random.Random(2)
    rows = [(2000.0 * (v / 230.0) ** 2 * (1 + rnd.gauss(0, 0.002)), v) for v in (rnd.uniform(225, 245) for _ in range(40))]
    k, nom = D._volt_k(rows)
    assert 1.8 < k < 2.2 and abs(nom - 2000.0) < 10.0, (k, nom)
    assert D._volt_k([(1800.0 + rnd.gauss(0, 10), 230.0 + rnd.gauss(0, 0.3)) for _ in range(40)]) is None
    det = D.Detector()
    det.phases["a"].noise = 7.0
    g = det._classify_step("a", T0, 2000.0, None, 0.0)
    g.k, g.c_nom, g.count = 2.0, 2000.0, 40.0
    det.volts_at = lambda ph, t, before: 245.0
    assert det._classify_step("a", T0 + 600.0, 2269.0, None, 0.0) is g     # 2,269 W at 245 V is 2,000 at 230


def test_a_run_of_a_shared_rise_group_is_filed_by_its_stop():
    """A rise group ending in two partner fall groups (the hidrofor's wide
    starts and another load's of their size): each run is that pair's device,
    its stop the reliable signature; one with no stop is the start's."""
    det = _order_cycles(90.0)
    det.order["1>8"] = list(det.order["1>9"])
    det.fall_n[8] = det.fall_n[9]
    assert sorted(det.partners_of(1)) == [8, 9]
    run = lambda stop: D.Run(phases="a", start=T0, end=T0 + 90.0, levels={"a": [(T0, 900.0)]}, pair=(1, stop))   # noqa: E731
    assert det.device_of(run(9)) != det.device_of(run(8)) and det.device_of(run(9)) < 0
    assert det.device_of(run(None)) in (det.device_of(run(8)), det.device_of(run(9)))   # its strongest partner's pair
    alone = D.Detector()
    assert alone.device_of(run(None)) == 1                                  # no partner learned yet: the start alone


def test_identical_units_are_not_split():
    """A water heater's two identical elements overlapping: their group has
    cannot-link pairs no size separates, and stays one."""
    det = D.Detector()
    det.phases["a"].noise = 20.0
    for k in range(12):
        det._classify_step("a", T0 + 600.0 * k, 2000.0 + (k % 3), None, 0.0)
    g = next(c for c in det.edges if c.up)
    g.cannot = [(2000.0, None, 2001.0, None)] * 10
    assert det.mix_daily()[0] == 0 and len([c for c in det.edges if c.up and c.joined is None]) == 1


def test_a_second_rise_under_the_noise_keeps_the_first_but_counts_no_second_unit():
    """A 20 W run under 15 W of noise: the reading cannot tell it fell, so it
    is not closed (the owner, 2026-10-29: never without a fall or the readings),
    and no overlap is recorded - the noise hides whether it was a second unit."""
    det, st = _scoring_phase((0.0, 20.0, 1))
    det.edges = [D.StepGroup(id=1, phase="a", up=True, watts=20.0)]
    st.noise = 15.0
    keep = D._Open(T0 + 600.0, 20.0, None, [(T0 + 600.0, 20.0)], cluster=1)
    st.declared = [(T0 + 600.0, 20.0, None, T0 + 595.0, T0 + 600.0, T0 + 605.0, 340.0)]
    st.declared_t = [T0 + 600.0]
    st.open_edges.append(keep)
    assert st.second_rise(1, keep) == [] and len(st.open_edges) == 2 and det.edges[0].cannot == []


def test_a_lone_open_start_does_not_take_the_phases_other_loads():
    """Home's 39 W Inkubator pulse read 306 W: alone on its phase, it was
    followed to the phase's whole level. It draws its own, and what the phase
    carries beyond it is no one's (run lifecycle, 2026-10-28)."""
    det = D.Detector()
    a = series(2400, lambda s: (40.0 if s >= 600 else 0.0) + min(300.0, max(0.0, (s - 900.0) * 0.5)), noise=3.0)   # a ramp, no step
    det.process({"a": a}, now_ts=T0 + 2400)
    st = det.phases["a"]
    small = [o for o in st.open_edges if o.watts < 100.0]
    assert small and st.level_of(small[0]) < 60.0, [(o.watts, o.now, st.level_of(o)) for o in st.open_edges]


def test_a_run_ends_when_the_reading_less_the_others_falls_under_half():
    """A 1 kW run beside a 2 kW boiler, the reading at 2.4 kW: the 1 kW run is
    not on, the boiler is (run lifecycle, the owner 2026-10-28)."""
    det, st = _scoring_phase((0.0, 2000.0, 1), (600.0, 1000.0, 2))
    closed = st._unseen_stop(T0 + 1200.0, 2400.0)
    assert [x.start for x in closed] == [T0 + 600.0] and [o.watts for o in st.open_edges] == [2000.0]


def test_age_alone_ends_nothing():
    """A start ten times its group's usual run, the reading still carrying it,
    stays open - and no longer counts live (LIVE_STALE_FACTOR of its group's
    usual length)."""
    fleet, t = _named_kettle()
    det = fleet.main
    t2 = _feed(fleet, t, [(300.0, 60), (1800.0, 3000)])                  # on for 3,000 s, its runs 300 s
    st = next(x for x in det.phases.values() if x.open_edges)
    usual = det.usual_length(st.open_edges[0].cluster)
    assert usual and 250.0 < usual < 350.0, usual
    assert len(st.open_edges) == 1 and det.live_runs(t2) == []


def test_a_two_level_session_is_one_session_with_two_levels():
    # a washer, after five idle minutes: 2000 W for 10 min, then 400 W for 20 min, on B
    def washer(s):
        s -= 300
        if s < 0:
            return 0.0
        if s < 600:
            return 2000.0
        if s < 1800:
            return 400.0
        return 0.0
    det = D.Detector()
    b = series(3600, washer)
    det.process({"b": b}, now_ts=T0 + 3700)
    assert len(det.signatures) == 1
    sig = det.signatures[0]
    assert sig.phases == "b" and sig.count == 1 and round(sig.level_count) == 2, (sig.phases, sig.count, sig.level_count)
    assert 1700 < sig.duration_s < 1900
    rec = det.recent[-1]
    assert rec["levels"] == 2 and 0.42 < rec["kwh"] < 0.52, rec     # 2 kW x 10 min + 0.4 kW x 20 min = 0.467 kWh


def test_a_window_that_begins_mid_load_still_finds_the_floor():
    """The backfill starts wherever the recorder's window starts - possibly
    with a load on. The seed must not take that load for the idle level, and
    the detector must recover the session structure afterwards."""
    det = D.Detector()
    a = series(3600, lambda s: 2000.0 if s < 400 or 1200 <= s < 1500 else 0.0)   # on at t=0
    det.process({"a": a}, now_ts=T0 + 3700)
    assert 250 < det.phases["a"].baseline < 350, det.phases["a"].baseline
    # the second, complete session is recorded with the right power
    assert det.recent and abs(det.recent[-1]["max_w"] - 2000) < 200, det.recent


def test_noise_alone_makes_no_session():
    det = D.Detector()
    det.process({"a": series(3600, lambda s: 0.0, noise=40.0)}, now_ts=T0 + 3700)
    assert det.signatures == [] and det.recent == []
    assert det.phases["a"].baseline is not None and det.phases["a"].noise >= D.MIN_NOISE_W


def test_a_blip_is_dropped_but_a_short_real_load_is_kept():
    det = D.Detector()
    blip = series(600, lambda s: 300.0 if 100 <= s < 110 else 0.0)        # 300 W for 10 s: 0.8 Wh
    det.process({"a": blip}, now_ts=T0 + 700)
    assert det.recent == []
    det2 = D.Detector()
    kettle = series(600, lambda s: 2000.0 if 100 <= s < 160 else 0.0)     # 2 kW for a minute: 33 Wh
    det2.process({"a": kettle}, now_ts=T0 + 700)
    assert len(det2.recent) == 1


def test_processing_in_slices_equals_processing_at_once():
    a = series(3 * 3600, kiln()); c = series(3 * 3600, kiln(), seed=1)
    whole = D.Detector(); whole.process({"a": a, "c": c}, now_ts=T0 + 3 * 3600 + 120)
    sliced = D.Detector()
    for i in range(0, len(a), 137):
        end_ts = max(a[min(i + 136, len(a) - 1)][0], c[min(i + 136, len(c) - 1)][0])
        sliced.process({"a": a[i:i + 137], "c": c[i:i + 137]}, now_ts=end_ts)
    sliced.process({}, now_ts=T0 + 3 * 3600 + 120)
    assert len(whole.signatures) == len(sliced.signatures) == 1
    assert whole.signatures[0].count == sliced.signatures[0].count, (whole.signatures[0].count, sliced.signatures[0].count)


def _passes(rows_by_phase, cuts, end):
    """``rows_by_phase`` read the way a runner reads the recorder: in passes
    ending at each of ``cuts`` and then ``end``, each [start, end)."""
    t0 = -float("inf")
    for e in list(cuts) + [end]:
        yield {p: [r for r in rows if t0 <= r[0] < e] for p, rows in rows_by_phase.items()}, e
        t0 = e


def _as_filed(sessions):
    return [(s.phases, round(s.start, 3), round(s.end, 3), round(s.energy_wh, 6), s.signature_id) for s in sessions]


def _sliced_history(hours=6.0):
    """Three phases read together every 5 s: a two-phase pulser on A and C
    whose legs settle a reading apart, and on B a 900 W load read as a plug
    reports - on change, and once a minute otherwise - so a change is
    confirmed by the meter's silence."""
    secs = hours * 3600.0
    a = series(secs, kiln(period=300.0, on=50.0), seed=1)
    c = [(t + (5.0 if (t - T0) % 300.0 < 5.0 else 0.0), w) for t, w in series(secs, kiln(period=300.0, on=50.0), seed=2)]
    c = sorted({t: w for t, w in c}.items())
    b, last, t = [], None, T0
    while t < T0 + secs:
        w = 300.0 + (900.0 if (t - T0) % 1700.0 < 400.0 else 0.0)
        if w != last or not b or t - b[-1][0] >= 60.0:
            b.append((t, w))
            last = w
        t += 5.0
    return {"a": a, "b": b, "c": c}, T0 + secs + 600.0


def test_a_pass_is_only_a_pause():
    """The same history read in one call, in passes of an hour or ten
    minutes, or a minute or 37 s at a time, files the same sessions into the
    same signatures (2026-10-02). Before, a pass's end confirmed every pending
    change, formed every rise still in its event window alone, filed every
    run that had waited out its tail and none that had not, and the library a
    one-call replay consulted stayed empty until the end."""
    rows, end = _sliced_history()
    whole = _as_filed(D.Detector().process(rows, now_ts=end))
    assert len(whole) > 60 and any(p == "ac" for p, *_ in whole) and any(p == "b" for p, *_ in whole), whole[:5]
    for step in (3600.0, 600.0, 60.0, 37.0):
        det, got = D.Detector(), []
        for part, e in _passes(rows, [T0 + k * step for k in range(1, int(6 * 3600 / step) + 1)], end):
            got += det.process(part, now_ts=e)
        assert _as_filed(got) == whole, (step, [x for x in _as_filed(got) if x not in whole][:3])


def _fleet_filed(rows, plug, cuts, end, wait):
    """A fleet fed the grid ``rows`` and one plug in passes; every house
    session it filed, and where each signature stands at the end."""
    fleet = D.Fleet()
    fleet.wait_cap_s = wait
    filed, file = [], fleet.main._file

    def keep(s, *a, **kw):
        file(s, *a, **kw)
        filed.append(s)
    fleet.main._file = keep
    for (part, e), (sub, _) in zip(_passes(rows, cuts, end), _passes({"a": plug}, cuts, end)):
        fleet.process(part, {"Plug": sub}, now_ts=e, single={"Plug": True})
    where = sorted((x.id, sorted(x.locations.items())) for x in fleet.main.signatures)
    return _as_filed(filed), where


def test_every_meter_is_read_on_one_clock():
    """A plug under B reports its load two seconds after the grid, on change
    and once a minute. Read in one call, by the hour or a minute at a time,
    the fleet files the same house sessions into the same signatures, placed
    at the same meters (2026-10-02). Before, the meters were read a whole
    pass ahead of the grid and the fleet filed once a pass: in one call no
    meter had voted on its phase until the end, so none was ever asked."""
    rows, end = _sliced_history()
    plug, last = [], None
    for t, w in rows["b"]:
        v = w - 300.0
        if v != last or not plug or t - plug[-1][0] >= 60.0:
            plug.append((t + 2.0, v))
            last = v
    whole, where = _fleet_filed(rows, plug, [], end, D.METER_WAIT_CAP_S)
    assert len(whole) > 60 and any(dict(w).get("Plug") for _, w in where), where
    for step in (3600.0, 60.0, 37.0):
        got, at = _fleet_filed(rows, plug, [T0 + k * step for k in range(1, int(6 * 3600 / step) + 1)], end,
                               D.METER_WAIT_CAP_S)
        assert got == whole, (step, [x for x in got if x not in whole][:3])
        assert at == where, step


def _fleet_restarted(rows, plug, step, end, restart, state=False):
    """A fleet fed the grid ``rows`` and one plug in passes of ``step`` seconds;
    with ``restart``, stored through JSON and read back after every pass, as a
    site restarted between two passes. Every house session it filed."""
    cuts = [T0 + k * step for k in range(1, int(6 * 3600 / step) + 1)]
    fleet = D.Fleet()
    filed = _keep_filed(fleet)
    for (part, e), (sub, _) in zip(_passes(rows, cuts, end), _passes({"a": plug}, cuts, end)):
        fleet.process(part, {"Plug": sub}, now_ts=e, single={"Plug": True})
        if restart:
            fleet = D.Fleet.from_dict(json.loads(json.dumps(fleet.to_dict())))
            fleet.main.tz_offset_s = 0.0
            fleet.load_older(json.loads(json.dumps(fleet.older_to_dict())))
            filed_so_far = list(filed)
            filed = _keep_filed(fleet)
            filed[:0] = filed_so_far
    return fleet if state else _as_filed(filed)


def test_a_restart_is_only_a_pause():
    """The same history read in passes of an hour, held in memory or stored
    and read back after every pass, files the same sessions: what a pass was
    in the middle of deciding - a step still settling, a run a meter was about
    to end - goes into the store with the library (the owner, 2026-10-12)."""
    rows, end = _sliced_history()
    plug, last = [], None
    for t, w in rows["b"]:
        v = w - 300.0
        if v != last or not plug or t - plug[-1][0] >= 60.0:
            plug.append((t + 2.0, v))
            last = v
    for step in (3600.0, 600.0):
        held = _fleet_restarted(rows, plug, step, end, False)
        got = _fleet_restarted(rows, plug, step, end, True)
        assert len(held) > 60, held[:3]
        assert got == held, (step, [x for x in got if x not in held][:3], [x for x in held if x not in got][:3])


def test_a_restart_leaves_the_stored_state_as_it_was():
    """What a store holds after a run read in hourly passes is the same with
    or without a restart between them: nothing a later pass reads is left out -
    the voltage beside the steps, the phase each pair was learned on, the
    devices still to judge again, the runs the day keeps and those waiting for
    a meter being the same runs (2026-10-19)."""
    rows, end = _sliced_history()
    plug, last = [], None
    for t, w in rows["b"]:
        v = w - 300.0
        if v != last or not plug or t - plug[-1][0] >= 60.0:
            plug.append((t + 2.0, v))
            last = v
    trim, D.TRIM = D.TRIM, 0                    # the store's rounding is its own cost, measured apart
    try:
        held = _fleet_restarted(rows, plug, 3600.0, end, False, state=True)
        got = _fleet_restarted(rows, plug, 3600.0, end, True, state=True)
        a, b = held.to_dict(), got.to_dict()
    finally:
        D.TRIM = trim
    gone = lambda d: json.dumps(d, sort_keys=True, default=str)  # noqa: E731
    for k in set(a) | set(b):
        if isinstance(a.get(k), dict) and k in ("main", "subs"):
            for j in set(a[k]) | set(b[k]):
                assert gone(a[k].get(j)) == gone(b[k].get(j)), (k, j)
        else:
            assert gone(a.get(k)) == gone(b.get(k)), k


def test_every_field_is_persisted_or_derived():
    """A restart must not lose what a pass was in the middle of deciding, and
    a new field must not slip past it unnoticed (2026-10-12): every field of
    PhaseState, Detector and Fleet is in its to_dict, or says it is derived -
    a cache, a hook set each pass, a reading fed again by the recorder -
    with metadata=DERIVED. (The one stored under another name is listed.)"""
    import dataclasses
    renamed = {"_pending": "pending_rises"}
    for cls in (D.PhaseState, D.Detector, D.Fleet):
        keys = set(cls().to_dict())
        lost = [f.name for f in dataclasses.fields(cls) if not f.metadata.get("derived") and f.name not in renamed
                and f.name.lstrip("_") not in keys]
        assert not lost, (cls.__name__, lost)


def test_a_step_vote_mark_of_minus_infinity_survives_the_store():
    """_vote_steps' mark is -inf until a meter's first vote; the store writes
    that as null, and the next pass read None where it needs a number: "Load
    detection run failed", TypeError in bisect (Home, 2026-10-18 20:44 UTC)."""
    f = D.Fleet()
    f._step_votes_from = {"Plug": -math.inf, "Other": 12.5}
    raw = json.loads(json.dumps(f.to_dict()).replace("-Infinity", "null"))
    assert raw["step_votes_from"]["Plug"] is None
    g = D.Fleet.from_dict(raw)
    assert g._step_votes_from == {"Other": 12.5}
    g.subs["Plug"] = D.Detector()
    g._read_chans = lambda name: ["a"]
    g._vote_steps(T0)            # raised TypeError with the None


def test_a_rise_still_in_its_event_window_waits_across_a_restart():
    """A pass's end no longer forms the rises waiting in their event window,
    so they are stored: a two-phase start whose second leg is declared after
    the pass ended - and after a restart - is still one A+C event."""
    a = [(T0 + 5.0 * k, 300.0 + (3000.0 if k >= 120 else 0.0)) for k in range(200)]
    c = [(t + 2.5, w) for t, w in a]
    cut = T0 + 616.0                                   # A's rise declared, C's not yet
    det = D.Detector()
    det.process({"a": [r for r in a if r[0] < cut], "c": [r for r in c if r[0] < cut]}, now_ts=cut)
    assert det._pending and det.phases["a"].open_edges and not det.phases["c"].open_edges
    det = D.Detector.from_dict(json.loads(json.dumps(det.to_dict())))
    det.process({"a": [r for r in a if r[0] >= cut], "c": [r for r in c if r[0] >= cut]}, now_ts=T0 + 1000.0)
    legs = [st.open_edges[0].cluster for st in (det.phases["a"], det.phases["c"])]
    assert legs[0] == legs[1] and det.cluster(legs[0]).phase == "ac", [(c.id, c.phase) for c in det.edges]


def _plug_day(busy: bool, fan: float = 0.0, watts: float = 300.0, blip: float = 0.0, leg: float = 0.0):
    """A plug's one load: +``watts`` (a 15 W wobble) for 20 hours, read every
    60 s, two seconds after the grid's 5 s readings; the grid's phase B with
    it, alone or with four other loads cycling on it - 150 W ten minutes in
    thirty, 1.2 kW three minutes an hour, a 95 W cycler and 2 kW for 90 s
    every 90 minutes, none switching with the plug - and, with ``fan``, a
    load of that size switching on with the plug and off three hours later;
    with ``blip``, one switching on with the plug and off 50 s later; with
    ``leg``, a rise of that size on phase C with the plug, two hours long."""
    rnd = random.Random(3)
    on, off = T0 + 3600.0, T0 + 21 * 3600.0
    end = off + 2 * 3600.0
    warm = (T0 + 600.0, T0 + 1200.0)          # a 100 W blip in the first hour: the plug's cadence is measured
    plug, grid = [], []
    t = T0
    while t < end:
        w = watts + rnd.uniform(-15, 15) if on <= t < off else (100.0 if warm[0] <= t < warm[1] else 0.0)
        plug.append((t + 2.0, round(w, 1)))
        t += 60.0
    t = T0
    while t < end:
        w = 400.0 + rnd.uniform(-5, 5) + (watts if on <= t < off else 0.0) + (100.0 if warm[0] <= t < warm[1] else 0.0)
        w += fan if on <= t < on + 3 * 3600.0 else 0.0
        w += blip if on <= t < on + 50.0 else 0.0
        if busy:
            s = t - T0
            w += 150.0 if (s + 737.0) % 1800.0 < 600.0 else 0.0
            w += 1200.0 if (s + 1313.0) % 3600.0 < 180.0 else 0.0
            w += 95.0 if (s + 101.0) % 420.0 < 200.0 else 0.0
            w += 2000.0 if (s + 2411.0) % 5400.0 < 90.0 else 0.0
        grid.append((t, round(w, 1)))
        t += 5.0
    rows = {"b": grid}
    if leg:
        rows["c"] = [(t, round(300.0 + rnd.uniform(-5, 5) + (leg if on <= t < on + 2 * 3600.0 else 0.0), 1)) for t, _ in grid]
    return rows, plug, on, off, end


def test_a_one_device_meters_run_lasts_as_long_as_the_meter_draws():
    """Susilna's plug (Home, 09-24 to 09-27, on the busy phase A; not declared
    one device, never placed by the phase map): one +270 W
    step at 18:00 and its stop twenty hours later, read every minute, nothing
    else on the plug. The plug's own detector files the 20-hour, 6 kWh run
    every time, yet the house credited the plug 11 % of its energy: the grid's
    run of the plug's start was ended within 12-68 minutes five ways over five
    nights - a multi-close, another load's stop of its size, a joint stop
    after held drops were booked as its step-downs, an unseen stop at its
    followed 3.3 kW - while the plug still read 254-285 W. A run a meter's own
    step started is that meter's until the meter shows it stopped
    (PhaseState.owned, the converse of Fleet._meter_stop). With the phase
    quiet it always was. A young plug holds several devices, so the run is
    its own signature's (Run.parent_signature)."""
    for busy in (False, True):
        filed, fleet, on, off = _plug_fleet(busy)
        # the plug's own detector: one 20 h run of its load (a 15 W wobble step
        # before its relative noise is learned may stand as a run of its own)
        own = [s for s in fleet.subs["Plug"].recent if s["end"] - s["start"] > 10 * 3600 and s["max_w"] > 100]
        assert len(own) == 1 and abs(own[0]["kwh"] - 6.0) < 0.1, own
        run = [s for s in filed if abs(s.start - on) < 60 and "b" in s.phases]
        assert run, ("busy" if busy else "quiet", "no house run at the plug's start")
        run = max(run, key=lambda s: s.duration_s)
        assert abs(run.end - off) < 120 and abs(run.energy_wh - 6000.0) < 300 and _at(fleet, run, "Plug"), (
            "busy" if busy else "quiet", round(run.duration_s / 3600, 2), round(run.energy_wh), run.parent_signature)


def _plug_fleet(busy: bool, fan: float = 0.0, watts: float = 300.0, blip: float = 0.0, leg: float = 0.0):
    """_plug_day through a Fleet in 6-hour passes: (the house's filed
    sessions, the fleet, on, off). The plug's lag against the grid counts as
    learned - a plug's is after a day - or, alone below the grid, it would
    set no horizon and the grid would be judged before it had reported."""
    rows, plug, on, off, end = _plug_day(busy, fan, watts, blip, leg)
    fleet = D.Fleet()
    fleet.wait_cap_s = D.METER_WAIT_CAP_S
    fleet.meter_lag["Plug"] = [[2.0, 185.0]] * D.LAG_MIN_SAMPLES
    filed = _keep_filed(fleet)
    cuts = [T0 + 6 * 3600.0 * k for k in range(1, 4)]
    for (part, e), (sub, _) in zip(_passes(rows, cuts, end), _passes({"a": plug}, cuts, end)):
        fleet.process(part, {"Plug": sub}, now_ts=e)
    return filed, fleet, on, off


def test_a_run_its_one_device_meter_owns_is_booked_at_what_the_meter_drew():
    """Home's EVBox charges at 8-10 kW and tapers before it stops; a run is
    booked by its levels, one level the mean of its start and stop where they
    agree and the smaller where not, so a charge was booked at its start for
    the whole of it or at its last level (2026-10-04). Here a plug declared
    one device draws 3 kW for a quarter of an hour, then 2.7 and 2.6 kW - no
    fall of the meter's ends a run before it reads below its size less the
    pairing tolerance (Fleet._ended_by) - until two hours are up, 5.35 kWh
    against the 5.6 the mean of its start and stop gives; on a quiet phase
    and a busy one, the grid's run its rise started is booked at what its
    meter's declared levels drew."""
    for busy in (False, True):
        rnd = random.Random(5)
        on, steps, off = T0 + 3600.0, ((T0 + 4500.0, 2700.0), (T0 + 6300.0, 2600.0)), T0 + 10800.0
        end = off + 3600.0

        def draw(t):
            return 0.0 if not on <= t < off else ([w for at, w in steps if t >= at] or [3000.0])[-1]
        plug = [(T0 + 2.0 + 10.0 * k, round(draw(T0 + 2.0 + 10.0 * k) + rnd.uniform(-5, 5), 1))
                for k in range(int((end - T0) / 10.0))]
        grid, t = [], T0
        while t < end:
            w = 400.0 + rnd.uniform(-5, 5) + draw(t)
            if busy:
                s = t - T0
                w += 150.0 if (s + 737.0) % 1800.0 < 600.0 else 0.0
                w += 95.0 if (s + 101.0) % 420.0 < 200.0 else 0.0
            grid.append((t, round(w, 1)))
            t += 5.0
        fleet = D.Fleet()
        fleet.wait_cap_s = D.METER_WAIT_CAP_S
        fleet.single = {"Charger": True}
        fleet.meter_lag["Charger"] = [[2.0, 20.0]] * D.LAG_MIN_SAMPLES
        filed = _keep_filed(fleet)
        cuts = [T0 + 3600.0 * k for k in range(1, 5)]
        for (part, e), (sub, _) in zip(_passes({"b": grid}, cuts, end), _passes({"a": plug}, cuts, end)):
            fleet.process(part, {"Charger": sub}, now_ts=e)
        run = [s for s in filed if abs(s.start - on) < 30]
        got = [(round(s.start - T0), round(s.end - T0), round(s.energy_wh)) for s in filed if s.energy_wh > 100]
        assert len(run) == 1 and abs(run[0].end - off) < 60, ("busy" if busy else "quiet", got)
        assert abs(run[0].energy_wh - 5350.0) < 60, ("busy" if busy else "quiet", got)


def test_two_runs_one_device_meter_owns_at_once_share_what_it_draws():
    """Home's EVBox began a charge at +1,255 W and stepped up +2.2 kW a
    minute later, each step its meter's own, so each opened a run the meter
    owns - and each was booked at the meter's whole level: 39.6 kWh for a
    29.8 kWh charge (09-25, 2026-10-04). Every run a meter holding one device
    owns at a moment shares what it draws then, by their sizes (Fleet._meter_wh).
    Here 1 kW for ten minutes, then 3 kW for an hour and fifty: 5,667 Wh in
    all the runs of it together, on a quiet phase and a busy one."""
    for busy in (False, True):
        rnd = random.Random(7)
        on, up, off = T0 + 3600.0, T0 + 4200.0, T0 + 10800.0
        end = off + 3600.0

        def draw(t):
            return 0.0 if not on <= t < off else 3000.0 if t >= up else 1000.0
        plug = [(T0 + 2.0 + 10.0 * k, round(draw(T0 + 2.0 + 10.0 * k) + rnd.uniform(-5, 5), 1))
                for k in range(int((end - T0) / 10.0))]
        grid, t = [], T0
        while t < end:
            w = 400.0 + rnd.uniform(-5, 5) + draw(t)
            if busy:
                s = t - T0
                w += 150.0 if (s + 737.0) % 1800.0 < 600.0 else 0.0
                w += 95.0 if (s + 101.0) % 420.0 < 200.0 else 0.0
            grid.append((t, round(w, 1)))
            t += 5.0
        fleet = D.Fleet()
        fleet.wait_cap_s = D.METER_WAIT_CAP_S
        fleet.single = {"Charger": True}
        fleet.meter_lag["Charger"] = [[2.0, 20.0]] * D.LAG_MIN_SAMPLES
        filed = _keep_filed(fleet)
        cuts = [T0 + 3600.0 * k for k in range(1, 5)]
        for (part, e), (sub, _) in zip(_passes({"b": grid}, cuts, end), _passes({"a": plug}, cuts, end)):
            fleet.process(part, {"Charger": sub}, now_ts=e)
        got = [(round(s.start - T0), round(s.end - T0), round(s.energy_wh)) for s in filed if s.energy_wh > 20]
        runs = [s for s in filed if on - 30 < s.start < off and max(w for lv in s.levels.values() for _, w in lv) >= 500.0]
        assert len(runs) >= 2, ("busy" if busy else "quiet", got)             # two runs, each the meter's
        assert abs(sum(s.energy_wh for s in runs) - 5667.0) < 100, ("busy" if busy else "quiet", got)


def test_a_dip_its_one_device_meter_reads_once_is_the_charge_not_a_run_beside_it():
    """Home's EVBox dipped from 3.45 to 1.30 kW for a single 10 s reading 22
    times in one charge: under its sustain, its own detector declared
    nothing, the grid declared both edges, and every return opened a 2.15 kW
    run no meter owned, booked until the next dip closed it - ~14 kWh beside
    a 29.8 kWh charge (09-25, 2026-10-04). A grid step the one-device meter's
    readings show at the same moment, while a run it owns is on there, is
    that run's (Fleet._meter_read): the dip no stop, the return no start.
    Here 1.25 kW for a minute, then 3.45 kW for four hours with a 15 s dip
    every half hour: what the charger drew, in the runs its meter owns."""
    for busy in (False, True):
        rnd = random.Random(13)
        on, up, off = T0 + 3600.0, T0 + 3670.0, T0 + 3670.0 + 4 * 3600.0
        dips = [up + 1200.0 + 1800.0 * k for k in range(8)]
        end = off + 3600.0

        def draw(t):
            if not on <= t < off:
                return 0.0
            if t < up:
                return 1255.0
            return 1300.0 if any(d <= t < d + 15.0 for d in dips) else 3450.0
        plug = [(T0 + 2.0 + 10.0 * k, round(draw(T0 + 2.0 + 10.0 * k) + rnd.uniform(-5, 5), 1))
                for k in range(int((end - T0) / 10.0))]
        grid, t = [], T0
        while t < end:
            w = 400.0 + rnd.uniform(-5, 5) + draw(t)
            if busy:
                s = t - T0
                w += 150.0 if (s + 737.0) % 1800.0 < 600.0 else 0.0
                w += 95.0 if (s + 101.0) % 420.0 < 200.0 else 0.0
            grid.append((t, round(w, 1)))
            t += 2.5
        fleet = D.Fleet()
        fleet.wait_cap_s = D.METER_WAIT_CAP_S
        fleet.single = {"Charger": True}
        fleet.meter_lag["Charger"] = [[2.0, 20.0]] * D.LAG_MIN_SAMPLES
        filed = _keep_filed(fleet)
        cuts = [T0 + 3600.0 * k for k in range(1, 7)]
        for (part, e), (sub, _) in zip(_passes({"b": grid}, cuts, end), _passes({"a": plug}, cuts, end)):
            fleet.process(part, {"Charger": sub}, now_ts=e)
        drew = (1255.0 * (up - on) + 3450.0 * (off - up) - 2150.0 * 15.0 * len(dips)) / 3600.0
        got = [(round(s.start - T0), round(s.end - T0), round(s.energy_wh), bool(s.wh)) for s in filed if s.energy_wh > 20]
        runs = [s for s in filed if on - 30 < s.start < off and max(w for lv in s.levels.values() for _, w in lv) >= 250.0]
        assert not [s for s in runs if s.start > up + 60], ("busy" if busy else "quiet", got)    # no return a run
        assert abs(sum(s.energy_wh for s in runs) - drew) < 0.03 * drew, ("busy" if busy else "quiet", round(drew), got)


def test_a_meters_run_carries_only_a_step_it_can_hold():
    """Home 10-08 14:02: the EVBox's charge dipped 2.1 kW for 6 s; the run
    its meter owned on A was a 392 W piece of the charge's ramp, which took
    the 2.1 kW return for its own (Fleet._meter_read), and the charge's next
    66 minutes, 2.3 kWh, went into no run. The runs a meter owns there carry
    a step only up to their size together."""
    for carried, owns in ((392.0, False), (2500.0, True)):
        f = _fleet_with_meters({"Charger": 0.0})
        f.single = {"Charger": True}
        plug = f.subs["Charger"].phases["a"]
        plug.interval, plug.last_ts = 10.0, T0 + 700.0
        f.sub_rows = {"Charger": {"a": [(T0 - 100.0, 0.0), (T0 + 2.0, 2500.0), (T0 + 608.0, 400.0), (T0 + 618.0, 2500.0)]}}
        f._now = T0 + 700.0
        f.main.phases["c"].open_edges = [D._Open(since=T0, watts=carried, var=None, meter="Charger")]
        got = f._meter_read("c", T0 + 620.0, 2100.0, True, T0 + 640.0)
        assert (got == "Charger") == owns, (carried, got)


def _keep_filed(fleet):
    """Every house session the fleet files, and every one a meter's own
    signature takes (Run.parent_signature), in order, as the bench keeps them."""
    filed, file, owns = [], fleet.main._file, fleet._meter_owns

    def keep(s, *a, **kw):
        file(s, *a, **kw)
        filed.append(s)

    def owned(s, *a):
        owns(s, *a)
        filed.append(s)
    fleet.main._file, fleet._meter_owns = keep, owned
    return filed


def _at(fleet, s, meter):
    """Is the house session ``s`` placed at ``meter``: its own signature's
    (a meter holding several - Run.parent_signature), or filed in a house signature
    located there?"""
    if s.parent_signature:
        return s.parent_signature[0] == meter
    sig = fleet.main.signature_of(s)
    return bool(sig is not None and sig.locations.get(meter))


def test_a_meters_rise_inside_a_bigger_start_owns_its_part():
    """Susilna's plug (265 W) switching on in the same reading as a 48 W fan
    (Home 09-21 and 09-24 18:00: a +313 W start the plug neither owned nor
    matched, cut with the fan's stop; the owner: "a separate fan in all
    likelihood"). The plug's rise inside the bigger start takes its part - a
    20-hour run at 265 W, the plug's - and the rest is the fan's own run,
    closed by its stop three hours on. On the busy phase the fan, unmetered,
    is the detector's as ever: a 54 W rise two hours in reads as a start of
    its kind and ends it there (end_older) - its size right, its end early."""
    for busy in (False, True):
        filed, fleet, on, off = _plug_fleet(busy, fan=48.0, watts=265.0)
        at_start = sorted((s for s in filed if abs(s.start - on) < 60 and "b" in s.phases), key=lambda s: -s.duration_s)
        assert len(at_start) >= 2, ("busy" if busy else "quiet", [(round(s.duration_s / 3600, 2), round(s.energy_wh)) for s in at_start])
        run, fan = at_start[0], at_start[1]
        assert abs(run.end - off) < 120 and abs(run.energy_wh - 265.0 * 20.0) < 300 and _at(fleet, run, "Plug"), (
            "busy" if busy else "quiet", round(run.duration_s / 3600, 2), round(run.energy_wh), run.parent_signature)
        assert abs(fan.energy_wh / (fan.duration_s / 3600.0) - 48.0) < 12 and fan.duration_s >= 2 * 3600.0, (
            "busy" if busy else "quiet", round(fan.duration_s / 3600, 2), round(fan.energy_wh))
        if not busy:
            assert abs(fan.end - (on + 3 * 3600.0)) < 120 and abs(fan.energy_wh - 48.0 * 3.0) < 40, (round(fan.duration_s / 3600, 2), round(fan.energy_wh))


def test_state_round_trips_through_json_mid_session():
    a = series(400, lambda s: 3000.0 if s >= 100 else 0.0)     # still on at the end
    det = D.Detector(); det.process({"a": a}, now_ts=T0 + 400)
    assert det.active(T0 + 400) and det.active(T0 + 400)[0]["watts"] > 2800
    copy = D.Detector.from_dict(json.loads(json.dumps(det.to_dict())))
    assert copy.active(T0 + 400) == det.active(T0 + 400)
    # finish the session on the copy: one recorded session
    tail = [(T0 + 400 + i * DT, 300.0) for i in range(1, 30)]
    copy.process({"a": tail}, now_ts=T0 + 700)
    assert len(copy.recent) == 1 and copy.recent[0]["max_w"] > 2800


def test_active_reports_what_is_on_now_and_merges_phases_started_together():
    a = series(400, lambda s: 3000.0 if s >= 100 else 0.0)
    c = series(400, lambda s: 3000.0 if s >= 100 else 0.0, seed=3)
    det = D.Detector(); det.process({"a": a, "c": c}, now_ts=T0 + 400)
    act = det.active(T0 + 400)
    assert len(act) == 1 and act[0]["phases"] == "ac" and 5600 < act[0]["watts"] < 6400, act
    assert 5600 < det.unknown_power(T0 + 400) < 6400


def test_the_deepest_meter_that_saw_a_load_is_where_it_lives():
    """The Energy dashboard nests devices: the boiler sits inside the
    workshop. A load both meters saw belongs to the boiler, not the workshop."""
    parents = {"boiler": "workshop", "workshop": None}
    assert D.most_specific({"workshop": 10, "boiler": 10}, 10, parents) == "boiler"
    assert D.most_specific({"workshop": 10}, 10, parents) == "workshop"
    assert D.most_specific({}, 10, parents) == "main"
    # a meter that saw only a few of the sessions does not claim the load
    assert D.most_specific({"boiler": 2}, 10, parents) == "main"
    # two unrelated meters, neither inside the other: a stable answer, not a coin toss
    assert D.most_specific({"garage": 9, "workshop": 9}, 10, {}) == "garage"
    # a cycle in the hierarchy must not hang
    assert D.most_specific({"x": 9, "y": 9}, 10, {"x": "y", "y": "x"}) in ("x", "y")


def test_a_total_only_meter_locates_by_size_and_learns_the_phase():
    """Most device meters report one total, not three phases - they cannot
    say which phase the load is on, so only the magnitude is compared, and
    the main meter's session supplies the phase."""
    fleet = D.Fleet()
    hours = 2
    end = T0 + hours * 3600 + 200
    main_b = series(hours * 3600, kiln(period=900.0, on=240.0, watts=2100.0))
    main_a = series(hours * 3600, lambda s: 0.0, seed=8)
    # the boiler's own meter: one total channel, fed into phase "a" of its detector
    boiler = series(hours * 3600, kiln(period=900.0, on=240.0, watts=2100.0), seed=9, base=0.0, noise=5.0)
    n = len(main_b)
    for i in range(0, n, 300):
        j = min(i + 300, n)
        fleet.process({"a": main_a[i:j], "b": main_b[i:j]}, {"boiler": {"a": boiler[i:j]}},
                      now_ts=main_b[j - 1][0], single={"boiler": True})
    fleet.process({}, {}, now_ts=end)
    sig = fleet.main.signatures[0]
    assert sig.phases == "b", sig.phases          # the main meter knows the phase
    assert sig.location == "boiler", sig.locations
    assert sig.locations["boiler"] >= sig.count * 0.8, (sig.locations, sig.count)


def test_a_one_device_meter_takes_only_the_phases_its_device_uses():
    """The Hidrofor plug on phase A was credited a 308 + 421 W load on A and B:
    a meter the votes do not place yet is matched by its total and moment
    alone, and that load started with a pump run of about its size (the owner,
    2026-09-28). Its one device's phases restrict it (meter_phases); once the
    votes place it, the map does, whatever it holds."""
    fleet = D.Fleet()
    fleet.subs["pump"] = D.Detector()
    # the plug's own library is one load, so it holds one device by its shape...
    fleet.subs["pump"].signatures.append(_sig(1, 880.0, 140.0, 50, first_seen=T0, last_seen=T0))
    # ...and the house has placed the pump there, on A, often enough to know
    fleet.main.signatures.append(_sig(1, 880.0, 140.0, 45, first_seen=T0, last_seen=T0,
                                      locations={"pump": D.METER_PHASES_MIN + 20}))
    assert fleet.meter_phases() == {"pump": "a"}
    two = D.Run("ab", T0 + 1000, T0 + 1140, {"a": [(T0 + 1000, 300.0)], "b": [(T0 + 1000, 420.0)]})
    other = D.Run("b", T0 + 3000, T0 + 3140, {"b": [(T0 + 3000, 720.0)]})
    on_a = D.Run("a", T0 + 2000, T0 + 2140, {"a": [(T0 + 2000, 720.0)]})
    fleet.pending_sub["pump"] = [D.Run("a", t, t + 140, {"a": [(t, 720.0)]}) for t in (T0 + 1000, T0 + 2000, T0 + 3000)]
    pairs = fleet._session_pairs([two, on_a, other], 5.0)
    assert [mi for _, mi, _, _ in pairs] == [1], pairs          # only the load on A
    # declared as holding several devices, the plug not placed takes anything that fits again...
    fleet.single = {"pump": False}
    assert fleet.meter_phases() == {}
    assert sorted(mi for _, mi, _, _ in fleet._session_pairs([two, on_a, other], 5.0)) == [0, 1, 2]
    # ...and placed on A by its votes, only what is on A, whatever it holds
    fleet.phase_votes = {"pump": {"a": {"a": D.PHASE_MAP_MIN_VOTES}}}
    assert [mi for _, mi, _, _ in fleet._session_pairs([two, on_a, other], 5.0)] == [1]
    fleet.single, fleet.phase_votes = {}, {}
    # a young meter says nothing: two sightings on A are not yet a rule
    fleet.main.signatures[0].locations["pump"] = 2
    assert fleet.meter_phases() == {}
    # ...and a young library is not guessed to be one device at all: Kozolec's
    # Inverter meter, the whole house, looked like one three sightings in
    fleet.subs["pump"].signatures[0].count = 3
    assert not fleet.guess_one_device("pump") and not fleet.holds_one_device("pump")


def test_a_switch_is_a_meter_that_knows_only_when():
    """Home's bathroom floor mat starts and stops with its thermostat's heating:
    a session on such an on-period is credited to the thermostat, and a switch
    is deeper than the circuit meter that saw it too (2026-09-28)."""
    fleet = D.Fleet()
    sig = _sig(1, 640.0, 120.0, 1, phases="c", first_seen=T0, last_seen=T0)
    fleet.main.signatures.append(sig)
    fleet.switch_on[D.SWITCH_PREFIX + "climate.mat"] = {T0 + 1000: T0 + 1120, T0 + 5000: None}
    fleet._now = T0 + 6000.0                                                  # the clock is past the off

    def session(start, end, phase="c"):
        s = D.Run(phase, start, end, {phase: [(start, 640.0)]})
        s.signature_id = 1
        return s

    name = D.SWITCH_PREFIX + "climate.mat"
    assert fleet._switch_for(session(T0 + 1004, T0 + 1118), 6.0) == name     # on and off agree
    assert fleet._switch_for(session(T0 + 5003, T0 + 5130), 6.0) == name     # still on: the start decides
    assert fleet._switch_for(session(T0 + 1060, T0 + 1180), 6.0) is None     # started a minute late
    assert fleet._switch_for(session(T0 + 1002, T0 + 2400), 6.0) is None     # ran on long after the off
    fleet._now = T0 + 1100.0                                                  # ...an off the clock has not reached
    assert fleet._switch_for(session(T0 + 1002, T0 + 2400), 6.0) == name     # is not known yet, whatever a pass read
    fleet._now = T0 + 6000.0
    # once it has shown its phase, a load on another one is not its own
    sig.locations[name] = D.METER_PHASES_MIN
    assert fleet._switch_for(session(T0 + 1004, T0 + 1118, "a"), 6.0) is None
    assert fleet._switch_for(session(T0 + 1004, T0 + 1118, "c"), 6.0) == name
    # seen by the circuit AND switched by the thermostat: the thermostat's
    parents = {"Hiša": None}
    assert D.most_specific({"Hiša": 30, name: 28}, 30, parents) == name
    assert D.most_specific({"Hiša": 30}, 30, parents) == "Hiša"

def test_a_load_seen_downstream_is_located_there_and_one_not_seen_is_main():
    fleet = D.Fleet()
    hours = 2
    end = T0 + hours * 3600 + 200
    # the kiln (A+C, 3 kW each) is in the workshop: main AND workshop meters see it
    main_a = series(hours * 3600, kiln()); main_c = series(hours * 3600, kiln(), seed=1)
    ws_a = series(hours * 3600, kiln(), seed=4, base=50.0, noise=5.0); ws_c = series(hours * 3600, kiln(), seed=5, base=50.0, noise=5.0)
    # a 2 kW heater on B every 20 min in the house: only the main meter sees it
    heater = kiln(period=1200.0, on=300.0, watts=2000.0)
    main_b = series(hours * 3600, heater, seed=6)
    ws_b = series(hours * 3600, lambda s: 0.0, seed=7, base=50.0, noise=5.0)
    # feed in slices, like the runner
    n = len(main_a)
    for i in range(0, n, 300):
        j = min(i + 300, n)
        fleet.process({"a": main_a[i:j], "b": main_b[i:j], "c": main_c[i:j]},
                      {"workshop": {"a": ws_a[i:j], "b": ws_b[i:j], "c": ws_c[i:j]}}, now_ts=main_a[j - 1][0])
    fleet.process({}, {}, now_ts=end)
    sigs = {s.phases: s for s in fleet.main.signatures}
    assert set(sigs) == {"ac", "b"}, [s.describe(None) for s in fleet.main.signatures]
    assert sigs["ac"].location == "workshop", sigs["ac"].locations
    assert sigs["b"].location == "main" and sigs["b"].locations == {}
    assert sigs["ac"].locations["workshop"] >= sigs["ac"].count * 0.8, (sigs["ac"].locations, sigs["ac"].count)
    # the workshop meter has its own, finer library: just the kiln
    assert len(fleet.subs["workshop"].signatures) == 1
    # and all of it survives a restart
    copy = D.Fleet.from_dict(json.loads(json.dumps(fleet.to_dict())))
    assert {s.phases: s.location for s in copy.main.signatures} == {"ac": "workshop", "b": "main"}


def test_signatures_sharing_a_name_are_one_device():
    det = D.Detector()
    a = series(1200, lambda s: 2000.0 if 100 <= s < 300 else 0.0)
    det.process({"a": a}, now_ts=T0 + 1300)
    b = series(1200, lambda s: 900.0 if 600 <= s < 800 else 0.0, seed=9)
    det.process({"b": b}, now_ts=T0 + 1300)
    assert len(det.signatures) == 2
    for sig in det.signatures:
        assert det.rename(sig.id, "Hob")
    assert det.names() == {"Hob": sorted(s.id for s in det.signatures)}
    assert det.rename(999, "Nope") is False
    # clearing a name forgets it
    det.rename(det.signatures[0].id, "  ")
    assert list(det.names()) == ["Hob"] and len(det.names()["Hob"]) == 1


def test_active_power_is_reported_per_name():
    det = D.Detector()
    a = series(400, lambda s: 3000.0 if s >= 100 else 0.0)
    det.process({"a": a}, now_ts=T0 + 400)
    # name the signature the open session will be guessed as
    sig_id = det._guess("a", 3000.0, 250.0)
    assert sig_id is None      # nothing closed yet, so no signature to guess
    tail = [(T0 + 400 + i * 5.0, 300.0) for i in range(1, 30)]
    det.process({"a": tail}, now_ts=T0 + 700)
    det.rename(det.signatures[0].id, "Kettle")
    again = series(400, lambda s: 3000.0 if s >= 100 else 0.0, seed=2)
    det2 = D.Detector.from_dict(json.loads(json.dumps(det.to_dict())))
    det2.process({"a": [(T0 + 800 + t, w) for t, w in [(x - T0, y) for x, y in again]]}, now_ts=T0 + 1200)
    assert D.Fleet(main=det2).active_by_name(T0 + 1200).get("Kettle", 0) > 2800, det2.active(T0 + 1200)


def test_levels_of_one_device_are_suggested_and_a_shared_load_is_not():
    """Two sizes on one phase that never run together look like settings of
    one appliance; two that overlap in time cannot be."""
    det = D.Detector()
    # a hob: 1 kW then 2 kW, never at once
    a = series(2400, lambda s: 1000.0 if 100 <= s < 400 else (2000.0 if 900 <= s < 1200 else 0.0))
    det.process({"a": a}, now_ts=T0 + 2500)
    a2 = series(2400, lambda s: 1000.0 if 100 <= s < 400 else (2000.0 if 900 <= s < 1200 else 0.0), seed=11)
    det.process({"a": [(t + 3000, w) for t, w in a2]}, now_ts=T0 + 5600)
    groups = D.suggest_levels(det.signatures, det.recent)
    assert len(groups) == 1 and len(groups[0]) == 2, (groups, [s.describe(None) for s in det.signatures])
    # Naming one does NOT settle the others, which is what the old rule
    # assumed: it dropped named signatures from the pool, so naming a single
    # setting switched off the suggestion that would have found the rest of
    # the same machine. The owner's kiln is split across sixteen balanced A+C
    # signatures holding 304 sessions and only 205 of them are named, and it
    # was never offered a single one of them (2026-09-22).
    det.rename(groups[0][0], "Hob")
    still = D.suggest_levels(det.signatures, det.recent)
    assert still == groups, "the unnamed sibling still belongs with the Hob"
    # ...and once every member is named there is nothing left to suggest
    det.rename(groups[0][1], "Hob")
    assert D.suggest_levels(det.signatures, det.recent) == []


def test_overlapping_signatures_are_never_suggested_as_one_device():
    sigs = [
        _sig(1, 1000.0, 300, first_seen=T0, last_seen=T0),
        _sig(2, 2000.0, 300, first_seen=T0, last_seen=T0),
    ]
    recent = [{"signature": 1, "start": T0, "end": T0 + 600}, {"signature": 2, "start": T0 + 300, "end": T0 + 900}]
    assert D.suggest_levels(sigs, recent) == []
    apart = [{"signature": 1, "start": T0, "end": T0 + 300}, {"signature": 2, "start": T0 + 600, "end": T0 + 900}]
    assert D.suggest_levels(sigs, apart) == [[1, 2]]


def test_a_load_that_starts_while_another_runs_still_closes():
    """The regression that made the whole library useless: the phase never
    returned to its idle floor, so ONE session ran for 155 hours and 778 kWh
    at the owner's home meter. Each load must close on its own step down."""
    def two(s):
        w = 0.0
        if 300 <= s < 3900:      # the fridge-sized one, an hour
            w += 900.0
        if 900 <= s < 1500:      # a kettle-sized one inside it, ten minutes
            w += 2000.0
        return w
    det = D.Detector()
    det.process({"a": series(4500, two)}, now_ts=T0 + 4600)
    got = sorted(round(sum(x.power.values())) for x in det.signatures)
    assert len(det.signatures) == 2, [(x.id, x.power, x.duration_s) for x in det.signatures]
    assert 1900 < got[1] < 2100 and 850 < got[0] < 950, got
    durations = sorted(round(x.duration_s) for x in det.signatures)
    assert 560 < durations[0] < 640 and 3500 < durations[1] < 3700, durations


def test_a_slow_ramp_is_not_a_load():
    """Sunrise on a grid meter moves it by kilowatts over an hour. It has to
    move the FLOOR, not book itself as four loads - which is where the
    negative-power signatures in the home dump came from."""
    det = D.Detector()
    det.process({"a": series(7200, lambda s: -2500.0 * min(1.0, s / 5400.0), base=3000.0)},
                now_ts=T0 + 7300)
    assert det.signatures == [], [(x.power, x.duration_s) for x in det.signatures]


def test_a_real_load_is_still_found_while_the_sun_comes_up():
    det = D.Detector()
    def ramp_and_load(s):
        w = -2500.0 * min(1.0, s / 5400.0)
        return w + (2000.0 if 3000 <= s < 3600 else 0.0)
    det.process({"a": series(7200, ramp_and_load, base=3000.0)}, now_ts=T0 + 7300)
    assert len(det.signatures) == 1, [(x.power, x.duration_s) for x in det.signatures]
    sig = det.signatures[0]
    assert 1850 < sum(sig.power.values()) < 2150, sig.power
    assert 550 < sig.duration_s < 650, sig.duration_s


def test_the_power_factor_is_the_loads_own_not_the_meters():
    """A motor and a heater of the same size are the same watts. What tells
    them apart is how far the REACTIVE power moved with them - and the
    meter's own factor, dominated by whatever else is running, does not."""
    def run(var_share):
        det = D.Detector()
        rows = series(2400, lambda s: 2000.0 if 600 <= s < 1800 else 0.0)
        # the site already runs a big reactive load, so the meter's own
        # factor is poor throughout and says nothing about this one
        q = {ts: 1500.0 + var_share * max(0.0, w - 400.0) for ts, w in rows}
        det.process({"a": rows}, {"a": q}, now_ts=T0 + 2500)
        assert len(det.signatures) == 1, det.signatures
        return det.signatures[0].pf
    heater = run(0.0)
    motor = run(0.75)
    assert heater is not None and heater > 0.98, heater
    assert motor is not None and 0.75 < motor < 0.85, motor


def test_a_stop_we_never_saw_start_is_dropped():
    """Half a load is not a load. It must not be pinned on something else."""
    det = D.Detector()
    rows = series(1200, lambda s: 0.0)
    det.process({"a": rows}, now_ts=T0 + 1300)
    tail = [(T0 + 1200 + i * DT, 300.0 + (2000.0 if i < 60 else 0.0)) for i in range(120)]
    det.process({"a": tail}, now_ts=T0 + 1900)
    # the 2 kW start was never seen (the window opens mid-load), so its stop
    # closes nothing and no phantom signature appears
    assert all(sum(x.power.values()) > 1500 for x in det.signatures), [x.power for x in det.signatures]


def test_a_cloud_is_the_sun_not_a_load():
    """A 6 kW array dropping into cloud lifts the grid meter by 2 kW on each
    phase, which is exactly the shape of a load switching on - and of one
    switching off again when it clears."""
    def pv(s):
        return 200.0 if 1800 <= s < 2400 else 2000.0        # this phase's share
    n = int(3600 / DT)
    grid = [(T0 + i * DT, 400.0 - pv(i * DT)) for i in range(n)]
    own = {T0 + i * DT: pv(i * DT) for i in range(n)}
    det = D.Detector()
    det.process({"a": grid}, None, T0 + 3700, {"a": own})
    assert det.signatures == [], [(x.power, x.duration_s) for x in det.signatures]

    # the same, with only the inverter's TOTAL to go on: a third each
    total = {T0 + i * DT: 3 * pv(i * DT) for i in range(n)}
    det2 = D.Detector()
    det2.process({"a": grid}, None, T0 + 3700, {"a": total})
    assert det2.signatures == [], [(x.power, x.duration_s) for x in det2.signatures]

    # and without the array to explain it, the cloud IS filed as a load -
    # which is what the whole test is about
    blind = D.Detector()
    blind.process({"a": grid}, now_ts=T0 + 3700)
    assert blind.signatures, "a cloud with no PV series to explain it should still be detected"


def test_a_load_is_still_found_under_a_steady_sun():
    n = int(3600 / DT)
    grid = [(T0 + i * DT, 400.0 - 2000.0 + (2200.0 if 600 <= i * DT < 1800 else 0.0)) for i in range(n)]
    own = {T0 + i * DT: 2000.0 for i in range(n)}
    det = D.Detector()
    det.process({"a": grid}, None, T0 + 3700, {"a": own})
    assert len(det.signatures) == 1, [(x.power, x.duration_s) for x in det.signatures]
    assert 2050 < sum(det.signatures[0].power.values()) < 2350, det.signatures[0].power


def _repeat(durations, watts=2000.0, gap=600.0):
    """A load that runs for each of ``durations`` in turn, ``gap`` apart."""
    rows, t = [], T0
    for _ in range(24):                      # seed the floor first
        rows.append((t, 300.0)); t += DT
    for d in durations:
        for _ in range(int(d / DT)):
            rows.append((t, 300.0 + watts)); t += DT
        for _ in range(int(gap / DT)):
            rows.append((t, 300.0)); t += DT
    return rows, t


def test_a_load_seen_once_has_no_evidence():
    det = D.Detector()
    rows, end = _repeat([600])
    det.process({"a": rows}, now_ts=end + 100)
    assert len(det.signatures) == 1
    assert det.signatures[0].evidence == 0.0, det.signatures[0].evidence


def test_evidence_rises_with_repetition_and_falls_with_scatter():
    """Five identical runs are a device. Five runs of wildly different
    length that happen to share a power are the detector pairing edges."""
    tight = D.Detector()
    rows, end = _repeat([600] * 6)
    tight.process({"a": rows}, now_ts=end + 100)
    loose = D.Detector()
    rows, end = _repeat([300, 900, 420, 1200, 600, 240])
    loose.process({"a": rows}, now_ts=end + 100)
    assert len(tight.signatures) == 1 and len(loose.signatures) == 1, (tight.signatures, loose.signatures)
    a, b = tight.signatures[0], loose.signatures[0]
    assert a.count == b.count == 6, (a.count, b.count)
    assert a.evidence > 0.9, a.evidence
    assert b.evidence < a.evidence - 0.1, (a.evidence, b.evidence)


def test_a_repeating_load_is_called_regular_and_a_sporadic_one_is_not():
    clockwork = D.Detector()
    rows, end = _repeat([600] * 6, gap=600.0)
    clockwork.process({"a": rows}, now_ts=end + 100)
    assert clockwork.signatures[0].regular, clockwork.signatures[0].interval_mad


def test_where_a_load_is_said_by_exclusion():
    parents = {"Hiša": None, "Blaževa Soba": "Hiša", "Mansarda": None, "Vtičnice - pisarna": "Mansarda"}
    # the house meter saw it, the rooms inside it did not
    seen = {"Hiša": 9}
    assert D.describe_location(seen, 10, parents, "b") == "in Hiša, outside Blaževa Soba"
    assert D.location_confidence(seen, 10, parents) == 0.9
    # the room saw it too: the deepest meter wins and there is nothing to exclude
    both = {"Hiša": 9, "Blaževa Soba": 8}
    assert D.describe_location(both, 10, parents) == "in Blaževa Soba"
    # nothing downstream saw it: the phase is the only clue left
    assert D.describe_location({}, 9, parents, "ac") == "under no meter, on phase A+C"
    assert D.location_confidence({}, 9, parents) == 1.0
    # seen sometimes, not enough to own it
    part = D.describe_location({"Hiša": 2}, 9, parents, "b")
    assert part.startswith("under no meter, though Hiša saw it 2 of 9"), part
    assert D.location_confidence({"Hiša": 2}, 9, parents) < 0.8


def test_the_description_offers_a_guess_when_the_factor_allows_one():
    det = D.Detector()
    rows = series(2400, lambda s: 2000.0 if 600 <= s < 1500 else 0.0)
    q = {ts: 0.0 for ts, _ in rows}                      # purely resistive
    det.process({"a": rows}, {"a": q}, now_ts=T0 + 2500)
    words = det.signatures[0].describe(None)
    assert "maybe a heating element" in words, words
    assert "seen 1 times" in words, words


def test_two_loads_stopping_together_both_close():
    """The oven and its fan go at once, leaving one step too big for either
    alone. Dropping it left both 'running' for the rest of the day."""
    def two(s):
        w = 0.0
        if 300 <= s < 1800:
            w += 2000.0
        if 600 <= s < 1800:
            w += 900.0
        return w
    det = D.Detector()
    det.process({"a": series(2400, two)}, now_ts=T0 + 2500)
    got = sorted(round(sum(x.power.values())) for x in det.signatures)
    assert len(det.signatures) == 2, [(x.power, x.duration_s) for x in det.signatures]
    assert 1900 < got[1] < 2100 and 850 < got[0] < 950, got
    assert det.phases["a"].open_edges == [], det.phases["a"].open_edges


def test_back_at_the_idle_floor_nothing_is_left_running():
    """A stop that was never matched must not leave a load 'on' for hours -
    at the owner's home meter eight of them had piled up on phase A, adding to
    an unknown-load figure of nearly 12 kW."""
    det = D.Detector()
    rows = series(600, lambda s: 0.0)
    det.process({"a": rows}, now_ts=T0 + 700)
    # a start we see, then a stop hidden inside a much larger simultaneous
    # change, then a long quiet stretch at the floor
    t = T0 + 600
    tail = []
    for i in range(240):
        tail.append((t, 300.0 + 1500.0)); t += DT
    for i in range(240):
        tail.append((t, 300.0)); t += DT
    det.process({"a": tail}, now_ts=t)
    assert det.phases["a"].open_edges == [], det.phases["a"].open_edges
    assert det.active(t) == [], det.active(t)


def test_a_named_load_is_never_the_first_thing_evicted():
    """Eviction tiers, tested together on a full library. NAMED survives
    however stale. ESTABLISHED survives a pause - the kiln reached 299 runs
    and was thrown out during twelve quiet hours by junk seen since. YOUNG
    survives long enough to be seen twice - a singleton is always the weakest
    and used to be pruned in the call that created it. The rest go weakest
    first."""
    hour = 3600.0
    cap = D.MAX_SIGNATURES
    det = D.Detector()
    det.signatures = [
        _sig(i, 100.0 + i, 60.0, 1, last_seen=i * hour)
        for i in range(cap + 5)
    ]
    det.signatures[0].name = "Boiler"                # named, stalest of all, count 1
    det.signatures[1].count = 190                    # the kiln: paused 20 hours, evidence high
    det.signatures[1].last_seen = (cap + 4 - 20) * hour
    newest = cap + 4                                 # a singleton seen just now
    det._prune(now=newest * hour)
    kept = {s.id for s in det.signatures}
    assert len(det.signatures) == cap
    assert 0 in kept, "a named load was evicted"
    assert 1 in kept, "a well-evidenced load lost its place by pausing"
    assert newest in kept, "a signature seen once was pruned before it could be seen twice"
    # what went: the stalest singletons that are past the grace period
    assert kept.isdisjoint({2, 3, 4, 5, 6}), sorted(set(range(2, 7)) & kept)

    # a library FULL of established loads still admits a newcomer - over the cap
    det2 = D.Detector()
    det2.signatures = [
        _sig(i, 100.0 + i, 60.0, 20, last_seen=i * hour)
        for i in range(cap)
    ]
    now = cap * hour
    det2.signatures.append(_sig(9999, 5000.0, 40.0, 1, first_seen=now, last_seen=now))
    det2._prune(now=now)
    assert any(s.id == 9999 for s in det2.signatures)
    assert len(det2.signatures) == cap + 1, "established loads are not traded for a cap"

    # an appliance that has genuinely left the house is fair game again -
    # but the horizon is over a year, because a load that runs twice a year
    # is rare, not stale (see the twice-a-year test)
    day = 86400.0
    det3 = D.Detector()
    det3.signatures = [
        _sig(i, 100.0 + i, 60.0, 20, last_seen=500 * day + i)
        for i in range(cap + 1)
    ]
    det3.signatures[0].last_seen = 0.0               # gone for five hundred days
    det3._prune(now=500 * day + cap)
    assert not any(s.id == 0 for s in det3.signatures)
    assert D.ESTABLISHED_HORIZON_S > 365 * day, "a yearly load must survive its own year"


def test_a_start_is_known_by_its_own_record_and_every_pair_it_starts():
    """An open run has a start and no stop yet: its name and its meter's homes
    come from where the runs of its start went - filed as the start alone
    before a partner was learned and as each pair it starts since (pair_home;
    start_home is gone, 2026-10-29)."""
    det = D.Detector()
    pair = lambda start, stop: str(-(start * D.PAIR_ID_BASE + stop))   # noqa: E731
    det.pair_home = {"7": {1: 5.0}, pair(7, 9): {2: 3.0, 1: 1.0}, pair(8, 9): {3: 4.0}}
    assert det.start_homes(7) == {1: 6.0, 2: 3.0} and det.start_homes(8) == {3: 4.0} and det.start_homes(None) == {}


def test_a_signature_evicted_is_not_where_its_device_files_next():
    """_prune left the signature lookup holding what it had just evicted, so
    a device whose home it was filed its next run into a signature no longer
    in the library - one signature_of then finds nowhere (the pass audit,
    2026-10-02)."""
    hour = 3600.0
    cap = D.MAX_SIGNATURES
    det = D.Detector()
    det.signatures = [_sig(i, 100.0 + i, 60.0, 1, last_seen=i * hour) for i in range(cap + 5)]
    det.pair_home = {"7": {2: 5.0}}                 # cluster 7's runs went to signature 2, the stalest
    assert det._device_signature(7, None, (), "a").id == 2
    det._prune(now=(cap + 4) * hour)
    assert all(s.id != 2 for s in det.signatures)
    assert det._device_signature(7, None, (), "a") is None


def test_a_run_kept_out_of_a_signature_is_not_merged_back_into_it():
    """Kozolec 09-28 17:05: a 1 kW run of the well pump's start cluster while
    the pump's plug held at 0 W was kept out of the pump's signature (the plug
    did not start it) and founded one of its own - which the device merge,
    now after every filing, folded straight back in: the pump's signature
    fell under half its own and its 1.5 kWh went unowned. A signature born of
    a run kept out of others is never merged into them."""
    det = D.Detector()
    det.signatures = [_sig(1, 1000.0, 300.0, 12, first_seen=T0, last_seen=T0)]
    det.next_id = 2
    det.pair_home = {"7": {1: 12.0}}
    run = D.Run(phases="a", start=T0 + 3600.0, end=T0 + 4560.0, levels={"a": [(T0 + 3600.0, 1000.0)]}, pair=(7, None))
    det._file(run, avoid=[1])
    assert run.signature_id == 2 and det._sig(2).apart == [1]
    assert det._merge_devices() == 0 and len(det.signatures) == 2
    back = D.Signature.from_dict(json.loads(json.dumps(det._sig(2).to_dict())))
    assert back.apart == [1]


def test_a_run_of_a_shared_start_cluster_joins_the_devices_family_only_when_it_fits_a_member():
    """Home's Hidrofor (827 W, 110 s) shares a start cluster with a 1 kW heater's starts: the heater's runs founded
    signatures that adopted the pump's family, so the name covered their energy (2026-10-27: 20.2 % impure by
    family against 10.4 per signature, 12.6 once a run that fits no member founds a family of its own)."""
    det = D.Detector()
    det.signatures = [_sig(1, 827.0, 110.0, 12, first_seen=T0, last_seen=T0, family=1)]
    det.next_id, det.next_family = 2, 2
    det.pair_home = {"7": {1: 12.0}}
    heater = D.Run(phases="a", start=T0 + 3600.0, end=T0 + 3789.0, levels={"a": [(T0 + 3600.0, 1026.0)]}, pair=(7, None))
    det._file(heater, avoid=[1])               # kept out of the pump's signature: its plug did not start it
    assert heater.signature_id == 2 and det._sig(2).family != 1
    like = D.Run(phases="a", start=T0 + 7200.0, end=T0 + 7310.0, levels={"a": [(T0 + 7200.0, 840.0)]}, pair=(7, None))
    det._file(like, avoid=[1, 2])
    assert like.signature_id == 3 and det._sig(3).family == 1


def test_a_reading_with_generation_in_it_is_the_one_that_goes_negative():
    """Physics, not statistics. I tried correlating the two series' changes
    first and the owner's own data threw it out: on a day when the grid meter
    exported 4.5 kW the correlation called the array absent from it."""
    import random
    rnd = random.Random(3)
    n = 400
    house = [400.0 + rnd.uniform(-20, 20) for _ in range(n)]
    pv = [1000.0 + 900.0 * (i % 40) / 40.0 for i in range(n)]
    grid = [(T0 + i * DT, house[i] - pv[i]) for i in range(n)]      # exports all day
    assert D.carries_generation(grid) is True
    assert D.carries_generation([(T0 + i * DT, house[i]) for i in range(n)]) is False
    # an off-grid AC input sitting at zero carries nothing either way
    assert D.carries_generation([(T0 + i * DT, 0.0) for i in range(n)]) is False
    # one dip is noise, not an export
    dip = [(T0 + i * DT, -900.0 if i == 7 else house[i]) for i in range(n)]
    assert D.carries_generation(dip) is False
    assert D.carries_generation([(T0, 5.0)]) is None


def test_a_short_pass_cannot_switch_the_house_floor_guard_off():
    """A live pass reads one minute - some 25 readings - and judged alone,
    one dip is 4 % of it, eight times the share that marks an export. Read
    that way, the guard that keeps the house's floor at or above zero was
    off on every live pass, and one cloud edge left Home's phase B floor at
    -1483 W with every later load measured from it (2026-09-25). So a pass
    that short cannot tell, and the verdict it cannot give is kept - across
    a restart too."""
    minute = [(T0 + i * DT, 400.0) for i in range(25)]
    assert D.carries_generation(minute) is None
    dipped = [(t, -900.0 if i == 3 else w) for i, (t, w) in enumerate(minute * 4)]
    assert len(dipped) < D.GENERATION_MIN_SAMPLES
    assert D.carries_generation(dipped) is None
    kept = D.PhaseState.from_dict(D.PhaseState(floor_zero=True).to_dict())
    assert kept.floor_zero is True
    assert D.PhaseState.from_dict({"baseline": 120.0}).floor_zero is False


def test_a_glitch_below_zero_cannot_drag_the_floor_down():
    """A consumption reading cannot go below zero, and the owner's template dips
    there for 49 samples out of 20764 when its own inputs do not line up.
    One of those dragged the idle floor to -569 W and every step after it
    was measured from nonsense (2026-09-18)."""
    det = D.Detector()
    det.phases["a"].floor_zero = True
    rows, t = [], T0
    for _ in range(40):
        rows.append((t, 300.0)); t += DT
    for _ in range(4):                       # the glitch, sustained enough to count
        rows.append((t, -1700.0)); t += DT
    for _ in range(200):
        rows.append((t, 300.0)); t += DT
    for _ in range(120):
        rows.append((t, 2300.0)); t += DT
    for _ in range(80):
        rows.append((t, 300.0)); t += DT
    det.process({"a": rows}, now_ts=t)
    assert det.phases["a"].baseline >= 0.0, det.phases["a"].baseline
    got = [round(sum(x.power.values())) for x in det.signatures]
    assert any(1900 < w < 2100 for w in got), got


def test_two_loads_starting_together_are_not_one_two_phase_load():
    """A real multi-phase load is balanced by design. Coinciding in time was
    the only test, so a 2 kW load on A married a 163 W blip on C and the pair
    was filed as one 2.2 kW two-phase load - a phantom invented, and the
    session stolen from the single-phase load it belonged to."""
    det = D.Detector()
    a = series(1200, lambda s: 2000.0 if 200 <= s < 260 else 0.0)
    c = series(1200, lambda s: 163.0 if 200 <= s < 260 else 0.0, seed=4, noise=5.0)
    det.process({"a": a, "c": c}, now_ts=T0 + 1300)
    assert all(s.phases != "ac" for s in det.signatures), [s.phases for s in det.signatures]
    assert any(s.phases == "a" and abs(s.power["a"] - 2000) < 200 for s in det.signatures)
    # a genuinely balanced pair still groups
    det2 = D.Detector()
    a2 = series(1200, lambda s: 2000.0 if 200 <= s < 260 else 0.0)
    c2 = series(1200, lambda s: 1900.0 if 200 <= s < 260 else 0.0, seed=5)
    det2.process({"a": a2, "c": c2}, now_ts=T0 + 1300)
    assert any(s.phases == "ac" for s in det2.signatures), [s.phases for s in det2.signatures]


def test_how_well_a_run_was_measured_decides_its_weight_and_its_tolerance():
    """Eight samples of a 43-second run and forty-three of the same run are
    not equally good evidence of its power."""
    coarse = D.Run(phases="a", start=0.0, end=40.0, levels={"a": [(0.0, 3000.0)]}, samples=4)
    fine = D.Run(phases="a", start=0.0, end=40.0, levels={"a": [(0.0, 3000.0)]}, samples=40)
    assert coarse.confidence < fine.confidence
    assert D.Run(phases="a", start=0.0, end=40.0, levels={"a": [(0.0, 3000.0)]}).confidence == 1.0

    # and it pulls the running mean less far
    lax = _sig(2, 3000.0, 40.0, 10)
    strict = _sig(3, 3000.0, 40.0, 10)
    import datetime as _dt
    lax.absorb(D.Run(phases="a", start=0.0, end=40.0, levels={"a": [(0.0, 2000.0)]}, samples=4), _dt.timezone.utc)
    strict.absorb(D.Run(phases="a", start=0.0, end=40.0, levels={"a": [(0.0, 2000.0)]}, samples=40), _dt.timezone.utc)
    assert lax.power["a"] > strict.power["a"], (lax.power, strict.power)


def test_a_named_load_that_changed_points_at_what_replaced_it():
    """A named signature is never evicted, so when its load changes in a step
    the name stays on a fingerprint nothing matches while the successor sits
    unnamed. The hint is recorded; nothing is renamed without the user."""
    day = 86400.0
    det = D.Detector()
    old = _sig(1, 3000.0, 40.0, 200, pf=0.95, name="Kiln")
    old.last_seen = 0.0
    new = _sig(2, 2400.0, 42.0, 60, pf=0.95)
    new.last_seen = 9 * day
    unrelated = _sig(3, 300.0, 42.0, 60, pf=0.95)      # nothing like it
    unrelated.last_seen = 9 * day
    det.signatures = [old, new, unrelated]
    det._link_successors(now=10 * day)
    assert old.successor_id == 2, old.successor_id
    assert new.successor_id is None and unrelated.successor_id is None
    # still being seen: no successor is looked for
    old.successor_id, old.last_seen = None, 10 * day - 3600.0
    det._link_successors(now=10 * day)
    assert old.successor_id is None
    # a thin candidate is not offered
    old.last_seen = 0.0
    new.count = 2
    det._link_successors(now=10 * day)
    assert old.successor_id is None


def test_a_mature_signature_still_follows_a_load_that_changes():
    """Without a cap the running mean ossifies: at 300 sightings a new one
    moves it by 0.3%, so a load that genuinely changes can never drag its own
    fingerprint across - the new sessions stop matching and found a sibling
    first. The count keeps counting; only the WEIGHT is bounded."""
    import datetime as _dt
    tz = _dt.timezone.utc
    run = lambda w: D.Run(phases="a", start=0.0, end=40.0, levels={"a": [(0.0, w)]}, samples=40)
    young, mature = _sig(1, 3000.0, 40.0, 10), _sig(2, 3000.0, 40.0, 300)
    before = mature.power["a"]
    young.absorb(run(2000.0), tz)
    mature.absorb(run(2000.0), tz)
    # the young one moves further - convergence is still the point
    assert (3000.0 - young.power["a"]) > (3000.0 - mature.power["a"])
    # but the mature one is not frozen: about 1% of the gap, not 0.3%
    moved = (before - mature.power["a"]) / (before - 2000.0)
    assert 0.008 < moved < 0.012, moved
    assert mature.count == 301, "the history is kept, only the weight is capped"

    # over fifty runs at the new power it gets most of the way there
    sig = _sig(3, 3000.0, 40.0, 300)
    for _ in range(50):
        sig.absorb(run(2000.0), tz)
    assert sig.power["a"] < 2650.0, sig.power["a"]


def test_a_load_that_runs_twice_a_year_is_rare_not_stale():
    """A kiln fired twice a year, or a pump that only runs in a wet spring,
    has a strong signature and deserves to be measured and tracked as well as
    the kettle. Evidence is the gate; age is only the backstop for an
    appliance that has genuinely left the house."""
    day = 86400.0
    cap = D.MAX_SIGNATURES
    det = D.Detector()
    # a library already full of well-evidenced everyday loads
    det.signatures = [
        _sig(i, 100.0 + i, 60.0, 30, last_seen=400 * day + i)
        for i in range(cap)
    ]
    rare = _sig(9999, 7000.0, 3600.0, 8, pf=0.99, last_seen=400 * day - 180 * day)
    rare.power_mad, rare.duration_mad = 40.0, 20.0        # tight: real evidence
    det.signatures.append(rare)
    assert rare.evidence >= D.ESTABLISHED_EVIDENCE
    det._prune(now=400 * day + cap)
    assert any(s.id == 9999 for s in det.signatures), "a strong twice-a-year load was evicted"

    # and it is not offered a successor merely for being rare
    rare.name = "Kiln"
    rare.interval_s = 180 * day
    other = _sig(1234, 7000.0, 3600.0, 20, pf=0.99)
    other.last_seen = 400 * day
    det.signatures = [rare, other]
    det._link_successors(now=400 * day)
    assert rare.successor_id is None, "a rare load was declared replaced for running rarely"

    # a load that runs every five minutes and has not for a week IS quiet
    fast = _sig(5, 2000.0, 60.0, 200, pf=0.95, name="Pump")
    fast.interval_s, fast.last_seen = 300.0, 0.0
    heir = _sig(6, 2100.0, 60.0, 20, pf=0.95)
    heir.last_seen = 8 * day
    det.signatures = [fast, heir]
    det._link_successors(now=8 * day)
    assert fast.successor_id == 6


def test_a_named_load_publishes_a_meter_that_only_ever_goes_up():
    """Naming a load gives it a device and a power reading; without an energy
    reading it cannot appear on the Energy dashboard, which is where anyone
    would go to ask what the thing costs. The figure has to be sound AS A
    METER, not merely plausible - a total that dips reads as a meter reset."""
    import datetime as _dt
    tz = _dt.timezone.utc
    det = D.Detector()
    sig = _sig(1, 2000.0, 60.0, 0, last_seen=0.0, name="Boiler")
    det.signatures = [sig]
    run = lambda i: D.Run(phases="a", start=T0 + i * 3600, end=T0 + i * 3600 + 1800.0,
                              levels={"a": [(T0 + i * 3600, 2000.0)]}, samples=20)
    seen = []
    for i in range(5):                       # half an hour at 2 kW is 1 kWh
        sig.absorb(run(i), tz)
        seen.append(det.energy_by_name()["Boiler"] / 1000.0)
    assert [round(x, 3) for x in seen] == [1.0, 2.0, 3.0, 4.0, 5.0], seen
    assert seen == sorted(seen)

    # two signatures under one name are one device, so their energy adds
    twin = _sig(2, 2000.0, 60.0, 3, last_seen=0.0, name="Boiler")
    twin.hour_wh = [500.0] + [0.0] * 23
    det.signatures.append(twin)
    assert round(det.energy_by_name()["Boiler"] / 1000.0, 3) == 5.5

    # and a merge carries the history across rather than losing half of it
    sig.swallow(twin)
    det.signatures = [sig]
    assert round(det.energy_by_name()["Boiler"] / 1000.0, 3) == 5.5
    assert sig.name == "Boiler"

    # an unnamed signature contributes nothing to anyone's meter
    det.signatures.append(_sig(3, 9.0, 1.0, 1, last_seen=0.0))
    assert set(det.energy_by_name()) == {"Boiler"}


def test_stored_state_carries_only_the_precision_it_has():
    """A watt-hour written as 276.1825572400394 spends fifteen digits on a
    figure the energy meter publishes to two decimals of a kilowatt-hour, and
    the whole library is rewritten every pass. Trimming must not cost the
    energy total anything that matters: a total that steps DOWN reads as a
    meter reset."""
    import json
    det = D.Detector()
    sig = _sig(1, 2000.123456789, 61.987654321, 40, pf=0.9543210987, first_seen=1_789_000_000.25,
               last_seen=1_789_050_000.75, name="Boiler")
    sig.hour_wh = [276.1825572400394] * 24
    sig.day_wh = [946.9116248229 for _ in range(7)]
    det.signatures = [sig]
    blob = json.dumps(det.to_dict())
    copy = D.Detector.from_dict(json.loads(blob))
    back = copy.signatures[0]
    assert "276.1825572400394" not in blob, "full float precision still stored"
    assert abs(back.energy_wh - sig.energy_wh) < 2.5, (back.energy_wh, sig.energy_wh)
    assert abs(back.energy_wh - sig.energy_wh) / sig.energy_wh < 1e-4
    # timestamps keep their precision - a rounded epoch second is a second lost
    assert back.first_seen == sig.first_seen and back.last_seen == sig.last_seen
    assert back.name == "Boiler" and back.count == 40
    assert abs(back.power["a"] - 2000.1) < 0.05 and abs((back.pf or 0) - 0.9543) < 1e-4


def test_a_name_can_be_moved_to_the_load_that_replaced_it():
    """The hint on its own changes nothing. Moving the name leaves the old
    fingerprint its history and its energy - a kiln that drew 5.9 kW really
    did draw it - but stops it answering to a name nothing matches."""
    day = 86400.0
    det = D.Detector()
    old = _sig(1, 3000.0, 40.0, 200, pf=0.95, name="Kiln")
    old.last_seen, old.hour_wh = 0.0, [1000.0] + [0.0] * 23
    new = _sig(2, 2400.0, 42.0, 60, pf=0.95)
    new.last_seen, new.hour_wh = 9 * day, [250.0] + [0.0] * 23
    det.signatures = [old, new]
    det._link_successors(now=10 * day)
    assert det.predecessor_of(2) is old
    assert det.predecessor_of(1) is None

    assert det.adopt(2) == "Kiln"
    assert new.name == "Kiln" and old.name is None
    assert old.successor_id is None
    # the meter must not step backwards when a name moves - Home Assistant
    # reads a drop as a reset - so the appliance's whole history comes along
    assert det.energy_by_name() == {"Kiln": 1250.0}
    assert new.carried_wh == 1000.0
    # ...but the charts still describe THIS behaviour, not an average of two
    assert new.hour_wh == [250.0] + [0.0] * 23
    assert old.hour_wh == [1000.0] + [0.0] * 23
    assert old.count == 200, "history is kept, only the name moves"

    # and it cannot be done twice, or to a signature nobody is pointing at
    assert det.adopt(2) is None
    assert det.adopt(1) is None


def test_the_wiring_comes_from_the_inverters_not_from_the_grid_page():
    """Where the battery sits is a fact about an INVERTER, not about the grid.
    Series if any inverter is series - the series formula on the summed
    outputs is exact for a mix, because a parallel member contributes no
    battery term."""
    assert D.site_topology([]) is None, "nothing said means read it off the data"
    assert D.site_topology([{"topology": "parallel"}]) == "parallel"
    assert D.site_topology([{"topology": "series"}]) == "series"
    # a SolarEdge on the bus beside a Deye hybrid: the mix reads as series
    assert D.site_topology([{"topology": "parallel"}, {"topology": "series"}]) == "series"
    assert D.site_topology([{"topology": "series"}, {"topology": "parallel"}]) == "series"
    # an inverter with no wiring stated does not vote
    assert D.site_topology([{"power": "sensor.x"}]) is None
    # a layout stored before the inverter list existed is still honoured...
    assert D.site_topology([], "series") == "series"
    # ...and loses to an inverter that says otherwise
    assert D.site_topology([{"topology": "parallel"}], "series") == "parallel"


def test_a_meter_reporting_kilowatts_is_not_read_as_watts():
    """Home's EV charger publishes kW while every other meter in the house
    publishes W, so its 2 kW session arrived as the number 2 and could never
    match the 2000 W session the main meter saw. The load stayed
    unattributed and turned up in the naming list as an unexplained car."""
    assert D.unit_scale("W") == 1.0
    assert D.unit_scale("kW") == 1000.0
    assert D.unit_scale("MW") == 1_000_000.0
    assert D.unit_scale("mA") == 0.001
    assert D.unit_scale("kV") == 1000.0
    # a power factor published as a percentage is a ratio
    assert D.unit_scale("%") == 0.01
    # whitespace and absence are survivable; an unknown unit is assumed base,
    # because a reading that is probably watts beats no reading
    assert D.unit_scale(" kW ") == 1000.0
    assert D.unit_scale(None) == 1.0
    assert D.unit_scale("") == 1.0
    assert D.unit_scale("furlongs") == 1.0


def test_the_house_is_the_sum_and_needs_no_wiring_flag():
    """house = grid + SUM over inverters of (output - input).

    A PV inverter has no AC input and contributes its whole output; a hybrid
    with the grid flowing through it contributes the difference, so the grid
    it passed on is not counted twice. The point of the form is that it is
    arrangement-independent - the same expression is right whether a second
    inverter feeds the main bus or sits on the first one's load port."""
    n = 300
    stamp = lambda i: T0 + i * DT
    loads_main = [(stamp(i), 400.0) for i in range(n)]
    se = [(stamp(i), 1500.0) for i in range(n)]            # a PV inverter, no input

    # (a) the PV inverter on the MAIN bus, a hybrid feeding a backup panel
    deye_in = [(stamp(i), 900.0) for i in range(n)]        # what the hybrid draws
    deye_out = [(stamp(i), 900.0) for i in range(n)]       # what it delivers
    grid = [(stamp(i), 400.0 + 900.0 - 1500.0) for i in range(n)]
    house = D.combine([(grid, 1.0), (deye_out, 1.0), (deye_in, -1.0), (se, 1.0)])
    assert all(abs(w - 1300.0) < 1e-6 for _, w in house), house[:3]

    # (b) the SAME PV inverter cabled to the hybrid's LOAD PORT. The hybrid now
    # delivers the backup loads less what the array feeds in, and the grid
    # meter no longer sees the array at all - a different site, same expression
    deye_out_b = [(stamp(i), 900.0 - 1500.0) for i in range(n)]
    grid_b = [(stamp(i), 400.0 + (900.0 - 1500.0)) for i in range(n)]
    house_b = D.combine([(grid_b, 1.0), (deye_out_b, 1.0), (deye_in := [(stamp(i), 900.0 - 1500.0)
                                                                        for i in range(n)], -1.0),
                         (se, 1.0)])
    assert all(abs(w - 1300.0) < 1e-6 for _, w in house_b), house_b[:3]

    # a term that has not started yet holds the sum back rather than biasing it
    late = [(stamp(i), 100.0) for i in range(100, n)]
    mixed = D.combine([(loads_main, 1.0), (late, 1.0)])
    assert mixed[0][0] == stamp(100) and abs(mixed[0][1] - 500.0) < 1e-6
    assert D.combine([]) == []


def test_a_load_that_keeps_a_clock_says_so_in_its_row():
    """How often was left off the row as the least use for telling one from
    another. True of an irregular load, wrong for a regular one: Kozolec's
    hot water cycles 66 seconds every five minutes for twelve hours a day,
    and that is what its owner would recognise first."""
    import datetime as _dt
    tz = _dt.timezone.utc
    clock = _sig(1, 1800.0, 70.0, 60, pf=0.96)
    clock.interval_s, clock.interval_mad = 840.0, 60.0      # every 14 min, tight
    clock.first_seen = clock.last_seen - 59 * 840.0             # 60 starts over 13.8 h...
    clock.hour_wh = [500.0] * 24
    assert clock.regular
    assert "a day" not in " ".join(clock.menu_row(tz)), "under a day seen, a rate a day would be made up"
    clock.first_seen = clock.last_seen - 5 * 86400.0            # ...or over five days
    assert "12 times a day" in clock.menu_row(tz)[1], clock.menu_row(tz)


def test_a_menu_row_is_a_short_headline_and_a_line_that_wraps():
    """A menu row's label is cut at the dialog's width, so the naming page
    lost the end of every row (2026-09-23). The headline carries what
    tells loads apart; everything else goes underneath, where it wraps."""
    import datetime as _dt
    tz = _dt.timezone.utc
    clock = _sig(1, 1800.0, 70.0, 60, pf=0.96)
    clock.interval_s, clock.interval_mad = 840.0, 60.0
    clock.first_seen = clock.last_seen - 5 * 86400.0
    clock.hour_wh = [500.0] * 24
    clock.day_wh = [100.0, 0.0, 50.0, 0.0, 0.0, 0.0, 10.0]
    head, rest = clock.menu_row(tz, clock.last_seen + 600.0)
    assert len(head) <= 45, head
    assert "12 times a day" in rest, rest   # starts counted a day (2026-09-28)
    assert "a week" in rest and "a run" in rest and "last ran" in rest, rest
    assert "Mon-Sun" in rest, "the week drawn as well as in words - there is room now"
    assert " " not in rest.split("Mon-Sun")[1], "no day of the week may be a place to wrap"
    assert head.startswith("1.8 kW on phase A, runs 70 s"), head
    assert "a day" not in head, "how often goes underneath, said as how often"
    assert "runs in" in rest, rest


def test_a_possible_second_setting_is_described_not_numbered():
    """"set 4 of one device" meant nothing on a page where no other row was in
    set 4. Say what the other load looks like instead (2026-09-23)."""
    other = _sig(9, 1000.0, 600.0, 20, pf=0.99)
    assert D.same_device_phrase([other]) == "maybe the same device as the 1.0 kW, 10 min load"
    assert D.same_device_phrase([]) == ""
    three = [_sig(i, 1000.0 * i, 60.0, 20, pf=0.99) for i in (1, 2, 3)]
    assert D.same_device_phrase(three).endswith("and 1 more"), D.same_device_phrase(three)
    assert D.on_phases("a") == "on phase A" and D.on_phases("ac") == "on phases A and C"
    assert D.on_phases("abc") == "on all three phases"


def test_the_step_threshold_is_measured_and_scales_with_what_is_running():
    """A fixed 100 W floor was the binding constraint on both real sites,
    whose sample-to-sample movement is 3 to 5 W - which is why Kozolec has
    two fridges and detected neither. The floor is now a backstop and the
    rest is measured: how far the reading moves BETWEEN SAMPLES, and what
    share of the running level that is."""
    st = D.PhaseState()
    # a quiet signal: the measured figure lands near the floor
    for i in range(400):
        st.process(T0 + i * DT, 300.0 + (3.0 if i % 2 else -3.0))
    assert st.noise <= 40.0, st.noise
    assert st.noise_at(300.0) < 60.0

    # ...and the same phase is deliberately deafer while something big runs,
    # because a reading wanders more when more is flowing through it
    st.noise_rel = 0.02
    assert st.noise_at(5000.0) > st.noise_at(300.0)
    assert st.noise_at(5000.0) >= 100.0

    # a 60 W fridge on a quiet phase is now a step, where it never was
    quiet = D.PhaseState()
    quiet.floor_zero = True
    rows = []
    for i in range(600):
        on = 60.0 if (i // 40) % 2 else 0.0
        rows.append((T0 + i * DT, 250.0 + on + (2.0 if i % 2 else -2.0)))
    closed = []
    for ts, w in rows:
        closed += quiet.process(ts, w)
    assert closed, f"nothing detected at noise {quiet.noise:.0f} W"
    assert any(abs(sum(s.power_by_phase().values()) - 60.0) < 25.0 for s in closed), \
        [round(sum(s.power_by_phase().values())) for s in closed]


def test_one_devices_signatures_are_one_and_a_name_survives():
    """Filed by device, the signatures a device's runs went to are merged:
    the named one survives with the other's energy, two named apart stay
    apart (2026-09-30)."""
    def lib():
        det = D.Detector()
        det.edges = [D.StepGroup(id=1, phase="a", up=True, watts=600.0)]        # one start cluster: one device
        a = _sig(10, 600.0, 300.0, first_seen=T0, last_seen=T0)
        b = _sig(11, 610.0, 300.0, 3, first_seen=T0, last_seen=T0)
        a.hourly, b.hourly = {1000: 100.0}, {1000: 50.0, 4600: 20.0}
        det.signatures = [a, b]
        det.pair_home = {"1": {10: 5.0, 11: 3.0}}          # its runs went to two signatures
        return det, a, b
    det, a, b = lib()
    b.name = "Oven"
    assert det._merge_devices() == 1
    (kept,) = det.signatures
    assert kept.name == "Oven" and kept.hourly == {1000: 150.0, 4600: 20.0}, (kept.name, kept.hourly)
    assert det._moved == {10: 11}
    # naming the id that was merged away names what it became (2026-10-23)
    assert det.rename(10, "Stove") and kept.name == "Stove" and det.rename(999, "Nope") is False
    det, a, b = lib()
    a.name, b.name = "Oven", "Kettle"
    assert det._merge_devices() == 0 and len(det.signatures) == 2


def test_the_device_merge_judged_where_something_changed_is_the_full_walk():
    """_merge_devices keeps pair_home's votes between filings and judges
    only the devices whose signatures changed; after every filing it must
    merge what a walk over the whole library would, in the same order. A
    device's runs filed into a second signature by a meter's word move that
    signature's home as the votes tip, a run kept out stays apart, a leg on
    another phase stays apart (2026-10-02)."""
    import copy
    det = D.Detector()
    t = [T0]

    def run(dev, phases="a", watts=1000.0):
        t[0] += 900.0
        return D.Run(phases=phases, start=t[0], end=t[0] + 300.0, pair=(dev, None),
                         levels={p: [(t[0], watts / len(phases))] for p in phases})

    def file(s, **kw):
        det._file(s, **kw)
        full = copy.deepcopy(det)
        full._votes = None                            # the walk over everything, as before
        incremental_judged_all = det._rejudge_all
        assert det._merge_devices() == full._merge_devices()
        assert list(det._moved.items()) == list(full._moved.items()), (det._moved, full._moved)
        assert [x.id for x in det.signatures] == [x.id for x in full.signatures]
        return incremental_judged_all

    for _ in range(4):
        file(run(7))                                  # device 7's runs: signature 1
    for _ in range(3):
        file(run(8))                                  # device 8's: signature 2
    file(run(7, phases="b"))                          # a B leg of 7: signature 3, apart by phase
    file(run(7), avoid=[1, 2])                        # kept out of both: signature 4, never merged into them
    judged_all = []
    for _ in range(5):                                # device 7's runs sent to 2 by a meter: its home tips to 7
        judged_all.append(file(run(7), prefer=2))
    assert det._moved == {1: 2}, det._moved          # 2 had the more runs when the votes tied
    assert {x.id for x in det.signatures} == {2, 3, 4}
    assert not all(judged_all), "every pass judged every device: nothing was kept between filings"


def test_the_day_is_drawn_as_a_block_chart():
    hours = [0] * 24
    hours[7], hours[8], hours[20] = 6, 3, 2
    lines = D.hour_histogram(hours)
    assert len(lines) == D.HISTOGRAM_ROWS + 2, lines          # bars, axis, ruler
    assert all(len(x) == 24 * D.HISTOGRAM_COL + 1 for x in lines[:-1]), [len(x) for x in lines]
    assert "█" in lines[0] and lines[0].index("█") // D.HISTOGRAM_COL == 7, lines[0]
    assert D.hour_histogram([0] * 24) == []


def test_the_charts_hold_energy_spread_over_the_hours_it_ran():
    """Runtime times draw, not a count of starts: what a load costs you on a
    Saturday is the thing worth seeing, and a run from 23:40 to 01:20
    belongs to three hours and two days (2026-09-17)."""
    from datetime import datetime, timezone
    det = D.Detector()
    start = datetime(2026, 9, 18, 23, 40, tzinfo=timezone.utc).timestamp()   # a Friday
    det._file(D.Run(phases="a", start=start, end=start + 6000,
                        levels={"a": [(start, 2000.0)]}, pf=1.0))
    sig = det.signatures[0]
    assert round(sum(sig.hour_wh)) == round(2000 * 6000 / 3600), sig.hour_wh
    assert round(sig.hour_wh[23]) == 667 and round(sig.hour_wh[0]) == 2000, sig.hour_wh
    assert round(sig.hour_wh[1]) == 667, sig.hour_wh
    assert round(sig.day_wh[4]) == 667 and round(sig.day_wh[5]) == 2667, sig.day_wh
    assert round(sum(sig.day_wh)) == round(sum(sig.hour_wh))

    lines = D.day_histogram(sig.day_wh)
    assert lines[-1].strip().startswith("Mo"), lines[-1]
    assert len(lines) == 5, lines                                 # 3 rows, axis, labels
    assert D.day_histogram([0.0] * 7) == []


def test_the_wiring_follows_from_the_same_reading():
    """Grid-tied, the meter carries the house MINUS what the inverter makes,
    so the load is their sum - and that meter is exactly the one that goes
    negative. Behind a transfer switch nothing exports through the reading,
    and adding the grid would count the pass-through twice."""
    import random
    rnd = random.Random(11)
    n = 400
    house = [600.0 + (900.0 if (i // 37) % 3 == 0 else 0.0) + rnd.uniform(-40, 40) for i in range(n)]
    pv = [1500.0 + 1200.0 * ((i % 60) / 60.0) for i in range(n)]
    grid_tied = [(T0 + i * DT, house[i] - pv[i]) for i in range(n)]
    behind_a_switch = [(T0 + i * DT, house[i]) for i in range(n)]
    assert D.carries_generation(grid_tied) is True       # add the inverter back
    assert D.carries_generation(behind_a_switch) is False  # it is already the house


def test_which_way_round_the_grid_meter_is_wired():
    """"House = meter + inverter" holds only where importing is positive.
    The owner's SolarEdge M1 is the other way up, and summing it unflipped would
    count the array twice instead of cancelling it."""
    n = 600
    def solar(i):                       # a day: nothing, then a broad arc
        return max(0.0, 3000.0 - abs(i - n / 2) * 12.0)
    house = [700.0 for _ in range(n)]
    gen = [(T0 + i * DT, solar(i)) for i in range(n)]
    standard = [(T0 + i * DT, house[i] - solar(i)) for i in range(n)]
    inverted = [(T0 + i * DT, solar(i) - house[i]) for i in range(n)]
    assert D.exports_positive(standard, gen) is False
    assert D.exports_positive(inverted, gen) is True
    # a site that never exports has no export sign to find, and the answer
    # does not matter: there is nothing to cancel
    never = [(T0 + i * DT, 4000.0 - solar(i)) for i in range(n)]
    assert D.exports_positive(never, gen) is None
    assert D.exports_positive(inverted, gen[:3]) is None


def test_a_negative_sample_on_a_house_reading_is_a_glitch_not_a_load():
    """Home's templates dip to -3000 W when their two inputs update out of
    step, then return - and the return is a +3000 W step on every phase at
    once. A dip too short to satisfy SUSTAIN already fails it; one long
    enough would be accepted as a real step, so on a reading that cannot go
    below zero the samples are simply not readings.

    The dip is built long enough to clear SUSTAIN whatever it is set to. It
    was two samples, written when two samples were enough - and once the
    sustain guard was made to work, a two-sample dip was rejected before the
    floor-zero rule this test is about ever got a say (2026-09-23)."""
    n = 200
    house = [(T0 + i * DT, 400.0) for i in range(n)]
    for k in range(80, 86):                              # six samples, so
        house[k] = (house[k][0], -3000.0 + 10.0 * (k - 80))   # SUSTAIN is satisfied
    def run(floor):
        st = D.PhaseState(); st.floor_zero = floor
        out = []
        for ts, w in house:
            out += st.process(ts, w)
        return st, out
    st, sessions = run(True)
    assert sessions == [], [(round(s.duration_s), s.power_by_phase()) for s in sessions]
    assert abs(st.level - 400.0) < 50
    # the same samples on a reading that CAN export are taken at face value
    st2, sessions2 = run(False)
    assert sessions2 or st2.open_edges, "a real reading, a real step"


def test_a_summed_reading_keeps_only_the_last_of_each_burst():
    """Home's inverter is read about 20 ms before its meter on every poll, so
    summing them emits each update twice: first against the partner's stale
    value - a phantom step of the whole change - then correctly. A third of
    Home's house readings were phantoms (2026-09-23)."""
    meter = [(0.0, 1000.0), (6.02, 4000.0), (12.02, 4000.0)]
    inverter = [(0.0, 300.0), (6.00, 330.0), (12.00, 330.0)]
    raw = D.combine([(meter, 1.0), (inverter, 1.0)])
    assert (6.00, 1330.0) in raw, "the phantom: new inverter, old meter"
    settled = D.combine([(meter, 1.0), (inverter, 1.0)], settle_s=0.3)
    assert all(abs(t - 6.00) > 1e-9 for t, _ in settled), settled
    assert (6.02, 4330.0) in settled, "the corrected sum is what survives"
    # an input that records nothing - an inverter at 0 W all night - costs
    # nothing: there is no burst, so every reading stays. This is what
    # max_skew_s got wrong on recorder data.
    night = [(t, 500.0 + 3000.0 * (40 <= t < 90)) for t in range(0, 200, 6)]
    quiet = [(0.0, 0.0)]
    assert len(D.combine([(night, 1.0), (quiet, 1.0)], settle_s=0.3)) == len(night)


def test_the_idle_noise_is_not_learned_while_the_array_moves_under_the_reading():
    """A house built from a grid meter and an inverter that are not read at
    one moment wanders with every cloud: here 30 % of each change of the
    array's shows a poll before the meter has it. Learned from those moves,
    the idle noise rose with the sun (the third site by day 12.5-14.5 W, 10 at night)
    and so did the floor's footing; learned only while the array holds still,
    it is the house's own (2026-10-04, exp12)."""
    rnd = random.Random(2)
    n = 3000
    sun = [0.0] * 600 + [1500.0] * 2400
    for k in range(601, n):                    # morning: a hazy sky on one phase, 14-21 W a poll
        sun[k] = sun[k - 1] + rnd.choice([1, -1]) * rnd.uniform(14.0, 21.0)
    rows = [(T0 + DT * k, 300.0 + rnd.choice([-1.0, 0.0, 1.0]) + 0.3 * (sun[k] - sun[k - 1] if k else 0.0))
            for k in range(n)]
    # the highest the noise got, fed in passes of 50 readings: the readings
    # wander in two modes (+-4..6 W), so where a move from the level counts
    # (NOISE_FROM_MOVES) the blind noise ends a run at 7 W or ~27 by chance
    # (4 of 8 seeds at 10 before NOISE_REL_FROM_LEVEL, 6 of 8 since). The wander
    # sits just under the floor and is scaled with it: it was 20-30 W array
    # steps (+-6..9 W) under the 10 W floor, where at 7 W no seed inflated
    # (2026-10-27, CI failed on it); bigger moves are steps, not noise. Now
    # 5 of 8 seeds inflate, this one to 26.4 W
    def highest(pv):
        det = D.Detector()
        det.phases["a"].floor_zero = True
        top = 0.0
        for i in range(0, n, 50):
            part = rows[i:i + 50]
            det.process({"a": part}, None, part[-1][0], {"a": dict(zip((t for t, _ in part), sun[i:i + 50]))} if pv else None)
            top = max(top, det.phases["a"].noise)
        return top
    assert highest(True) <= D.MIN_NOISE_W + 1e-9, highest(True)
    assert highest(False) > 2 * D.MIN_NOISE_W, highest(False)   # the array not handed over: its moves are the reading's


def test_a_meter_that_reports_on_change_learns_its_idle_noise_from_its_silences():
    """Home's hidrofor plug: reports every second while the pump runs and
    idle once in minutes, flat at about 1 W. Seeded on a run and re-learned
    from its moves alone, its noise sat at the run's wander (158-298 W): ten
    days held 62 idle moves. The recorder writes only a change, so each of
    the meter's own intervals in a silence is a move of nothing."""
    rnd = random.Random(7)
    rows, t = [], T0
    for _ in range(72):                         # a day: a 3-minute run every 20 minutes
        for _ in range(180):                    # 850 W, a report every second
            rows.append((t, 850.0 + rnd.gauss(0.0, 30.0)))
            t += 1.0
        for _ in range(2):                      # idle: a report every 8 minutes
            rows.append((t, 1.0 + rnd.choice([-0.1, 0.1])))
            t += 480.0
    det = D.Detector()
    det.process({"a": rows}, None, t)
    st = det.phases["a"]
    assert st.interval <= 1.5, st.interval
    assert st.noise <= max(D.MIN_NOISE_W, st.quantum) + 1e-9, st.noise

def test_a_dip_back_to_the_level_is_no_idle_move():
    """Home's grid b, 10-12 19:20: a load cycling 280 <-> 660 W under a floor
    that had taken it in, nothing open. Each reading back at the level moved
    380 W from the dip before it, and those moves held the idle noise at
    700 W for an hour. A move counts only from a reading at the level too."""
    rnd = random.Random(3)
    st = D.PhaseState(min_noise=5.0)
    st.baseline, st.level, st.noise, st.interval = 660.0, 660.0, 15.0, 2.0
    for i in range(1200):
        st.process(T0 + 2.0 * i, (280.0 if i % 3 == 2 else 660.0) + rnd.gauss(0, 1.5))
    assert st.noise < 30.0, st.noise

def test_a_dip_back_to_a_loaded_level_is_no_relative_move():
    """A loaded phase (2 kW, wandering 3 W a reading) with a load dipping it
    300 W for one reading in three: each reading back at the level moved
    300 W from the dip, and those moves counted as its wander put the relative
    noise at the cap (Home's grid c, ~108 W at 2.2 kW). A move counts only
    from a reading at the level too, as the idle noise's (2026-10-14)."""
    rnd = random.Random(4)
    st = D.PhaseState(min_noise=10.0)
    st.baseline, st.level, st.noise, st.interval = 0.0, 2000.0, 10.0, 2.0
    st.open_edges = [D._Open(T0, 2000.0, None, [(T0, 2000.0)])]
    for i in range(1500):
        st.process(T0 + 2.0 * i, (1700.0 if i % 3 == 2 else 2000.0) + rnd.gauss(0, 3.0))
    assert 0.0 < st.noise_rel < 0.01, st.noise_rel


def test_a_slow_meter_learns_its_relative_noise_from_a_seeds_worth_of_moves():
    """Kozolec's car charger plug reports once a minute while it charges:
    ten days gave it 185 moves at a loaded level, under the NOISE_WINDOW the
    relative noise waited for, so it never learned one and its noise at 2.2 kW
    stayed the idle 10 W. Like the idle noise, a seed's worth of moves
    (BASELINE_SEED_SAMPLES) is enough to start from."""
    rnd = random.Random(6)
    st = D.PhaseState(min_noise=10.0)
    st.baseline, st.level, st.noise, st.interval = 0.0, 2200.0, 10.0, 60.0
    st.open_edges = [D._Open(T0, 2200.0, None, [(T0, 2200.0)])]
    for i in range(D.BASELINE_SEED_SAMPLES + 5):
        st.process(T0 + 60.0 * i, 2200.0 + rnd.gauss(0, 3.0))
    assert st.noise_rel > 0.0, st.noise_rel


def test_the_relative_noises_moves_survive_a_restart():
    """Production restores a phase from its store: the moves the relative
    noise is the median of were not in it, so every restart began the count
    again and a meter that never gathered a window between restarts never
    learned one."""
    st = D.PhaseState()
    st.rel_diffs = [0.001 * k for k in range(30)]
    assert D.PhaseState.from_dict(json.loads(json.dumps(st.to_dict()))).rel_diffs == st.rel_diffs


def test_a_readings_interval_is_its_cadence_not_how_often_it_changes():
    """Home Assistant records only a CHANGE. A quiet phase of Home's grid meter
    records fewer, so a running mean of its gaps came out 7.1 s against the
    busy phases' 6.0 - one meter, three cadences - and the sustain guard judged
    two legs of one load by different thresholds (2026-09-23). Here a quiet
    leg's reading changes on a quarter of the 6 s polls and holds - unrecorded
    - between: the median of its gaps reads 12-18 s, the gaps after it MOVED
    still say 6."""
    rnd = random.Random(5)
    busy, quiet = D.PhaseState(min_noise=10.0), D.PhaseState(min_noise=10.0)
    t, wobble, last = 0.0, 1.0, None
    for i in range(1200):
        t += 6.0
        level = 300.0 + 400.0 * ((i // 10) % 2)           # a load switching every minute
        busy.process(t, level + (1.0 if i % 2 else -1.0))  # changes every poll
        if rnd.random() < 0.25:
            wobble = -wobble
        w = level + wobble
        if w != last:                                      # unchanged: not recorded
            quiet.process(t, w)
            last = w
    assert abs(busy.interval - 6.0) < 0.01, busy.interval
    assert abs(quiet.interval - 6.0) < 0.01, f"the cadence is still 6 s ({quiet.interval})"


def test_another_leg_of_the_same_load_vouches_for_a_stop():
    """A real multi-phase device switches its legs together, so one leg
    closing is evidence the other's short off-gap was real. Home's kiln is off
    only one or two readings between pulses; without this, whichever leg
    swallowed its gap ran on and could not merge with the other (2026-09-23)."""
    det = D.Detector()
    a, c = det.phases["a"], det.phases["c"]
    for st in (a, c):
        st.level, st.baseline, st.interval, st.noise = 3500.0, 500.0, 6.0, 20.0
    a.open_edges = [D._Open(since=100.0, watts=3000.0, var=None, levels=[(100.0, 3000.0)])]
    check = det._corroborate("a", {"a": [], "c": []})
    c.recent_closed = [(100.0, 2950.0, 148.0)]          # C's leg: same start, just closed
    assert check(100.0, 3000.0, 148.0)
    assert not check(100.0, 3000.0, 400.0), "closed long before now"
    c.recent_closed = [(130.0, 2950.0, 148.0)]
    assert not check(100.0, 3000.0, 148.0), "started 30 s later: another load"
    c.recent_closed = [(100.0, 600.0, 148.0)]
    assert not check(100.0, 3000.0, 148.0), "a fifth of the power: not the same device"
    # a phase with nothing on another leg - any single-phase site - never vouches
    lone = D.Detector()
    lone.phases["a"].level = 3500.0
    assert not lone._corroborate("a", {"a": []})(100.0, 3000.0, 148.0)


def test_a_vouched_stop_closes_the_edge_it_was_vouched_for():
    """Two loads of one size on one phase - Home's Kompresor leg (~840 W) and
    hidrofor (~870 W) on A - are where closing the NEWEST same-sized edge is
    wrong. The edge the other legs vouch for is the one that closes."""
    st = D.PhaseState(min_noise=10.0)
    st.level, st.baseline, st.interval, st.noise = 2210.0, 500.0, 6.0, 20.0
    kompresor = D._Open(since=100.0, watts=840.0, var=None, levels=[(100.0, 840.0)])
    pump = D._Open(since=130.0, watts=870.0, var=None, levels=[(130.0, 870.0)])
    st.open_edges = [kompresor, pump]                    # the pump is the newer
    st.pending = [(160.0, 2210.0 - 840.0, None, None)]
    st.corroborate = lambda since, watts, ts: since == 100.0   # only the Kompresor's legs agree
    assert st._corroborated_stop(160.0) and st.close_hint is kompresor
    closed = st._pair(160.0, 840.0, None, 2210.0 - 840.0)
    assert closed and closed[0].start == 100.0, "the Kompresor closed, not the pump"
    assert st.open_edges == [pump]
    st.corroborate = lambda since, watts, ts: False
    st.pending = [(170.0, 500.0, None, None)]
    assert not st._corroborated_stop(170.0) and st.close_hint is None


def test_a_level_is_the_readings_that_agree_not_the_median_of_a_transition():
    """Home's hidrofor stopping read [1251, 1082, 436]: a sag, a half-caught
    switch, the new level. Their median made it a 198 W step, which closed an
    unrelated 179 W start, and left the pump's own session open for 7.8 hours.
    The new level is the readings that agree with each other (2026-09-23)."""
    st = D.PhaseState(min_noise=10.0)
    st.level, st.baseline, st.interval, st.noise = 1280.0, 250.0, 6.0, 20.0
    st.open_edges = [D._Open(since=0.0, watts=179.0, var=None, levels=[(0.0, 179.0)]),
                     D._Open(since=500.0, watts=845.0, var=None, levels=[(500.0, 845.0)])]
    closed = []
    for t, w in ((560.0, 1251.0), (566.0, 1082.0), (572.0, 436.0), (607.0, 437.0)):
        closed += st.process(t, w)
    assert len(closed) == 1 and closed[0].start == 500.0, closed         # the pump, not the 179 W
    assert abs(closed[0].levels[""][0][1] - 845.0) < 5.0, closed
    assert [o.watts for o in st.open_edges] == [179.0]
    # and a real off-gap after a half-caught reading still counts: the time
    # away - SUSTAIN_CADENCES of the 6 s interval - is measured from the first
    # reading that left the level, which alone makes it long enough
    st = D.PhaseState(min_noise=10.0)
    st.level, st.baseline, st.interval, st.noise = 4150.0, 1250.0, 6.0, 16.0
    st.open_edges = [D._Open(since=0.0, watts=2900.0, var=None, levels=[(0.0, 2900.0)])]
    closed = []
    for t, w in ((48.0, 1622.0), (54.0, 1251.0), (60.0, 1232.0), (66.0, 1240.0)):
        closed += st.process(t, w)
    assert len(closed) == 1 and abs(st.level - 1241.5) < 15.0, (closed, st.level)


def test_the_recorders_start_of_window_copy_is_not_a_reading():
    """Asked for the state at a window's start, the recorder returns the last
    reading again, stamped the start. At one-minute ticks that was a repeat on
    every phase every minute, and it cost Home 2 points of purity."""
    rows = {"a": [(60.0, 500.0), (62.1, 510.0)], "b": [(61.0, 20.0)]}
    assert D.without_window_start(rows, 60.0) == {"a": [(62.1, 510.0)], "b": [(61.0, 20.0)]}


def test_a_three_phase_meters_channels_are_mapped_by_what_they_see():
    """Home's attic 3EM calls the house's C "b" and its A "c". Its channels are
    mapped onto the house's phases by which house phase each one's sessions
    coincide with - as a permutation, so a two-phase load cannot tie - and
    its own labels stand until there is evidence (2026-09-23)."""
    rotated = {"a": {"b": 20}, "b": {"c": 60, "b": 2}, "c": {"a": 64}}
    assert D.phase_mapping(rotated) == {"a": "b", "b": "c", "c": "a"}
    # the kiln steps on house A and C at once: channel a ties between them
    # alone, and the permutation settles it with the other channels
    kiln = {"a": {"a": 79, "c": 79}, "b": {"b": 23}, "c": {"c": 55, "a": 17}}
    assert D.phase_mapping(kiln) == {"a": "a", "b": "b", "c": "c"}
    s = D.Run(phases="bc", start=0.0, end=60.0, levels={"b": [(0.0, 100.0)], "c": [(0.0, 90.0)]})
    house = D.Run(phases="ac", start=0.0, end=60.0, levels={"c": [(0.0, 100.0)], "a": [(0.0, 90.0)]})
    assert D._same_load(house, s, {"a": "b", "b": "c", "c": "a"}) and not D._same_load(house, s, {"b": "b", "c": "c"})


def test_a_sub_meter_decides_which_signature_a_session_joins():
    """A detection on a sub-meter overrides the house's: the device's own meter
    saw it run, so its signature takes the run whatever the house's power
    reading of it looks like (2026-09-22; trusted whether or not it
    fits since 2026-09-30: Home 79.8 / 76.0 -> 86.0 / 85.4 %)."""
    det = D.Detector()
    for p in "a":
        det.phases[p].noise = 10.0
    near = _sig(1, 1000.0, last_seen=0.0)
    other = _sig(2, 1040.0, last_seen=0.0)
    far = _sig(3, 3000.0, last_seen=0.0)
    det.signatures = [near, other, far]
    s = D.Run(phases="a", start=100.0, end=160.0, levels={"a": [(100.0, 1000.0)]})
    det._file(s, prefer=2)
    assert s.signature_id == 2, "the sub-meter's choice, although 1 fits a little better"
    t = D.Run(phases="a", start=200.0, end=260.0, levels={"a": [(200.0, 1000.0)]})
    det._file(t, prefer=3)
    assert t.signature_id == 3, "taken even where the house read it differently"


def test_a_session_waits_only_for_meters_that_could_have_seen_it():
    """Filing waits for a sub-meter partner - but only from a meter that reads
    at least twice inside the run. Home's workshop boiler meter reports every
    seven minutes and can partner no one-minute pump run. The wait is on the
    one clock every meter is read on: the plug's tolerance, its sustain, a
    reading and its own wait for a partner leg past the run's end - never
    past the patience."""
    fleet = D.Fleet()
    fast, slow = D.Detector(), D.Detector()
    fast.phases["a"].interval, slow.phases["a"].interval = 10.0, 420.0
    fleet.subs = {"plug": fast, "workshop": slow}
    run = D.Run(phases="a", start=0.0, end=60.0, levels={"a": [(0.0, 900.0)]})
    # the two meters' tolerance (the grid's 6 s and the plug's latency, three
    # of its 10 s), the plug's declaring lag - its latency twice until one is
    # learned - and its wait for a partner leg
    assert fleet._ready_at(run, 6.0) == 60.0 + 36.0 + 60.0 + D.HELD_TAIL_S   # the slow meter not waited for
    fleet.meter_lag["plug"] = [[3.0, 12.0]] * D.LAG_MIN_SAMPLES                 # learned: declared 12 s after the grid
    assert fleet._ready_at(run, 6.0) == 60.0 + 36.0 + 12.0 + D.HELD_TAIL_S
    fast.phases["a"].interval = 50.0                                 # a plug as slow as the run: not waited for either
    assert fleet._ready_at(run, 6.0) == 60.0
    long_run = D.Run(phases="a", start=0.0, end=3000.0, levels={"a": [(0.0, 900.0)]})
    slow.phases["a"].interval = 1400.0
    assert fleet._ready_at(long_run, 6.0) == 3000.0 + D.MATCH_PATIENCE_S, "never past the patience"


def test_energy_between_is_watt_hours_by_sample_and_hold():
    """A meter holds its last reading until it sends another, so the energy
    it accounts for over a window is each level times the time it stood."""
    rows = [(0.0, 0.0), (3600.0, 1000.0), (7200.0, 0.0)]
    assert D.energy_between(rows, 3600.0, 7200.0) == 1000.0        # a full hour at 1 kW
    assert D.energy_between(rows, 3600.0, 5400.0) == 500.0         # half of it
    assert D.energy_between(rows, 0.0, 3600.0) == 0.0
    # spanning the step: half an hour off, half an hour on
    assert D.energy_between(rows, 1800.0, 5400.0) == 500.0


def test_energy_between_refuses_a_window_it_cannot_see_both_ends_of():
    """Sample and hold will carry a reading forward forever if you let it, so
    a meter that fell silent must not answer for what happened afterwards -
    "it drew exactly what it was drawing before" is not an observation."""
    rows = [(1000.0, 500.0), (2000.0, 500.0)]
    assert D.energy_between(rows, 1500.0, 1800.0) is not None      # inside
    assert D.energy_between(rows, 1500.0, 9000.0) is None          # past the last sample
    assert D.energy_between(rows, 500.0, 1500.0) is None           # before the first
    assert D.energy_between(rows, 1500.0, 1500.0) is None          # empty window
    assert D.energy_between([], 0.0, 10.0) is None


def test_a_session_waits_for_a_slow_meter_to_say_what_it_saw():
    """A device meter need not have reported by the time the main meter's
    session closes. HELD_TAIL_S is about two phases of one load closing
    together - seconds - and judging on that clock threw the question away
    before the answer arrived: at Kozolec the boiler kept 1 sighting of 378
    when a pass was a minute long instead of six hours."""
    assert D.MATCH_PATIENCE_S > D.HELD_TAIL_S * 2
    assert D.MATCH_PATIENCE_S >= 10 * 60.0



def test_a_merge_owns_up_to_the_distance_it_just_closed():
    """Averaging two signatures' deviations throws away the gap between their
    MEANS, so folding two tight signatures 300 W apart produced one claiming
    its sightings sat within a few watts of each other. That figure feeds
    tightness, which feeds evidence, which feeds the confidence the user is
    shown - a merge made a signature look BETTER measured the further apart
    the things it merged."""
    keep, other = _sig(1, 1000.0, 100.0, 10), _sig(2, 700.0, 100.0, 10)
    keep.swallow(other)
    assert round(sum(keep.power.values())) == 850
    # 150 W from the new mean on each side, and neither had any spread before
    assert 140 <= keep.power_mad <= 160, keep.power_mad
    # durations were identical, so that spread stays where it was
    assert keep.duration_mad == 0.0


def test_a_pool_may_not_be_stretched_wider_than_the_tolerance_that_made_it():
    """Merging is transitive: each one re-centres the band on the new mean,
    so A reaches B, the pair reaches C, and it walks. What it has already
    absorbed has to stay inside the tolerance that let the pair match."""
    keep = _sig(1, 400.0, 100.0, 10)
    assert keep.alike(_sig(2, 330.0, 100.0, 10), 115.0)          # 70 apart, inside 115
    keep.swallow(_sig(2, 330.0, 100.0, 10))                      # now 365 W, spread 35
    assert keep.alike(_sig(3, 260.0, 100.0, 10), 115.0)          # 105 apart, spread would be 70
    keep.swallow(_sig(3, 260.0, 100.0, 10))                      # now 330 W, spread 70
    # 200 W is 130 away - outside the band on its own terms
    assert not keep.alike(_sig(4, 200.0, 100.0, 10), 115.0)
    # and a pool already at the limit refuses a partner that is inside the
    # band on its own terms, because taking it would push the pool past it
    stretched = _sig(5, 330.0, 100.0, 100, power_mad=120.0)
    assert abs(330.0 - 260.0) < 115.0                       # the pair would match
    assert not stretched.alike(_sig(6, 260.0, 100.0, 10), 115.0)  # the pool would not


def test_a_three_phase_load_is_not_judged_by_a_single_phase_yardstick():
    """power_mad and the distance travelled are TOTALS across the phases;
    the tolerance that bounds them is one phase's. A three-phase load's total
    wanders about three times what one leg does, so it was refused merges an
    identical single-phase load was granted (2026-09-22)."""
    def leg(i, per_phase, count, phases, mad=0.0):
        sig = _sig(i, per_phase * len(phases), 60.0, count, phases=phases, last_seen=1.0, power_mad=mad)
        sig.hour_wh = [10.0] * 24
        return sig

    # one leg apart by 70 W, tolerance 115 W: fine on any number of phases
    single = leg(1, 400.0, 10, "a")
    assert single.alike(leg(2, 330.0, 10, "a"), 115.0)
    triple = leg(3, 400.0, 10, "abc")
    assert triple.alike(leg(4, 330.0, 10, "abc"), 115.0), \
        "each leg is 70 W apart, exactly as in the single-phase case"

    # and the guard still bites when the pool really would be stretched
    stretched = leg(5, 330.0, 100, "abc", mad=120.0)
    assert not stretched.alike(leg(6, 260.0, 10, "abc"), 115.0)


def _session(start, watts, dur, phase="a"):
    """One flat run, long enough to be a session."""
    rows, t = [], start
    for _ in range(30):
        rows.append((t, 100.0)); t += 10.0
    for _ in range(int(dur // 10)):
        rows.append((t, 100.0 + watts)); t += 10.0
    for _ in range(30):
        rows.append((t, 100.0)); t += 10.0
    return {phase: rows}, t


def test_a_name_outlives_the_library_it_was_written_on():
    """Naming a load is the one thing in the library the user put there. A
    reset re-learns everything else from history in minutes; it must not take
    the name, the device it created, or the energy meter on the Energy
    dashboard with it (2026-09-22: "we dont want people loosing their
    named entities if they update integration")."""
    det = D.Detector()
    samples, t = _session(T0, 2000.0, 600.0)
    det.process(samples, now_ts=t)
    assert det.signatures, "no signature to name"
    sig = det.signatures[0]
    assert det.rename(sig.id, "Kiln")

    # what a reset keeps
    orphans = det.name_descriptors()
    assert [o["name"] for o in orphans] == ["Kiln"]

    # the library is thrown away and learned again from the same history
    fresh = D.Detector()
    fresh.orphan_names = orphans
    samples, t2 = _session(T0 + 100000.0, 2000.0, 600.0)
    fresh.process(samples, now_ts=t2)
    assert [s.name for s in fresh.signatures] == ["Kiln"], [s.name for s in fresh.signatures]
    assert fresh.orphan_names == [], "the name was handed back, so it is no longer waiting"


def test_a_name_does_not_come_back_to_a_load_that_is_not_it():
    """The reason to reset BY HAND is that the site changed - a meter swapped,
    a phase rewired - and then the library describes something that is not
    there. A name that finds nothing like itself stays waiting rather than
    landing on the nearest stranger."""
    det = D.Detector()
    det.orphan_names = [{"name": "Kiln", "phases": "a", "power": {"a": 6000.0},
                         "duration_s": 3600.0, "pf": 0.99}]
    samples, t = _session(T0, 150.0, 120.0)          # a small brief thing, nothing like a kiln
    det.process(samples, now_ts=t)
    assert det.signatures, "no signature was filed"
    assert [s.name for s in det.signatures] == [None]
    assert [o["name"] for o in det.orphan_names] == ["Kiln"]


def test_waiting_names_survive_a_restart():
    """The backfill takes minutes and Home Assistant may be restarted inside
    it; a name still looking for its load has to be in the store."""
    det = D.Detector()
    det.orphan_names = [{"name": "Kompresor", "phases": "abc",
                         "power": {"a": 830.0, "b": 850.0, "c": 820.0},
                         "duration_s": 300.0, "pf": 0.85}]
    back = D.Detector.from_dict(det.to_dict())
    assert [o["name"] for o in back.orphan_names] == ["Kompresor"]
    # and an entry with no name is not carried
    det.orphan_names.append({"phases": "a"})
    assert len(D.Detector.from_dict(det.to_dict()).orphan_names) == 1


def test_names_are_read_out_of_a_library_the_detector_has_disowned():
    """This runs exactly when DETECTOR_GENERATION moves - the moment the
    stored shape is one the current code has given up on - so it may not
    assume today's schema, and a library it cannot read must yield nothing
    rather than raise."""
    today = {"generation": 5, "fleet": {"main": {"signatures": [
        {"name": "Kiln", "phases": "ac", "power": {"a": 2985, "c": 2937},
         "duration_s": 1200, "pf": 0.99},
        {"name": None, "phases": "a", "power": {"a": 50}, "duration_s": 60, "pf": None},
        {"name": "Compressor", "phases": "abc",
         "power": {"a": 828, "b": 852, "c": 817}, "duration_s": 300, "pf": 0.85},
    ]}}}
    got = D.names_in_store(today)
    assert [g["name"] for g in got] == ["Kiln", "Compressor"]
    assert got[0]["power"] == {"a": 2985, "c": 2937}

    # the shape from before downstream meters existed
    older = {"generation": 3, "detector": {"signatures": [
        {"name": "Boiler", "phases": "a", "power": {"a": 1800}, "duration_s": 70, "pf": 0.99}]}}
    assert [g["name"] for g in D.names_in_store(older)] == ["Boiler"]

    # nothing here may raise, whatever the store turns out to hold
    for bad in ({}, {"fleet": None}, {"fleet": []}, {"detector": 7},
                {"fleet": {"main": {"signatures": "nonsense"}}}):
        assert D.names_in_store(bad) == [], bad

    # a name whose description is unreadable is still KEPT - it shows as
    # awaiting its load, which beats vanishing
    odd = D.names_in_store({"fleet": {"main": {"signatures": [
        {"name": "Mystery", "power": "not a dict"}]}}})
    assert [g["name"] for g in odd] == ["Mystery"]
    assert odd[0]["power"] == {} and odd[0]["phases"] == ""


def test_a_rebuilt_library_gets_its_names_back():
    """A reset carries the NAMES across and nothing else: the ten days it
    rebuilds are the load's energy from then on, and the published meter
    counts only what it gains (named.carry_reading), so it neither steps
    down nor counts the ten days twice."""
    det = D.Detector()
    samples, t = _session(T0, 2000.0, 600.0)
    det.process(samples, now_ts=t)
    assert det.rename(det.signatures[0].id, "Kompresor")
    carried = det.name_descriptors()
    fresh = D.Detector()
    fresh.carry_names(carried)
    assert fresh.energy_by_name() == {}
    samples, t2 = _session(T0 + 100000.0, 2000.0, 600.0)
    fresh.process(samples, now_ts=t2)
    assert [s.name for s in fresh.signatures] == ["Kompresor"]
    assert round(fresh.energy_by_name()["Kompresor"]) == round(det.energy_by_name()["Kompresor"])


def test_a_reset_on_a_reset_keeps_the_names_still_waiting():
    """Home, 2026-09-29: a second reset fifteen minutes after the first
    dropped the mat, the kiln and the washer - none had run in between, so
    their names were still waiting, not on a signature."""
    det = D.Detector()
    samples, t = _session(T0, 2000.0, 600.0)
    det.process(samples, now_ts=t)
    det.rename(det.signatures[0].id, "Kompresor")
    first = D.Detector()
    first.carry_names(det.name_descriptors() + [{"name": "Kiln", "phases": "ac", "power": {"a": 2985.0, "c": 2937.0},
                                                 "duration_s": 23.0, "pf": 0.96}])
    samples, t2 = _session(T0 + 100000.0, 2000.0, 600.0)
    first.process(samples, now_ts=t2)                   # the compressor runs; the kiln does not
    assert [s.name for s in first.signatures] == ["Kompresor"]
    second = D.Detector()
    second.carry_names(first.name_descriptors())
    assert sorted(o["name"] for o in second.orphan_names) == ["Kiln", "Kompresor"]
    stored = D.names_in_store({"generation": 1, "fleet": {"main": first.to_dict()}})
    assert sorted(o["name"] for o in stored) == ["Kiln", "Kompresor"]


def test_the_names_survive_a_restart_mid_rebuild():
    det = D.Detector()
    det.carry_names([{"name": "Kiln", "phases": "ac", "power": {"a": 2985.0, "c": 2937.0},
                      "duration_s": 23.0, "pf": 0.96}])
    back = D.Detector.from_dict(det.to_dict())
    assert [o["name"] for o in back.orphan_names] == ["Kiln"]


def test_a_generation_bump_keeps_names():
    """The path nobody has ever walked: DETECTOR_GENERATION moves, the whole
    stored library is discarded, and every installation in the world does this
    at once on the next update. It is simulated here against a store in the
    shape the current code writes - which is what an installation would
    actually be holding - because the alternative is discovering it went wrong
    from someone's Energy dashboard (2026-09-22: "i just want this fixed
    for future updates/of the detection library versions/resets")."""
    det = D.Detector()
    samples, t = _session(T0, 2000.0, 600.0)
    det.process(samples, now_ts=t)
    sig = det.signatures[0]
    det.rename(sig.id, "Kiln")
    sig.carried_wh = 10436.2 - sum(sig.hour_wh)
    stored = {"generation": 5, "fleet": {"main": det.to_dict()}}

    # the bump: the store is read by a detector that has disowned its shape
    orphans = D.names_in_store(stored)
    assert [o["name"] for o in orphans] == ["Kiln"]

    fresh = D.Detector()                        # what `raw = {}` leaves behind
    fresh.carry_names(orphans)

    samples, t2 = _session(T0 + 100000.0, 2000.0, 600.0)
    fresh.process(samples, now_ts=t2)
    assert [x.name for x in fresh.signatures] == ["Kiln"]
    assert fresh.orphan_names == []


def test_the_only_paths_that_discard_the_library_both_carry_names():
    """Two ways the library goes: the reset the user asks for, and the
    generation bump they never see. Both take the same road out - a list of
    descriptors into carry_names - so neither can quietly grow a third
    behaviour."""
    det = D.Detector()
    samples, t = _session(T0, 2000.0, 600.0)
    det.process(samples, now_ts=t)
    det.rename(det.signatures[0].id, "Kiln")
    det.signatures[0].carried_wh = 9000.0

    by_reset = det.name_descriptors()
    by_bump = D.names_in_store({"generation": 4, "fleet": {"main": det.to_dict()}})
    for got in (by_reset, by_bump):
        assert [g["name"] for g in got] == ["Kiln"]
        assert got[0]["phases"] == "a" and got[0]["power"]


def test_a_row_says_whether_the_load_is_on_now_or_when_it_last_ran():
    """What someone naming a load actually has to go on is their own memory of
    the last hour: the dishwasher went on after dinner, nothing has run in the
    workshop since Tuesday. A row that says a load is on RIGHT NOW turns
    naming into walking over and looking at it."""
    now = 1_000_000.0
    sig = _sig(1, 2000.0, 600.0, 9, pf=0.99, first_seen=now - 86400.0, last_seen=now - 600.0)
    assert "last ran 10 min ago" in sig.describe(timezone.utc, now)
    assert "running now" in sig.describe(timezone.utc, now, running=True)
    assert "last ran" not in sig.describe(timezone.utc, now, running=True)
    # a run that has only just stopped reads better as that
    sig.last_seen = now - 30.0
    assert "just finished" in sig.describe(timezone.utc, now)
    # and without a clock the row is exactly what it always was
    assert "ran" not in sig.describe(timezone.utc)
    assert "running" not in sig.describe(timezone.utc)


def test_running_now_names_the_signatures_that_are_on():
    """A signature only exists once a run has FINISHED - the session is the
    step up paired with the step down that undoes it - so the first time a
    load ever runs there is nothing to say it is on. From the second time,
    the open edge is matched on its size and the row can say so."""
    det = D.Detector()
    rows, t = [], T0
    for _ in range(40):
        rows.append((t, 200.0)); t += 10.0
    for _ in range(40):                     # a complete run, so a signature exists
        rows.append((t, 2200.0)); t += 10.0
    for _ in range(40):
        rows.append((t, 200.0)); t += 10.0
    det.process({"a": rows}, now_ts=t)
    assert det.signatures, "a finished run should have made a signature"
    ids = lambda: {i for _, i in D.Fleet(main=det).running_refs(t)}      # noqa: E731
    assert ids() == set(), "nothing is on between runs"

    rows2 = []
    for _ in range(20):                     # it starts again, and stays on
        rows2.append((t, 2200.0)); t += 10.0
    det.process({"a": rows2}, now_ts=t)
    on = ids()
    assert on, "a load that has not stopped should read as running"
    assert on <= {x.id for x in det.signatures}


def test_when_a_load_generally_runs_is_said_only_when_it_keeps_a_time():
    """A phrase on every row distinguishes nothing, so this stays quiet unless
    the load really does keep to a time. It is a MEASUREMENT where the
    appliance guess is a prior - "runs in the evening" is a fact about this
    house, not a belief about houses - which is why it can be stated plainly
    rather than as a question."""
    week = 14 * 86400.0
    evening = [0.0] * 18 + [100.0, 120.0, 90.0, 40.0] + [0.0, 0.0]
    flat = [50.0] * 24
    assert D.when_phrase(evening, None, week, 12) == "evenings"
    assert D.when_phrase([0.0] * 22 + [80.0, 90.0], None, week, 9) == "overnight"
    # eight to five belongs to neither morning nor afternoon, and is plainly
    # a daytime load - with only the narrow windows it got no phrase at all
    assert D.when_phrase([0.0] * 8 + [60.0] * 9 + [0.0] * 7, None, week, 20) == "daytime"
    # a load scattered through the day keeps no time worth mentioning
    assert D.when_phrase(flat, None, week, 30) == ""


def test_the_weekday_split_is_per_day_not_per_group():
    """There are five weekdays and two weekend days, so a load running
    UNIFORMLY puts 71 % of its energy on weekdays. Comparing the groups'
    totals therefore called almost everything a weekday load - twenty of
    The owner's twenty-four rows, which distinguished nothing from nothing."""
    week = 14 * 86400.0
    flat = [50.0] * 24
    assert D.when_phrase(flat, [10.0] * 7, week, 30) == ""
    assert D.when_phrase(flat, [12, 12, 12, 12, 12, 8, 8], week, 30) == ""
    assert D.when_phrase(flat, [10, 10, 10, 10, 10, 0, 0], week, 30) == "weekdays"
    assert D.when_phrase(flat, [0, 0, 0, 0, 0, 10, 10], week, 30) == "weekends"


def test_a_time_is_not_claimed_on_the_strength_of_one_occasion():
    """Every run inside one evening falls in the same hours by construction,
    so a load seen five times over four hours would say "evenings" about what
    is really a single occasion. And two sightings can agree by chance about
    anything."""
    evening = [0.0] * 18 + [100.0, 120.0, 90.0, 40.0] + [0.0, 0.0]
    assert D.when_phrase(evening, None, 4 * 3600.0, 5) == ""       # one evening
    assert D.when_phrase(evening, None, 14 * 86400.0, 2) == ""     # twice
    assert D.when_phrase(evening, None, 14 * 86400.0, 3) == "evenings"
    # the weekday split wants a week, or one quiet weekend decides it
    flat = [50.0] * 24
    assert D.when_phrase(flat, [10, 10, 10, 10, 10, 0, 0], 3 * 86400.0, 9) == ""


def test_two_loads_are_not_one_device_just_because_nothing_was_recorded():
    """"Never two of them at once" has to be OBSERVED. The session list is
    finite - two hundred entries against a library several times that at a
    busy house - so for most pairs there is nothing recorded either way, and
    reading that silence as "they never overlap" offered a 149 W load and a
    2.7 kW one as one device (the owner's house, 2026-09-22)."""
    a, b = _sig(1, 1000.0, 100.0, pf=0.96), _sig(2, 2700.0, 100.0, pf=0.96, last_seen=1.0)
    assert D.suggest_levels([a, b], []) == []               # nothing seen of either
    seen_a = [{"signature": 1, "start": 0.0, "end": 50.0}]
    assert D.suggest_levels([a, b], seen_a) == []           # only one side seen
    both = seen_a + [{"signature": 2, "start": 500.0, "end": 550.0}]
    assert D.suggest_levels([a, b], both) == [[1, 2]]       # both seen, never together

    # ...and the pair this test was written around - 150 W against 2.7 kW,
    # eighteen to one - is refused whatever the recording says, because the
    # recording was never the whole fault. Being seen apart is necessary and
    # nowhere near sufficient: most short loads in a house never overlap.
    far = [_sig(3, 150.0, 100.0, pf=0.96, last_seen=1.0), _sig(4, 2700.0, 100.0, pf=0.96, last_seen=1.0)]
    apart = [{"signature": 3, "start": 0.0, "end": 50.0},
             {"signature": 4, "start": 500.0, "end": 550.0}]
    assert D.suggest_levels(far, apart) == []


def test_levels_of_one_device_run_for_about_as_long_each_time():
    """Sizes are not compared - a setting can be any fraction of another - but
    duration is a different question, and leaving it out was most of what let
    unrelated loads group. A hob on three settings boils the same pan for
    about as long each time; what differs is the power."""
    seen = [{"signature": 1, "start": 0.0, "end": 30.0},
            {"signature": 2, "start": 500.0, "end": 530.0},
            {"signature": 3, "start": 1000.0, "end": 1600.0}]
    brief_a, brief_b = _sig(1, 3000.0, 25.0, pf=0.96), _sig(2, 5900.0, 23.0, pf=0.96, last_seen=1.0)
    lengthy = _sig(3, 4100.0, 600.0, pf=0.96, last_seen=1.0)
    groups = D.suggest_levels([brief_a, brief_b, lengthy], seen)
    assert groups == [[1, 2]], groups


def test_a_load_that_overlaps_another_is_never_the_same_device():
    """The whole test: one appliance cannot run two of its own settings at
    once, so an observed overlap rules the pair out however well they match."""
    a, b = _sig(1, 1000.0, 100.0, pf=0.96), _sig(2, 2000.0, 100.0, pf=0.96, last_seen=1.0)
    together = [{"signature": 1, "start": 0.0, "end": 100.0},
                {"signature": 2, "start": 50.0, "end": 150.0}]
    assert D.suggest_levels([a, b], together) == []


def test_a_thermostat_absorbs_its_own_short_and_long_runs():
    """The whole point, end to end: the same tank reheating from nearly hot and
    from stone cold is one load, and was two."""
    det = D.Detector()
    t = T0
    for seconds in (70, 65, 75, 68, 20, 300, 72):          # one thermostat, varied
        rows = []
        for _ in range(30):
            rows.append((t, 100.0)); t += 5.0
        for _ in range(max(2, seconds // 5)):
            rows.append((t, 1900.0)); t += 5.0
        for _ in range(30):
            rows.append((t, 100.0)); t += 5.0
        det.process({"a": rows}, now_ts=t)
    watts = [round(sum(x.power.values()) / 100) * 100 for x in det.signatures]
    around = [w for w in watts if 1700 <= w <= 1900]
    assert len(around) == 1, (watts, [x.count for x in det.signatures])
    assert det.signatures[watts.index(around[0])].count >= 6


def test_the_naming_page_lengthens_as_loads_are_named():
    """No percentile suits two sites: set high it hides a big house's real
    loads for ever, set low it opens with two hundred rows and is put down
    unread. The right number of rows is not a property of the site but of how
    much work the person has already done (2026-09-22)."""
    sigs = []
    for i in range(40):
        sig = _sig(i, 1000.0 + i * 50, 60.0, 9, pf=0.95, last_seen=1.0)
        sig.hour_wh = [40.0] * 24
        sigs.append(sig)
    assert all(s.evidence >= 0.7 for s in sigs), "these should all clear the bar"

    def offer(named):
        return D.offer_for_naming(sigs, named, 0.7, min_rows=5, start_rows=6, rows_per_name=4)

    assert len(offer(0)) == 6, "opens with a handful"
    assert len(offer(1)) == 10
    assert len(offer(3)) == 18
    assert len(offer(100)) == len(sigs), "and never more than there are"


def test_the_naming_page_never_runs_dry():
    """A bar that hides everything is worse than one set too low."""
    weak = [_sig(i, 500.0, 60.0, 2, pf=0.9, last_seen=1.0) for i in range(9)]
    assert all(s.evidence < 0.7 for s in weak), "none of these clears the bar"
    got = D.offer_for_naming(weak, 0, 0.7, min_rows=5, start_rows=6, rows_per_name=4)
    assert len(got) == 5, "the best of the rest come along anyway"
    # a named load is always offered, whatever its evidence
    weak[0].name = "Kiln"
    got = D.offer_for_naming(weak, 1, 0.7, min_rows=5, start_rows=6, rows_per_name=4)
    assert weak[0] in got


def test_the_page_can_say_how_many_are_waiting():
    """The page promises it lengthens as loads are named; it has to be able
    to say there is something to lengthen INTO.

    It could not: the caller had only the already-shortened list and
    subtracted it from itself, so every site read "0 more than fit here" -
    Kozolec showing 6 of 12, Home 14 of 60 (the owner's screenshots, 2026-09-22)."""
    sigs = []
    for i in range(30):
        sig = _sig(i, 1000.0 + i * 50, 60.0, 9, pf=0.95, last_seen=1.0)
        sig.hour_wh = [40.0] * 24
        sigs.append(sig)
    assert all(s.evidence >= 0.7 for s in sigs), "these should all clear the bar"

    clear = D.clears_for_naming(sigs, 0.7, min_rows=5)
    assert len(clear) == 30, "the bar decides WHICH, and does not shorten"
    shown = D.offer_for_naming(sigs, 0, 0.7, min_rows=5, start_rows=6, rows_per_name=4)
    assert len(shown) == 6, "the earned length decides HOW MANY"
    assert len(clear) - len(shown) == 24, "and 24 are waiting, not 0"
    # naming eats into the backlog rather than inventing rows
    after = D.offer_for_naming(sigs, 2, 0.7, min_rows=5, start_rows=6, rows_per_name=4)
    assert len(clear) - len(after) == 16


def test_a_reading_measures_what_it_can_resolve_not_just_how_it_jitters():
    """Noise and resolution are different, and the detector measured one.

    A coarse but STEADY reading deviates from its own baseline by nothing at
    all, so its measured noise is zero and the global floor stands in - and
    then its first quantum jump is taken for a load. Home's workshop boiler
    publishes in 46 W steps and was credited with 4 kW of noise."""
    assert D.measure_quantum([46.0 * (i // 3) for i in range(300)]) == 46.0
    # a continuous reading is left alone
    fine = D.measure_quantum([i * 0.01 for i in range(300)])
    assert fine < 0.02, fine
    # and nothing is claimed from too little evidence
    assert D.measure_quantum([0.0, 46.0, 92.0]) == 0.0

    st = D.PhaseState(min_noise=1.0)
    for i in range(400):
        st.process(float(i) * 60.0, 46.0 * (i // 3))
    assert st.quantum == 46.0, st.quantum
    assert st.noise >= 46.0, "a step it cannot resolve is not a step"


def test_a_load_stepping_in_one_size_is_not_the_meters_resolution():
    """Home's phase A, 13 Oct 22:38 local: a hob simmering stepped 150, 150,
    -300 W every 2 s for twenty minutes, so nearly every change of the reading
    was a multiple of 150 W - two 2 W ones among eighty - and the resolution
    test took 144 W for the meter's, the phase's noise 7 -> 144 W, minutes
    after the reading had moved by 1-2 W. One change smaller than a resolution
    disproves it (QUANTUM_MEMORY_BLOCKS)."""
    window = [150.0, 150.0, 300.0] * 26 + [2.0, 2.0]
    assert D.quantum_of_steps(window) == 0.0                       # its smallest change is off the lattice
    assert D.quantum_of_steps([150.0, 150.0, 300.0] * 27) == 150.0  # ...and alone it would pass
    st = D.PhaseState(min_noise=7.0)
    t, w = 0.0, 800.0
    for i in range(300):                                           # a quiet reading first: 1-2 W moves
        w += (1.0 if i % 2 else -1.0) * (1 + i % 2)
        st.process(t, w)
        t += 2.0
    for i in range(600):                                           # then the hob, nothing else
        w += (150.0, 150.0, -300.0)[i % 3]
        st.process(t, w)
        t += 2.0
    assert st.quantum < 10.0 and st.noise < 20.0, (st.quantum, st.noise)
    back = D.PhaseState.from_dict(json.loads(json.dumps(st.to_dict())))
    assert back.change_mins == st.change_mins and back.change_mins


def test_a_power_factor_carries_how_far_wrong_it_could_be():
    """A factor from coarse amps is not thrown away - it is given its error
    bar, and the bar is what stops it constraining anything.

    The owner asked for this rather than the outright gate it replaces: one
    mechanism reads cleaner than a cliff, and a factor that IS well measured
    on a small load still gets to count (2026-09-22)."""
    coarse = D.PhaseState(min_noise=5.0)
    coarse.q_quantum = 23.0                  # 0.1 A at 230 V
    o = D._Open(since=0.0, watts=62.0, var=30.0, levels=[(0.0, 62.0)])
    small = coarse._close(o, 600.0, 62.0, 30.0)
    assert small.pf is not None, "the measurement is kept..."
    assert small.pf_mad > 0.15, f"...and owns its uncertainty ({small.pf_mad})"

    big = D._Open(since=0.0, watts=2500.0, var=600.0, levels=[(0.0, 2500.0)])
    large = coarse._close(big, 600.0, 2500.0, 600.0)
    assert large.pf_mad < 0.02, f"a load many quanta over is measured well ({large.pf_mad})"

    # the same load on amps ten times finer is trusted
    fine = D.PhaseState(min_noise=5.0)
    fine.q_quantum = 2.3
    o2 = D._Open(since=0.0, watts=62.0, var=30.0, levels=[(0.0, 62.0)])
    assert fine._close(o2, 600.0, 62.0, 30.0).pf_mad <= D.PF_TRUST_MAD, \
        "fine enough amps: the classifier may still use this factor"

    # a VAr clamped to zero is no measurement at all, not a precise one
    clamped = D._Open(since=0.0, watts=62.0, var=0.0, levels=[(0.0, 62.0)])
    assert coarse._close(clamped, 600.0, 62.0, 0.0).pf_mad == 1.0


def test_two_factors_agree_when_their_error_bars_overlap():
    """The flat tolerance assumed every factor was measured equally well, and
    so REFUSED matches between sightings of one load at Kozolec."""
    assert D.pf_tolerance(0.0, 0.0) == D.MATCH_PF_TOL, "well measured: unchanged"
    # two badly-resolved factors 0.5 apart are not evidence of two loads
    assert D.pf_tolerance(0.2, 0.2) > 0.5
    # and a wide bar never tightens the test
    assert D.pf_tolerance(0.3, 0.0) > D.pf_tolerance(0.0, 0.0)


def test_where_a_relative_noise_figure_starts_to_mean_something():
    """The 300 W it replaces was wrong in both directions at once - too low
    for Home's noisier phases, too high for Kozolec's quiet one."""
    quiet = D.PhaseState(min_noise=10.0)
    quiet.noise, quiet.quantum = 10.0, 1.0
    assert quiet.rel_floor == 300.0, quiet.rel_floor

    noisy = D.PhaseState(min_noise=10.0)
    noisy.noise, noisy.quantum = 37.0, 1.0
    assert noisy.rel_floor == 1110.0, noisy.rel_floor

    # a coarse reading is held to its resolution even when it sits still
    coarse = D.PhaseState(min_noise=10.0)
    coarse.noise, coarse.quantum = 10.0, 46.0
    assert coarse.rel_floor == 1380.0, coarse.rel_floor

    # one quantum at the floor is exactly the cap, which is the whole idea
    for st in (quiet, noisy, coarse):
        assert abs(max(st.quantum, st.noise) / st.rel_floor
                   - D.NOISE_REL_CAP / D.NOISE_REL_FLOOR_FACTOR) < 1e-9


def test_the_same_constant_serves_both_sites():
    """Every gate is a COUNT of a reading's own quanta, so neither site is
    configured for (2026-09-22)."""
    for value in (D.PF_MIN_QUANTA, D.ENERGY_MIN_QUANTA):
        assert 1.0 <= value <= 50.0, "a count, not a number of watts"
    # Kozolec's 0.1 A at 230 V and Home's 0.01 A: one rule, two answers
    assert D.PF_MIN_QUANTA * 0.1 * 230.0 > 200.0
    assert D.PF_MIN_QUANTA * 0.01 * 230.0 < 30.0


def test_a_grid_meter_filed_as_the_house_is_dropped():
    """power_a means "this reading already IS the house" and wins outright
    over grid-plus-inverters. When setup became three pages the flat fields
    from before stayed put and nothing offers them any more, so a meter
    configured before the change sits in BOTH roles and the older copy quietly
    wins. At the owner's house that was the grid meter read as the house - sign
    inverted, solar never added back, the detector settling on a baseline of
    minus six kilowatts - while the pages he had just filled in did nothing
    (2026-09-22)."""
    home = {"power_a": "sensor.m1_a", "power_b": "sensor.m1_b", "power_c": "sensor.m1_c",
            "grid_power_a": "sensor.m1_a", "grid_power_b": "sensor.m1_b",
            "grid_power_c": "sensor.m1_c",
            "current_a": "sensor.m1_ca", "grid_current_a": "sensor.m1_ca",
            "source_kind": "auto"}
    got = D.drop_stale_load_override(home)
    assert not any(k.startswith("power_") for k in got), got
    assert not any(k == "current_a" for k in got), got
    # everything the grid role owns survives untouched
    assert got["grid_power_a"] == "sensor.m1_a"
    assert got["grid_current_a"] == "sensor.m1_ca"
    assert got["source_kind"] == "auto"


def test_a_real_house_reading_is_left_alone():
    """The override is a feature: a dedicated CT, or a template someone built
    before any of this existed, really is the house and should win. Only the
    same entity in both roles is a duplicate rather than a choice."""
    both = {"power_a": "sensor.house_ct_a", "grid_power_a": "sensor.m1_a"}
    assert D.drop_stale_load_override(both) == both
    alone = {"power_a": "sensor.house_ct_a"}
    assert D.drop_stale_load_override(alone) == alone
    assert D.drop_stale_load_override({}) == {}


def test_a_house_does_not_draw_less_than_nothing():
    """The check that would have caught Home days earlier. A load reading is
    what the house DRAWS, so its quiet floor is a small positive number; when
    it settles deeply negative the reading is something else wearing that name
    - most often a grid meter reporting import as negative, or generation
    still in it with no inverter configured to take it back out. Home sat at
    -6318, -4554 and -4340 W and detected loads in that for days in silence,
    because nothing breaks: sessions still open and close, signatures still
    form, and every one of them is nonsense (2026-09-22)."""
    assert D.implausible_baseline({"a": -6318.0, "b": -4554.0, "c": -4340.0}) == ["A", "B", "C"]
    assert D.implausible_baseline({"a": 120.0, "b": 80.0, "c": 260.0}) == []
    # one phase upside down is worth saying on its own
    assert D.implausible_baseline({"a": -5000.0, "b": 80.0}) == ["A"]
    # a shallow dip is ordinary: the sum is a difference of meters that do not
    # sample together, so it can cross zero briefly without anything being wrong
    assert D.implausible_baseline({"a": -50.0}) == []
    assert D.implausible_baseline({"a": None}) == []
    assert D.implausible_baseline({}) == []


def test_a_small_load_is_described_in_watts():
    """Everything was printed as kilowatts to one decimal, which is fine for a
    kettle and useless for everything a submeter sees. A whole library of an
    office plug - a couple of computers and a power station behind one meter -
    read "0.0 kW on A" line after line, every row identical and none of them
    wrong (2026-09-22)."""
    def row(watts):
        sig = _sig(1, float(watts), 180.0, 50, pf=0.95, last_seen=9 * 86400.0)
        return sig.describe(timezone.utc)
    assert row(28).startswith("28 W on phase A")
    assert row(92).startswith("92 W on phase A")
    assert row(345).startswith("345 W on phase A")
    # and a kilowatt is still a kilowatt
    assert row(4400).startswith("4.4 kW on phase A")
    assert row(5918).startswith("5.9 kW on phase A")
    # the boundary belongs to kW, not to 1000 W
    assert row(999).startswith("999 W")
    assert row(1000).startswith("1.0 kW")


def test_a_motors_starting_surge_is_not_a_load_of_its_own():
    """The owner's pressure pump reads 8886 W in one sample and 830 W in every
    sample after, four times over in a day. Held for the length of a sample
    that single reading dominates the run - a 40 s session came out at 2.8 kW
    for an 830 W pump - and the power it recorded depended on how long the
    session happened to last, so one pump arrived as several loads."""
    t = T0
    pump = D.Run(phases="a", start=t, end=t + 40.0, samples=5,
                     levels={"a": [(t, 8886.0), (t + 10, 833.0),
                                   (t + 20, 818.0), (t + 30, 807.0)]})
    assert round(sum(pump.power_by_phase().values())) == 819, pump.power_by_phase()
    assert round(pump.inrush_w) == 8053


def test_a_first_stage_that_lasts_is_not_a_surge():
    """A washing machine heats before it spins. That is a real stage of a real
    programme, it runs for minutes, and it must not be mistaken for a motor
    coming up to speed - which is over within a sample or two. The window
    comes from the session's own sampling rate, which is what tells ten
    seconds on a slow meter from ten minutes of heating on a fast one."""
    t = T0
    washer = D.Run(phases="a", start=t, end=t + 3000.0, samples=200,
                       levels={"a": [(t, 2000.0), (t + 600, 200.0)]})
    assert round(sum(washer.power_by_phase().values())) == 560
    assert washer.inrush_w == 0.0

    # and a run with a single level has nothing to strip
    flat = D.Run(phases="a", start=t, end=t + 60.0, samples=12,
                     levels={"a": [(t, 1800.0)]})
    assert round(sum(flat.power_by_phase().values())) == 1800
    assert flat.inrush_w == 0.0


def test_the_surge_is_kept_as_evidence_and_survives_a_restart():
    """It is the most diagnostic thing a house produces - only a motor does it
    - so once it is out of the power it is worth keeping as a feature (the owner,
    2026-09-22: "that spike is a very good device signature, but it has to be
    taken into account properly to not show as separate loads")."""
    det = D.Detector()
    t, rows = T0, []
    for _ in range(5):                                 # one pump, started five times
        for _ in range(20):
            rows.append((t, 100.0)); t += 5.0
        rows.append((t, 9000.0)); t += 5.0             # the surge, one sample of it
        for _ in range(12):
            rows.append((t, 930.0)); t += 5.0
        for _ in range(20):
            rows.append((t, 100.0)); t += 5.0
    det.process({"a": rows}, now_ts=t)

    # ONE load, not several, and at what it actually draws
    assert len(det.signatures) == 1, [round(sum(x.power.values())) for x in det.signatures]
    sig = det.signatures[0]
    assert sig.count == 5
    assert 750 <= sum(sig.power.values()) <= 900, sum(sig.power.values())
    # with the surge kept beside it as evidence
    assert sig.inrush_w > 1000, sig.inrush_w
    back = D.Signature.from_dict(sig.to_dict())
    assert round(back.inrush_w, 1) == round(sig.inrush_w, 1)



def test_what_a_switch_and_a_number_taught_follows_a_rename():
    fleet = D.Fleet()
    sig = _sig(1, 635.0, 130.0, 10, phases="c", first_seen=T0, last_seen=T0)
    sig.locations = {D.SWITCH_PREFIX + "climate.t": 6, "Hiša": 10}
    fleet.main.signatures.append(sig)
    fleet.switch_on[D.SWITCH_PREFIX + "climate.t"] = {T0: None}
    fleet.main.numbers["sensor.floor"] = [(T0, 20.0)]
    fleet.rename_entities({"climate.t": "climate.termostat", "sensor.floor": "sensor.tla"})
    assert sig.locations == {D.SWITCH_PREFIX + "climate.termostat": 6, "Hiša": 10}
    assert list(fleet.switch_on) == [D.SWITCH_PREFIX + "climate.termostat"]
    assert list(fleet.main.numbers) == ["sensor.tla"]



def test_a_load_that_runs_in_one_phase_of_a_setting_is_learned_as_its_stage():
    """A washer's heater runs while its cycle phase says Wash, a small share
    of the day; a fridge runs whatever the washer is doing. Only the heater
    is tied to the phase, and being tied is what vouches for it (the owner,
    2026-09-28: "its cycle/sub cycle sensors could help with load detection")."""
    fleet = D.Fleet()
    rnd = random.Random(5)
    t, rows, phase = T0, [], []
    for day in range(14):
        day0 = T0 + day * 86400.0
        phase += [(day0, "Off"), (day0 + 36000.0, "Wash"), (day0 + 37800.0, "Rinse"), (day0 + 40000.0, "Off")]
    t = T0
    while t < T0 + 14 * 86400.0:
        tod = (t - T0) % 86400.0
        w = 300.0 + rnd.uniform(-3, 3)
        if 36300.0 <= tod < 37200.0:
            w += 2000.0                                   # the heater, inside Wash
        if (t - T0) % 5400.0 < 1500.0:
            w += 80.0                                     # a fridge, all day
        rows.append((t, w))
        t += DT
    step = 6 * 3600.0
    a = T0
    while a < t:
        b = a + step
        fleet.process({"a": [r for r in rows if a <= r[0] < b]}, {}, now_ts=b,
                      inputs={"sensor.phase": [p for p in phase if a - D.SWITCH_MEMORY_S <= p[0] < b]
                              or [max((p for p in phase if p[0] < a), default=phase[0])]})
        a = b
    heater = max(fleet.main.signatures, key=lambda x: sum(x.power.values()) * (x.count >= 10))
    fridge = max((x for x in fleet.main.signatures if 50 < sum(x.power.values()) < 120), key=lambda x: x.count)
    got = heater.input_of("sensor.phase")
    assert got is not None and got[0] == "Wash" and got[1] > 0.9 and got[2] > 10, (got, heater.inputs)
    assert fridge.input_of("sensor.phase") is None, fridge.inputs
    share = fleet.main.input_time["sensor.phase"]
    assert 0.01 < share["Wash"] / sum(share.values()) < 0.03, share
    back = D.Detector.from_dict(json.loads(json.dumps(fleet.main.to_dict())))
    assert back.input_time.keys() == fleet.main.input_time.keys()
    assert next(x for x in back.signatures if x.id == heater.id).input_of("sensor.phase")[0] == "Wash"
    fleet.rename_entities({"sensor.phase": "sensor.cyclephase"})
    assert heater.input_of("sensor.cyclephase")[0] == "Wash" and "sensor.cyclephase" in fleet.main.input_time



def test_loads_tied_to_one_setting_are_offered_as_one_device():
    def tied(sid, setting, value):
        sig = _sig(sid, 100.0 * sid, 60.0 * sid, 20, first_seen=T0, last_seen=T0)
        for k in range(12):
            sig.note_input(setting, value, {value: 0.05, "Off": 0.95}, T0 + k * 3600.0)
        return sig
    sigs = [tied(1, "sensor.phase", "Wash"), tied(2, "sensor.phase", "Spin"), tied(3, "fan.x", "33 %"),
            _sig(4, 50.0, 30.0, 20, first_seen=T0, last_seen=T0)]
    assert D.input_groups(sigs) == [[1, 2]]



def _two_starts(overlap: bool, seed: int = 7):
    """Two 80 W loads, one surging for a reading as it starts and one starting
    15 W high: two fridges on their own clocks, which overlap now and then -
    or, with ``overlap`` False, ONE device that starts either way in turn."""
    rnd = random.Random(seed)
    starts = []
    if overlap:
        starts += [(T0 + 600 + k * 7200.0, "A") for k in range(30)]
        starts += [(T0 + 1500 + k * 6420.0, "B") for k in range(33)]
    else:
        starts += [(T0 + 600 + k * 3600.0, "AB"[k % 2]) for k in range(60)]
    end = max(t for t, _ in starts) + 3000.0
    rows, t = [], T0
    while t < end:
        w = 20.0 + rnd.uniform(-1, 1)
        for t0, kind in starts:
            if t0 <= t < t0 + 1500.0:
                w += 80.0
                if kind == "A" and t0 <= t < t0 + DT:
                    w += 740.0                                    # the surge, one reading
                if kind == "B" and t0 <= t < t0 + 25.0:
                    w += 15.0                                     # the bump
        rows.append((t, w))
        t += DT
    det = D.Detector()
    det.process({"a": rows}, now_ts=end)
    by_kind = {"A": set(), "B": set()}
    for t0, kind in starts:
        for r in det.recent:
            if abs(r["start"] - t0) <= 2 * DT:
                by_kind[kind].add(det._moved.get(r["signature"], r["signature"]))
    return det, by_kind


def test_one_device_that_starts_two_ways_is_two_starts_for_now():
    """Home's office plug starts one way and the other. Filed by device, its two
    start sizes are two starts sharing only a stop, and a shared stop no longer
    joins devices (it made the floor mat one with every 600 W load on C), so
    it is two signatures (2026-09-30). Open: join starts that share their stop
    and never run at once - one device never runs twice."""
    det, by_kind = _two_starts(overlap=False)
    assert len(by_kind["A"]) == 1 and len(by_kind["B"]) == 1, by_kind


def test_a_heater_that_runs_only_while_washing_gets_its_own_signature():
    """The washer's 2 kW heater and a 2 kW kettle on the same phase: filed
    by the phase they ran in, the heater ends up tied to Wash and the kettle
    apart from it, while a fridge that runs right through every wash is not
    split in two (2026-09-29)."""
    fleet = D.Fleet()
    rnd = random.Random(11)
    phase = []
    for day in range(14):
        day0 = T0 + day * 86400.0
        phase += [(day0, "Off"), (day0 + 36000.0, "Wash"), (day0 + 37800.0, "Rinse"), (day0 + 40000.0, "Off")]
    rows, t = [], T0
    while t < T0 + 14 * 86400.0:
        tod = (t - T0) % 86400.0
        w = 300.0 + rnd.uniform(-3, 3)
        if 36300.0 <= tod < 37200.0:
            w += 2000.0                                   # the heater, in Wash
        if tod in (7200.0, 54000.0, 72000.0) or 7200.0 < tod < 8100.0 or 54000.0 < tod < 54900.0 or 72000.0 < tod < 72900.0:
            w += 2000.0                                   # a kettle-sized thing, three times a day, never in Wash
        if (t - T0) % 5400.0 < 1500.0:
            w += 80.0                                     # a fridge, all day
        rows.append((t, w))
        t += DT
    a = T0
    while a < t:
        b = a + 6 * 3600.0
        fleet.process({"a": [r for r in rows if a <= r[0] < b]}, {}, now_ts=b,
                      inputs={"sensor.phase": [p for p in phase if a - D.SWITCH_MEMORY_S <= p[0] < b]
                              or [max((p for p in phase if p[0] < a), default=phase[0])]})
        a = b
    big = [x for x in fleet.main.signatures if 1800 < sum(x.power.values()) < 2200 and x.count >= 5]
    tied = [x for x in big if (x.input_of("sensor.phase") or ("",))[0] == "Wash"]
    assert len(tied) == 1 and tied[0].count <= 16, [(x.id, x.count, x.born_in, x.input_of("sensor.phase")) for x in big]
    kettle = [x for x in big if x is not tied[0]]
    assert kettle and max(k.count for k in kettle) >= 30, [(x.id, x.count, x.born_in) for x in big]
    fridge = [x for x in fleet.main.signatures if 50 < sum(x.power.values()) < 120 and x.count >= 5]
    assert max(f.count for f in fridge) >= 200 and not any(f.input_of("sensor.phase") for f in fridge), \
        [(x.id, x.count, x.born_in, x.takes_in) for x in fridge]


def test_a_value_that_came_once_and_stayed_does_not_split_a_load():
    """A fan switched to 33 % on day 16 and left there: every fridge run after it is in the value, every run
    before is out - a perfect lift by timing alone. Home's fridge and freezer stopped matching their named
    signatures that way (791 stray signatures, 2026-10-25). The twin stopped before the fragment began, so it
    goes back."""
    fleet = D.Fleet()
    rnd = random.Random(7)
    days = 20
    phase = [(T0, "off"), (T0 + 16 * 86400.0, "33 %")]
    rows, t = [], T0
    while t < T0 + days * 86400.0:
        w = 300.0 + rnd.uniform(-3, 3)
        if (t - T0) % 1500.0 < 400.0:
            w += 80.0
        rows.append((t, w))
        t += DT
    a = T0
    while a < t:
        b = a + 6 * 3600.0
        fleet.process({"a": [r for r in rows if a <= r[0] < b]}, {}, now_ts=b,
                      inputs={"fan.x": [p for p in phase if a - D.SWITCH_MEMORY_S <= p[0] < b]
                              or [max((p for p in phase if p[0] < a), default=phase[0])]})
        a = b
    fridge = [x for x in fleet.main.signatures if 50 < sum(x.power.values()) < 120 and x.count >= 3]
    assert len(fridge) == 1 and not fridge[0].born_in, \
        [(x.id, x.count, x.born_in, x.born_judged, x.takes_in) for x in fridge]


def _fridge_through_a_washer(days):
    """A fridge (80 W for 400 s every 25 min, all day) in a house whose washer's phase reads "Spin" 15 min a day."""
    fleet = D.Fleet()
    rnd = random.Random(5)
    phase = []
    for day in range(days):
        day0 = T0 + day * 86400.0
        phase += [(day0, "Off"), (day0 + 36000.0, "Spin"), (day0 + 36900.0, "Off")]
    rows, t = [], T0
    while t < T0 + days * 86400.0:
        w = 300.0 + rnd.uniform(-3, 3)
        if (t - T0) % 1500.0 < 400.0:
            w += 80.0
        rows.append((t, w))
        t += DT
    a = T0
    while a < t:
        b = a + 6 * 3600.0
        fleet.process({"a": [r for r in rows if a <= r[0] < b]}, {}, now_ts=b,
                      inputs={"select.washer": [p for p in phase if a - D.SWITCH_MEMORY_S <= p[0] < b]
                              or [max((p for p in phase if p[0] < a), default=phase[0])]})
        a = b
    return fleet


def test_a_family_that_runs_in_a_value_at_chance_takes_its_runs_without_a_fragment():
    """An established fridge runs through the washer's rare phase at chance: its runs there join it at once, the
    way the judgement would after three more days - no signature is born in the value (Home's kiln through the
    washer's Dry, Wash and Rinse, 2026-10-25: nine fragments, none of them named)."""
    fleet = _fridge_through_a_washer(3)
    sigs = fleet.main.signatures
    fridge = [x for x in sigs if 50 < sum(x.power.values()) < 120 and x.count >= 3]
    assert len(fridge) == 1 and not any(x.born_in for x in sigs), [(x.id, x.count, x.born_in) for x in sigs]
    assert all(x.family for x in sigs)


def _hint_family(minority_hours, minority_len=470.0, n_rest=120, n_rare=12):
    """A family whose runs are mostly short ones all day and a few long ones at ``minority_hours``."""
    det = D.Detector()
    sig = D.Signature(id=1, phases="b", power={"b": 2060.0}, duration_s=150.0, pf=None, count=n_rest + n_rare,
                      first_seen=T0, last_seen=T0 + 86400.0, family=1)
    det.signatures.append(sig)
    rnd = random.Random(2)
    det.family_runs[1] = ([(140.0 + rnd.uniform(-30, 40), rnd.uniform(10, 21)) for _ in range(n_rest)]
                          + [(minority_len + rnd.uniform(-150, 300), minority_hours[k % len(minority_hours)] + rnd.uniform(0, 0.9))
                             for k in range(n_rare)])
    return det, sig


def test_a_minority_mode_of_length_that_keeps_to_a_few_hours_splits_its_family_in_the_repair():
    """Home's Workshop boiler against Hiša's 2 kW element on one phase: 11 runs of 273-992 s at 12:00-15:00 among 134
    of about 140 s over the day. Length alone or the hour alone never splits (spec section 11)."""
    det, sig = _hint_family([12.0, 13.0, 14.0])
    D._hint_split(det)
    assert sig.split_s is not None and 200.0 < sig.split_s < 400.0 and sig.split_side == -1, (sig.split_s, sig.split_side)
    assert not sig.takes_len(470.0) and sig.takes_len(140.0)


def test_one_hint_alone_does_not_split_a_family():
    det, sig = _hint_family([6.0, 10.0, 14.0, 18.0, 22.0])        # long runs all over the day: the hour hint is missing
    D._hint_split(det)
    assert sig.split_s is None
    det, sig = _hint_family([12.0, 13.0, 14.0], minority_len=150.0)   # at noon, but as long as the rest: the length hint is missing
    D._hint_split(det)
    assert sig.split_s is None


def test_a_name_waiting_for_its_load_after_a_rebuild_still_has_its_entities():
    """A rebuild that finds no run of a named load in the recorder's days leaves its name waiting (orphan_names):
    its entities go on being made (Fleet.entity_names), and the name is still one when the library has it."""
    fleet = D.Fleet()
    fleet.carry_names([{"name": "Kiln", "phases": "ac", "power": {"a": 2900.0, "c": 2900.0}, "duration_s": 50.0, "pf": None},
                       {"name": "Pump", "phases": "a", "power": {"a": 850.0}, "duration_s": 120.0, "pf": None, "meter": "Mansarda"}])
    assert fleet.names() == {} and fleet.entity_names() == ["Kiln", "Pump"]
    fleet.main.signatures.append(D.Signature(id=1, phases="b", power={"b": 100.0}, duration_s=60.0, pf=None, count=3,
                                             first_seen=T0, last_seen=T0, name="Fan", family=1))
    assert fleet.entity_names() == ["Fan", "Kiln", "Pump"]


def test_a_meter_set_on_the_settings_page_beats_its_own_classification():
    """Fleet.cadence_forced: True reports on change, False is polled, a meter missing from it is classified from
    its gaps (PhaseState._classify)."""
    old = D.SUSTAIN_CADENCES_CHANGE
    D.SUSTAIN_CADENCES_CHANGE = 2.0
    try:
        polled = D.PhaseState()
        polled.interval = 4.0
        assert polled.latency() == 12.0                    # auto, never classified: polled
        polled.forced = True
        assert polled.latency() == 8.0
        sparse = D.PhaseState()
        sparse.interval = 4.0
        sparse.all_gaps = [4.0] * 70 + [60.0] * 30         # 30 % of the gaps are long: reports on change
        sparse._classify()
        assert sparse.latency() == 8.0
        sparse.forced = False
        assert sparse.latency() == 12.0
        fleet = D.Fleet()
        fleet.cadence_forced = {"Plug": True}
        fleet.process({}, {"Plug": {"a": [(T0, 10.0)]}, "Other": {"a": [(T0, 10.0)]}}, now_ts=T0 + 60)
        assert fleet.subs["Plug"].phases["a"].forced is True
        assert fleet.subs["Other"].phases["a"].forced is None
    finally:
        D.SUSTAIN_CADENCES_CHANGE = old


def test_a_family_is_named_as_a_whole_and_survives_a_store():
    fleet = _fridge_through_a_washer(3)
    det = fleet.main
    big = max(det.signatures, key=lambda x: x.count)
    twin = D.Signature(id=999, phases=big.phases, power=dict(big.power), duration_s=big.duration_s, pf=big.pf, count=2,
                       first_seen=big.first_seen, last_seen=big.last_seen, family=big.family)
    det.signatures.append(twin)                      # a second signature of the same family
    assert det.rename_family(big.family, "Fridge")
    assert big.name == twin.name == "Fridge"
    back = D.Detector.from_dict(det.to_dict())
    assert {x.family for x in back.signatures if x.name == "Fridge"} == {big.family}
    assert back.next_family == det.next_family


def test_a_library_stored_without_families_gets_them_and_one_name_is_one_family():
    fleet = _fridge_through_a_washer(3)
    det = fleet.main
    big = max(det.signatures, key=lambda x: x.count)
    twin = D.Signature(id=999, phases=big.phases, power=dict(big.power), duration_s=big.duration_s, pf=big.pf, count=2,
                       first_seen=big.first_seen, last_seen=big.last_seen, name="Fridge")
    big.name = "Fridge"
    det.signatures.append(twin)
    stored = det.to_dict()
    for x in stored["signatures"]:
        x.pop("family", None)
    back = D.Detector.from_dict(stored)
    assert all(x.family for x in back.signatures)
    assert len({x.family for x in back.signatures if x.name == "Fridge"}) == 1


def _drifting_kiln(sizes, noise=30.0):
    """A detector whose two-phase kiln start is cut into one start cluster per size in ``sizes``, each with a
    signature of its own on the phase set, the way a drifting start size is cut (Home's kiln, 2026-10-26)."""
    det = D.Detector()
    for ph in "ac":
        det.phases[ph] = D.PhaseState()
        det.phases[ph].noise = noise
    for i, w in enumerate(sizes, 1):
        det.edges.append(D.StepGroup(id=i, phase="ac", up=True, watts=w, count=40.0, watts_mad=60.0))
        det.signatures.append(D.Signature(id=100 + i, phases="ac", power={"a": w / 2, "c": w / 2 - 50}, duration_s=45.0,
                                          pf=None, count=20, first_seen=T0, last_seen=T0))
        det.pair_home[str(i)] = {100 + i: 20.0}
    det._votes = None
    det._assign_families()
    return det


def _kiln_pulsing_fast(c_lag_s):
    """A two-phase 2.9 kW kiln pulsing 40 s on, 14 s off (shorter than three of the 5 s cadences), read only when
    a value changes, every reading of the c leg ``c_lag_s`` after the a leg's: Home's Hiša on 25 Oct, a at .155 and
    c at .156, one reading in each off gap."""
    det = D.Detector()
    rows = {"a": [], "c": []}
    rnd = random.Random(3)
    for k in range(40):                              # idle: a reading every 5 s until the first pulse
        rows["a"].append((T0 + 5.0 * k, 34.0 + rnd.uniform(-4, 4)))
        rows["c"].append((T0 + 5.0 * k + c_lag_s, 280.0 + rnd.uniform(-4, 4)))
    t = T0 + 200.0
    while t < T0 + 3600.0:
        rows["a"].append((t, 3000.0 + rnd.uniform(-8, 8)))
        rows["c"].append((t + c_lag_s, 3200.0 + rnd.uniform(-8, 8)))
        for dt_ in (10.0, 20.0, 30.0):               # the pulse is read every 10 s
            rows["a"].append((t + dt_, 3000.0 + rnd.uniform(-8, 8)))
            rows["c"].append((t + dt_ + c_lag_s, 3200.0 + rnd.uniform(-8, 8)))
        rows["a"].append((t + 40.0, 34.0))
        rows["c"].append((t + 40.0 + c_lag_s, 280.0))
        t += 54.0
    det.process(rows, now_ts=T0 + 3700.0)
    return {ph: sum(x.count for x in det.signatures if x.phases == ph) for ph in ("a", "c", "ac")}


def test_the_legs_of_one_pulse_stamped_a_millisecond_apart_stop_together():
    """A fall on one leg is corroborated by the other leg's reading in the same instant, even when that reading
    is stamped a millisecond after (SAME_UPDATE_S); without it the leg whose reading came first never closed a
    pulse shorter than the sustain, and every later pulse of it ran on as one."""
    runs = _kiln_pulsing_fast(0.001)
    assert runs["ac"] >= 55 and runs["a"] + runs["c"] <= 3, runs


def test_families_of_neighbouring_start_clusters_with_alike_signatures_are_one():
    det = _drifting_kiln([5764.0, 5837.0, 5914.0, 6039.0])
    assert len({x.family for x in det.signatures}) == 1, [x.family for x in det.signatures]
    det.rename(101, "Kiln")
    assert {x.name for x in det.signatures} == {"Kiln"}


def test_families_of_start_clusters_further_apart_than_their_spread_stay_apart():
    det = _drifting_kiln([5764.0, 6400.0])
    assert len({x.family for x in det.signatures}) == 2


def test_a_start_too_small_for_its_noise_does_not_join_families():
    """Steps a few noises tall: every cluster's spread reaches every look-alike, and nothing may be called one."""
    det = _drifting_kiln([5764.0, 5837.0], noise=400.0)
    assert len({x.family for x in det.signatures}) == 2


def test_an_all_day_load_born_in_a_rare_value_goes_back_to_its_twin():
    """A fridge that runs through every episode of a short daily value passes
    the coverage judgement in every value (it ran in all of them), so a
    signature of it born in the value stayed its own for good - Hiša's 60 W
    incubator in 20 pieces with the washer's states fed (2026-10-23). An
    alike ordinary twin that runs through the value at chance takes it
    back at judgement."""
    fleet = D.Fleet()
    rnd = random.Random(5)
    phase = []
    for day in range(14):
        day0 = T0 + day * 86400.0
        phase += [(day0, "Off"), (day0 + 36000.0, "Spin"), (day0 + 36900.0, "Off")]   # 15 min a day
    rows, t = [], T0
    while t < T0 + 14 * 86400.0:
        w = 300.0 + rnd.uniform(-3, 3)
        if (t - T0) % 1500.0 < 400.0:
            w += 80.0                                     # the fridge, every 25 min, all day
        rows.append((t, w))
        t += DT
    a = T0
    while a < t:
        b = a + 6 * 3600.0
        fleet.process({"a": [r for r in rows if a <= r[0] < b]}, {}, now_ts=b,
                      inputs={"sensor.phase": [p for p in phase if a - D.SWITCH_MEMORY_S <= p[0] < b]
                              or [max((p for p in phase if p[0] < a), default=phase[0])]})
        a = b
    fridge = [x for x in fleet.main.signatures if 50 < sum(x.power.values()) < 120 and x.count >= 3]
    assert len(fridge) == 1 and fridge[0].count >= 700 and not fridge[0].born_in, \
        [(x.id, x.count, x.born_in, x.born_judged, x.takes_in) for x in fridge]



def test_a_load_keeps_its_energy_by_the_clock_hour_for_the_backfill():
    """Naming a load writes its past into the statistics, so each signature
    keeps its energy per clock hour for HOURLY_KEEP_S (2026-09-29)."""
    sig = _sig(1, 600.0, 1800.0, 0, first_seen=T0, last_seen=T0)
    hour0 = int(T0 // 3600 * 3600)
    s = D.Run(phases="a", start=hour0 + 3000.0, end=hour0 + 4800.0, levels={"a": [(hour0 + 3000.0, 600.0)]})
    sig._spread(s, timezone.utc)
    assert {h: round(w) for h, w in sig.hourly.items()} == {hour0: 100, hour0 + 3600: 200}, sig.hourly
    back = D.Signature.from_dict(json.loads(json.dumps(sig.to_dict())))
    assert back.hourly == {hour0: 100.0, hour0 + 3600: 200.0}
    late = D.Run(phases="a", start=hour0 + D.HOURLY_KEEP_S + 7200.0, end=hour0 + D.HOURLY_KEEP_S + 7260.0,
                     levels={"a": [(hour0 + D.HOURLY_KEEP_S + 7200.0, 600.0)]})
    sig._spread(late, timezone.utc)
    assert hour0 not in sig.hourly and len(sig.hourly) == 1          # the old hours aged out
    det = D.Detector()
    sig.name = "Mat"
    det.signatures.append(sig)
    assert list(det.hourly_by_name("Mat").values()) == [10.0]


def test_an_inputs_lag_is_where_its_changes_pile_up_against_the_edges():
    bins = int(2 * D.EDGE_LAG_REACH_S / D.EDGE_LAG_BIN_S)
    even = [2.0] * bins                                     # chance: spread evenly
    assert D.lag_window(even) is None
    piled = list(even)
    at = int((-5.0 + D.EDGE_LAG_REACH_S) / D.EDGE_LAG_BIN_S)
    piled[at] += 60.0                                       # a thermostat telling 5 s before the meter
    piled[at - 1] += 20.0
    lo, hi = D.lag_window(piled)
    assert lo < -5.0 < hi and hi - lo < 20.0, (lo, hi)
    assert D.lag_window([0.0] * bins) is None                # nothing seen yet


def test_a_load_switched_by_an_input_learns_that_input_at_its_edges():
    """Home's floor mat: +635 W on C about 5 s after its thermostat turns on,
    -635 W about 5 s after it turns off; a look-alike of the same size runs
    with the thermostat idle. Their edges must not share clusters, and the
    mat's story must say which input moves with it (2026-09-29)."""
    fleet = D.Fleet()
    rows, spans, t = [], [], T0
    base = 200.0
    def hold(w, secs):
        nonlocal t
        for _ in range(int(secs / 5)):
            rows.append((t, w))
            t += 5.0
    hold(base, 600)
    for k in range(60):
        spans.append((t - 5.0, t + 295.0))            # the thermostat tells 5 s early
        hold(base + 635.0, 300)                       # the mat
        hold(base, 600)
        if k % 2:
            hold(base + 630.0, 200)                   # the look-alike, thermostat idle
            hold(base, 600)
    temps = [(ts, 19.0 + (ts - T0) % 900 / 450) for ts, _ in rows[::10]]
    step = 6 * 3600.0
    a = T0
    while a < t:
        b = a + step
        fleet.process({"c": [r for r in rows if a <= r[0] < b]}, {}, now_ts=b,
                      switches={"climate.mat": [(on, off if off < b else None) for on, off in spans if on < b and off > a - D.SWITCH_MEMORY_S]},
                      numbers={"sensor.room": [r for r in temps if a - D.SWITCH_MEMORY_S <= r[0] < b]})
        a = b
    det = fleet.main
    assert D.lag_window(det.lag_hist["climate.mat"]), det.lag_hist
    keyed = [c for c in det.edges if c.keys.get("climate.mat")]
    assert keyed and all(abs(c.watts - 635.0) < 60.0 for c in keyed), [(c.watts, c.keys) for c in keyed]
    # the mat's signature is the one its thermostat-keyed edges filed into;
    # its first runs, before the lag was learned, stay with the look-alike's
    mat = max((x for x in det.signatures if x.phases == "c"),
              key=lambda x: (bool((D.edge_story(det.edges, x).get("start") or {}).get("signals", {}).get("climate.mat")), x.count))
    story = D.edge_story(det.edges, mat)
    starts, stops = story["start"]["signals"]["climate.mat"], story["stop"]["signals"]["climate.mat"]
    assert starts["kind"] == "off→on" and stops["kind"] == "on→off", story
    assert -8.0 < starts["lag_s"] < -2.0, starts
    assert "sensor.room" in story["start"]["values"], story["start"]
    back = D.StepGroup.from_dict(keyed[0].to_dict())
    assert back.keys == keyed[0].keys and back.watts == keyed[0].watts


def test_an_input_flipping_back_does_not_end_a_run_the_meter_still_shows():
    """Home's kettle and panini press: 1.5 kW on Mansarda's B that starts as
    the living area's motion sensor - flipping every ~10 s while someone is in
    the kitchen - goes off. Keyed to that, INPUT_ENDS ended every run at the
    sensor's next flip back on: 29 s of a 298 s panini run (2026-10-12),
    with the phase flat at +1.5 kW throughout. A stop hidden in another
    load's step still leaves a step on the phase; with none, the run is on."""
    fleet = D.Fleet()
    rows, flips, t = [], [], T0
    base = 300.0
    def hold(w, secs):
        nonlocal t
        for _ in range(int(secs / 5)):
            rows.append((t, w))
            t += 5.0
    hold(base, 600)
    for k in range(60):
        on = t - 10.0
        for j in range(32):                       # someone in the kitchen: 10 s on, 10 s off, ...
            flips.append((on + 20.0 * j, on + 20.0 * j + 10.0))
        hold(base + 1500.0, 300)                  # ...the kettle starts as the sensor goes off
        hold(base, 1800)
    step = 6 * 3600.0
    a = T0
    while a < t:
        b = a + step
        fleet.process({"b": [r for r in rows if a <= r[0] < b]}, {}, now_ts=b,
                      switches={"binary_sensor.motion": [(x, y if y < b else None) for x, y in flips
                                                         if x < b and y > a - D.SWITCH_MEMORY_S]})
        a = b
    kettle = [x for x in fleet.main.signatures if 1300.0 < sum(x.power.values()) < 1700.0]
    runs = [y - x for k in kettle for x, y in k.runs]
    assert sum(k.count for k in kettle) >= 55, [(k.id, k.count) for k in kettle]
    assert runs and min(runs) > 250.0, sorted(round(r) for r in runs)


def _named_kettle():
    """A fleet that has learned a 1.5 kW, 300 s load on B and named it Kettle,
    and the time its readings reach."""
    fleet = D.Fleet()
    rows, t = [], T0
    for k in range(25):
        for w, secs in ((300.0, 1500 + 37 * (k % 5)), (1800.0, 300)):
            for _ in range(int(secs / 5)):
                rows.append((t, w))
                t += 5.0
    for _ in range(60):
        rows.append((t, 300.0))
        t += 5.0
    fleet.process({"b": rows}, {}, now_ts=t)
    sig = max((x for x in fleet.main.signatures if 1300.0 < sum(x.power.values()) < 1700.0), key=lambda x: x.count)
    assert fleet.rename(("", sig.id), "Kettle")
    return fleet, t


def _feed(fleet, t, steps):
    """Feed (watts, seconds) after ``t`` in one pass; returns where it ends."""
    rows = []
    for w, secs in steps:
        for _ in range(int(secs / 5)):
            rows.append((t, w))
            t += 5.0
    fleet.process({"b": rows}, {}, now_ts=t)
    return t


def test_a_named_load_reads_its_power_while_its_run_is_still_open():
    """The owner, 2026-10-13: the kettle's power shows as soon as its start is
    declared, not when its run has closed - and a start whose stop never comes
    stops counting instead of sitting high (LIVE_STALE_FACTOR)."""
    fleet, t = _named_kettle()
    marks, shown = {}, {}
    fleet.live_power(t, 60.0, marks, shown)
    t2 = _feed(fleet, t, [(300.0, 60), (1800.0, 120)])     # on, no stop yet
    live = fleet.live_power(t2, t2 - t, marks, shown)
    assert 1400.0 < live["Kettle"] < 1600.0, live
    assert fleet.main.live_runs(t2 + 3600.0) == []           # open for an hour: no longer counted


def test_open_starts_never_add_up_to_more_than_the_phase_carries():
    """Kozolec's Apartment Fridge read 226 W as 60 + 83 + 83: starts whose stops
    were never seen stayed open and were summed (2026-10-18). What is on now is
    clipped to the phase's level above its baseline, per name and in total."""
    import copy
    fleet, t = _named_kettle()
    t2 = _feed(fleet, t, [(300.0, 60), (1800.0, 120)])     # the kettle is on
    st = next(x for x in fleet.main.phases.values() if x.open_edges)
    one = fleet.main.live_runs(t2)
    assert len(one) == 1 and 1400.0 < one[0][2] < 1600.0, one
    st.open_edges.append(copy.copy(st.open_edges[0]))      # two more starts that never ended
    st.open_edges.append(copy.copy(st.open_edges[0]))
    carried = st.level - st.baseline
    assert sum(w for _, _, w in fleet.main.live_runs(t2)) <= carried + 1e-6
    assert fleet.main.active(t2)[0]["watts"] <= round(carried)
    assert fleet.main.unknown_power(t2) <= carried + 1.0
    st.baseline = None                                     # nothing to measure against: as before
    assert st.open_scale() == 1.0


def test_a_named_loads_live_power_never_reads_far_above_its_signatures():
    """Home's Inkubator read 574 W off four stacked open starts and a lumped stretch
    of energy, its signatures topping out at 75 W (2026-10-28, the owner: "a
    detected load should never be significantly above its signature group").
    The phase's level no longer stops it (nothing to measure against), so only
    the ceiling does: LIVE_CAP_FACTOR times its strongest signature."""
    import copy
    fleet, t = _named_kettle()
    marks, shown = {}, {}
    fleet.live_power(t, 60.0, marks, shown)
    t2 = _feed(fleet, t, [(300.0, 60), (1800.0, 120)])     # the kettle is on
    st = next(x for x in fleet.main.phases.values() if x.open_edges)
    st.baseline = None
    for _ in range(4):
        st.open_edges.append(copy.copy(st.open_edges[0]))   # four more starts that never ended
    top = max(s.watts for s in fleet.main.signatures if s.name == "Kettle")
    assert sum(w for _, _, w in fleet.main.live_runs(t2)) > 3.0 * top           # stacked, before the ceiling
    live = fleet.live_power(t2, t2 - t, marks, shown)["Kettle"]
    assert 0.9 * top < live <= D.LIVE_CAP_FACTOR * top + 1e-6, (live, top)


def test_an_open_start_stops_counting_after_the_usual_run_not_the_longest_ever_seen():
    """One 2,000 s fragment among a name's runs kept the Inkubator's 20 s pulses
    counted open for 55 minutes (LIVE_STALE_QUANTILE, 2026-10-28) - the name's
    runs being what a group with no usual length of its own falls back on."""
    fleet, t = _named_kettle()
    t2 = _feed(fleet, t, [(300.0, 60), (1800.0, 120)])     # on for 120 s, no stop yet
    fleet.main.order.clear()                               # its group's own length unknown
    sig = next(s for s in fleet.main.signatures if s.name == "Kettle")
    sig.runs = [(i * 1000.0, i * 1000.0 + 30.0) for i in range(20)] + [(30000.0, 32000.0)]
    assert fleet.main.live_runs(t2) == []                   # 120 s open against 2 x 30 s: stale
    sig.runs = [(i * 1000.0, i * 1000.0 + 300.0) for i in range(21)]
    assert len(fleet.main.live_runs(t2)) == 1               # the same start against runs of 300 s: live
    assert D._quantile([], 0.95) == 0.0 and D._quantile([1.0, 2.0, 3.0], 0.95) == 3.0


def test_a_named_run_that_began_and_ended_between_two_readings_comes_in_as_its_energy():
    """A run no reading saw open still counts, spread over the stretch (the
    kiln's 44 s pulses, 2026-09-18) - and one a reading did see open is not
    counted again when it closes."""
    fleet, t = _named_kettle()
    marks, shown = {}, {}
    fleet.live_power(t, 60.0, marks, shown)
    t2 = _feed(fleet, t, [(300.0, 60), (1800.0, 300), (300.0, 600)])   # a whole run inside one stretch
    got = fleet.live_power(t2, t2 - t, marks, shown)["Kettle"]
    assert abs(got - 1500.0 * 300.0 / (t2 - t)) < 0.15 * 1500.0 * 300.0 / (t2 - t), got
    t3 = _feed(fleet, t2, [(1800.0, 120)])
    assert fleet.live_power(t3, t3 - t2, marks, shown)["Kettle"] > 1400.0          # seen open...
    t4 = _feed(fleet, t3, [(1800.0, 180), (300.0, 600)])
    assert fleet.live_power(t4, t4 - t3, marks, shown)["Kettle"] == 0.0             # ...so not again once filed


def test_a_load_powered_through_a_switch_is_not_given_a_run_the_switch_was_off_for():
    """Home's floor mat draws through its thermostat's relay: a 600 W run on C
    while the thermostat was not heating is another load (2026-09-29)."""
    fleet = D.Fleet()
    sw = D.SWITCH_PREFIX + "climate.thermostat"
    fleet.switch_on = {sw: {T0: T0 + 300.0, T0 + 3600.0: None}}          # on for 5 min, and again from an hour on
    mat = _sig(1, 635.0, 200.0, 100, phases="c", first_seen=T0, last_seen=T0, locations={sw: 45, "Hiša": 50})
    fridge = _sig(2, 600.0, 900.0, 100, phases="c", first_seen=T0, last_seen=T0, locations={sw: 10})
    fleet.main.signatures = [mat, fridge]
    run = lambda a, b: D.Run(phases="c", start=a, end=b, levels={"c": [(a, 600.0)]})
    assert fleet._switched_off(run(T0 + 1000.0, T0 + 1200.0), 2.0) == [1]  # off throughout: not the mat
    assert fleet._switched_off(run(T0 + 100.0, T0 + 250.0), 2.0) == []     # while it was on
    assert fleet._switched_off(run(T0 + 3000.0, T0 + 3700.0), 2.0) == []   # on for part of it
    fleet.switch_on = {sw: {}}
    assert fleet._switched_off(run(T0 + 1000.0, T0 + 1200.0), 2.0) == [1]  # fed, and off for all it remembers
    fleet.switch_on = {}
    assert fleet._switched_off(run(T0 + 1000.0, T0 + 1200.0), 2.0) == []   # nothing on record: no say



def test_a_signature_on_twice_at_once_counts_the_overlap_once():
    """One device never runs twice at once: Home's 635 W mat was credited
    1.96 kWh in one hour when other runs of its size overlapped its own."""
    sig = _sig(1, 600.0, 1800.0, 0, phases="c", first_seen=T0, last_seen=T0)
    hour0 = int(T0 // 3600 * 3600)
    for a, b in ((hour0, hour0 + 1800.0), (hour0 + 900.0, hour0 + 2700.0)):
        sig.runs.append((a, b))                                     # as add() does, before spreading
        sig._spread(D.Run(phases="c", start=a, end=b, levels={"c": [(a, 600.0)]}), timezone.utc)
    assert round(sig.hourly[hour0]) == 450, sig.hourly              # 45 minutes on, not 60
    assert D._uncovered(0, 10, [(2, 3), (1, 4), (6, 12)]) == [(0, 1), (4, 6)]
    assert D._uncovered(0, 10, []) == [(0, 10)] and D._uncovered(0, 10, [(-5, 20)]) == []



def test_a_groups_usual_length_comes_from_its_partners_by_order():
    """The step pairs learned from closes went (run lifecycle, 2026-10-28): a
    group's usual length is its partners' learned length, weighted by falls."""
    det = _order_cycles(90.0)
    assert det.partners_of(1) == [9] and abs(det.usual_length(1) - 90.0) < 1.0
    assert _order_cycles(500.0).usual_length(1) is None


def test_a_pair_is_accepted_when_it_is_far_above_chance():
    """A switch-on closing 40 runs and a switch-off closing 30 of 1000 on the
    phase meet 1.2 times by chance: 20 is a pair, 3 is not (2026-09-30)."""
    assert D.above_chance(20, 40 * 30 / 1000)
    assert not D.above_chance(3, 40 * 30 / 1000)
    assert not D.above_chance(10, 12.0) and not D.above_chance(5, 0.0)


def test_steps_join_the_cluster_their_size_piles_up_in():
    """Two loads 7 % apart are two groups; one load wandering +-3 % is held
    by at most two neighbouring groups once the daily pass has joined what it
    founded beside its spread (mix_daily) - a uniform 120 W wide pile at 2 kW
    stays two at MIX_JOIN_WIDTHS 2, 62 W apart (2026-09-30; the mixture's
    since 2026-10-29, dev/TODO.md INS-32). Its runs still file as one device
    where both pair with its stop group (device_of)."""
    rnd = random.Random(3)
    det = D.Detector()
    got = {}
    t = T0
    for k in range(400):
        w = rnd.choice([600.0, 645.0]) * (1 + rnd.gauss(0, 0.006))
        got.setdefault(round(w / 645.0), set()).add(det._classify_step("a", t, w, None, 0.0).id)
        t += 60.0
    wander = {det._classify_step("a", t + 60.0 * k, 2000.0 * (1 + rnd.uniform(-0.03, 0.03)), None, 0.0).id for k in range(200)}
    det.mix_daily()
    main = lambda ids: max(ids, key=lambda i: next(c.count for c in det.edges if c.id == i))  # noqa: E731
    assert main(got[1]) != main(got[round(600 / 645)]) or len(got) == 1, got
    six, forty = [c for c in det.edges if abs(c.watts - 600) < 15], [c for c in det.edges if abs(c.watts - 645) < 15]
    assert six and forty and max(six, key=lambda c: c.count).count > 150 and max(forty, key=lambda c: c.count).count > 150, \
        [(round(c.watts), c.count) for c in det.edges]
    kept = [c for c in det.edges if c.id in wander and c.joined is None]
    assert len(kept) <= 2 and sum(c.count for c in kept) >= 190, [(round(c.watts), c.count, c.joined) for c in det.edges if c.id in wander]
    assert D.valley_segments({}) == []


def test_a_creep_rise_does_not_end_the_big_run_it_follows():
    """Home's phase A, 24 Sep 03:41 UTC: Mansarda's washer-dryer starts +261 W
    and creeps +42 W six seconds on. The size scale's unit had frozen at the
    phase's first rise of the window, three minutes after a dawn seed read
    372 W of noise - a measurement error of 93 W, against 10 W the rest of the
    ten days - so one density segment ran 14-300 W, the creep rise was the big
    start's own cluster "starting again", and it ended the 261 W run 6 s in
    (2026-10-04). The unit follows the phase's quietest measured noise down
    (EDGE_NOISE_SHARE, Detector._rescale): the run lasts to its stop, read in
    one call or in ten-minute passes alike."""
    rnd = random.Random(0)
    on = [(150.0, 210.0, 1000.0)]                       # a kettle while the seed still swings
    t = 1800.0
    while t < 20000.0:                                  # a quiet night of small loads, 12-40 W
        on.append((t, t + rnd.uniform(20.0, 60.0), rnd.choice((12.0, 14.0, 16.0, 20.0, 24.0, 30.0, 40.0))))
        t += rnd.uniform(40.0, 100.0)
    big = 15000.0
    on += [(big, big + 600.0, 261.0), (big + 20.0, big + 600.0, 42.0)]
    rows, t = [], T0
    while t < T0 + 21600.0:
        s = t - T0
        swing = rnd.uniform(-250.0, 250.0) if s < 125.0 else rnd.uniform(-3.0, 3.0)
        rows.append((t, 450.0 + swing + sum(w for a, b, w in on if a <= s < b)))
        t += 5.0
    end = T0 + 22200.0
    whole = D.Detector().process({"a": rows}, now_ts=end)
    det, sliced = D.Detector(), []
    for part, e in _passes({"a": rows}, [T0 + 600.0 * k for k in range(1, 37)], end):
        sliced += det.process(part, now_ts=e)
    assert _as_filed(sliced) == _as_filed(whole)
    run = [s for s in whole if abs(s.start - (T0 + big)) < 10]
    assert run and max(s.duration_s for s in run) > 580, [(round(s.start - T0 - big), round(s.duration_s)) for s in run]
    assert det.edge_unit["a|1"] < 5.0, det.edge_unit          # a quarter of the quiet night's 10 W, not the seed's swing


def test_runs_open_at_a_fall_to_the_idle_floor_close_there():
    """Home's phase A, 24 Sep 04:05 UTC: Mansarda's washer-dryer starts +282 W
    and creeps to ~395 W below the step threshold while small loads run beside
    it, so no run is followed; it stops -394 W, back to the idle floor. The
    fall fits no run and no multi-close (282 + 21 + 14 against 394), and the
    idle-floor drop discarded every run still open - no session at all
    (2026-10-04). A fall to the floor means what was on went off: the runs
    still open close at it, read in one call or in ten-minute passes alike."""
    rnd = random.Random(0)
    small, big, stop = (1000.0, 60.0), 1500.0, 4000.0
    rows, t = [], T0
    while t < T0 + 6000.0:
        s = t - T0
        w = 450.0 + rnd.uniform(-2.0, 2.0)
        if small[0] <= s < stop:
            w += small[1]
        if big <= s < stop:
            w += 200.0 + min(100.0, 0.04 * (s - big))   # +200 W, then 100 W more over 2500 s, unseen
        rows.append((t, w))
        t += 5.0
    end = T0 + 6600.0
    whole = D.Detector().process({"a": rows}, now_ts=end)
    det, sliced = D.Detector(), []
    for part, e in _passes({"a": rows}, [T0 + 600.0 * k for k in range(1, 11)], end):
        sliced += det.process(part, now_ts=e)
    assert _as_filed(sliced) == _as_filed(whole)
    got = [(round(s.start - T0), round(s.end - T0), round(s.energy_wh, 1)) for s in whole]
    for since in (small[0], big):
        assert any(abs(s.start - (T0 + since)) < 10 and abs(s.end - (T0 + stop)) < 10 for s in whole), got


def test_a_live_meter_carries_load_in_a_one_minute_pass():
    """Home's phase A reports every 4.3 s, so a live pass holds about 14 of its
    readings; a dead port reads zero however many there are (2026-09-30)."""
    live = [(T0 + 4.3 * k, 480.0 + k) for k in range(14)]
    assert D.carries_load(live)
    assert not D.carries_load([(T0 + 4.3 * k, 0.0) for k in range(14)])
    assert not D.carries_load(live[:2])


def test_a_slow_meters_silence_is_a_held_value():
    """A Shelly plug reports a change within seconds and otherwise once a
    minute. The IR panel it meters is cycled by a thermostat, 15 minutes on
    and 2.4 off: read as a sampler every 60 s, the offs never lasted two
    readings and the runs merged into hours (Kozolec, 2026-09-29)."""
    rows, t = [], T0
    for k in range(20):
        on_at = t
        rows.append((on_at + 5.0, 523.0))           # a Shelly reports the element settling within seconds
        while t < on_at + 900.0:                     # on: the change at once, then a heartbeat a minute
            rows.append((t, 520.0 if t == on_at else 521.0)); t += 60.0
        off_at = on_at + 900.0
        rows.append((off_at, 0.0))                   # the switch-off, reported as it happens
        rows.append((off_at + 60.0, 0.0))            # one heartbeat, still off
        t = off_at + 144.0                           # and back on before the next
    det = D.Detector()
    det.process({"a": sorted(rows)}, now_ts=t)
    assert det.signatures, "no run found at all"
    sig = max(det.signatures, key=lambda x: x.count)
    assert sig.count >= 18 and sig.duration_s < 1200, (sig.count, sig.duration_s)




def test_a_device_files_a_run_into_a_signature_on_the_runs_own_phases():
    """A device whose starts on A and C are linked (a kiln's legs) has a
    signature per phase set; an A run never lands in the C one, whose power
    would then carry an 'a' entry it was never measured on (2026-09-30)."""
    det = D.Detector()
    for p in "ac":
        det.phases[p] = D.PhaseState(min_noise=10.0)
    sig_c = _sig(1, 600.0, 100.0, 20, phases="c", last_seen=0.0)
    det.signatures = [sig_c]
    det.next_id = 2
    det.edges = [D.StepGroup(id=1, phase="a", up=True, watts=650.0, count=30), D.StepGroup(id=2, phase="c", up=True, watts=600.0, count=30)]
    det.pair_home = {"2": {"1": 20.0}}             # the C starts went to the C signature
    run = D.Run(phases="a", start=1000.0, end=1100.0, levels={"a": [(1000.0, 650.0)]}, pair=(2, None))   # a run of that start on A alone
    det._file(run)
    got = det.signature_of(run)
    assert got is not sig_c and got.phases == "a", (got.id, got.phases, got.power)
    assert "a" not in sig_c.power


def test_rises_on_several_phases_within_the_window_are_one_event():
    """A three-phase compressor (~840 W a leg) and a single-phase pump (~900 W
    on A) are the same size on A; as events they are (840, 850, 830) and
    (900, 0, 0) and never share a cluster (2026-09-30). See EVENT_WINDOW_INTERVALS."""
    det = D.Detector()
    rows, base = {p: [] for p in "abc"}, {"a": 300.0, "b": 200.0, "c": 400.0}
    def hold(p, w, t0, t1):
        for t in range(int(t0), int(t1), 2):
            rows[p].append((T0 + t, base[p] + w))
    for p in "abc":
        hold(p, 0.0, 0, 3600)
    # the compressor: all three legs within 2 s, ten times
    for k in range(10):
        t = 200 + 300 * k
        for p, w, d in (("a", 840.0, 0), ("b", 850.0, 2), ("c", 830.0, 1)):
            rows[p] = [r for r in rows[p] if not (T0 + t + d <= r[0] < T0 + t + 120)]
            hold(p, w, t + d, t + 120)
    # the pump: A alone, ten times, between the compressor's runs
    for k in range(10):
        t = 350 + 300 * k
        rows["a"] = [r for r in rows["a"] if not (T0 + t <= r[0] < T0 + t + 60)]
        hold("a", 900.0, t, t + 60)
    for p in "abc":
        rows[p].sort()
    det.process(rows, now_ts=T0 + 3700)
    rises = [c for c in det.edges if c.up and c.count >= 5]
    patterns = sorted((c.phase, round(c.watts, -1)) for c in rises)
    assert ("abc", 2520.0) in patterns, patterns                # the compressor, one event of three legs
    assert ("a", 900.0) in patterns, patterns                   # the pump, alone on A
    assert not any(c.phase == "a" and 800 <= c.watts <= 880 for c in rises), patterns   # no per-phase 840 W cluster left
    sigs = {(x.phases, round(sum(x.power.values()), -2)) for x in det.signatures if x.count >= 5}
    assert ("abc", 2500.0) in sigs and ("a", 900.0) in sigs, sigs        # one three-phase signature, one single-phase


def test_a_pump_and_a_heater_of_one_size_are_two_kinds_of_edge_by_their_reactive_angle():
    """Hart's plane: 900 W at 0 var and 900 W at 630 var (35 deg) share a size
    and nothing else. Without a var reading only the size is there to go on."""
    rnd = random.Random(3)
    def run(with_var):
        det = D.Detector()
        ids = {}
        for k in range(200):
            heater = k % 2 == 0
            w = 900.0 + rnd.gauss(0, 8)
            var = rnd.gauss(0, 10) if heater else 630.0 + rnd.gauss(0, 15)
            c = det._classify_step("a", T0 + 60 * k, w, var if with_var else None, 0.0)
            ids.setdefault(heater, set()).add(c.id)
        return ids
    off, on = run(False), run(True)
    assert off[True] & off[False], off                                 # by size alone they share a cluster
    assert not (on[True] & on[False]), on                              # by angle they never do


def test_a_start_that_shares_a_reading_with_a_metered_pulse_is_split_by_the_meter():
    """Kozolec 27.09 10:17: the charger's 3.4 kW start and the boiler's 1.9 kW
    pulse in one reading. The boiler's meter knows its share, so the house
    books two starts, not one of 5.3 kW."""
    det = D.Detector()
    for ph in "a":
        det.phases[ph] = D.PhaseState()
        det.phases[ph].noise, det.phases[ph].level, det.phases[ph].interval = 20.0, 300.0, 5.0
    det.meter_steps = lambda ph, since, window, up=None, size=None: [("Boiler", 1900.0)]
    assert det.metered_parts("a", T0, 5300.0) == [1900.0, 3400.0]
    assert det.metered_parts("a", T0, 1910.0) == [1910.0]              # all of it is the boiler's
    # the Fleet offers a meter's step the other way only where the grid netted
    # it into this one - then the rest is the larger for it
    assert det.metered_parts("a", T0, -5300.0) == [1900.0, -7200.0]
    det.meter_steps = lambda ph, since, window, up=None, size=None: []
    assert det.metered_parts("a", T0, 5300.0) == [5300.0]


def test_a_load_cycling_inside_the_noise_band_does_not_hold_the_band_open():
    """Hiša's phase C: a 65 W load toggling every few readings, inside a floor
    learned at 128 W. Measured from the level it sat 30 W out and kept the
    floor up for good; measured by how far the reading moves, a held level
    moves by nothing and the floor comes down under the load."""
    import random
    rnd = random.Random(5)
    st = D.PhaseState(min_noise=5.0)
    st.baseline, st.level, st.noise = 410.0, 410.0, 128.0
    for i in range(1200):
        on = (i // 5) % 2 == 1
        st.process(T0 + 5.0 * i, 410.0 + (65.0 if on else 0.0) + rnd.gauss(0, 1.5))
    assert st.noise < 65.0, st.noise


def test_a_step_and_a_run_carry_how_sure_the_detector_is_of_them():
    st = D.PhaseState()
    st.noise, st.level, st.interval = 20.0, 300.0, 2.0
    held = lambda *w: [(T0 + k, x, None, None) for k, x in enumerate(w)]
    clean = st._step_quality(1000.0, held(1300, 1301, 1299), T0, 300.0)
    assert clean > 0.95, clean                                           # 50 x the noise, readings agree
    assert st._step_quality(25.0, held(325, 326), T0, 300.0) < 0.1       # barely above the noise
    assert st._step_quality(1000.0, held(1100, 1300, 1500), T0, 300.0) < 0.7   # never settled
    st.last_step_ts = T0 - 1.0
    assert abs(st._step_quality(1000.0, held(1300, 1301), T0, 300.0) - D.QUALITY_CROWDED * clean) < 0.05
    o = D._Open(T0, 1000.0, None, [(T0, 1000.0)], q=0.9)
    assert abs(st._close(o, T0 + 60, 1000.0, None).quality - D.QUALITY_UNSEEN_STOP) < 1e-9   # no stop seen
    st.stop_q = 0.8
    assert abs(st._close(o, T0 + 60, 1000.0, None).quality - 0.8) < 1e-9                   # the lesser end
    assert st._close(o, T0 + 60, 700.0, None).quality == 0.0                               # 30 % apart in size


def _declare(st, *entries):
    """Steps a phase declared, (since, watts, var, span start, span end), kept in time order."""
    st.declared[:] = sorted(st.declared + list(entries))
    st.declared_t[:] = [e[0] for e in st.declared]


def _fleet_with_meters(steps, parents=None):
    """A fleet whose meters each declared ``steps[name]`` on their channel a at
    T0, every one measured to carry grid phase c."""
    f = D.Fleet()
    f.main.phases["c"] = D.PhaseState()
    f.main.phases["c"].noise, f.main.phases["c"].interval = 10.0, 2.0
    for name, w in steps.items():
        det = D.Detector()
        det.phases["a"] = D.PhaseState()
        det.phases["a"].noise = 5.0
        if w:
            _declare(det.phases["a"], (T0 + 0.5, w, None, T0 - 1.0, T0 + 2.0))
        f.subs[name] = det
    f.parents = dict(parents or {})
    f.phase_votes = {n: {"a": {"c": D.PHASE_MAP_MIN_VOTES}} for n in steps}
    return f



def test_the_union_reaches_as_far_as_the_slower_meters_span():
    """A 10 s plug's span reaches three cadences back, 30 s; the grid, polled
    every 2 s, saw the same change as two steps 23 s apart. The union is
    capped by the slower meter's reach, not the grid's 24 s alone, so it
    holds both steps and the plug is all of them."""
    f = _fleet_with_meters({"Hidrofor": 0.0})
    f.subs["Hidrofor"].phases["a"].interval = 10.0                       # sustain 3 x 10 s
    _declare(f.subs["Hidrofor"].phases["a"], (T0, 850.0, None, T0 - 30.0, T0 + 1.0))
    _declare(f.main.phases["c"], (T0 - 25.0, 400.0, None, T0 - 27.0, T0 - 25.0), (T0, 450.0, None, T0 - 2.0, T0))
    assert D.UNION_CAP_WINDOWS * f.main.event_window() < 31.0            # the grid's window alone: 24 s
    share = f._meter_totals("c", T0, f.main.event_window(), True)["Hidrofor"][0]
    assert share is None, share


def test_a_meter_that_held_its_value_did_not_take_the_step():
    """Home's workshop boiler meter read 0 W for hours while an unmetered 2.2 kW
    load on phase B switched 69 times; each unplaced step joined the boiler's
    cluster as the busiest at its size. A meter that wrote nothing that moved
    for three cadences after the step - its value held - cannot be where it
    happened. One that stepped, is still changing, or cannot be judged yet can."""
    f = _fleet_with_meters({"Workshop boiler": 0.0})
    boiler = f.subs["Workshop boiler"].phases["a"]
    boiler.interval = 10.0                                               # sustain 3 x 10 s
    f._now = T0 + 3600.0
    assert f._meter_held("Workshop boiler", "c", T0, True)               # silent all along: held
    assert not f._meter_held("Workshop boiler", "a", T0, True)           # not measured on A: cannot say
    f._now = T0 + 20.0
    assert not f._meter_held("Workshop boiler", "c", T0, True)           # too soon to tell
    f._now = T0 + 3600.0
    boiler.pending = [(T0 + 4.0, 2100.0, None, None)]
    assert not f._meter_held("Workshop boiler", "c", T0, True)           # changing
    boiler.pending = []
    _declare(boiler, (T0 + 6.0, 2140.0, None, T0 - 4.0, T0 + 6.0))
    assert not f._meter_held("Workshop boiler", "c", T0, True)           # it stepped: a placement missed
    assert f._meter_held("Workshop boiler", "c", T0, False)              # ...but not the other way


def test_the_grid_is_read_as_far_behind_as_its_slowest_meter_needs():
    """The horizon: how long a meter takes to declare a step it shares with
    the grid, learned from those steps, the slowest meter's, capped at five
    minutes (2026-10-02) - and only a meter that has shared a step sets
    it, twice its latency until its lag is believed: one that never shares a
    step has nothing the grid waits for. A meter left out on the settings
    page does not set it. With an input fed, at least the reach its changes
    are looked for in."""
    f = D.Fleet()
    f.wait_cap_s = D.METER_WAIT_CAP_S
    for name, cad in (("Hidrofor", 10.0), ("Hisa", 5.0), ("Workshop boiler", 146.0)):
        f.subs[name] = D.Detector()
        f.subs[name].phases["a"].interval = cad
    assert f._horizon() == 0.0                                      # no step shared yet: nothing to wait for
    f.meter_lag["Hidrofor"] = [[2.0, 14.0]] * 18 + [[4.0, 35.0]]          # 19 steps shared
    assert f._horizon() == 60.0                                     # it shares: twice its latency until believed
    f.meter_lag["Hidrofor"].append([4.0, 35.0])                            # 20: its 95th, 35 s
    assert f._horizon() == 35.0
    f.meter_lag["Workshop boiler"] = [[3.0, 400.0]] * D.LAG_MIN_SAMPLES     # slow, learned: the cap
    assert f._horizon() == D.METER_WAIT_CAP_S
    f.horizon_skip = {"Workshop boiler"}
    assert f._horizon() == 35.0
    f.switch_on[D.SWITCH_PREFIX + "climate.mat"] = {T0: None}
    assert f._horizon() == D.EDGE_LAG_REACH_S
    f.wait_cap_s = 0.0
    assert f._horizon() == 0.0


def test_a_meter_that_never_shares_a_step_does_not_set_the_horizon():
    """Home's Server UPS: a 60 s heartbeat that never shows a step the grid
    shows, so no lag can be learned. Its fallback - twice its latency, 360 s
    - held Home's horizon at the 300 s cap; it has nothing the grid waits
    for, and the horizon is the 3EMs' (2026-10-02)."""
    f = D.Fleet()
    f.wait_cap_s = D.METER_WAIT_CAP_S
    for name, cad in (("Server UPS", 60.0), ("Hisa", 5.0), ("Mansarda", 4.9)):
        f.subs[name] = D.Detector()
        f.subs[name].phases["a"].interval = cad
    f.meter_lag["Hisa"] = [[1.0, 28.5]] * D.LAG_MIN_SAMPLES
    f.meter_lag["Mansarda"] = [[1.0, 26.7]] * D.LAG_MIN_SAMPLES
    assert f._horizon() == 28.5
    assert f._declare_lag("Server UPS") == 360.0                    # what a session waits for it, not the grid
    f.meter_lag["Server UPS"] = [[2.0, 70.0]]                         # one step shared: it does take part, at its fallback
    assert f._horizon() == D.METER_WAIT_CAP_S


def test_a_meters_lag_is_learned_from_the_steps_it_shares_with_the_grid():
    """A plug declaring the grid's steps 1.5 s after they happened, and its
    own a reading's latency later: learned once each grid step is LAG_REACH_S
    old on the clock - the same whatever the slicing - it sets the plug's
    latency and the grid's horizon. A step the plug has two of nearby is
    learned from the nearer."""
    f = _fleet_with_meters({"Hidrofor": 0.0})
    plug, grid = f.subs["Hidrofor"].phases["a"], f.main.phases["c"]
    plug.interval, grid.noise = 10.0, 10.0
    for k in range(30):
        t = T0 + 600.0 * k
        _declare(grid, (t, 900.0, None, t - 1.0, t))
        _declare(plug, (t + 1.5, 880.0, None, t - 10.0, t + 1.5, t + 12.0))
    _declare(grid, (T0 + 600.0 * 30, -900.0, None, T0, T0))
    _declare(plug, (T0 + 600.0 * 30 + 2.0, -880.0, None, T0, T0, T0), (T0 + 600.0 * 30 + 9.0, -880.0, None, T0, T0, T0))
    f.wait_cap_s = D.METER_WAIT_CAP_S
    f._learn_lags(T0 + 600.0 * 30 + D.LAG_REACH_S + 1.0)
    rows = f.meter_lag["Hidrofor"]
    assert len(rows) == 31 and all(r == [1.5, 12.0] for r in rows[:30]) and rows[30][0] == 2.0, rows[-3:]
    assert plug.lag == 1.5 and plug.latency() == 30.0              # never under three of its repeat intervals
    assert f._horizon() == 12.0


def test_a_meters_lag_is_where_its_shared_steps_pile_up_among_chance():
    """Home's Mansarda: two channels the map placed, a third it did not - so
    every phase had two of its channels and, learned from one-of-a-kind
    steps on one channel only, it learned no lag in ten days (2026-10-14).
    Each grid step's nearest step of its size on any channel that may carry
    the phase is learned from, chance and all: the lags are where those pile
    up - here 1-3 s late on the 3EM - and a meter whose nearest steps lie
    anywhere in the reach shares none and does not hold the grid back."""
    f = _fleet_with_meters({"Mansarda": 0.0})
    m = f.subs["Mansarda"]
    m.phases["b"] = D.PhaseState()
    for st in m.phases.values():
        st.interval, st.last_ts, st.noise = 0.5, T0, 5.0                # "b" placed nowhere yet
    grid = f.main.phases["c"]
    for k in range(60):
        t = T0 + 600.0 * k
        _declare(grid, (t, 900.0, None, t - 1.0, t))
        if k % 3:                                                       # shared, on the placed channel
            _declare(m.phases["a"], (t + 1.0 + (k % 5) * 0.5, 880.0, None, t - 2.0, t + 3.0, t + 8.0))
        else:                                                           # chance: another load's, on the other
            _declare(m.phases["b"], (t - 55.0 + (k * 7) % 110, 880.0, None, t - 60.0, t + 50.0, t + 60.0))
    f.wait_cap_s = D.METER_WAIT_CAP_S
    f._learn_lags(T0 + 600.0 * 60 + D.LAG_REACH_S + 1.0)
    assert len(f.meter_lag["Mansarda"]) == 60
    st = m.phases["a"]
    assert st.lag == 3.0 and st.lead == 0.0, (st.lag, st.lead)
    assert f._horizon() == 8.0
    f.meter_lag["Mansarda"] = [[-55.0 + (k * 7) % 110, 60.0] for k in range(60)]   # all chance
    assert not D._shared(f.meter_lag["Mansarda"]) and f._horizon() == 0.0


def test_a_meter_ahead_of_the_grid_learns_a_lead_and_its_spans_reach_the_grids_step():
    """A 3EM whose steps come 5 s BEFORE the grid's (its lags negative, Home's
    grid read a median 4.6 s after the 3EM's, 2026-10-14): its lag stays 0,
    its lead is 5 s, and a step whose span ended before the grid's began is
    found over the grid's span - not without the lead."""
    f = _fleet_with_meters({"Hiša": 0.0})
    plug, grid = f.subs["Hiša"].phases["a"], f.main.phases["c"]
    plug.interval, grid.noise = 2.0, 10.0
    for k in range(30):
        t = T0 + 600.0 * k
        _declare(grid, (t, 900.0, None, t - 1.0, t))
        _declare(plug, (t - 5.0, 880.0, None, t - 8.0, t - 3.0, t - 1.0))
    f.wait_cap_s = D.METER_WAIT_CAP_S
    f._learn_lags(T0 + 600.0 * 30 + D.LAG_REACH_S + 1.0)
    assert plug.lag == 0.0 and plug.lead == 5.0
    assert len(f._near(plug, T0 - 1.0, T0, 6.0)) == 1                  # spans t-8 .. t-3+5 meet the grid's t-1 .. t
    plug.lead = 0.0
    assert f._near(plug, T0 - 1.0, T0, 6.0) == []
    assert D.PhaseState.from_dict(D.PhaseState(lead=5.0).to_dict()).lead == 5.0


def test_the_grid_readings_newer_than_the_horizon_wait_for_the_next_pass():
    """A live pass reads up to now and the grid is read behind its meters:
    its readings newer than that, reactive power too, wait for the next pass,
    kept across a restart."""
    f = D.Fleet()
    f.wait_cap_s = 45.0
    rows = {"c": [(T0 + k * 2.0, 300.0) for k in range(31)]}                 # T0 .. T0+60
    q = {"c": {T0 + k * 2.0: 20.0 for k in range(31)}}
    got, got_q, _ = f._hold_back(rows, q, None, T0 + 60.0 - f.wait_cap_s)
    assert got["c"][-1][0] == T0 + 14.0 and all(got_q["c"][t] == 20.0 for t, _ in got["c"])
    f = D.Fleet.from_dict(f.to_dict())                                      # kept across a restart
    got, got_q, _ = f._hold_back({"c": [(T0 + 62.0, 310.0)]}, {"c": {T0 + 62.0: 21.0}}, None, T0 + 120.0 - 45.0)
    assert [r[0] for r in got["c"]] == [T0 + 16.0 + 2.0 * k for k in range(23)] + [T0 + 62.0]
    assert [got_q["c"].get(t) for t, _ in got["c"]] == [20.0] * 23 + [21.0]   # the reactive values came along
    got, _, _ = f._hold_back({"c": [(T0 + 130.0, 300.0)]}, None, None, T0 + 130.0)   # a cap of 0: judged at once
    assert got["c"] == [(T0 + 130.0, 300.0)]


def test_a_meter_that_held_through_a_runs_start_is_not_paired_by_energy():
    """Home's hidrofor plug read 0.5 W from half an hour before a 102 W,
    47-minute run started; the pump then cycled inside it, 60 Wh, and the
    energy pairing filed the run as the hidrofor. A meter that held its value
    through a run's start did not start it."""
    f = _fleet_with_meters({"Hidrofor": 0.0})
    f.subs["Hidrofor"].phases["a"].interval = 10.0
    rows = [(T0 - 1800.0, 0.5)]
    for k in range(4):                                                   # four pump runs inside the run, 70 s at 800 W
        t = T0 + 300.0 + k * 600.0
        rows += [(t, 800.0), (t + 70.0, 0.5)]
    rows.append((T0 + 3500.0, 0.6))
    f.sub_rows["Hidrofor"] = {"a": rows}
    f._now = T0 + 3600.0
    run = D.Run(phases="c", start=T0, end=T0 + 2820.0, levels={"c": [(T0, 80.0)]})
    assert 0.65 <= D.energy_between(rows, run.start, run.end) / run.energy_wh <= 1.35   # the energies agree
    assert not f._energy_pairs([run])                                     # but the plug held at its start
    f.sub_rows["Hidrofor"] = {"a": [(T0 - 1800.0, 0.5), (T0 + 2.0, 85.0), (T0 + 2810.0, 0.5), (T0 + 3500.0, 0.6)]}   # a plug that started it
    _declare(f.subs["Hidrofor"].phases["a"], (T0 + 2.0, 84.5, None, T0 - 8.0, T0 + 2.0))
    assert f._energy_pairs([run])


def test_a_run_of_the_size_a_held_meters_run_holds_is_not_of_its_kind():
    """Home 09-24 22:55 local: the dehumidifier on Susilna's plug had run since
    20:00 at 254 W, the house's run of it open and the plug's (owned); a 400 W
    workshop load dipped for 6 s, its return (+392) was split and a 286 W rest
    opened in the dehumidifier's start cluster - its device - and was filed
    into the device's 263 W signature, 158 minutes while the plug read 294 W
    unchanged. 60 such runs, 3.6 kWh, in ten days: the plug, guessed a strip,
    was never asked whether it held; the signature carried none of its
    locations until the 20-hour run was filed at its end; and the
    dehumidifier's runs start with a surge, in a cluster of their own. Here
    the map places the plug nowhere, as for a meter with no votes yet. A run of the size of a run a meter holds open, started while
    that meter held, is another load of that size: the signatures that run's
    device went to, and those placed at the meter, are kept clear of it."""
    f = _fleet_with_meters({"Susilna": 0.0})
    f.phase_votes = {}                                                   # the map places it nowhere yet
    plug = f.subs["Susilna"].phases["a"]
    plug.interval, plug.last_ts = 5.8, T0 + 3600.0
    _declare(plug, (T0 - 10500.0, 254.0, None, T0 - 10518.0, T0 - 10500.0))   # on since 20:00
    f._now = T0 + 3600.0
    grid = f.main.phases["c"]
    owned = D._Open(since=T0 - 10500.0, watts=254.0, var=None, levels=[(T0 - 10500.0, 254.0)], cluster=163, meter="Susilna")
    grid.open_edges = [owned]
    home = _sig(136, 263.0, 600.0, 65, phases="c", first_seen=T0, last_seen=T0)
    home.locations.update({"Mansarda": 7, "Vtičnice - pisarna": 2})       # as on the day: none of the plug's yet
    other = _sig(140, 270.0, 600.0, 20, phases="c", first_seen=T0, last_seen=T0)
    f.main.signatures = [home, other]
    f.main.pair_home = {"163": {136: 3.0}, "165": {136: 40.0}, "77": {140: 20.0}}
    run = D.Run(phases="c", start=T0, end=T0 + 9480.0, levels={"c": [(T0, 286.0)]}, pair=(165, None))
    assert f._held_homes(run) == [136]                                   # its level, held: not of its kind
    big = D.Run(phases="c", start=T0, end=T0 + 600.0, levels={"c": [(T0, 1000.0)]}, pair=(77, None))
    assert f._held_homes(big) == []                                      # not its size: nothing to say
    owned.meter = None
    assert f._held_homes(run) == []                                      # a run no meter holds: no word on it
    owned.meter = "Susilna"
    _declare(plug, (T0 + 3.0, 280.0, None, T0 - 3.0, T0 + 3.0))
    assert f._held_homes(run) == []                                      # the plug stepped with it


def test_a_fall_ends_the_run_the_rise_of_its_size_opened_seconds_ago():
    """Home 09-24 20:55 UTC: a 400 W workshop load dipped twice for 6 s (3980
    -> 3581 -> 3973 -> 3572 -> 3973 W). The first dip closed its run and the
    return (+392) opened a new one - right, and it stays (the owner) - but a plug's
    coincident wobble took 107 W of the return, leaving a 286 W rest that
    the second dip (-402) did not match; it joint-stopped a 515 W run 102
    minutes old with a held 113 W drop instead. The newest run a rise of the
    fall's size opened seconds ago is the one ending now: the 515 W run goes
    on, the 400 W load's runs are back-to-back pieces."""
    st = D.PhaseState()
    st.noise, st.interval, st.baseline, st.level, st.name = 10.0, 1.1, 116.0, 3973.0, "a"
    lib = D.Detector()
    lib.meter_on = lambda name, ph, since, size, t=None: name == "Blaževa Soba"     # its wobble still on
    st.lib = lib
    old = D._Open(since=T0 - 6137.0, watts=515.0, var=None, levels=[(T0 - 6137.0, 530.0), (T0 - 6131.0, 515.0)], cluster=1)
    rest = D._Open(since=T0 - 5.0, watts=286.0, var=None, levels=[(T0 - 5.0, 286.0)], cluster=2)
    pc = D._Open(since=T0 - 5.0, watts=107.0, var=None, levels=[(T0 - 5.0, 107.0)], cluster=3, meter="Blaževa Soba")
    st.open_edges = [old, rest, pc]
    st.held_drops = [(T0 - 3000.0, 113.0, None)]
    _declare(st, (T0 - 5.0, 392.0, None, T0 - 7.0, T0 - 5.0))              # the first return
    closed = st._pair(T0, 402.0, None, 3571.0)                              # the second dip
    assert [round(s.start - T0) for s in closed] == [-5], [(round(s.start - T0), round(s.duration_s)) for s in closed]
    assert old in st.open_edges and pc in st.open_edges and rest not in st.open_edges


def _washer_dryer_day():
    """Home 24 Sep 03:41 UTC in outline: the AEG heat-pump washer-dryer under
    Mansarda drying - each cycle a ~20 s ramp, ~13 min on, a couple off. Its
    panel's channel C (= grid A, a reading every 5 s) 157 -> 404 -> 445 ->
    465 -> 483 W over 21 s, ~487 at the end, 87 after; the next start 94 ->
    277 -> 397 -> 435 -> 465. The grid (a reading every 2 s) is 386 W of other
    loads plus what the panel's channel truly draws, 359 after the first
    cycle. (grid rows, panel rows, start 1, end 1, start 2, end 2, end)."""
    rnd = random.Random(17)
    T = T0 + 3 * 3600.0
    s1, e1, s2, e2 = T, T + 783.0, T + 902.0, T + 1700.0
    pts = [(-1e9, 157.0), (s1 - 0.01, 157.0), (s1, 180.0), (s1 + 2, 337.0), (s1 + 4, 404.0), (s1 + 6, 418.0),
           (s1 + 10, 445.0), (s1 + 14, 465.0), (s1 + 25, 483.0), (e1 - 0.01, 487.0), (e1, 87.0),
           (s2 - 0.01, 94.0), (s2, 150.0), (s2 + 2, 277.0), (s2 + 4, 350.0), (s2 + 8, 397.0), (s2 + 13, 435.0),
           (s2 + 23, 465.0), (e2 - 0.01, 470.0), (e2, 90.0), (1e12, 90.0)]

    def draw(t):
        k = bisect.bisect_right([x for x, _ in pts], t) - 1
        (x0, y0), (x1, y1) = pts[k], pts[k + 1]
        return y0 if x1 == x0 or y1 == y0 or x1 - x0 > 60 else y0 + (y1 - y0) * (t - x0) / (x1 - x0)
    grid, panel = [], []
    t = T - 1800.0
    while t < T + 2400.0:
        grid.append((t, round((386.0 if t < T + 850.0 else 359.0) + draw(t) + rnd.uniform(-2, 2), 1)))
        t += 2.0
    t = T - 1800.0 + 1.0
    while t < T + 2400.0:
        panel.append((t, round(draw(t) + rnd.uniform(-1, 1), 1)))
        t += 5.0
    return {"a": grid}, {"c": panel}, s1, e1, s2, e2, T + 2400.0


def test_a_ramp_two_meters_declared_over_different_stretches_is_placed_by_the_grids_change_over_both():
    """Home 24 Sep 05:41 local: the washer-dryer's ramp - the grid declared
    +261 at its first plateau, Mansarda's channel +313 once settled, beyond
    each other's tolerance, so nobody's: the run ran on 49 minutes past its
    end at 05:54:48. Over both steps' window the grid read 543 W before and
    ~850 after, +307: the panel's step agrees with that, the run is placed at
    Mansarda (its own), booked at the ramp's whole size, and the owner's rule ends
    it when the panel reads below it - at 05:54:48. The next start the same."""
    rows, panel, s1, e1, s2, e2, end = _washer_dryer_day()
    fleet = D.Fleet()
    fleet.wait_cap_s = D.METER_WAIT_CAP_S
    fleet.meter_lag["Mansarda"] = [[1.0, 15.0]] * D.LAG_MIN_SAMPLES
    fleet.phase_votes["Mansarda"] = {"c": {"a": D.PHASE_MAP_MIN_VOTES}}
    filed = _keep_filed(fleet)
    cuts = [s1 + 400.0, s2 + 400.0]
    for (part, e), (sub, _) in zip(_passes(rows, cuts, end), _passes(panel, cuts, end)):
        fleet.process(part, {"Mansarda": sub}, now_ts=e, single={"Mansarda": False})
    got = sorted(((round(s.start - s1), round(s.end - s1), round(s.energy_wh * 3600.0 / s.duration_s),
                   _at(fleet, s, "Mansarda")) for s in filed if s.duration_s > 120.0), key=lambda g: g[0])
    # what each ramp's runs book together: the ramp still opens more than one
    # grid run (its first plateau and the rest - open), each the meter's;
    # booked at what Mansarda drew they share it, where each booked at its own
    # level they drew the ramp about twice over (302 + 326 + 24 + 24 W for a
    # ~307 W ramp, before 2026-10-30)
    first = [g for g in got if -5 <= g[0] <= 30]
    second = [g for g in got if -5 <= g[0] - (s2 - s1) <= 30]
    assert first and all(abs(g[1] - (e1 - s1)) <= 10 and g[3] for g in first), got
    assert 280 <= sum(g[2] for g in first) <= 360, got
    assert second and all(abs(g[1] - (e2 - s1)) <= 10 and g[3] for g in second), got
    assert 320 <= sum(g[2] for g in second) <= 410, got


def test_an_owned_runs_size_is_what_its_meter_read_at_that_moment():
    """An owned run alone on its phase follows its meter, not the phase - and
    what the meter read at the grid's reading, not its declared level: that is
    read ahead of the grid by the horizon and lags its own readings by its
    sustain. Its change since just before the run started, not its level: a
    plug from 1 W to 291 then, 271 now; a circuit carrying 700 W of other
    loads, a 100 W run of its own is 100 W, not 800 (the unify audit,
    2026-10-03). Without readings, its declared steps since."""
    f = _fleet_with_meters({"Plug": 0.0})
    plug = f.subs["Plug"].phases["a"]
    plug.interval = 10.0
    _declare(plug, (T0 - 1.0, 290.0, None, T0 - 12.0, T0 - 2.0))
    f.sub_rows["Plug"] = {"a": [(T0 - 60.0, 1.0), (T0 - 2.0, 291.0), (T0 + 30.0, 271.0)]}
    assert f._meter_level("Plug", "c", T0, T0) == 290.0
    assert f._meter_level("Plug", "c", T0 + 60.0, T0) == 270.0
    f.sub_rows["Plug"]["a"] = [(T0 - 60.0, 701.0), (T0 - 2.0, 801.0), (T0 + 30.0, 790.0)]
    assert f._meter_level("Plug", "c", T0 + 60.0, T0) == 89.0             # the circuit's 700 W left out
    f.sub_rows, f._sub_seed = {}, {}
    _declare(plug, (T0 + 600.0, -40.0, None, T0 + 590.0, T0 + 600.0))
    assert f._meter_level("Plug", "c", T0 + 900.0, T0) == 250.0            # no readings: its steps since


def test_a_meters_home_for_a_run_is_the_signature_of_its_kind():
    """Home's dehumidifier plug had more of its sessions in an 18 W Hiša
    signature (its fan alone) than in its own 263 W one, and its 20-hour
    runs, filed where the plug's word sent them, went with the fan (09-24).
    Asked for a run, the meter's home is the one of the run's kind."""
    f = _fleet_with_meters({"Susilna": 0.0})
    own = _sig(139, 263.0, 600.0, 43, phases="c")
    own.locations.update({"Susilna": 3, "Mansarda": 4})
    fan = _sig(618, 18.0, 600.0, 83, phases="c")
    fan.locations.update({"Hiša": 59, "Susilna": 4})
    f.main.signatures = [own, fan]
    night = D.Run(phases="c", start=T0, end=T0 + 72000.0, levels={"c": [(T0, 270.0)]})
    assert f._meter_home("Susilna") == 618                                   # most of its sessions
    assert f._meter_home("Susilna", night) == 139                            # of this run's kind


def test_a_strips_reading_below_a_run_it_owns_ends_the_run():
    """A run cannot still be running on less than it started with (the owner,
    2026-10-03): a meter reporting a total that holds several devices - a
    strip, or a plug its library takes for one, as Home's Susilna plug -
    ends a run it owns once its own reading after its fall is below the run's
    size. A fall of another of its devices, leaving it above, does not; nor
    a run it neither owns nor rose for."""
    f = _fleet_with_meters({"Strip": 0.0})
    f.single = {"Strip": False}
    strip = f.subs["Strip"].phases["a"]
    strip.interval = 6.0
    _declare(strip, (T0 + 2.0, 279.0, None, T0 - 4.0, T0 + 2.0), (T0 + 2219.0, -426.0, None, T0 + 2213.0, T0 + 2219.0))
    f.sub_rows["Strip"] = {"a": [(T0 - 60.0, 167.0), (T0 + 2.0, 446.0), (T0 + 2219.0, 20.0)]}
    run = D._Open(since=T0, watts=279.0, var=None, levels=[(T0, 279.0)], meter="Strip")
    older = D._Open(since=T0 - 3600.0, watts=400.0, var=None, levels=[(T0 - 3600.0, 400.0)])
    assert f._meter_stop("c", T0 + 2216.0, T0 + 2218.0, [older, run], 385.0) is run
    f._stops_used = {}
    f.sub_rows["Strip"]["a"][-1] = (T0 + 2219.0, 300.0)                    # still above the run: not its stop
    assert f._meter_stop("c", T0 + 2216.0, T0 + 2218.0, [older, run], 385.0) is None
    f._stops_used = {}
    f.sub_rows["Strip"]["a"][-1] = (T0 + 2219.0, 20.0)
    other = D._Open(since=T0 + 600.0, watts=279.0, var=None, levels=[(T0 + 600.0, 279.0)])
    assert f._meter_stop("c", T0 + 2216.0, T0 + 2218.0, [older, other], 385.0) is None   # not its: it did not rise for it


def test_a_meters_run_changes_level_with_its_meter_and_ends_when_it_stops():
    """Kozolec's pond EVSE 5 Oct: 3,548 W from 14:41, lowered to 2,868 W at
    15:11, stopped at 15:36. The step down, the grid falling by about as much,
    is a level of the run its meter owns, not its end (the owner, 2026-10-29:
    a charge can go from 16 A to 6 A); the stop to its idle reading ends it. A
    fall bigger than the run's level is another load's."""
    f = _fleet_with_meters({"EVSE": 0.0})
    f.single = {"EVSE": True}                  # a meter holding one device - see Fleet._ended_by
    ch = f.subs["EVSE"].phases["a"]
    ch.interval = 10.0
    _declare(ch, (T0 + 2.0, 3548.0, None, T0 - 8.0, T0 + 2.0), (T0 + 1800.0, -732.0, None, T0 + 1790.0, T0 + 1800.0),
             (T0 + 3300.0, -2850.0, None, T0 + 3290.0, T0 + 3300.0))
    f.sub_rows["EVSE"] = {"a": [(T0 - 60.0, 36.0), (T0 + 2.0, 3584.0), (T0 + 1800.0, 2852.0), (T0 + 3300.0, 2.0)]}
    run = D._Open(since=T0, watts=3548.0, var=None, levels=[(T0, 3548.0)], meter="EVSE")
    got = f._meter_stop("c", T0 + 1797.0, T0 + 1799.0, [run], 740.0)
    assert isinstance(got, tuple) and got[1] is run and [round(w) for _, w in run.levels] == [3548, 2816], got
    assert f._meter_stop("c", T0 + 3297.0, T0 + 3299.0, [run], 2840.0) is run


def test_a_channel_the_votes_do_not_place_yet_may_carry_any_phase():
    """A channel's phase is what the votes say and nothing else (the unify
    audit, 2026-10-03): with 29 votes a plug is placed nowhere and asked
    about every phase - its rise is a step on b, its silence a hold on b,
    though its label says "a" and its votes c - and with the 30th on c only.
    Netting a meter's change out of a grid step needs it placed."""
    f = _fleet_with_meters({"Plug": 0.0})
    f.main.phases["b"] = D.PhaseState()
    plug = f.subs["Plug"].phases["a"]
    plug.interval, plug.last_ts = 10.0, T0 + 900.0
    _declare(plug, (T0 + 1.0, 300.0, None, T0 - 9.0, T0 + 1.0))
    f.sub_rows["Plug"] = {"a": [(T0 - 60.0, 0.0), (T0 + 1.0, 300.0)]}
    f._now = T0 + 3600.0
    f.phase_votes = {"Plug": {"a": {"c": D.PHASE_MAP_MIN_VOTES - 1}}}
    assert f.phase_map("Plug") == {}
    assert f._meter_stepped("Plug", "b", T0, 150.0, True) and f._meter_on("Plug", "b", T0, 300.0)
    assert f._meter_level("Plug", "b", T0 + 60.0, T0) == 300.0
    assert f._meter_held("Plug", "b", T0 + 1800.0, True)                    # silent then: held, on b too
    assert f._meter_totals("b", T0, 5.0, True) == {}                        # placed nowhere: nothing netted
    f.phase_votes = {"Plug": {"a": {"c": D.PHASE_MAP_MIN_VOTES}}}
    assert f.phase_map("Plug") == {"a": "c"}
    assert not f._meter_stepped("Plug", "b", T0, 150.0, True) and not f._meter_on("Plug", "b", T0, 300.0)
    assert f._meter_level("Plug", "b", T0 + 60.0, T0) is None and not f._meter_held("Plug", "b", T0 + 1800.0, True)
    assert f._meter_stepped("Plug", "c", T0, 150.0, True) and f._meter_held("Plug", "c", T0 + 1800.0, True)
    assert "Plug" in f._meter_totals("c", T0, 5.0, True)


def test_any_meters_rise_owns_a_start_and_the_innermost_of_alike():
    """A 3EM's channel owns a start through its rise as a plug does - the
    kiln's legs at Hiša (2026-10-03) - on a channel the votes have not
    placed yet; and where a meter inside it rose alike, that meter: its
    channel is the load's alone."""
    f = _fleet_with_meters({})
    grid = f.main.phases["c"]
    _declare(grid, (T0, 500.0, None, T0 - 2.0, T0))
    his = D.Detector()
    for c in "abc":
        his.phases[c] = D.PhaseState(noise=5.0, interval=10.0, last_ts=T0 + 60.0)
    _declare(his.phases["b"], (T0 + 3.0, 495.0, None, T0 - 7.0, T0 + 3.0))
    f.subs["Hiša"] = his
    assert f._meter_started("c", T0, 500.0) == ("Hiša", 495.0)
    pc = D.Detector()
    pc.phases["a"] = D.PhaseState(noise=2.0, interval=10.0, last_ts=T0 + 60.0)
    _declare(pc.phases["a"], (T0 + 5.0, 492.0, None, T0 - 5.0, T0 + 5.0))
    f.subs["Blaž PC"], f.parents = pc, {"Blaž PC": "Hiša"}
    assert f._meter_started("c", T0, 500.0) == ("Blaž PC", 492.0)


def test_a_meters_steps_are_asked_over_the_grid_steps_span():
    """One window for what a meter did at a grid step - the grid step's own
    span, as the meters' totals are (2026-10-01): a meter's span
    already reaches its latency back, and a moment padded by its latency
    counted it twice (the unify audit, 2026-10-03). A step of the meter's
    that ended before the grid's began, though within its latency of it,
    neither makes it "stepped" nor un-holds it; one overlapping does both."""
    f = _fleet_with_meters({"Plug": 0.0})
    plug, grid = f.subs["Plug"].phases["a"], f.main.phases["c"]
    plug.interval = 10.0                                                    # latency 30 s
    _declare(grid, (T0, 300.0, None, T0 - 4.0, T0))
    _declare(plug, (T0 - 20.0, 300.0, None, T0 - 30.0, T0 - 20.0))
    f._now = T0 + 3600.0
    assert not f._meter_stepped("Plug", "c", T0, 150.0, True) and f._meter_held("Plug", "c", T0, True)
    _declare(plug, (T0 + 5.0, 300.0, None, T0 - 6.0, T0 + 5.0))
    assert f._meter_stepped("Plug", "c", T0, 150.0, True) and not f._meter_held("Plug", "c", T0, True)


def test_a_circuits_stop_ends_its_run_by_its_change_not_its_level():
    """A 3EM channel carrying 700 W of other loads never reads below its 100 W
    run's size, read absolutely: its stop ends the run once the channel is
    back to what it read before the run started (the unify audit,
    2026-10-03). Another load's 300 W stop, though it leaves the channel
    below that, is not the run's: unlike it in size, and still reading
    enough to carry it."""
    f = _fleet_with_meters({"Hiša": 0.0})
    ch, grid = f.subs["Hiša"].phases["a"], f.main.phases["c"]
    ch.interval = 10.0
    _declare(grid, (T0, 100.0, None, T0 - 2.0, T0))
    _declare(ch, (T0 + 3.0, 100.0, None, T0 - 7.0, T0 + 3.0), (T0 + 1203.0, -300.0, None, T0 + 1193.0, T0 + 1203.0),
             (T0 + 2403.0, -100.0, None, T0 + 2393.0, T0 + 2403.0))
    f.sub_rows["Hiša"] = {"a": [(T0 - 60.0, 700.0), (T0 + 3.0, 800.0), (T0 + 1203.0, 500.0), (T0 + 2403.0, 400.0)]}
    run = D._Open(since=T0, watts=100.0, var=None, levels=[(T0, 100.0)], meter="Hiša")
    assert f._meter_stop("c", T0 + 1198.0, T0 + 1200.0, [run], 300.0) is None   # another load's stop
    f.sub_rows["Hiša"]["a"] = [(T0 - 60.0, 700.0), (T0 + 3.0, 800.0), (T0 + 2403.0, 700.0)]
    assert f._meter_stop("c", T0 + 2398.0, T0 + 2400.0, [run], 100.0) is run      # its own


def test_every_meters_session_is_matched_by_its_peak_in_the_grids_terms():
    """One match for every meter (the unify audit; the owner, 2026-10-03): per
    phase, by the PEAK, times the meter's gain. A slow 3EM channel dilutes a
    66 s boiler pulse's mean (1,241 W of 1,813) as Kozolec's Shelly did, and
    still pairs by what it peaked at; a meter reading 20 % low pairs once its
    gain is learned, and votes by it."""
    pulse = D.Run("c", T0, T0 + 66.0, {"c": [(T0, 1813.0)]})
    slow = D.Run("b", T0 + 5.0, T0 + 71.0, {"b": [(T0 + 5.0, 1800.0), (T0 + 30.0, 900.0)]})
    assert abs(slow.power_by_phase()["b"] - 1813.0) > D.MATCH_POWER_REL * 1813.0       # its mean misses
    assert D._same_load(pulse, slow, {"b": "c"}, 1.0, 60.0)
    assert not D._same_load(pulse, slow, {"b": "a"}, 1.0, 60.0)                          # placed elsewhere
    low = D.Run("a", T0 + 5.0, T0 + 71.0, {"a": [(T0 + 5.0, 1450.0)]})
    assert not D._same_load(pulse, low, {}, 1.0, 60.0) and D._same_load(pulse, low, {}, 1.25, 60.0)
    f = D.Fleet()
    f.meter_gain["Plug"] = {"p": [math.log(1.25), D.METER_GAIN_MIN]}
    f._vote_phases({"Plug": [low]}, 5.0, [pulse])
    assert f.phase_votes["Plug"] == {"a": {"c": 1}}
    assert abs(f.phase_energy["Plug"]["a"]["c"] - low.energy_wh * 1.25) < 1e-6      # its energy, in the grid's terms


def test_a_meter_reading_one_total_of_a_three_phase_load_maps_to_all_three():
    """Home's EVBox (Polnilnica): one entity, the 11 kW total of charges on
    all three phases. Its charges vote for the phase set, its one channel
    carries all three - a third of its step on each against one grid phase -
    and its sessions pair with the house's three-phase ones. A plug under a
    single-phase load still maps to that one phase, and a 3EM's channel never
    votes for a set (edge card 6fcb280: Polnilnica mapped to A alone)."""
    f = D.Fleet()
    f.subs["Charger"], f.subs["Plug"], f.subs["Hiša"] = D.Detector(), D.Detector(), D.Detector()
    f.subs["Charger"].phases["a"].last_ts = f.subs["Plug"].phases["a"].last_ts = T0
    for p in "abc":
        f.subs["Hiša"].phases[p].last_ts = T0
    charges, kettles, pool = [], [], []
    for k in range(D.PHASE_MAP_MIN_VOTES):
        t = T0 + 3600.0 * k
        pool.append(D.Run("abc", t, t + 1800.0, {p: [(t, 3670.0)] for p in "abc"}))
        charges.append(D.Run("a", t + 3.0, t + 1803.0, {"a": [(t + 3.0, 11000.0)]}))
        pool.append(D.Run("b", t + 900.0, t + 1080.0, {"b": [(t + 900.0, 2000.0)]}))
        kettles.append(D.Run("a", t + 902.0, t + 1082.0, {"a": [(t + 902.0, 1990.0)]}))
    pool.sort(key=lambda m: m.start)
    f._vote_phases({"Charger": charges, "Plug": kettles, "Hiša": charges[:3]}, 5.0, pool)
    assert f.phase_votes["Charger"] == {"a": {"abc": D.PHASE_MAP_MIN_VOTES}} and f.phase_votes["Hiša"] == {}
    assert f.phase_map("Charger") == {"a": "abc"} and f.phase_map("Plug") == {"a": "b"}
    assert all(f._chans("Charger", p) == ["a"] for p in "abc")
    assert f._chans("Plug", "b") == ["a"] and f._chans("Plug", "a") == []
    assert f._leg_gain("Charger", "a") == 1.0 / 3.0 and f._leg_gain("Plug", "a") == 1.0
    assert f._several("Charger", "a") and not f._several("Plug", "a")       # its falls end no leg's run
    assert D._same_load(pool[0], charges[0], {"a": "abc"}, 1.0, 60.0)
    assert not D._same_load(pool[0], charges[0], {"a": "a"}, 1.0, 60.0)


def test_a_set_carries_its_subsets():
    """Home's EVBox also charges on L1 alone (10-08 15:50, 3.6 kW on A): its
    votes a 52 / abc 16 / c 6 (3,352 / 1,751 / 42 Wh) carry the set - a
    tenth of them saw several phases at once (PHASE_SET_MIN), by count or by
    energy - and an L1 charge pairs with it; Hidrofor's stray ab votes do not
    move it off A. A
    meter inside a circuit meter is mapped through the circuit's view of it
    (Blaževa Soba, on Hiša's C)."""
    f = D.Fleet()
    f.phase_votes = {"Charger": {"a": {"a": 52, "abc": 16, "c": 6}}, "Pump": {"a": {"a": 1441, "ab": 16, "c": 3}}}
    f.phase_energy = {"Charger": {"a": {"a": 3352.0, "abc": 1751.0, "c": 42.0}},
                      "Pump": {"a": {"a": 24728.0, "ab": 154.0, "c": 55.0}}}
    assert f.phase_map("Charger") == {"a": "abc"} and f.phase_map("Pump") == {"a": "a"}
    f.phase_votes["L1"], f.phase_energy["L1"] = {"a": {"a": 65, "abc": 4}}, {"a": {"a": 9355.0, "abc": 5051.0}}
    assert f.phase_map("L1") == {"a": "abc"}                    # a sixteenth by count, a third by energy
    l1 = D.Run("a", T0, T0 + 1800.0, {"a": [(T0, 3600.0)]})
    own = D.Run("a", T0 + 3.0, T0 + 1803.0, {"a": [(T0 + 3.0, 3610.0)]})
    assert D._same_load(l1, own, {"a": "abc"}, 1.0, 60.0) and not D._same_load(l1, own, {"a": "bc"}, 1.0, 60.0)
    view = D.Fleet()
    view.subs["PC"] = D.Detector()
    view.phase_votes = {"PC": {"a": {"c": D.PHASE_MAP_MIN_VOTES, "a": 2}}}
    f.views, f.parents = {"Hiša": view}, {"PC": "Hiša"}
    f.phase_votes.update({"Hiša": {p: {q: D.PHASE_MAP_MIN_VOTES} for p, q in zip("abc", "bca")},
                          "PC": {"a": {"b": D.PHASE_MAP_MIN_VOTES}}})      # its own, against the house
    assert f.phase_map("PC") == {"a": "a"}                     # Hiša's c is the grid's a


def test_a_one_channel_meters_step_votes_for_the_phases_that_moved_with_it():
    """Home's EVBox, 10-12 19:18:09 (the grid's per-phase readings): the
    charge rose 4 -> 10.5 kW as a +965, b +1434, c +964 stepped together and
    a, c ramped +1100 / +1750 and b +380 within 30 s. Its step votes for the
    set that moved its way in the moment, the ramp counted, with its layer's
    energy once the meter reads back below half of it; a plug's step votes
    for its one phase; a coincident load on another phase makes the sum miss
    - no vote."""
    f = D.Fleet()
    for p in "abc":
        f.main.phases[p] = D.PhaseState(noise=10.0, interval=2.0, last_ts=T0 + 4000.0)
    _declare(f.main.phases["a"], (T0, 965.0, None, T0 - 2.0, T0), (T0 + 15.0, 1100.0, None, T0 + 13.0, T0 + 15.0),
             (T0 + 2000.0, 300.0, None, T0 + 1998.0, T0 + 2000.0))
    _declare(f.main.phases["b"], (T0 + 0.5, 1434.0, None, T0 - 1.5, T0 + 0.5), (T0 + 12.0, 380.0, None, T0 + 10.0, T0 + 12.0),
             (T0 + 2001.0, 800.0, None, T0 + 1999.0, T0 + 2001.0))
    _declare(f.main.phases["c"], (T0 + 0.3, 964.0, None, T0 - 1.7, T0 + 0.3), (T0 + 20.0, 1750.0, None, T0 + 18.0, T0 + 20.0),
             (T0 + 1000.0, 500.0, None, T0 + 998.0, T0 + 1000.0))
    car, plug, lamp = D.Detector(), D.Detector(), D.Detector()
    car.phases["a"] = D.PhaseState(noise=20.0, interval=10.0, last_ts=T0 + 4000.0)
    _declare(car.phases["a"], (T0 + 5.0, 6600.0, None, T0 - 5.0, T0 + 5.0, T0 + 30.0, 10600.0),
             (T0 + 3600.0, -10500.0, None, T0 + 3590.0, T0 + 3600.0, T0 + 3630.0, 100.0))
    plug.phases["a"] = D.PhaseState(noise=5.0, interval=10.0, last_ts=T0 + 4000.0)
    _declare(plug.phases["a"], (T0 + 1002.0, 495.0, None, T0 + 995.0, T0 + 1002.0, T0 + 1030.0, 500.0),
             (T0 + 1500.0, -495.0, None, T0 + 1495.0, T0 + 1500.0, T0 + 1530.0, 5.0))
    lamp.phases["a"] = D.PhaseState(noise=5.0, interval=10.0, last_ts=T0 + 4000.0)
    _declare(lamp.phases["a"], (T0 + 2000.0, 300.0, None, T0 + 1995.0, T0 + 2000.0, T0 + 2030.0, 300.0),
             (T0 + 2100.0, -300.0, None, T0 + 2095.0, T0 + 2100.0, T0 + 2130.0, 0.0))     # b's +800 beside it
    f.subs.update({"Charger": car, "Plug": plug, "Lamp": lamp})
    f._vote_steps(T0 + 3000.0)
    assert f.phase_votes == {"Charger": {"a": {"abc": 1}}, "Plug": {"a": {"c": 1}}}, f.phase_votes
    assert abs(f.phase_energy["Charger"]["a"]["abc"] - 6600.0 * 3595.0 / 3600.0) < 1e-6
    assert f._step_votes_from["Charger"] == T0 + 5.0         # its fall not yet LAG_REACH_S old
    f._vote_steps(T0 + 3000.0)
    assert f.phase_votes["Charger"] == {"a": {"abc": 1}}       # each step votes once


def test_a_meters_energy_answers_only_on_the_sessions_phases():
    """A 3EM's energy is its channels' that may carry the session's phases,
    in the grid's terms: its rise on a channel on another phase is another
    load (the unify audit, 2026-10-03). Summed whole, Hiša's 2 kW on A
    drowned its 500 W run on C."""
    f = _fleet_with_meters({"Hiša": 0.0})
    his = f.subs["Hiša"]
    his.phases["a"].interval = 10.0
    his.phases["b"] = D.PhaseState(noise=5.0, interval=10.0)
    _declare(his.phases["a"], (T0 + 2.0, 500.0, None, T0 - 8.0, T0 + 2.0))
    f.phase_votes = {"Hiša": {"a": {"c": D.PHASE_MAP_MIN_VOTES}, "b": {"a": D.PHASE_MAP_MIN_VOTES}}}
    run = D.Run(phases="c", start=T0, end=T0 + 1800.0, levels={"c": [(T0, 500.0)]})
    f._now = T0 + 3600.0
    f.sub_rows["Hiša"] = {"a": [(T0 - 1800.0, 100.0), (T0 + 2.0, 600.0), (T0 + 1800.0, 100.0), (T0 + 3500.0, 100.0)],
                          "b": [(T0 - 1800.0, 50.0), (T0 + 2.0, 2050.0), (T0 + 1800.0, 50.0), (T0 + 3500.0, 50.0)]}
    assert f._energy_pairs([run])
    f.phase_votes = {"Hiša": {"a": {"a": D.PHASE_MAP_MIN_VOTES}, "b": {"b": D.PHASE_MAP_MIN_VOTES}}}
    assert not f._energy_pairs([run])                                     # placed on no channel of C


def test_a_run_the_reading_carries_half_of_is_still_on():
    """A load sags and is still on - as a meter's run is until its meter fell
    by half its size: a run is ended as one the reading cannot carry only
    once the whole reading is below half of it (Kozolec's 10.5 kW charge on
    a phase reading far less still is)."""
    st = D.PhaseState(noise=10.0, baseline=0.0, level=200.0, interval=5.0, floor_zero=True)
    st.open_edges = [D._Open(since=T0, watts=300.0, var=None, levels=[(T0, 300.0)])]
    assert st._unseen_stop(T0 + 60.0, 200.0) == [] and st.open_edges           # sagged to 200: on
    got = st._unseen_stop(T0 + 120.0, 120.0)
    assert len(got) == 1 and not st.open_edges                                 # under half: it cannot be


def _tree_fleet(hours=12, f=None, since=0):
    """Home's Hiša with Blaž PC inside it, through a Fleet in 2-hour passes:
    the PC's plug +300 W for 10 minutes every hour; Hiša's own channel the PC
    plus a 1 kW load of its own every 90 minutes and 150 W idle; the grid
    Hiša plus 200 W. The hours from ``since`` to ``hours`` into ``f``, or a
    new fleet. (the fleet)"""
    pc = lambda s: 300.0 if (s % 3600.0) >= 600.0 and (s % 3600.0) < 1200.0 else 0.0       # noqa: E731
    other = lambda s: 1000.0 if (s % 5400.0) >= 2400.0 and (s % 5400.0) < 3000.0 else 0.0  # noqa: E731
    secs = hours * 3600
    later = lambda rows: [r for r in rows if r[0] >= T0 + since * 3600]    # noqa: E731
    plug = later(series(secs, pc, seed=1, base=0.0, noise=2.0))
    his = later(series(secs, lambda s: pc(s) + other(s), seed=2, base=150.0, noise=4.0))
    grid = later(series(secs, lambda s: pc(s) + other(s), seed=3, base=350.0, noise=6.0))
    if f is None:
        f = D.Fleet()
        f.wait_cap_s = D.METER_WAIT_CAP_S
    f.parents = {"Blaž PC": "Hiša"}
    cuts = [T0 + 7200.0 * k for k in range(since // 2 + 1, hours // 2)]
    end = T0 + secs
    for (g, e), (h, _), (p, _) in zip(_passes({"a": grid}, cuts, end), _passes({"a": his}, cuts, end),
                                      _passes({"a": plug}, cuts, end)):
        f.process(g, {"Hiša": h, "Blaž PC": p}, now_ts=e, single={"Blaž PC": True, "Hiša": False})
    return f


def test_a_meter_others_hang_under_reads_them_as_the_grid_reads_every_meter():
    """The fleet as a tree (2026-10-03): Hiša's own detector reads Blaž
    PC inside it - its own fleet, its sessions filed after the PC's have had
    their say - so its library knows which of its loads is the PC, as the
    grid's does; the PC is read by both, and only the grid's fleet learns a
    meter's report lag. A meter no read meter hangs under any longer files
    its own sessions again and asks no one."""
    f = _tree_fleet()
    assert list(f.views) == ["Hiša"] and f.views["Hiša"].main is f.subs["Hiša"]
    assert list(f.views["Hiša"].subs) == ["Blaž PC"] and not f.views["Hiša"].reference
    assert f.views["Hiša"]._wait > 0.0 and f.views["Hiša"].meter_lag.get("Blaž PC")   # read behind the PC
    his = f.subs["Hiša"]
    assert not his._file_now and his.meter_stop is not None                 # its fleet files and asks for it
    pc_at_his = [sig for sig in his.signatures if sig.locations.get("Blaž PC")]
    assert pc_at_his and all(abs(sum(sig.power.values()) - 300.0) < 60.0 for sig in pc_at_his), \
        [(sig.id, sig.power, sig.locations) for sig in his.signatures]
    assert any(sig.locations.get("Blaž PC") for sig in f.main.signatures)
    back = D.Fleet.from_dict(json.loads(json.dumps(f.to_dict())))
    assert back._view_states["Hiša"]["phase_votes"] == f.views["Hiša"].phase_votes
    f.parents = {}
    f.process({}, {}, now_ts=T0 + 12 * 3600.0 + 60.0)
    assert not f.views and his.meter_stop is None and his._file_now


def _kw(det):
    """The 1 kW load's signatures in a detector of _tree_fleet's - its runs
    that do not start with the PC's: every third one does, which Hiša files
    as the PC's run and the grid as one 1.3 kW run of neither."""
    return [x for x in det.signatures if abs(sum(x.power.values()) - 1000.0) < 150.0]


def test_a_load_inside_a_circuit_is_the_circuits_own_and_is_named_there():
    """Mansarda's fridge and freezer (2026-10-03: "if a same load is
    detected by both meters, shouldn't the reading collapse into a single
    device anyway?"): a load a meter holding several devices saw is that
    meter's own signature - the grid files its runs as that meter's
    (Run.parent_signature) and grows no copy - so it is offered in that meter's
    list, and named there it is a named load like any other: its energy
    and its running are the meter's own sessions'."""
    f = _tree_fleet()
    his = f.subs["Hiša"]
    kw = _kw(his)
    assert len(kw) == 1 and kw[0].count >= 4, [(x.id, x.power, x.count) for x in his.signatures]
    assert not _kw(f.main), [(x.id, x.power, x.count) for x in _kw(f.main)]       # no copy on the grid
    sid = kw[0].id
    groups = f.namable(lambda m: m == "Blaž PC", 2, 50.0)
    assert ("Hiša", sid) in [r for r, _ in groups.get("Hiša", [])], {k: [r for r, _ in v] for k, v in groups.items()}
    # the PC holds one device: none of its signatures - its plug's or Hiša's of it - is offered
    assert not any(m == "Blaž PC" or f.signature(r).locations.get("Blaž PC")
                   for rows in groups.values() for r, _ in rows for m in [r[0]])
    assert f.rename(("Hiša", sid), "Bojler")
    assert f.names() == {"Bojler": [("Hiša", sid)]}
    assert ("Hiša", sid) not in [r for rows in f.namable(lambda m: m == "Blaž PC", 2, 50.0).values() for r, _ in rows]
    own = sum(r["kwh"] for r in his.recent if r["signature"] == sid) * 1000.0      # Hiša's own sessions of it
    assert abs(f.energy_by_name()["Bojler"] - own) < 5.0 and own > 600.0, (f.energy_by_name(), own)
    assert sum(f.hourly_by_name("Bojler").values()) == kw[0].energy_wh
    # it runs again: what it gains is what Hiša's next session of it brought
    _tree_fleet(15, f, since=12)
    now = sum(r["kwh"] for r in his.recent if r["signature"] == his._current(sid)) * 1000.0
    assert now > own + 150.0 and abs(f.energy_by_name()["Bojler"] - now) < 5.0, (f.energy_by_name(), own, now)
    # ...and it is on while the grid's run of it, placed by Hiša's own step
    # (_Open.meter), is open beside Hiša's run of it. Hiša's run alone is not
    # a load the house is running (one source of truth, Fleet.running): the
    # grid's run, placed by the meter's step, is what is running (INS-17).
    t = T0 + 16 * 3600.0
    his.phases["a"].open_edges = [D._Open(since=t - 60.0, watts=1000.0, var=None, levels=[(t - 60.0, 1000.0)])]
    his.phases["a"].level = (his.phases["a"].baseline or 0.0) + 1000.0      # the phase really carries it
    grid = f.main.phases["a"]
    grid.open_edges = [D._Open(t - 60.0, 1000.0, None, [(t - 60.0, 1000.0)], meter="Hiša")]
    grid.level = (grid.baseline or 0.0) + 1000.0
    f._chans = lambda m, ph: ["a"]
    f._steps_over = lambda m, c, ph, since, up: [(t - 60.0, 1000.0, None, t - 60.0, t - 55.0)]   # Hiša's rise
    assert f.active_by_name(t) == {"Bojler": 1000.0}, f.active_by_name(t)
    grid.open_edges = []
    assert f.active_by_name(t) == {}, "Hiša's run without the grid's is not a running load"


LINK = "/config/integrations/integration/load_insights#config_entry=E"


def _row(name, watts, ref=None, phases="a", since=0.0, tag="", where=None):
    return {"name": name, "ref": ref, "refs": {ref} if ref else set(), "watts": watts, "phases": phases,
            "since": since, "where": where, "tag": tag}


def test_running_markdown_empty():
    assert D.running_markdown([], LINK) == "Nothing detected running."


def test_running_markdown_order_links_and_new_load():
    rows = [_row(None, 1200.0, ("", 42), tag="kettle?"), _row(None, 300.0, ("", 57)), _row(None, 640.0),
            _row("Hladilnik", 95.0, ("", 3), phases="b", where="Mansarda"),
            _row(None, 270.0, ("Mansarda", 11), where="Mansarda")]
    md = D.running_markdown(rows, LINK, offered={("", 42), ("Mansarda", 11)}, now=180.0).split("\n")
    assert md[0] == "- **Hladilnik** · 95 W on phase B · 3 min · Mansarda", md
    assert md[1] == f"- [Unnamed #42]({LINK}) · 1.2 kW on phase A · 3 min · kettle?", md      # biggest unnamed first
    assert md[2] == "- New load · 640 W on phase A · 3 min", md
    assert md[3] == "- Unnamed #57 · 300 W on phase A · 3 min", md                            # not offered: no link
    assert md[4] == f"- [Unnamed #11]({LINK}) · 270 W on phase A · 3 min · Mansarda", md


def test_running_markdown_cap():
    rows = [_row(None, 100.0 + i, ("Mansarda", 100 + i), where="Mansarda") for i in range(15)]
    md = D.running_markdown(rows, LINK, offered={r["ref"] for r in rows}, now=3600.0)
    lines = md.split("\n")
    assert len(lines) == 11 and lines[-1] == "- … 5 more" and len(md.encode()) < 3000, md


def _running_fleet(grid_runs, subs=None, single=None, rise=None):
    """A Fleet with open runs put straight in: grid_runs = [(phase, _Open)],
    subs = {meter: Detector}. The two span helpers are stubbed to say the
    meter's one channel "a" rose at ``rise`` - the test is about the rows."""
    f = D.Fleet()
    f.subs = dict(subs or {})
    f.single = dict(single or {})
    for ph, o in grid_runs:
        f.main.phases.setdefault(ph, D.PhaseState()).open_edges.append(o)
    f._chans = lambda m, ph: ["a"]
    f._steps_over = lambda m, c, ph, since, up: [] if rise is None else [(rise, 95.0, None, since, since + 5.0)]
    return f


def _open(since, watts, meter=None):
    return D._Open(since, watts, None, [(since, watts)], meter=meter)


def _mansarda(name):
    """Mansarda's detector: one signature, #11, 95 W on its channel a, named ``name`` or not, and the run
    of it open since T0 + 100."""
    det = D.Detector()
    det.signatures = [_sig(11, 95.0, phases="a", name=name)]
    det.phases["a"] = D.PhaseState()
    det.phases["a"].open_edges = [_open(T0 + 100.0, 95.0)]
    return det


def test_running_named_submeter_load_once():
    t = T0 + 160.0
    grid = _sig(42, 95.0, phases="b", count=6)                     # the grid's unnamed twin of the fridge
    f = _running_fleet([("b", _open(T0 + 100.0, 95.0, "Mansarda"))], {"Mansarda": _mansarda("Hladilnik")},
                       {"Mansarda": False}, rise=T0 + 100.0)
    f.main.signatures = [grid]
    rows = f.running(t)
    assert len(rows) == 1, rows
    r = rows[0]
    assert (r["name"], r["where"], r["watts"], r["phases"], r["since"]) == ("Hladilnik", "Mansarda", 95.0, "b", T0 + 100.0)
    assert r["refs"] == {("Mansarda", 11)}
    md = D.running_markdown(rows, LINK, now=t)
    assert "**Hladilnik**" in md and "Unnamed #42" not in md and md.count("\n") == 0


def test_running_unnamed_submeter_load_links_meter_id():
    t = T0 + 160.0
    f = _running_fleet([("b", _open(T0 + 100.0, 95.0, "Mansarda"))], {"Mansarda": _mansarda(None)},
                       {"Mansarda": False}, rise=T0 + 100.0)
    f.main.signatures = [_sig(42, 95.0, phases="b", count=6)]
    rows = f.running(t)
    assert len(rows) == 1 and rows[0]["ref"] == ("Mansarda", 11), rows
    md = D.running_markdown(rows, LINK, offered={("Mansarda", 11)}, now=t)
    assert f"[Unnamed #11]({LINK})" in md and md.endswith("· Mansarda"), md
    assert f.running_refs(t) == {("Mansarda", 11)}


def test_running_one_device_meter():
    f = _running_fleet([("a", _open(T0, 7000.0, "EVBox"))], single={"EVBox": True})
    rows = f.running(T0 + 60.0)
    assert len(rows) == 1 and rows[0]["name"] == "EVBox" and rows[0]["ref"] is None and rows[0]["where"] is None, rows


def test_running_unmatched_meter_run():
    """The meter's run already closed (nothing opened at the rise): the grid's own guess, still one row."""
    t = T0 + 160.0
    det = _mansarda(None)
    det.phases["a"].open_edges = []
    f = _running_fleet([("b", _open(T0 + 100.0, 95.0, "Mansarda"))], {"Mansarda": det}, {"Mansarda": False},
                       rise=T0 + 100.0)
    f.main.signatures = [_sig(42, 95.0, phases="b", count=6, name="Hladilnik")]
    rows = f.running(t)
    assert len(rows) == 1 and rows[0]["name"] == "Hladilnik" and rows[0]["where"] == "Mansarda", rows
    assert rows[0]["ref"] == ("", 42)


def test_running_same_name_merges():
    f = _running_fleet([("a", _open(T0, 1000.0)), ("c", _open(T0 + 600.0, 1000.0))])
    f.main.signatures = [_sig(1, 1000.0, phases="a", name="Pec", dur=3600.0),
                         _sig(2, 1000.0, phases="c", name="Pec", dur=3600.0)]
    rows = f.running(T0 + 900.0)
    assert len(rows) == 1 and rows[0]["name"] == "Pec", rows
    assert rows[0]["watts"] == 2000.0 and rows[0]["phases"] == "ac" and rows[0]["since"] == T0
    assert rows[0]["refs"] == {("", 1), ("", 2)}


def test_named_loads_on_one_phase_are_on_independently():
    """A 95 W fridge beside a 2 kW boiler on one phase: the phase's total (2095 W) matched neither
    signature, so both read off. Each run is read alone now."""
    t = T0 + 700.0
    boiler, fridge = _open(T0, 2000.0), _open(T0 + 600.0, 95.0)
    f = _running_fleet([("a", boiler), ("a", fridge)])
    f.main.signatures = [_sig(1, 2000.0, name="Bojler", dur=3600.0), _sig(2, 95.0, name="Hladilnik", dur=3600.0)]

    def agree(want):
        assert f.active_by_name(t) == want, f.active_by_name(t)
        assert set(f.active_by_name(t)) == {r["name"] for r in f.running(t) if r["name"]}
        md = D.running_markdown(f.running(t), LINK, now=t)
        assert all(f"**{n}**" in md for n in want), md

    agree({"Bojler": 2000.0, "Hladilnik": 95.0})
    f.main.phases["a"].open_edges.remove(boiler)
    agree({"Hladilnik": 95.0})
    f.main.phases["a"].open_edges.remove(fridge)
    agree({})


def test_running_refs_agree_with_rows():
    t = T0 + 160.0
    f = _running_fleet([("b", _open(T0 + 100.0, 95.0, "Mansarda")), ("a", _open(T0, 2000.0))],
                       {"Mansarda": _mansarda(None)}, {"Mansarda": False}, rise=T0 + 100.0)
    f.main.signatures = [_sig(7, 2000.0, dur=3600.0)]
    rows = f.running(t)
    assert f.running_refs(t) == set().union(*(r["refs"] for r in rows)) == {("Mansarda", 11), ("", 7)}


def test_a_circuits_named_load_survives_a_restart_and_a_reset():
    """The name is in the store with the meter's library (Fleet.to_dict), and
    a reset carries it to that meter's detector alone, which hands it back
    to the rebuilt signature that looks like it."""
    f = _tree_fleet()
    sid = _kw(f.subs["Hiša"])[0].id
    f.rename(("Hiša", sid), "Bojler")
    back = D.Fleet.from_dict(json.loads(json.dumps(f.to_dict())))
    assert back.names() == {"Bojler": [("Hiša", sid)]}
    assert abs(back.energy_by_name()["Bojler"] - f.energy_by_name()["Bojler"]) < 0.5     # stored to 0.1 Wh an hour
    assert [(d["name"], d.get("meter")) for d in D.names_in_store({"fleet": f.to_dict()})] == [("Bojler", "Hiša")]
    carried = f.name_descriptors()
    assert [(d["name"], d.get("meter")) for d in carried] == [("Bojler", "Hiša")]
    fresh = D.Fleet()
    fresh.wait_cap_s = D.METER_WAIT_CAP_S
    fresh.carry_names(carried)
    assert not fresh.main.orphan_names and [o["name"] for o in fresh.subs["Hiša"].orphan_names] == ["Bojler"]
    _tree_fleet(f=fresh)
    assert list(fresh.names()) == ["Bojler"] and fresh.names()["Bojler"][0][0] == "Hiša", fresh.names()
    assert not fresh.subs["Hiša"].orphan_names


def test_a_name_given_on_the_grid_stays_on_the_grid():
    """Home's kiln was named on the grid's signature before its circuit's
    own counted (Peč za glino, inside Hiša): the grid keeps filing the runs
    its device takes into that named signature, and a reset hands the name
    back there too - a name stays where it was given; moving it is the owner's
    call. Hiša's own signature of it is that named load, not offered."""
    f = D.Fleet()
    f.wait_cap_s = D.METER_WAIT_CAP_S
    # both meters mapped, as at a site past its first days: with step votes
    # (2026-10-14) Hiša mapped within these 12 hours and the PC not yet, and
    # the 1 kW load's runs beside the PC's filed as 1.3 kW ones - the name's
    # place is this test's question, not the maps' timing
    f.phase_votes = {n: {"a": {"a": D.PHASE_MAP_MIN_VOTES}} for n in ("Hiša", "Blaž PC")}
    f.carry_names([{"name": "Bojler", "phases": "a", "power": {"a": 1000.0}, "duration_s": 600.0, "pf": None}])
    _tree_fleet(f=f)
    kw = _kw(f.main)
    assert len(kw) == 1 and kw[0].name == "Bojler" and kw[0].count >= 4, [(x.id, x.name, x.count) for x in kw]
    assert not f.main.orphan_names
    his = _kw(f.subs["Hiša"])
    groups = f.namable(lambda m: m == "Blaž PC", 2, 50.0)
    assert his and not any(r == ("Hiša", x.id) for x in his for rows in groups.values() for r, _ in rows), groups


def test_an_event_on_several_phases_is_placed_where_every_leg_was_one_meters():
    """The kiln, on A and C, under Hiša: an event on several phases is placed
    at the meter whose own steps were all of every leg - a 3EM's two channels
    - as a start on one phase is (the unify audit's F1, 2026-10-03); a plug
    can take one leg only, and places nothing."""
    f = D.Fleet()
    for ph in "ac":
        f.main.phases[ph] = D.PhaseState(noise=10.0, interval=2.0)
        _declare(f.main.phases[ph], (T0, 2000.0, None, T0 - 2.0, T0))
    his = D.Detector()
    for c in "ac":
        his.phases[c] = D.PhaseState(noise=5.0, interval=10.0, last_ts=T0 + 60.0)
        _declare(his.phases[c], (T0 + 3.0, 2000.0, None, T0 - 7.0, T0 + 3.0))
    f.subs["Hiša"] = his
    f.phase_votes = {"Hiša": {"a": {"a": D.PHASE_MAP_MIN_VOTES}, "c": {"c": D.PHASE_MAP_MIN_VOTES}}}
    f._bind()
    cl = f.main._classify_step("ac", T0, 4000.0, None, 0.0, legs=[("a", T0, 2000.0), ("c", T0, 2000.0)])
    assert cl.owner == "Hiša", cl.owner
    f.phase_votes = {"Hiša": {"a": {"a": D.PHASE_MAP_MIN_VOTES}}}
    his.phases["c"].declared, his.phases["c"].declared_t = [], []
    cl = f.main._classify_step("ac", T0, 4000.0, None, 0.0, legs=[("a", T0, 2000.0), ("c", T0, 2000.0)])
    assert cl.owner != "Hiša"


def test_a_meters_stop_the_grid_did_not_show_ends_its_run():
    """Home 09-22 11:20: the pump stopped (its plug -942 W) as a 3.2 kW load
    on its phase rose by about as much - the grid declared no fall at all,
    and the pump's 755 W run, freed by its plug's fall, stayed open two
    hours, 1.5 kWh in the hidrofor's signature. A run a meter's own step
    started ends at the meter's fall where the grid, read past it, showed
    none of its own - by _meter_stop's rule, the fall asked once - if the
    meter still shows it stopped: a pause too short for the grid is not -
    and only a stop the meter timed, its span within the merge tolerance."""
    f = _fleet_with_meters({"Hidrofor": 0.0})
    plug, grid = f.subs["Hidrofor"].phases["a"], f.main.phases["c"]
    plug.interval = 10.0
    _declare(grid, (T0, 755.0, None, T0 - 4.0, T0))
    _declare(plug, (T0 + 1.0, 946.0, None, T0 - 9.0, T0 + 1.0), (T0 + 61.0, -942.0, None, T0 + 51.0, T0 + 61.0))
    f.sub_rows["Hidrofor"] = {"a": [(T0 - 60.0, 1.0), (T0 + 1.0, 947.0), (T0 + 61.0, 5.0)]}
    f._bind()
    grid.lib, grid.name, grid.baseline, grid.level = f.main, "c", 3000.0, 3755.0
    run = D._Open(since=T0, watts=755.0, var=None, levels=[(T0, 755.0)], meter="Hidrofor")
    grid.open_edges = [run]
    assert grid._meter_ended(T0 + 60.0) == []                               # the cursor starts here
    assert grid._meter_ended(T0 + 70.0) == [] and grid.open_edges           # the grid has not read past it yet
    f.sub_rows["Hidrofor"]["a"].append((T0 + 70.0, 950.0))                # back on after a pause the grid never showed
    assert grid._meter_ended(T0 + 120.0) == [] and grid.open_edges
    f.sub_rows["Hidrofor"]["a"].pop()
    grid.ended_upto, f._stops_used = T0 + 50.0, {}
    got = grid._meter_ended(T0 + 120.0)
    assert len(got) == 1 and got[0].end == T0 + 61.0 and not grid.open_edges
    assert grid._meter_ended(T0 + 200.0) == []
    run = D._Open(since=T0, watts=755.0, var=None, levels=[(T0, 755.0)], meter="Hidrofor")
    grid.open_edges, grid.ended_upto, f._stops_used = [run], T0 + 50.0, {}
    plug.declared[-1] = (T0 + 61.0, -942.0, None, T0 + 1.0, T0 + 61.0)       # a fall it did not time: its span a minute
    plug.declared_t[-1] = T0 + 61.0
    assert grid._meter_ended(T0 + 120.0) == [] and grid.open_edges


def test_a_channels_phase_is_learned_from_the_count_or_the_energy_of_its_votes():
    """The owner (2026-10-03): score the votes by both their number and their
    energy. Home's Susilna plug votes 2-4 times in ten days, each a 20-hour
    run: as much energy as PHASE_MAP_MIN_VOTES of the site's votes carry
    places it, as that many votes would. A few tiny votes do not, nor do
    votes whose count and energy clearly disagree."""
    f = D.Fleet()
    f.phase_votes = {"Hiša": {"a": {"a": 100}}}                      # the site's votes: 10 Wh each,
    f.phase_energy = {"Hiša": {"a": {"a": 1000.0}}}                  # 30 of them 300 Wh
    f.phase_votes["Susilna"] = {"a": {"c": 2, "b": 1}}
    f.phase_energy["Susilna"] = {"a": {"c": 11000.0, "b": 4.0}}
    assert f.phase_map("Susilna") == {"a": "c"}
    f.phase_votes["UPS"] = {"a": {"b": 2, "c": 2}}
    f.phase_energy["UPS"] = {"a": {"b": 9.0, "c": 11.0}}
    assert f.phase_map("UPS") == {}                                  # 4 votes, 20 Wh: nothing yet
    f.phase_votes["Odd"] = {"a": {"c": 30, "b": 3}}
    f.phase_energy["Odd"] = {"a": {"c": 30.0, "b": 3000.0}}
    assert f.phase_map("Odd") == {}                                  # the count says c, the energy b
    f.phase_votes["Odd"]["a"]["b"] = 20
    f._votes_rev += 1                                                # as _vote_phases counts each vote it casts
    assert f.phase_map("Odd") == {"a": "b"}                          # the energy's, the count not clearly against
    back = D.Fleet.from_dict(json.loads(json.dumps(f.to_dict())))
    assert back.phase_energy == f.phase_energy and back.phase_map("Susilna") == {"a": "c"}
    g = D.Fleet()
    g.phase_votes, g.phase_energy = {"Plug": {"a": {"c": 2}}}, {"Plug": {"a": {"c": 9000.0}}}
    assert g.phase_map("Plug") == {}                                 # a site of 2 votes says nothing of a typical one


def test_a_meters_run_is_on_while_its_reading_holds_half_of_it():
    """Net, as read: Kozolec's water pump wandered +184, -17, -78, +74 W - its
    declared steps never down by half the run - and drifted off unseen; its
    run held five hours (09-27). A run its meter's own step started is on
    while the meter reads at least half its size over what it read just
    before the run started, at the grid's moment - drift it never declared
    counts - and a circuit's other load starting and stopping meanwhile
    does not free it."""
    f = _fleet_with_meters({"Pump": 0.0})
    pump = f.subs["Pump"].phases["a"]
    pump.interval = 10.0
    _declare(pump, (T0 + 1.0, 184.0, None, T0 - 9.0, T0 + 1.0), (T0 + 40.0, -17.0, None, T0 + 30.0, T0 + 40.0),
             (T0 + 61.0, -78.0, None, T0 + 51.0, T0 + 61.0), (T0 + 93.0, 74.0, None, T0 + 83.0, T0 + 93.0))
    rows = [(T0 - 60.0, 2.0), (T0 + 1.0, 186.0), (T0 + 40.0, 169.0), (T0 + 61.0, 91.0), (T0 + 93.0, 165.0)]
    f.sub_rows["Pump"] = {"a": rows}
    assert f._meter_on("Pump", "c", T0, 188.0, T0 + 120.0)                  # wandering about its size: on
    rows += [(T0 + 300.0, 120.0), (T0 + 900.0, 60.0), (T0 + 1500.0, 25.0)]   # drifting down, no step declared
    assert f._meter_on("Pump", "c", T0, 188.0, T0 + 600.0)
    assert not f._meter_on("Pump", "c", T0, 188.0, T0 + 1000.0)              # under half of it: free
    rows[-3:] = [(T0 + 300.0, 2186.0), (T0 + 900.0, 165.0)]                  # another load on and off: still on
    assert f._meter_on("Pump", "c", T0, 188.0, T0 + 1000.0)


def test_a_meter_others_hang_under_is_never_guessed_one_device():
    """A meter with meters inside it holds several by definition - Home's
    Hiša, with Blaž PC under it, though 52 % of its sightings are one
    signature. The runner asked that itself, so a bench replay that declared
    nothing ran Hiša as one device; the Fleet asks it now."""
    f = D.Fleet()
    f.subs["Hiša"] = D.Detector()
    f.subs["Hiša"].signatures = [_sig(1, 120.0, count=40), _sig(2, 900.0, count=5)]
    assert f.guess_one_device("Hiša") and f.holds_one_device("Hiša")          # its library looks like one
    f.parents = {"Blaž PC": "Hiša", "Hiša": None}
    assert not f.guess_one_device("Hiša") and not f.holds_one_device("Hiša")  # a meter inside it: several
    f.single = {"Hiša": True}
    assert f.holds_one_device("Hiša")                                         # the user's answer stands


def test_a_run_is_not_filed_as_a_meter_that_held_through_its_start():
    """Home's plain cluster of 1 kW phase-A starts filed its runs as the
    hidrofor, its majority: a 2.3 kW load's last 1,064 W step went with them
    while the hidrofor's plug showed no start. A signature placed at a
    one-device meter is avoided while that meter held through the start - as
    one placed at a switch that was off."""
    f = _fleet_with_meters({"Hidrofor": 0.0})
    f.single = {"Hidrofor": True}
    f.subs["Hidrofor"].phases["a"].interval = 10.0
    f._now = T0 + 3600.0
    home = _sig(31, 900.0, 70.0, 40, phases="c", first_seen=T0, last_seen=T0)
    home.locations["Hidrofor"] = 30
    other = _sig(32, 1000.0, 600.0, 40, phases="c", first_seen=T0, last_seen=T0)
    f.main.signatures = [home, other]
    run = D.Run(phases="c", start=T0, end=T0 + 450.0, levels={"c": [(T0, 1064.0)]})
    assert f._held_homes(run) == [31]                                    # the plug held: not the hidrofor
    f.single = {"Hidrofor": False}
    assert f._held_homes(run) == [31]                                    # a circuit that held did not start it either
    _declare(f.subs["Hidrofor"].phases["a"], (T0 + 3.0, 880.0, None, T0 - 7.0, T0 + 3.0))
    assert f._held_homes(run) == []                                      # the plug started with it


def test_a_meters_sessions_decide_identity_as_its_declaration_says():
    """One notion of "holds one device" (2026-10-03): a meter's
    session decides which signature a run joins - and its energy places one
    it has no session for - where the meter holds one device, as the user
    declared it or, undeclared, as its library's shape says. A circuit's
    session makes the run its own signature's (Run.parent_signature) and the grid
    files nothing - unless the grid's signature for the run's device wears
    a name: a name stays where it was given."""
    f = _fleet_with_meters({"Plug": 0.0})
    own = _sig(1, 300.0, 600.0, 40, first_seen=T0, last_seen=T0)
    f.subs["Plug"].signature_of = lambda s: own
    sub = D.Run("a", T0, T0 + 600.0, {"a": [(T0, 300.0)]})
    run = D.Run("c", T0, T0 + 600.0, {"c": [(T0, 300.0)]}, pair=(5, None))
    f.identity = {"Plug": {"1": 77}}
    asked = []
    f._place = lambda m, name, prefer: asked.append(prefer)
    f.single = {"Plug": False}
    f._file_as(run, "Plug", sub)
    assert asked == [] and run.parent_signature == ("Plug", sub), (asked, run.parent_signature)       # the circuit's own
    named = _sig(8, 300.0, 600.0, 40, phases="c", first_seen=T0, last_seen=T0, name="Kiln")
    f.main.signatures, f.main.pair_home, f.main._device_home = [named], {"5": {8: 3.0}}, None
    run.parent_signature = None
    f._file_as(run, "Plug", sub)
    assert asked == [8] and run.parent_signature is None, (asked, run.parent_signature)            # its device's named signature
    f.single = {"Plug": True}
    f._file_as(run, "Plug", sub)
    assert asked == [8, 77], asked

def test_a_run_kept_out_of_its_meters_signature_does_not_move_where_the_device_lives():
    """Home's hidrofor (09-30 12:29, 2026-10-04): a 998 W, eight-minute run
    its plug's session sent to the pump's signature was kept out of it and
    filed in a 1 kW phase-A one; the meter's identity followed it there, and
    so did the pump's next 102 runs. The device stays where its meter's word
    put it while that signature stands: a run filed where it was sent leaves
    it, and only one sent to a signature that is gone moves it."""
    f = _fleet_with_meters({"Plug": 0.0})
    f.single = {"Plug": True}
    own = _sig(1, 900.0, 60.0, 40, first_seen=T0, last_seen=T0)
    f.subs["Plug"].signature_of = lambda s: own
    sub = D.Run("a", T0, T0 + 60.0, {"a": [(T0, 900.0)]})
    pump = _sig(4, 900.0, 60.0, 40, phases="c", first_seen=T0, last_seen=T0)
    other = _sig(25, 1000.0, 60.0, 40, phases="c", first_seen=T0, last_seen=T0)
    f.main.signatures = [pump, other]
    f.identity = {"Plug": {"1": 4}}
    went, asked = [25, 4, 25], []

    def place(m, name, prefer):                      # the filing's own say, the guards' included
        asked.append(prefer)
        return f.main._sig(went.pop(0))
    f._place = place
    run = D.Run("c", T0, T0 + 60.0, {"c": [(T0, 900.0)]})
    f._file_as(run, "Plug", sub)                     # sent to #4, kept out of it, filed in #25
    assert f.identity["Plug"]["1"] == 4, f.identity
    f._file_as(run, "Plug", sub)                     # filed where it was sent
    assert f.identity["Plug"]["1"] == 4 and asked == [4, 4], (f.identity, asked)
    f.main.signatures, f.main._by_sig = [other], None    # the pump's signature gone
    f._file_as(run, "Plug", sub)
    assert f.identity["Plug"]["1"] == 25, f.identity


def test_a_circuit_meter_explains_only_what_its_own_sub_meters_did_not():
    """Blaž PC inside Hiša: the PC's declared step counts once, and Hiša adds
    only what else inside it changed - the pieces of a grid step do not overlap."""
    f = _fleet_with_meters({"Hiša": 2200.0, "Blaž PC": 300.0, "Bojler": 0.0}, {"Blaž PC": "Hiša"})
    got = dict(f._meter_steps("c", T0, 5.0))
    assert got == {"Hiša": 1900.0, "Blaž PC": 300.0, "Bojler": 0.0}, got
    assert f._meter_steps("a", T0, 5.0) == []                          # none of them is on phase A


def test_a_step_belongs_to_the_innermost_meter_whose_own_step_was_all_of_it():
    """The cycler inside Hisa is Hisa's; Blaz PC's step, which Hisa saw too,
    is Blaz PC's. Each compared by the step its own detector declared."""
    f = _fleet_with_meters({"Hiša": 300.0, "Blaž PC": 300.0}, {"Blaž PC": "Hiša"})
    assert f._step_meter("c", T0, 300.0, True) == "Blaž PC"
    f = _fleet_with_meters({"Hiša": 65.0, "Blaž PC": 0.0}, {"Blaž PC": "Hiša"})
    assert f._step_meter("c", T0, 65.0, True) == "Hiša"
    assert f._step_meter("c", T0, 65.0, False) is None                  # it rose; this step fell
    assert f._step_meter("c", T0, 900.0, True) is None                  # nothing saw all of it
    _declare(f.subs["Hiša"].phases["a"], (T0 - 3.0, -65.0, None, T0 - 5.0, T0 - 2.0))   # its last pulse ending just before,
    _declare(f.main.phases["c"], (T0 - 3.0, -65.0, None, T0 - 4.0, T0 - 3.0))          # a step the grid took on its own,
    assert f._step_meter("c", T0, 65.0, True) == "Hiša"                   # is not summed into this start



def test_a_stop_netted_into_another_loads_start_is_split_out():
    """Home 28.09 01:16:35: the pump stopped (its plug 810 W, then 0.5 W ten
    seconds later) in the reading a 1.9 kW load started - one +1,108 W rise on
    the grid. Its pieces are the pump's -810 and a +1,918 start; had the grid
    taken the stop on its own, the rise would stay whole."""
    f = _fleet_with_meters({"Hidrofor": 0.0})
    plug, grid = f.subs["Hidrofor"].phases["a"], f.main.phases["c"]
    _declare(plug, (T0 + 5.0, -810.0, None, T0 - 1.0, T0 + 9.0))
    _declare(grid, (T0, 1108.0, None, T0 - 3.0, T0 + 2.0))
    assert f._meter_steps("c", T0, 6.0, True, 1108.0) == [("Hidrofor", -810.0)]
    det = f.main
    det.meter_steps = f._meter_steps
    assert det.metered_parts("c", T0, 1108.0) == [-810.0, 1918.0]
    _declare(grid, (T0 + 6.0, -800.0, None, T0 + 2.0, T0 + 6.0))          # the grid saw the stop by itself
    assert det.metered_parts("c", T0, 1108.0) == [1108.0]

def test_a_meter_learns_its_gain_against_the_grid():
    """A plug reading 4 % low is matched in the grid's terms once learned."""
    f = _fleet_with_meters({"Plug": 960.0})
    _declare(f.main.phases["c"], (T0, 1000.0, None, T0 - 1.0, T0 + 2.0))   # the grid's own step
    for _ in range(D.METER_GAIN_MIN):
        assert f._step_meter("c", T0, 1000.0, True) == "Plug"
    assert abs(f.gain("Plug") - 1000.0 / 960.0) < 1e-6, f.gain("Plug")
    assert abs(f._meter_totals("c", T0, 5.0)["Plug"][0] - 1000.0) < 1e-6


def test_a_meter_reading_less_often_is_compared_over_its_own_span():
    """The kiln's element on and a 580 W load off six seconds later: +2,800 and
    -580 on the grid, read every 2 s; +2,240 on Hisa, which read only then.
    Over Hisa's span the grid's net is its step: the rise is Hisa's, whole."""
    f = _fleet_with_meters({"Hiša": 0.0})
    _declare(f.subs["Hiša"].phases["a"], (T0 + 0.3, 2240.0, None, T0 - 1.0, T0 + 7.0))
    g = f.main.phases["c"]
    _declare(g, (T0, 2800.0, None, T0 - 2.0, T0 + 2.0), (T0 + 6.0, -580.0, None, T0 + 4.0, T0 + 8.0))
    assert f._step_meter("c", T0, 2800.0, True) == "Hiša"
    assert dict(f._meter_steps("c", T0, 6.0, True, 2800.0)) == {"Hiša": 2800.0}   # all of it: no phantom
    g.declared.pop(); g.declared_t.pop()                                        # without the -580 ...
    assert f._step_meter("c", T0, 2800.0, True) is None                          # ... 2,240 is not all of 2,800


def test_a_change_reporters_span_starts_three_cadences_before_its_first_new_reading():
    """The recorder keeps only changes, so a meter's silence is a held value:
    the change is within SUSTAIN_CADENCES of its cadence before its first new
    reading - the hidrofor's 10 s plug three cycles, 30 s."""
    st = D.PhaseState()
    st.steady_ts, st.interval = T0 - 60.0, 1.2                                     # reports a change within ~1 s
    assert T0 - 3.9 <= st.span_start(T0) <= T0 - 3.0                               # not a minute back
    st.interval = 10.0                                                             # a 10 s poll
    assert st.span_start(T0) == T0 - 30.0                                          # three polls back
    grid = D.PhaseState()                                                          # polled every 2 s
    grid.interval, grid.steady_ts = 2.0, T0 - 3.8
    assert grid.span_start(T0) == T0 - 3.8                                         # nothing unwritten: from its last reading



def test_a_meters_cadence_is_how_often_it_writes_while_its_value_moves():
    """Kozolec's Victron: polled every 5.3 s, plus a refresh of every entity
    once a minute landing anywhere in the poll - its shortest gaps are that
    refresh, not how soon it reports. The IR panel: a 60 s heartbeat, a
    change reported within 5 s. Both read by the gaps after a reading that
    moved: 5.3 and 5 s."""
    import random
    rnd = random.Random(7)
    victron, ts, w = D.PhaseState(min_noise=10.0), T0, 3000.0
    for k in range(4000):
        if k % 8 == 0:
            w += rnd.choice([-1, 1]) * 500.0 if w > 1000 else 500.0       # a load switching
        victron.process(ts, w + rnd.uniform(-3, 3))
        if k % 12 == 5:                                                    # the minute's refresh
            victron.process(ts + rnd.uniform(0.6, 4.7), w + rnd.uniform(-3, 3))
        ts += 5.3
    assert 5.0 <= victron.interval <= 5.4, victron.interval
    panel, ts = D.PhaseState(min_noise=10.0), T0
    for cycle in range(30):
        for k in range(10):                                                # idle, a heartbeat a minute
            panel.process(ts, 0.4 + 0.2 * (k % 2)); ts += 60.0
        for w in (900.0, 905.0, 903.0):                                    # on: reported within 5 s, then settles
            panel.process(ts, w); ts += 5.0
        for k in range(5):
            panel.process(ts, 902.0 + k % 2); ts += 60.0
        for w in (0.5, 0.4):                                               # off
            panel.process(ts, w); ts += 5.0
    assert 4.9 <= panel.interval <= 5.1, panel.interval

def test_a_reading_of_the_changes_own_update_is_not_the_old_level_holding():
    """Home's grid is two writes per update - the inverter's, then the meter's
    ~25 ms later: the inverter's still showed the old level. The span starts at
    the update before, not 28 ms before the step (Home, 2026-09-23 16:20:06)."""
    st = D.PhaseState(min_noise=10.0)
    ts = T0
    for _ in range(60):                                              # pairs, 2 s apart
        st.process(ts, 230.0); st.process(ts + 0.025, 235.0); ts += 2.0
    st.process(ts, 232.0)                                            # the inverter's write: old level
    for k in range(4):
        st.process(ts + 0.028 + 2.0 * k, 3160.0); st.process(ts + 0.05 + 2.0 * k, 3165.0)
    step = next(e for e in st.declared if e[1] > 2000)
    assert abs(step[3] - (ts - 2.0 + 0.025)) < 0.01, step[3] - ts   # the last steady update, 2 s back


def test_a_reading_followed_by_silence_held_on_any_meter():
    """The hidrofor's plug, polled every 10 s, as the recorder keeps it: a run,
    ONE zero after the stop, then nothing until the next start - unchanged
    readings are not written. The stop is whole, and so is the next start."""
    st = D.PhaseState(min_noise=5.0)
    ts = T0
    for w in [0.0] * 30:
        st.process(ts, w); ts += 10.0
    st.noise = 160.0
    for w in [9095, 850, 913, 908, 811, 809, 0]:                     # the start's surge, the run, the stop
        st.process(ts, w); ts += 10.0
    ts += 900.0                                                      # fifteen minutes of unwritten zeros
    for w in [1253, 881, 838, 824, 811, 0]:
        st.process(ts, w); ts += 10.0
    ts += 900.0
    st.process(ts, 1100.0)
    sizes = [round(w) for _, w, *_ in st.declared]
    assert len(sizes) >= 4 and all(abs(x) > 750 for x in sizes[:4]), sizes



def test_a_load_left_running_after_a_multi_close_is_not_a_new_start():
    """Home 09-26 05:31: a fall closed a phase's last two open runs at once -
    700 and 500 W - while the phase still read 700 W above its floor, a load
    whose run had gone with another's. The level then snapped to the floor
    under it and the 700 W came back as a new start. With nothing open, the
    level is the floor only where the reading is."""
    st = D.PhaseState(min_noise=10.0)
    st.baseline, st.level, st.noise, st.interval = 200.0, 2100.0, 10.0, 2.0
    st.open_edges = [D._Open(since=T0 - 600.0, watts=700.0, var=None, levels=[(T0 - 600.0, 700.0)]),
                     D._Open(since=T0 - 300.0, watts=500.0, var=None, levels=[(T0 - 300.0, 500.0)])]
    closed, ts = [], T0
    for w in [2100.0, 2102.0] + [900.0, 902.0, 898.0, 901.0] + [903.0, 899.0, 900.0, 902.0] * 10:
        closed += st.process(ts, w); ts += 2.0
    assert len(closed) == 2, closed                                       # the multi-close
    assert not st.open_edges, [o.watts for o in st.open_edges]            # and no phantom 700 W start
    assert abs(st.level - 900.0) < 20.0, st.level


def test_a_one_device_meters_stop_ends_the_run_it_started():
    """Home 20.09 00:39: the pump started at +909 W, still settling, and
    stopped at -731 W while its plug fell 818 W to nothing. Too unlike to pair
    by size, the fall closed a 689 W and a 118 W run together, and the pump's
    ran on two hours. The plug says its device stopped: its run ends."""
    f = _fleet_with_meters({"Hidrofor": 0.0})
    f.single = {"Hidrofor": True}
    plug = f.subs["Hidrofor"].phases["a"]
    plug.interval = 10.0
    _declare(plug, (T0, 1000.0, None, T0 - 10.0, T0), (T0 + 70.0, -818.0, None, T0 + 60.0, T0 + 70.0))
    st = f.main.phases["c"]
    st.lib, st.name, st.baseline, st.level = f.main, "c", 130.0, 1846.0
    f.main.meter_stop = f._meter_stop
    st.open_edges = [D._Open(since=T0 - 7800.0, watts=689.0, var=None, levels=[(T0 - 7800.0, 689.0)]),
                     D._Open(since=T0 - 500.0, watts=118.0, var=None, levels=[(T0 - 500.0, 118.0)]),
                     D._Open(since=T0 + 1.7, watts=909.0, var=None, levels=[(T0 + 1.7, 909.0)])]
    _declare(st, (T0 + 1.7, 909.0, None, T0 - 2.6, T0 + 1.7), (T0 + 73.0, -731.0, None, T0 + 69.0, T0 + 73.0))
    closed = st._pair(T0 + 73.0, 731.0, None, 1115.0)
    assert [s.start for s in closed] == [T0 + 1.7], [s.start for s in closed]   # the pump's run
    assert sorted(o.watts for o in st.open_edges) == [118.0, 689.0]
    st.open_edges.append(D._Open(since=T0 + 200.0, watts=909.0, var=None, levels=[(T0 + 200.0, 909.0)]))
    _declare(st, (T0 + 75.0, -731.0, None, T0 + 74.0, T0 + 75.0))
    assert f._meter_stop("c", T0 + 74.0, T0 + 75.0, st.open_edges) is None   # one meter fall ends one run


def test_as_of_bisects_to_where_the_walk_went():
    """_as_of seeks instead of walking one row at a time (2026-10-02); on
    every series in time order - duplicate stamps, ts on a row, a start
    index past ts or off the end - it lands where the walk did."""
    def walk(rows, ts, i):
        if not rows or rows[0][0] > ts:
            return -1
        i = max(i, 0)
        while i + 1 < len(rows) and rows[i + 1][0] <= ts:
            i += 1
        return i
    rnd = random.Random(7)
    for _ in range(5000):
        rows = sorted((float(rnd.choice([0, 1, 1, 2, 3, 3, 3, 5, 8])), rnd.random()) for _ in range(rnd.randint(0, 12)))
        ts, i = rnd.choice([-1.0, 0.0, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 8.0, 9.0]), rnd.randint(-2, 14)
        assert D._as_of(rows, ts, i) == walk(rows, ts, i), (rows, ts, i)
    long = [(float(t), 0.0) for t in range(0, 1000, 2)]
    assert D._as_of(long, 501.0, 0) == walk(long, 501.0, 0) == 250
    assert D._as_of(long, 500.0, 3) == walk(long, 500.0, 3) == 250


def test_a_meter_step_the_grid_has_not_yet_shown_is_not_netted_into_an_earlier_rise():
    """Kozolec 09-22 11:33: the grid rises +223 W at 11:33:28 (its plateau from
    11:33:38); the boiler's meter, read a horizon ahead, has declared its
    -1929 W stop with a span 11:33:38-11:34:19, whose grid fall comes at
    11:34:17 - not yet read. Netted into the rise, it closed the boiler's run
    at 26 s and opened a phantom 2 kW start. A meter step the other way is
    netted only when it settled inside the grid step's span."""
    f = _fleet_with_meters({"Boiler": 0.0})
    f.main.meter_steps = f._meter_steps
    grid, boiler = f.main.phases["c"], f.subs["Boiler"].phases["a"]
    boiler.interval, grid.interval, grid.noise = 7.6, 2.0, 30.0
    _declare(grid, (T0, 223.0, None, T0 - 2.0, T0 + 10.0))                   # the rise, settled at T0 + 10
    _declare(boiler, (T0 + 51.0, -1929.0, None, T0 + 10.0, T0 + 51.0, T0 + 51.0))   # the stop, settled at T0 + 51
    assert f.main.metered_parts("c", T0, 223.0) == [223.0]                   # not netted: it settled after the rise did
    _declare(boiler, (T0 + 5.0, -600.0, None, T0 - 1.0, T0 + 5.0, T0 + 5.0))      # one that settled inside the rise's span
    parts = f.main.metered_parts("c", T0, 223.0)
    assert len(parts) == 2 and abs(parts[0] + 600.0) < 1.0, parts


def test_a_meters_stop_ends_the_run_its_own_rise_started():
    """Kozolec 09-22 11:34: the boiler's -1929 W stop was handed the IR
    panel's 514 W run (started two hours before, 41 s before a boiler rise)
    once the boiler's own run was gone, and the run was booked at 1,270 W.
    A meter's stop ends a run whose start is the meter's rise, in size too."""
    f = _fleet_with_meters({"Boiler": 0.0})
    f.single = {"Boiler": True}
    boiler, grid = f.subs["Boiler"].phases["a"], f.main.phases["c"]
    boiler.interval, grid.noise = 7.6, 30.0
    ir = D._Open(since=T0 - 7200.0, watts=514.0, var=None, levels=[(T0 - 7200.0, 514.0)])
    pulse = D._Open(since=T0, watts=1805.0, var=None, levels=[(T0, 1805.0)])
    _declare(boiler, (T0 - 7159.0, 1900.0, None, T0 - 7182.0, T0 - 7159.0, T0 - 7150.0),   # a rise 41 s after the IR's start, its span reaching back to it
             (T0 + 4.0, 1929.0, None, T0 - 10.0, T0 + 4.0, T0 + 10.0),                     # this pulse's rise
             (T0 + 78.0, -1929.0, None, T0 + 37.0, T0 + 78.0, T0 + 80.0))                   # and its stop
    assert f._meter_stop("c", T0 + 70.0, T0 + 76.0, [ir, pulse]) is pulse
    f._stops_used.clear()
    assert f._meter_stop("c", T0 + 70.0, T0 + 76.0, [ir]) is None            # the IR's 514 W is not the boiler's 1.9 kW rise


def test_a_run_closed_by_a_fall_far_from_its_size_is_booked_at_the_smaller():
    """One level throughout, the start and the stop are averaged - where they
    agree, or the stop agrees with what the run was followed to (a fridge
    sagging 63 -> 47 W, 55 W all along). A 514 W run a 2,027 W fall closed
    was booked at 1,270 W for two hours (Kozolec 09-22); far apart, the
    smaller."""
    st = D.PhaseState(noise=20.0, baseline=100.0, level=600.0, interval=2.0)
    o = D._Open(since=T0, watts=514.0, var=None, levels=[(T0, 514.0)])
    assert abs(st._close(o, T0 + 7200.0, 2027.0, None).energy_wh - 514.0 * 2.0) < 1.0
    o = D._Open(since=T0, watts=909.0, var=None, levels=[(T0, 909.0)])
    assert abs(st._close(o, T0 + 60.0, 820.0, None).power_by_phase()[""] - 864.5) < 0.1   # agree: the mean
    o = D._Open(since=T0, watts=63.0, var=None, levels=[(T0, 63.0)], now=47.0)
    assert abs(st._close(o, T0 + 1700.0, 47.0, None).power_by_phase()[""] - 55.0) < 0.1   # sagged to the stop: the mean
    o = D._Open(since=T0, watts=270.0, var=None, levels=[(T0, 270.0)], now=5007.0)
    assert abs(st._close(o, T0 + 4700.0, 5007.0, None).power_by_phase()[""] - 270.0) < 0.1  # grown 18x while "alone": its start


def test_a_meters_run_is_that_meters_until_the_meter_shows_it_stopped():
    """The converse of _meter_stop: a run a meter's own step started is not
    ended by a fall of its size, a held drop, a multi-close or a start of its
    kind while the meter, at the grid's moment, still shows half its size
    over what it read before the run started."""
    f = _fleet_with_meters({"Plug": 0.0})
    f.main.meter_on, f.main.meter_stop = f._meter_on, f._meter_stop
    plug, grid = f.subs["Plug"].phases["a"], f.main.phases["c"]
    plug.interval, grid.baseline, grid.level = 10.0, 100.0, 370.0
    grid.lib, grid.name = f.main, "c"
    _declare(plug, (T0 + 2.0, 270.0, None, T0 - 8.0, T0 + 2.0, T0 + 12.0))
    o = D._Open(since=T0, watts=270.0, var=None, levels=[(T0, 270.0)], meter="Plug")
    grid.open_edges = [o]
    grid.last_ts = T0 + 600.0
    assert grid.owned(o)
    assert grid._pair(T0 + 600.0, 268.0, None, 100.0) == []                   # a fall of its size is not its stop
    assert grid.open_edges == [o]
    _declare(plug, (T0 + 900.0, -265.0, None, T0 + 890.0, T0 + 900.0, T0 + 910.0))
    assert grid.owned(o)                                                       # not yet, at the grid's moment
    grid.last_ts = T0 + 905.0
    assert not grid.owned(o)                                                   # the plug fell: the run is free again
    assert len(grid._pair(T0 + 905.0, 268.0, None, 100.0)) == 1 and not grid.open_edges
    back = D._Open.of(o.as_list())
    assert back.meter == "Plug"


def test_a_lag_is_learned_only_within_the_reach():
    """Home's office plug, a computer: a step of its own 150 s after a grid
    step of the same size read as that step's late report, and its 95th
    report lag came to 109 s, its declaring lag 190 s - Home's horizon. A
    shared step teaches a lag only within EDGE_LAG_REACH_S; a true 20 s lag
    still does."""
    f = _fleet_with_meters({"Plug": 0.0})
    plug, grid = f.subs["Plug"].phases["a"], f.main.phases["c"]
    plug.interval, grid.interval, grid.noise = 10.0, 2.0, 10.0                    # latencies 30 and 6 s
    for k in range(30):
        t = T0 + 700.0 * k
        _declare(grid, (t, 900.0, None, t - 1.0, t))
        _declare(plug, (t + 150.0, 880.0, None, t + 140.0, t + 150.0, t + 160.0))  # a step of its own, 150 s on
    f.wait_cap_s = D.METER_WAIT_CAP_S
    f._learn_lags(T0 + 700.0 * 30 + D.LAG_REACH_S + 1.0)
    assert not f.meter_lag.get("Plug"), f.meter_lag.get("Plug")[:3]
    for k in range(31, 61):                                                            # after the first window
        t = T0 + 700.0 * k
        _declare(grid, (t, 900.0, None, t - 1.0, t))
        _declare(plug, (t + 20.0, 880.0, None, t + 10.0, t + 20.0, t + 30.0))     # a late report, 20 s on
    f._learn_lags(T0 + 700.0 * 61 + D.LAG_REACH_S + 1.0)
    rows = f.meter_lag["Plug"]
    assert len(rows) == 30 and all(r == [20.0, 30.0] for r in rows), rows[:3]


def test_a_run_that_settles_to_its_meters_size_is_that_meters():
    """Susilna 09-24 18:00: the plug's 265 W and a 30-60 W load switching on in
    one reading with a rise on another phase - a leg of a two-phase event,
    too much for the plug to own - and the load gone within the minute:
    stepped down to the plug's size, the run is the plug's (the _pair settle
    path asks again), and lasts its 20 hours."""
    for busy in (False, True):
        filed, fleet, on, off = _plug_fleet(busy, watts=265.0, blip=60.0, leg=500.0)
        run = [s for s in filed if abs(s.start - on) < 60 and "b" in s.phases]
        assert run, ("busy" if busy else "quiet", "no house run at the plug's start")
        run = max(run, key=lambda s: s.duration_s)
        sig = fleet.main.signature_of(run)
        # the plug's: filed under a signature located at it, or (the fold books the
        # blip's step down as the start's decay, so the run sits at the plug's own
        # size) owned by the plug's session
        plugs = bool(sig is not None and sig.locations.get("Plug")) or (run.parent_signature is not None and run.parent_signature[0] == "Plug")
        assert abs(run.end - off) < 120 and abs(run.energy_wh - 265.0 * 20.0) < 300 and plugs, (
            "busy" if busy else "quiet", round(run.duration_s / 3600, 2), round(run.energy_wh), sig.locations if sig else None)


def _charger_day():
    """Kozolec's car charger as its plug read it on 09-20 (local times): on
    14:34:40 at ~2.1 kW; a pause at 16:22:18 read 3, 7, 67 W over 14 s and on
    again at 16:22:38; off 16:23:08 (3, 6, 24 W), on 16:23:27; a pause at
    17:02:33, on 17:02:52; off 17:57:39 - the plug's readings every 6 s
    otherwise, the grid's (400 W besides) every 5 s."""
    rnd = random.Random(5)
    h = lambda hh, mm, ss: T0 + hh * 3600.0 + mm * 60.0 + ss            # noqa: E731
    on, p1, r1, off1, on2, p2, r2, off = (h(14, 34, 40), h(16, 22, 18), h(16, 22, 38), h(16, 23, 8), h(16, 23, 27),
                                          h(17, 2, 33), h(17, 2, 52), h(17, 57, 39))
    pauses = {p1: [(0.0, 2.7), (8.0, 6.7), (14.0, 67.0), (20.0, 2031.7), (25.0, 2197.6)],   # as the plug read them
              off1: [(0.0, 2.7), (7.0, 5.8), (13.0, 24.1), (19.0, 2039.5), (25.0, 2201.0)],
              p2: [(0.0, 2.7), (7.0, 5.8), (13.0, 24.1), (19.0, 2039.5), (25.0, 2201.0)]}

    def drawing(t):
        return on <= t < p1 or r1 <= t < off1 or on2 <= t < p2 or r2 <= t < off

    def draw(t):
        if drawing(t):
            return 2100.0 + rnd.uniform(-20, 20)
        for start, rows in pauses.items():
            if start <= t < start + 20.0:
                return [w for d, w in rows if d <= t - start][-1]
        return 0.0
    plug, grid = [], []
    t = T0 + 13 * 3600.0
    while t < T0 + 19 * 3600.0:
        pause = next((p for p in pauses if p <= t < p + 30.0), None)
        if pause is not None:
            plug.extend((pause + d, w) for d, w in pauses[pause])
            t = pause + 30.0
            continue
        plug.append((t, round(draw(t), 1)))
        t += 6.0
    t = T0 + 13 * 3600.0
    while t < T0 + 19 * 3600.0:
        grid.append((t, round(400.0 + rnd.uniform(-5, 5) + draw(t), 1)))
        t += 5.0
    return {"a": grid}, plug, (on, p1, r1, off1, on2, p2, r2, off)


def test_a_pause_the_plug_read_plainly_ends_its_run_and_the_restart_is_a_new_one():
    """Kozolec 09-20: two 20-s pauses of the car charger that neither the plug
    nor the grid declared - three readings below 70 W, the fourth back at
    2 kW - so the 14:34 run ran on to 17:57 and closed, with the 16:23 run,
    on the one 2.2 kW fall there (the owner: one session end was missed). A
    plateau the next reading leaves is a level once it held for the latency,
    and a total-only plug's declared fall ends its run though the map does
    not place it: four runs, each the plug's. The charger's runs are those of
    a kilowatt or more: the plug's own detector also opens 12-34 W runs on the
    charger's +-20 W wander before that plateau's relative noise is learnt, and
    since a fall to the idle floor closes what was open (d707840) one left open
    is booked to the 17:57 off, 33-66 Wh - with that commit alone on 9 of 12
    draws of this fixture's wander, the test's own draw passing by chance."""
    rows, plug, (on, p1, r1, off1, on2, p2, r2, off) = _charger_day()
    fleet = D.Fleet()
    fleet.wait_cap_s = D.METER_WAIT_CAP_S
    fleet.meter_lag["Plug"] = [[2.0, 40.0]] * D.LAG_MIN_SAMPLES
    filed, file = [], fleet.main._file

    def keep(s, *a, **kw):
        file(s, *a, **kw)
        filed.append(s)
    fleet.main._file = keep
    end = T0 + 19 * 3600.0
    cuts = [T0 + 15 * 3600.0, T0 + 17 * 3600.0]
    for (part, e), (sub, _) in zip(_passes(rows, cuts, end), _passes({"a": plug}, cuts, end)):
        fleet.process(part, {"Plug": sub}, now_ts=e, single={"Plug": True})
    want = [(on, p1), (r1, off1), (on2, p2), (r2, off)]                                 # the plug's own four runs
    own = sorted((s["start"], s["end"]) for s in fleet.subs["Plug"].recent if s["max_w"] >= 1000.0)
    assert len(own) == 4 and all(abs(a - wa) <= 10 and abs(b - wb) <= 10 for (a, b), (wa, wb) in zip(own, want)), own
    big = sorted((s for s in filed if s.energy_wh > 10.0), key=lambda s: s.start)
    got = [(s.start, s.start + s.duration_s, fleet.main.signature_of(s).locations.get("Plug", 0) if fleet.main.signature_of(s) else 0) for s in big]
    assert len(got) == 4 and all(abs(a - wa) <= 15 and abs(b - wb) <= 15 for (a, b, _), (wa, wb) in zip(got, want)), got
    assert all(g[2] for g in got), got                                                # each at the plug


def _boiler_gap_day():
    """Kozolec 09-20 10:23 UTC in outline: a boiler (1921 W on its own Shelly,
    which reports on change - a 35 s silence while it ran, then one 1885 W
    reading, then 0) and an EVSE starting at 3.6 kW 39 s after the boiler, 21 s
    before the boiler stops; the grid every 5 s. (grid rows, boiler rows,
    boiler on, EVSE on, boiler off, EVSE off)."""
    rnd = random.Random(7)
    h = lambda s: T0 + 10 * 3600.0 + s                      # noqa: E731
    b_on, e_on, b_off, e_off = h(0.0), h(39.0), h(60.0), h(39.0 + 2 * 3600.0)
    boiler = []
    t = h(-1800.0)
    while t < b_on:
        boiler.append((t, 0.0))
        t += 7.0
    boiler += [(b_on + 0.5 + d, 1921.0 + rnd.uniform(-3, 3)) for d in (0.0, 7.0, 14.0, 20.0)]
    boiler += [(b_on + 55.5, 1885.0), (b_on + 61.5, 0.0)]  # the droop, then off
    t = b_on + 68.5
    while t < h(3 * 3600.0):
        boiler.append((t, 0.0))
        t += 7.0
    grid = []
    t = h(-1800.0) + 1.0
    while t < h(3 * 3600.0):
        w = 114.0 + rnd.uniform(-4, 4) + (1826.0 if b_on <= t < b_off else 0.0) + (3603.0 if e_on <= t < e_off else 0.0)
        grid.append((t, round(w, 1)))
        t += 5.0
    return {"a": grid}, boiler, b_on, e_on, b_off, e_off


def test_a_meter_stop_read_after_a_silence_is_not_netted_into_a_start_inside_the_silence():
    """Kozolec 09-20 10:23 UTC: the boiler's Shelly reported 1921 W, nothing for
    35 s, 1885 W, then 0. Spanned from the 1921 reading its stop reached 41 s
    back, over the EVSE's 3.6 kW start 18 s before it, and was netted into the
    start - the grid's own fall for the stop was 11 s from being declared when
    the start's event formed. The start grew to 5.5 kW, the fall closed it at
    21 s, and 10.4 kWh of charging was never a session. A step's span begins
    at the last reading still on its old side - 1885 W is the boiler on - so
    the stop lies after the start: the boiler's minute and the EVSE's two
    hours are two sessions. (Here, without it, the netting ends the boiler's
    run at the EVSE's start, 40 s not 60, and opens the start at 5.5 kW; the
    grid's fall then settles it rather than closing it as on the day.)"""
    rows, boiler, b_on, e_on, b_off, e_off = _boiler_gap_day()
    fleet = D.Fleet()
    fleet.wait_cap_s = D.METER_WAIT_CAP_S
    fleet.meter_lag["Boiler"] = [[2.0, 25.0]] * D.LAG_MIN_SAMPLES
    fleet.phase_votes["Boiler"] = {"a": {"a": D.PHASE_MAP_MIN_VOTES}}     # placed on phase a, as after a day
    filed, file = [], fleet.main._file

    def keep(s, *a, **kw):
        file(s, *a, **kw)
        filed.append(s)
    fleet.main._file = keep
    end = T0 + 13 * 3600.0
    cuts = [T0 + 11 * 3600.0]
    for (part, e), (sub, _) in zip(_passes(rows, cuts, end), _passes({"a": boiler}, cuts, end)):
        fleet.process(part, {"Boiler": sub}, now_ts=e, single={"Boiler": True})
    big = sorted((s for s in filed if s.energy_wh > 10.0), key=lambda s: s.start)
    got = [(round(s.start - b_on), round(s.duration_s), round(s.energy_wh / (s.duration_s / 3600.0))) for s in big]
    evse = [g for g in got if abs(g[0] - 39) <= 10]
    assert len(evse) == 1 and abs(evse[0][1] - 7200) <= 60 and abs(evse[0][2] - 3603) <= 200, got
    assert any(abs(g[0]) <= 10 and abs(g[1] - 60) <= 15 and abs(g[2] - 1826) <= 200 for g in got), got


def _blip_before_stop_day():
    """Kozolec 09-28 13:53 UTC in outline: an EVSE at 3588 W on its plug for
    23 minutes; a 263 W load of nobody's on for 24 s, off 20 s before the EVSE
    stops; the plug (every 10 s) silent for 40 s around the EVSE's stop, so
    its fall's span reaches back over the small fall's. (grid rows, plug rows,
    EVSE on, blip on, blip off, EVSE off)."""
    rnd = random.Random(11)
    h = lambda s: T0 + 10 * 3600.0 + s                      # noqa: E731
    e_on, e_off = h(0.0), h(23 * 60.0)
    b_on, b_off = e_off - 44.0, e_off - 20.0
    plug, grid = [], []
    t = h(-1800.0) + 4.0
    while t < h(3600.0):
        if not (e_off - 40.0 <= t < e_off + 2.0):
            plug.append((t, round((3588.0 + rnd.uniform(-4, 4)) if e_on <= t < e_off else 0.0, 1)))
        t += 10.0
    t = h(-1800.0)
    while t < h(3600.0):
        w = 100.0 + rnd.uniform(-3, 3) + (3588.0 if e_on <= t < e_off else 0.0) + (263.0 if b_on <= t < b_off else 0.0)
        grid.append((t, round(w, 1)))
        t += 5.0
    return {"a": grid}, plug, e_on, b_on, b_off, e_off


def test_a_meters_stop_ends_its_run_only_through_a_fall_that_accounts_for_it():
    """Kozolec 09-28 13:53 UTC: a 263 W blip ended a second before the EVSE's
    3.6 kW fall; the EVSE plug's own fall lay within reach of the small fall's
    span, _meter_stop handed the small fall the EVSE's run - a meter's stop
    ends the run it started whatever the sizes - and the run was booked at 257
    W: 23 minutes, 1.4 kWh, as 98 Wh. One fall ends only runs whose sizes it
    accounts for: the grid's fall must be at least half the meter's. The
    EVSE's 23 minutes at 3.6 kW stay its own; the blip is a run of its own."""
    rows, plug, e_on, b_on, b_off, e_off = _blip_before_stop_day()
    fleet = D.Fleet()
    fleet.wait_cap_s = D.METER_WAIT_CAP_S
    fleet.meter_lag["Plug"] = [[2.0, 60.0]] * D.LAG_MIN_SAMPLES     # the horizon reaches past the plug's latency
    fleet.phase_votes["Plug"] = {"a": {"a": D.PHASE_MAP_MIN_VOTES}}  # placed on phase a, as after a day
    filed, file = [], fleet.main._file

    def keep(s, *a, **kw):
        file(s, *a, **kw)
        filed.append(s)
    fleet.main._file = keep
    end = T0 + 11 * 3600.0
    for (part, e), (sub, _) in zip(_passes(rows, [], end), _passes({"a": plug}, [], end)):
        fleet.process(part, {"Plug": sub}, now_ts=e, single={"Plug": True})
    got = sorted(((round(s.start - e_on), round(s.duration_s), round(s.energy_wh)) for s in filed if s.duration_s > 5), key=lambda g: g[0])
    evse = [g for g in got if abs(g[0]) <= 10 and abs(g[1] - 23 * 60) <= 30]
    assert len(evse) == 1 and abs(evse[0][2] - 3588.0 * evse[0][1] / 3600.0) <= 100, got      # 23 min at 3.6 kW, not at 263 W
    assert any(abs(g[0] - (b_on - e_on)) <= 10 and 15 <= g[1] <= 35 for g in got), got          # the blip, its own run


def _crowded_phase():
    """Home 09-25 in outline: a plug's dehumidifier (271 W, 20 hours from
    18:00) and, two minutes apart, MAX_OPEN_EDGES more loads of distinct
    sizes switching on on the same phase and staying on - the thirteenth
    makes the phase one run over the cap. (grid rows, plug rows, on, off,
    the small loads' (start, watts), end)."""
    rnd = random.Random(3)
    on, off = T0 + 18 * 3600.0, T0 + 38 * 3600.0
    sizes = [40.0, 52.0, 68.0, 88.0, 115.0, 150.0, 195.0, 340.0, 440.0, 570.0, 740.0, 960.0]
    small = [(on + 120.0 * (k + 1), sizes[k % len(sizes)] * (1.0 + k // len(sizes))) for k in range(D.MAX_OPEN_EDGES)]
    plug, grid = [], []
    t = T0 + 17 * 3600.0 + 2.0
    while t < T0 + 40 * 3600.0:
        plug.append((t, round((271.0 + rnd.uniform(-3, 3)) if on <= t < off else 0.0, 1)))
        t += 6.0
    t = T0 + 17 * 3600.0
    while t < T0 + 40 * 3600.0:
        w = 100.0 + rnd.uniform(-3, 3) + (271.0 if on <= t < off else 0.0) + sum(x for s, x in small if t >= s)
        grid.append((t, round(w, 1)))
        t += 5.0
    return {"a": grid}, plug, on, off, small, T0 + 40 * 3600.0


def _crowded_fleet():
    rows, plug, on, off, small, end = _crowded_phase()
    fleet = D.Fleet()
    fleet.wait_cap_s = D.METER_WAIT_CAP_S
    fleet.meter_lag["Plug"] = [[2.0, 30.0]] * D.LAG_MIN_SAMPLES
    filed, file = [], fleet.main._file

    def keep(s, *a, **kw):
        file(s, *a, **kw)
        filed.append(s)
    fleet.main._file = keep
    cuts = [T0 + 6 * 3600.0 * k for k in range(4, 7)]
    for (part, e), (sub, _) in zip(_passes(rows, cuts, end), _passes({"a": plug}, cuts, end)):
        fleet.process(part, {"Plug": sub}, now_ts=e, single={"Plug": True})
    return filed, fleet, on, off, small


def test_a_thirteenth_run_on_a_phase_drops_the_oldest_unowned_not_the_meters():
    """Home 09-25 21:40: the dehumidifier's 271 W run (Susilna's plug, 20
    hours, 5.4 kWh) was the oldest of twelve open runs on phase a when a +851
    W rise opened the thirteenth, and MAX_OPEN_EDGES popped it silently - no
    session at all, though the plug read it on all night. The oldest run no
    meter holds on goes instead: the plug's 20 hours are a session, the plug's."""
    filed, fleet, on, off, small = _crowded_fleet()
    runs = [s for s in filed if abs(s.start - on) < 60 and s.duration_s > 3600.0]
    assert len(runs) == 1 and abs(runs[0].end - off) < 120 and abs(runs[0].energy_wh - 271.0 * 20.0) < 300, [
        (round(s.start - on), round(s.duration_s / 3600, 2), round(s.energy_wh)) for s in filed if s.duration_s > 600]
    sig = fleet.main.signature_of(runs[0])
    assert sig is not None and sig.locations.get("Plug"), sig.locations if sig else None


def test_a_run_the_cap_drops_is_a_session_at_its_last_level():
    """The run the cap drops - the first small load, on since two minutes
    after the plug - is closed where it stood, at its last level, when the
    thirteenth opens: a session from its start to that moment, not nothing."""
    filed, fleet, on, off, small = _crowded_fleet()
    first_on, first_w = small[0]
    at = small[-1][0]                                        # the thirteenth run's start
    got = [s for s in filed if abs(s.start - first_on) < 15 and abs(s.end - at) < 15]
    assert len(got) == 1 and abs(got[0].energy_wh / (got[0].duration_s / 3600.0) - first_w) < 0.2 * first_w, [
        (round(s.start - first_on), round(s.end - at), round(s.energy_wh)) for s in filed if abs(s.start - first_on) < 600]


def test_a_meter_owns_a_start_only_where_its_own_rise_is_half_of_it():
    """Home 09-24 00:54 UTC, step for step: the grid rose +2995 (a 3EM's kiln
    pulse) at T0-31, +942 at T0 (the hidrofor's pump), and the kiln's -2995
    stop was netted into the pump's rise as a split step at T0, so the rise
    to place was 3937 W. The hidrofor's plug declared +942 at T0 with a span
    reaching 30 s back (silent at 0 W, its latency): over the union of both
    meters' spans the grid's net is the plug's, "all of it" - and the plug
    owned a 3937 W run its 942 W fall, less than half, could never release:
    15 hours, 60 kWh. A meter owns a start only where its own rise is at least
    half of it; the 942 W rise alone is the plug's as before."""
    f = _fleet_with_meters({"Hidrofor": 0.0})
    plug, grid = f.subs["Hidrofor"].phases["a"], f.main.phases["c"]
    plug.interval, plug.lag = 10.0, 30.0
    _declare(plug, (T0, 942.0, None, T0 - 30.0, T0 + 1.0))
    _declare(grid, (T0 - 31.0, 2995.0, None, T0 - 33.0, T0 - 26.0), (T0, 942.0, None, T0 - 3.0, T0 + 2.0),
             (T0, -2995.0, None, T0 - 3.0, T0 + 2.0))
    assert f._meter_totals("c", T0, 6.0, True)["Hidrofor"][0] is None          # "all of it", as on the day
    assert f._step_meter("c", T0, 3937.0, True) is None                       # ...but not of a start four times its rise
    assert f._step_meter("c", T0, 942.0, True) == "Hidrofor"                  # the rise alone is its


def test_a_meters_step_the_grid_took_as_its_own_is_no_share_of_the_next():
    """Kozolec 3 Oct 12:21:52: the boiler rose (+1,857 on the grid, +1,888 on
    its meter at :56), and a car charge 16 s later (+2,189 at 12:22:08) spanned
    back over the boiler meter's step: carved out of the charge again, it left
    a phantom boiler run and the charge at 377 W (2026-10-29). A meter's step
    the grid took as a step of its own, alike in size, is that step's; a bigger
    grid step beside it is not that (the kiln's beside a pump's rise)."""
    f = _fleet_with_meters({"Boiler": 0.0})
    boiler, grid = f.subs["Boiler"].phases["a"], f.main.phases["c"]
    boiler.interval, grid.interval = 5.0, 5.0
    _declare(boiler, (T0 + 4.0, 1888.0, None, T0 - 1.0, T0 + 4.0))
    _declare(grid, (T0, 1857.0, None, T0 - 5.0, T0), (T0 + 16.0, 2189.0, None, T0 + 3.0, T0 + 16.0))
    assert f._meter_totals("c", T0, 6.0, True)["Boiler"][0] != 0.0                 # its own step's
    assert f._meter_totals("c", T0 + 16.0, 6.0, True)["Boiler"][0] == 0.0          # not the charge's


def test_a_meter_stop_the_grid_has_not_read_to_is_not_netted_into_a_rise_before_it():
    """Home 09-24 00:54 UTC: the grid's +942 rise at 00:54:19 (span :17-:22);
    the 3EM's -2996 kiln stop at 00:54:31, its span a 14 s silence back to
    00:54:17; read ahead of the grid by the horizon, it was there when the
    rise's event formed, while the grid had read to 00:54:25 - and was netted
    into the rise, which grew to 3980 W, while the grid's own -3000 fall was
    six seconds from being read. A meter step the other way is netted only
    once the grid has read to the end of its span; a stop the grid has read
    past and shown no step of its own for is netted as before."""
    f = _fleet_with_meters({"Hiša": 0.0})
    f.main.meter_steps = f._meter_steps
    grid, hisa = f.main.phases["c"], f.subs["Hiša"].phases["a"]
    hisa.interval, grid.interval = 5.0, 5.0
    _declare(grid, (T0, 942.0, None, T0 - 2.0, T0 + 3.0))
    _declare(hisa, (T0 + 12.0, -2996.0, None, T0 - 2.0, T0 + 12.0))
    grid.last_ts = T0 + 6.0
    assert f.main.metered_parts("c", T0, 942.0) == [942.0]                   # the grid has not read to the stop
    grid.last_ts = T0 + 12.0
    parts = f.main.metered_parts("c", T0, 942.0)
    assert len(parts) == 2 and abs(parts[0] + 2996.0) < 1.0, parts           # read to it, no step of its own: netted


def test_a_reading_dropped_long_ago_leaves_the_noise_and_the_sessions_as_they_were():
    """The noise is the median of the last NOISE_WINDOW idle moves at every
    reading (_slide), the relative noise the median of its last NOISE_WINDOW
    worked out once every REL_REFRESH_S of the readings' clock. Re-measured in
    blocks - every 120 moves over the last 240, counted from the first - one
    reading dropped moved every later block's edges for good: Home circuits,
    one reading in 10,000 dropped, ran at another noise for 38 % of phase A's
    steps and another relative noise for 87 % from the hour of the first drop
    (2026-10-04). Here a 30 W load cycling at the noise beside a 1.5 kW one:
    in blocks the noise ended at 29.5 and 34.4 W with and without the 51st
    reading, and the last five hours had a session less."""
    big, small = kiln(period=900.0, on=120.0, watts=1500.0), kiln(period=300.0, on=100.0, watts=30.0)
    full = series(6 * 3600, lambda s: big(s) + small(s), seed=3)
    runs = []
    for rows in (full, full[:50] + full[51:]):          # the 51st reading, four minutes in, gone
        det = D.Detector()
        closed = det.process({"a": rows}, now_ts=rows[-1][0] + 60.0)
        st = det.phases["a"]
        runs.append((st.noise, st.noise_rel, [(s.start, s.end, s.energy_wh) for s in closed
                                              if s.start > T0 + 3600.0]))
    assert runs[0][:2] == runs[1][:2], (runs[0][:2], runs[1][:2])
    # a young run's level follows its readings (SETTLED_FOLLOW), so a dropped
    # reading moves its booked energy by a few thousandths of a Wh, not its edges
    assert len(runs[0][2]) == len(runs[1][2]) and all(
        (a[0], a[1]) == (b[0], b[1]) and abs(a[2] - b[2]) < 0.02 for a, b in zip(runs[0][2], runs[1][2])), (len(runs[0][2]), len(runs[1][2]))


def test_three_steps_are_one_group():
    """Steps stay together until the counts show two devices (run lifecycle,
    2026-10-28): three steps of about 100 W are one group."""
    det = D.Detector()
    det.phases["a"].noise = 20.0
    ids = {det._classify_step("a", T0 + 60.0 * k, w, None, 0.0).id for k, w in enumerate((100.0, 104.0, 98.0))}
    assert len(ids) == 1, ids


def test_a_group_never_spans_more_than_group_span():
    segs = D._significant([1.0] * 200 + [0.95] + [1.0] * 200, 10, [(10, 210), (211, 410)], 20.0)
    size = lambda b: 20.0 * math.sinh(b * D.EDGE_BIN * D.EDGE_SCALE_REL) / D.EDGE_SCALE_REL   # noqa: E731
    assert size(411) > D.GROUP_SPAN * size(10) and segs == [(10, 210), (211, 410)], segs    # cut for the span
    joined = D._significant([1.0] * 4 + [0.0] + [0.0] * 4, 200, [(200, 204), (205, 208)], 20.0)
    assert joined == [(200, 208)], joined                                   # one side empty: joined


def test_a_step_is_clustered_against_the_size_histogram_as_it_stands():
    """A group's step sizes are cut at their valleys again at every step,
    the step in them (EDGE_RECUT). Cut every 32 steps, the cuts were counted
    from the group's first step, so one step dropped or added moved every
    later cut, and a step near a valley went to another cluster for the rest
    of the replay: Home hidden, five cards apart only by a reading in 10,000
    dropped or 1 ms of jitter, read 13.2-25.2 % impurity over its devices
    (2026-10-04)."""
    rnd = random.Random(3)
    det = D.Detector()
    det.phases["a"].noise = 20.0
    for k in range(300):
        size = max(30.0, rnd.gauss(*rnd.choice([(300.0, 20.0), (420.0, 25.0), (900.0, 40.0)])))
        det._classify_step("a", T0 + 97.0 * k, size, None, 0.0)
        assert det._segs["a|1"] == D.valley_segments(det.edge_hist["a|1"], unit=det.edge_unit["a|1"]), k


def test_the_kept_density_is_the_full_pass_to_the_bit():
    """_valleys keeps a group's size density between steps and sums again only
    the slots the step can move, in the histogram's order: every slot, and so
    every cut, is what a full pass over the histogram makes - through new bins
    past either end, bins stepped again and a histogram put in its place."""
    rnd = random.Random(5)
    det = D.Detector()
    reach, kernel = D._kernel(D.EDGE_KERNEL / D.EDGE_BIN)
    h = {}
    for k in range(2000):
        if k % 500 == 499:
            h = {b: w * 0.9 for b, w in h.items()}                     # re-binned: another dict
        b = rnd.randint(-40, 400) if rnd.random() < 0.2 or not h else rnd.choice(list(h))
        added = b not in h
        h[b] = h.get(b, 0.0) + rnd.choice([1.0, 0.37])
        assert det._valleys("a|1", h, b, added) == D.valley_segments(h, unit=D.MIN_NOISE_W), k
        lo = min(h) - reach
        full = [0.0] * (max(h) + reach - lo + 1)
        for bb, w in h.items():
            for j, kw in enumerate(kernel):
                full[bb - lo - reach + j] += w * kw
        assert det._dens["a|1"][2:4] == [lo, full], k


def test_a_step_dropped_moves_few_later_steps_to_another_cluster():
    """...and so a step gone missing moves only the steps whose group it
    decides. The size density's groups moved at most 17 of the 1,459 steps
    after it over twenty draws (2026-10-04); the mixture's (2026-10-29), its
    daily joins not run here, moves none in most draws but founds a group a
    step earlier or later now and then: 0-22 in 18 of 20, 54 and 212 in two
    (dev/TODO.md, INS-32)."""
    def clusters(steps):
        det = D.Detector()
        det.phases["a"].noise = 20.0
        born = {}
        return {t: born.setdefault(det._classify_step("a", t, w, None, 0.0).id, t) for t, w in steps}
    moved = []
    for seed in range(20):
        rnd = random.Random(seed)
        sizes = [(300.0, 20.0), (420.0, 25.0), (900.0, 40.0), (1200.0, 50.0)]
        steps = [(T0 + 97.0 * k, max(30.0, rnd.gauss(*rnd.choice(sizes)))) for k in range(1500)]
        a, b = clusters(steps), clusters(steps[:40] + steps[41:])
        moved.append(sum(1 for t, _ in steps[41:] if a[t] != b[t]))
    assert max(moved) <= 0.15 * 1459 and sum(m > 30 for m in moved) <= 2, moved


def test_the_days_repair_gives_back_what_the_phase_never_drew():
    """The day's repair (exp15): a 1 kW run whose stop went unseen is booked
    for an hour while the phase carried it half of it; a 500 W run a meter
    measured beside it is left whole. The excess comes off the unmeasured
    run and off its signature's hours, and what the phase carried stays."""
    det = D.Detector()
    det.phases["a"].noise = 10.0
    loose, held = _sig(1, 1000.0), _sig(2, 500.0)
    det.signatures = [loose, held]
    hour = int(T0 // 3600 * 3600) + 3600
    a = D.Run(phases="a", start=hour, end=hour + 3600, levels={"a": [(hour, 1000.0)]}, signature_id=1)
    b = D.Run(phases="a", start=hour, end=hour + 3600, levels={"a": [(hour, 500.0)]}, signature_id=2,
                  wh={"a": 500.0})
    loose.hourly, held.hourly = {hour: 1000.0}, {hour: 500.0}
    loose.hour_wh[int(hour % 86400 // 3600)] = 1000.0
    day = hour - 7200.0
    rows = [(day, 200.0)] + [(hour + k * 10.0, 200.0 + 500.0 + (1000.0 if k < 180 else 0.0)) for k in range(360)]
    rows += [(hour + 3600.0 + k * 60.0, 200.0) for k in range(1200)]       # the rest of the day at the floor
    D._cap_day(det, [a, b], {"a": rows}, day, day + 86400.0)
    assert abs(a.energy_wh - 510.0) < 1.0, a.energy_wh          # half an hour of it, and twice the noise for 30 min
    assert b.energy_wh == 500.0
    assert abs(loose.hourly[hour] - 510.0) < 1.0 and held.hourly[hour] == 500.0, (loose.hourly, held.hourly)
    assert abs(loose.hour_wh[int(hour % 86400 // 3600)] - 510.0) < 1.0
    # ...and of two unmeasured runs the one open longer gives first: it is
    # the likelier to have stopped unseen
    old = D.Run(phases="a", start=hour - 600, end=hour + 3600, levels={"a": [(hour - 600, 1000.0)]})
    new = D.Run(phases="a", start=hour, end=hour + 3600, levels={"a": [(hour, 1000.0)]})
    rows = [(day, 200.0), (hour - 600, 1200.0), (hour, 2200.0), (hour + 1800, 1200.0), (hour + 3600, 200.0)]
    D._cap_day(det, [old, new], {"a": rows}, day, day + 86400.0)
    assert abs(new.energy_wh - 1000.0) < 1.0 and abs(old.energy_wh - 1000.0 / 6 - 510.0) < 1.0, (old.energy_wh, new.energy_wh)


def test_the_days_repair_runs_once_a_day_on_the_readings_clock():
    """The repair waits for the day's end on the readings' clock, whatever
    the passes; each detector then lets the day's runs and readings go, so
    it holds one day at most - read in one call or by the hour, the same."""
    def run(step):
        rows = series(3 * 86400.0, kiln(period=5400.0, on=1200.0, watts=800.0), seed=3)
        fleet, filed = D.Fleet(), []
        file = fleet.main._file

        def keep(s, *x, **kw):
            file(s, *x, **kw)
            filed.append(s)
        fleet.main._file = keep
        end = rows[-1][0] + 1.0
        for part, e in _passes({"a": rows}, [T0 + k * step for k in range(1, int(3 * 86400 / step))], end):
            fleet.process(part, {}, now_ts=e)
        return fleet, filed
    fleet, filed = run(3 * 86400.0)
    _, sliced = run(3600.0)
    assert _as_filed(filed) == _as_filed(sliced)
    days = int(fleet._repaired_to // 86400) - int(T0 // 86400)
    assert days == 3, days       # T0 is 1,600 s into a day: three days' ends and their wait passed, not a fourth
    assert all(s.start >= fleet._repaired_to for s in fleet.main._day_log)
    assert fleet.main._day_rows["a"][0][1] >= fleet._repaired_to - D.REPAIR_DAY_S      # the day before kept (INS-31)


def test_a_run_across_midnight_is_repaired():
    """INS-31: a run from 23:50 filed at 02:00, after its day's repair, was
    dropped from the day log unrepaired; the next day's repair takes it, with
    the readings from its start."""
    fleet = D.Fleet()
    det = fleet.main
    a, b = T0, T0 + D.REPAIR_DAY_S
    late = D.Run(phases="a", start=a - 600.0, end=a + 7200.0, levels={"a": [(a - 600.0, 900.0)]})
    det._day_log = [late]
    det._day_rows = {"a": (D.array("d", (a - 1200.0 + 10.0 * k for k in range(9000))), D.array("d", [300.0] * 9000))}
    seen = []
    old = D._cap_day
    D._cap_day = lambda d, log, rows, x, y: seen.append(([s.start for s in log], rows["a"][0][0]))
    try:
        fleet._repair(a, b)
    finally:
        D._cap_day = old
    assert seen == [([a - 600.0], a - 610.0)], seen
    assert det._day_log == []


def _repair_history():
    """Three days of a pulser, and each day at 10:00 a 1 kW start that nothing
    steps down: it fades to 600 W over two hours and holds there until 20:00,
    so its run stays open, booked far past what the phase drew (the repair's
    case). Before the run lifecycle it faded to nothing, and a fall of its
    faded size closed it by the size it had sagged to; that fall now matches
    no run (2026-10-28), and the run closed at its faded level was booked
    under what it drew, which the repair leaves alone."""
    def fn(s):
        day = s % 86400.0
        w = 800.0 if (s % 5400.0) < 1200.0 else 0.0
        if 36000.0 <= day < 36600.0:
            w += 1000.0
        elif 36600.0 <= day < 36600.0 + 7200.0:
            w += 1000.0 - 400.0 * (day - 36600.0) / 7200.0
        elif 43800.0 <= day < 72000.0:
            w += 600.0
        return w
    return series(3 * 86400.0, fn, seed=3)


def _repair_run(step, restart=False, off=False):
    old, trim = D.REPAIR_SLACK_NOISE, D.TRIM
    D.REPAIR_SLACK_NOISE = 1e9 if off else old        # 1e9: nothing is taken, the repair is off
    D.TRIM = False                                    # the store's rounding is its own cost; this asks for lost state
    try:
        rows = _repair_history()
        fleet = D.Fleet()
        filed = _keep_filed(fleet)
        end = rows[-1][0] + 1.0
        for part, e in _passes({"a": rows}, [T0 + k * step for k in range(1, int(3 * 86400 / step))], end):
            fleet.process(part, {}, now_ts=e)
            if restart:
                fleet = D.Fleet.from_dict(json.loads(json.dumps(fleet.to_dict())))
                fleet.load_older(json.loads(json.dumps(fleet.older_to_dict())))
                so_far = list(filed)
                filed = _keep_filed(fleet)
                # the day's runs are copies now, and the repair edits the copies
                # (two runs of one span follow in the order they were filed)
                key, where, copies = (lambda x: (x.phases, x.start, x.end)), {}, {}
                for i, x in enumerate(so_far):
                    where.setdefault(key(x), []).append(i)
                for x in fleet.main._day_log:
                    copies.setdefault(key(x), []).append(x)
                for k, cs in copies.items():
                    for c, i in zip(reversed(cs), reversed(where.get(k, []))):
                        so_far[i] = c
                filed[:0] = so_far
        return fleet, filed
    finally:
        D.REPAIR_SLACK_NOISE, D.TRIM = old, trim


def test_the_days_repair_is_the_same_at_every_slicing():
    """The repair bites here - the fading run is booked far past what the
    phase drew, and the day's repair takes it back - and it takes the same
    off at one call, hourly passes and ten-minute passes: the same runs, the
    same Wh, the same hours in every signature, and never a run above what it
    was booked at before (the 2026-10-12 re-bench's test: the first one
    fixed nothing, its kiln scenario booked the same with the repair off)."""
    off = _repair_run(3 * 86400.0, off=True)[1]
    whole_fleet, whole = _repair_run(3 * 86400.0)
    assert sum(s.energy_wh for s in whole) < sum(s.energy_wh for s in off) - 1000.0, \
        (sum(s.energy_wh for s in whole), sum(s.energy_wh for s in off))
    assert len(whole) == len(off) and all(a.energy_wh <= b.energy_wh + 1e-9 for a, b in zip(whole, off))
    hours = lambda f: sorted((x.id, sorted((h, round(v, 6)) for h, v in x.hourly.items())) for x in f.main.signatures)  # noqa: E731
    for step in (3600.0, 600.0):
        fleet, got = _repair_run(step)
        assert _as_filed(got) == _as_filed(whole), (step, [x for x in _as_filed(got) if x not in _as_filed(whole)][:3])
        assert hours(fleet) == hours(whole_fleet), step


def test_the_days_repair_after_a_restart():
    """A site restarted after every pass repairs each day as one that ran
    straight through: the day's runs and readings are stored with the library
    (before, the day in progress was repaired from the restart only, and a
    restart late in a busy evening could read a running load's level as the
    floor). The general rule is test_every_field_is_persisted_or_derived."""
    whole_fleet, whole = _repair_run(3600.0)
    off = _repair_run(3600.0, off=True)[1]
    assert sum(s.energy_wh for s in whole) < sum(s.energy_wh for s in off) - 1000.0
    fleet, got = _repair_run(3600.0, restart=True)
    assert _as_filed(got) == _as_filed(whole), [x for x in _as_filed(got) if x not in _as_filed(whole)][:3]
    hours = lambda f: sorted((x.id, sorted((h, round(v, 6)) for h, v in x.hourly.items())) for x in f.main.signatures)  # noqa: E731
    assert hours(fleet) == hours(whole_fleet)


def test_settled_start_books_a_young_run_at_the_level_it_settled_at():
    """INS-8 (a): Home's Hidrofor is sized from its first ~3 s (909 W) though
    it holds ~820 W. A 910 W start whose level drifts to ~860 W inside the
    noise is booked at what it settled at, not at the start's size."""
    st = D.PhaseState(min_noise=5.0)
    st.baseline, st.level, st.noise, st.interval = 100.0, 100.0, 70.0, 3.0
    out, t = [], T0
    for w, n in ((100.0, 20), (1010.0, 4), (960.0, 12), (100.0, 20)):
        for _ in range(n):
            out.extend(st.process(t, w))
            t += 3.0
    (run,) = out
    assert 840.0 < run.levels[""][0][1] < 880.0, run.levels


def test_settled_fold_books_a_pumps_start_at_the_level_its_decay_settled_at():
    """INS-9 (2): the grid declares a pump's start decay (971 -> 934 -> 857 W)
    as small falls that join the run as levels. The leading falls inside the
    share are the start, booked at the level they settled at; a fall later than
    SHAPE_SETTLED_S, or too big, stays a level of its own."""
    st = D.PhaseState()
    o = D._Open(since=T0, watts=971.0, var=None,
                levels=[(T0, 971.0), (T0 + 8.0, 934.0), (T0 + 14.0, 857.0), (T0 + 200.0, 700.0), (T0 + 300.0, 300.0)])
    assert st._close(o, T0 + 320.0, 300.0, None).levels[""] == [(T0, 857.0), (T0 + 200.0, 700.0), (T0 + 300.0, 300.0)]




def test_the_ins9_gain_dials_learn_from_falls_and_read_a_median():
    """INS-9 (1), plain behaviour since 2026-10-23. A fall goes through the
    placement hook as a rise does (a stop is grouped under its owner), and what
    the hook leaves on the detector is put back; the gain reads as the median
    of the last GAIN_RING log ratios, so five clean steps of 1.2 among twenty
    of 1.0 do not move it as they would move a running mean - which a library
    stored before the ring still reads."""
    calls = []
    det = D.Detector()
    det.phases["a"] = D.PhaseState()
    det.phases["a"].noise, det.phases["a"].level, det.phases["a"].interval = 25.0, 900.0, 3.0
    det.placement_conf = 0.5
    det.step_meter = lambda ph, since, size, up: calls.append((ph, size, up)) or (setattr(det, "placement_conf", 0.9) or "Pump")
    cluster = det._classify_step("a", T0, -800.0, None, 0.0)
    assert calls == [("a", 800.0, False)]
    assert cluster.owner == "Pump"
    assert det.placement_conf == 0.5
    f = D.Fleet()
    for k in range(25):
        f._learn_gain("Plug", "p", 1.2 if k % 5 == 4 else 1.0, 1.0)
    assert abs(f.gain("Plug") - 1.0) < 1e-9, f.gain("Plug")
    f.meter_gain["Plug"]["p"] = f.meter_gain["Plug"]["p"][:2]          # as stored before the ring
    assert f.gain("Plug") > 1.03, f.gain("Plug")


def test_an_inputs_state_at_the_step_splits_the_group_on_its_own():
    """Steps-and-claims stage 3: 24 rises of 1.9 kW on one phase, 16 while a washer
    reads Running (a fifth of the time) and 8 while Off - the Running ones pile up
    far above chance and get a group keyed by that one state; the Off ones, no
    rarer than chance, stay in the plain group. The key is ONE input's state, not
    the tuple of every input's (which never repeats)."""
    try:
        det = D.Detector()
        rows, spent = [], {"Running": 20000.0, "Off": 80000.0}
        t = 0.0
        for k in range(24):
            state = "Running" if k % 3 else "Off"
            rows.append((t, state))
            t += 600.0
        rows += [(t0 + t, s) for t0, s in rows]              # the same day twice over
        det.inputs = {"sensor.washer": rows, "sensor.other": [(0.0, "Idle")]}
        det.input_time = {"sensor.washer": spent, "sensor.other": {"Idle": 100000.0}}
        for i, (since, _) in enumerate(rows):
            det._classify_step("c", since + 10.0, 1900.0 + (i % 3), None, 0.0)
        ups = [c for c in det.edges if c.phase == "c" and c.up]
        keyed = [c for c in ups if c.keys.get("sensor.washer@") == "Running"]
        plain = [c for c in ups if not any(c.keys.values())]
        # streamed: the Running steps go plain until enough have piled up (PAIR_MIN_RUNS)
        # for the state to be above chance, keyed from then on - the second day's all are
        assert len(keyed) == 1 and keyed[0].count >= 16, [(c.keys, c.count) for c in ups]
        assert len(plain) == 1 and plain[0].count + keyed[0].count == 48, [(c.keys, c.count) for c in ups]
        assert not any(c.keys.get("sensor.washer@") == "Off" for c in ups)     # Off is chance, never a key
        assert "sensor.other@" not in keyed[0].keys
    finally:
        pass


def test_the_daily_rewrite_splits_a_signature_by_length():
    """The owner, 2026-10-23: length decides in the once-a-day rewrite, not live.
    A signature whose run memory holds four 60 s runs and six 600 s ones splits
    at their geometric middle; it keeps the side with more runs (the long ones),
    a 60 s run no longer files into it, and a twin of 60 s runs is not alike.
    Three short runs are too few (SPLIT_MIN_RUNS)."""
    sig = D.Signature(id=1, phases="a", power={"a": 1900.0}, duration_s=400.0, pf=None, count=10, first_seen=0.0, last_seen=0.0)
    sig.runs = [(k * 3600.0, k * 3600.0 + (60.0 if k < 4 else 600.0)) for k in range(10)]
    assert sig.learn_split() and sig.split_side == 1 and 150.0 < sig.split_s < 250.0, (sig.split_s, sig.split_side)
    short = D.Run(phases="a", start=0.0, end=60.0, levels={"a": [(0.0, 1900.0)]})
    long = D.Run(phases="a", start=0.0, end=600.0, levels={"a": [(0.0, 1900.0)]})
    assert not sig.takes_run(short) and sig.takes_run(long)
    twin = D.Signature(id=2, phases="a", power={"a": 1900.0}, duration_s=60.0, pf=None, count=4, first_seen=0.0, last_seen=0.0)
    assert not sig.alike(twin, 25.0) and not twin.alike(sig, 25.0)
    assert not sig.learn_split()                       # the same split again: unchanged
    few = D.Signature(id=3, phases="a", power={"a": 1900.0}, duration_s=400.0, pf=None, count=9, first_seen=0.0, last_seen=0.0)
    few.runs = [(k * 3600.0, k * 3600.0 + (60.0 if k < 3 else 600.0)) for k in range(9)]
    assert not few.learn_split() and few.split_s is None


def test_the_size_cuts_after_one_step_are_the_whole_scans():
    """Only the slots near a new step's bin change, so the cuts are worked out
    from there (_valley_cuts_near); they must be exactly what the whole pass
    over the density gives, however the array grows around the step."""
    import random
    rng = random.Random(7)
    reach, kernel = D._kernel(D.EDGE_KERNEL / D.EDGE_BIN)
    peak = kernel[reach]
    for _ in range(300):
        n, lo = rng.randint(5, 120), rng.randint(-50, 50)
        dens = [0.0] * n
        for _ in range(rng.randint(0, 8)):
            c, w = rng.randrange(n), rng.choice([1.0, 2.0, 5.0, 0.3])
            for k, kw in enumerate(kernel):
                if 0 <= c - reach + k < n:
                    dens[c - reach + k] += w * kw
        for _ in range(20):
            prev = D._valley_cuts(dens, lo, peak)
            c = rng.randrange(-reach, len(dens) + reach)
            if c - reach < 0:
                dens[:0] = [0.0] * (reach - c)
                lo -= reach - c
                c = reach
            if c + reach > len(dens) - 1:
                dens.extend([0.0] * (c + reach - (len(dens) - 1)))
            w = rng.choice([1.0, 1.0, 0.5])
            for k, kw in enumerate(kernel):
                dens[c - reach + k] += w * kw
            assert D._valley_cuts_near(dens, lo, peak, prev, lo + c - reach, lo + c + reach) == D._valley_cuts(dens, lo, peak)


def test_the_exact_sum_is_the_same_whatever_order_it_was_added_in():
    import math
    import random
    rng = random.Random(1)
    for _ in range(300):
        xs = [rng.choice([1.0, rng.random(), rng.random() * 1e-3, rng.random() * 1e3]) for _ in range(rng.randint(1, 60))]
        parts = []
        for x in xs:
            D._exact_add(parts, x)
        ys = xs[:]
        rng.shuffle(ys)
        assert math.fsum(parts) == math.fsum(xs) == math.fsum(ys)


def test_grid_phases_read_as_system_phases_with_the_meters_label():
    """Stage 1 of steps-and-claims: the grid's phases are X, Y, Z for a person."""
    assert [D.display_phase(p) for p in D.PHASES] == ["X (L1)", "Y (L2)", "Z (L3)"]


def _fridge_day(hole, bump=None):
    """A day of readings: 300 W idle, a 50 W fridge for 20 min every 55 min, none in ``hole`` (hours), and an
    optional (start s, length s, watts) bump of something else."""
    rnd = random.Random(3)
    rows, t = [], T0
    while t < T0 + 86400.0:
        w = 300.0 + rnd.uniform(-3, 3)
        if (t - T0) % 3300.0 < 1200.0:
            w += 50.0
        if bump and bump[0] <= t - T0 < bump[0] + bump[1]:
            w += bump[2]
        rows.append((t, w))
        t += DT
    return rows


class _Det:
    """What _recover_day reads of a Detector."""
    def __init__(self, sig):
        self.signatures, self.phases, self.tz_offset_s, self.recovered_log = [sig], {}, 0.0, []
        self._sig = sig

    def signature_of(self, s):
        return self._sig if s.signature_id == self._sig.id else None


def _fridge_sig(hole):
    sig = _sig(1, 50.0, dur=1200.0, count=300, phases="a")
    sig.duration_mad = 20.0
    sig.runs = [(T0 + k * 3300.0, T0 + k * 3300.0 + 1200.0) for k in range(12)]
    log = []
    for k in range(int(86400 // 3300) + 1):
        a = T0 + k * 3300.0
        if a + 1200.0 > T0 + 86400.0 or hole[0] <= a - T0 < hole[1]:
            continue
        log.append(D.Run(phases="a", start=a, end=a + 1200.0, levels={"a": [(a, 50.0)]}, wh={"a": 50.0 * 1200.0 / 3600.0},
                         signature_id=1))
    return sig, log


def test_a_regular_cyclers_missed_cycles_are_found_again_in_the_days_readings():
    """The daily repair books the fridge's cycles back where its runs have a hole and the readings show them
    (Home, 24 Oct 2026: the day's fridge and freezer energy was a third of normal)."""
    hole = (36000.0, 54000.0)                                      # 10:00 - 15:00 of its day
    sig, log = _fridge_sig(hole)
    rows = _fridge_day(hole)
    before = sum(sig.hourly.values())
    det = _Det(sig)
    D._recover_day(det, log, {"a": rows}, T0, T0 + 86400.0)
    found = det.recovered_log
    expect = [k for k in range(27) if hole[0] <= k * 3300.0 < hole[1] and k * 3300.0 + 1200.0 <= hole[1]]
    assert len(found) >= len(expect) - 1 and all(hole[0] - 60 <= r.start - T0 <= hole[1] + 60 for r in found), \
        [(round(r.start - T0), round(r.end - T0)) for r in found]
    assert all(abs(r.end - r.start - 1200.0) < 150.0 and abs(r.energy_wh - 16.7) < 4.0 for r in found), [(r.end - r.start, r.energy_wh) for r in found]
    assert abs(sum(sig.recovered.values()) - sum(r.energy_wh for r in found)) < 1e-6
    assert sig.count == 300 and len(sig.runs) == 12                  # what it learns is untouched


def test_a_bump_of_another_size_or_length_is_not_booked_as_the_cyclers():
    hole = (36000.0, 54000.0)
    for bump in ((40000.0, 1200.0, 120.0), (40000.0, 300.0, 50.0)):  # twice its size / a quarter of its length
        sig, log = _fridge_sig(hole)
        rows = [(t, w - 50.0 if hole[0] <= t - T0 < hole[1] and (t - T0) % 3300.0 < 1200.0 else w) for t, w in _fridge_day(hole, bump)]
        det = _Det(sig)
        D._recover_day(det, log, {"a": rows}, T0, T0 + 86400.0)
        assert not det.recovered_log, (bump, [(r.start - T0, r.end - T0) for r in det.recovered_log])


def test_a_day_without_a_hole_books_nothing():
    sig, log = _fridge_sig((0.0, 0.0))
    det = _Det(sig)
    D._recover_day(det, log, {"a": _fridge_day((0.0, 0.0))}, T0, T0 + 86400.0)
    assert not det.recovered_log and not sig.recovered


def test_a_run_while_an_input_is_unavailable_has_no_context():
    """The Sock Eater was unavailable for 19 hours of 27-28 Oct after a Rinse; its last value carried on, and every
    pulse of Home's Inkubator in that time was a run in Rinse - 262 signatures of their own (2026-10-28). While
    the integration is down no value is in force: a run then has no context, and the phase before it stays its own."""
    det = D.Detector()
    det.inputs = {"select.washer": [(T0, "Off"), (T0 + 1000.0, "Rinse"), (T0 + 2000.0, D.INPUT_DOWN), (T0 + 90000.0, "Off")]}
    det.input_time = {"select.washer": {"Off": 80000.0, "Rinse": 1000.0, D.INPUT_DOWN: 8000.0}}

    def run(at):
        return D.Run(phases="a", start=T0 + at, end=T0 + at + 60.0, levels={"a": [(T0 + at, 80.0)]})

    assert det._input_context(run(1200.0))[0] == "select.washer=Rinse"
    assert det._input_context(run(5000.0)) is None
    assert det._input_context(run(91000.0)) is None          # Off is most of the time, not a rare value


def test_input_rows_keep_an_outage_as_one_row_and_a_fans_speed_as_its_value():
    class State:
        def __init__(self, at, state, **attributes):
            self.state, self.attributes = state, attributes
            self.last_updated = datetime.fromtimestamp(T0 + at, timezone.utc)

    washer = [State(0.0, "Rinse"), State(60.0, "unavailable"), State(120.0, "unknown"), State(200.0, "Rinse")]
    assert D.input_rows(washer, "sensor") == [(T0, "Rinse"), (T0 + 60.0, D.INPUT_DOWN), (T0 + 200.0, "Rinse")]
    fan = [State(0.0, "on", percentage=33), State(30.0, "off"), State(90.0, "unavailable")]
    assert D.input_rows(fan, "fan") == [(T0, "33 %"), (T0 + 30.0, "off"), (T0 + 90.0, D.INPUT_DOWN)]


def test_a_library_fragment_born_in_a_value_goes_back_into_its_named_twin():
    """Home's Inkubator had 266 signatures of one or two runs born in the washer's Rinse, carried through 19 hours of
    the integration being unavailable (2026-10-28): input_lift answers None for them for ever and a named load is
    never pruned. retake_fragments puts each into the alike twin of its name with the most runs, and touches
    nothing else - a fragment that has run three times, one already judged, one nothing is alike, a named one
    whose only twin is unnamed or named otherwise."""
    value = "sensor.washer=Rinse"
    twin = _sig(1, 60.0, 30.0, count=50, phases="c", name="Inkubator")
    small = _sig(2, 61.0, 28.0, count=1, phases="c", name="Inkubator", born_in=value)
    unnamed = _sig(3, 59.0, 33.0, count=2, phases="c", born_in=value)
    three = _sig(4, 60.0, 30.0, count=3, phases="c", name="Inkubator", born_in=value)
    judged = _sig(5, 60.0, 30.0, count=1, phases="c", name="Inkubator", born_in=value, born_judged=True)
    far = _sig(6, 300.0, 30.0, count=1, phases="c", name="Inkubator", born_in=value)
    boiler = _sig(7, 500.0, 600.0, count=20, phases="c")
    named_apart = _sig(8, 505.0, 600.0, count=1, phases="c", name="Boiler", born_in=value)
    det = D.Detector()
    det.signatures = [twin, small, unnamed, three, judged, far, boiler, named_apart]
    runs = sum(s.count for s in det.signatures)
    assert det.retake_fragments() == 2
    assert sorted(s.id for s in det.signatures) == [1, 4, 5, 6, 7, 8]
    assert twin.count == 53 and twin.name == "Inkubator" and not twin.born_in
    assert value in twin.takes_in
    assert sum(s.count for s in det.signatures) == runs
    assert det._sig(2) is None and det._current(2) == 1 and det._current(3) == 1


def test_a_stuck_fragment_goes_back_in_the_daily_rewrite_once_its_value_came_round_without_it():
    """A fragment's lift is None below INPUT_MIN_RUNS runs, so _judge_born waits on it for ever (Home's 312 born-in
    signatures of two runs or fewer, 2026-10-28). Once a day (Fleet._repair, every meter's detector): one whose value
    has come round INPUT_MIN_EPISODES times since its birth and that ran in fewer than INPUT_MIN_COVERAGE of them goes
    into its twin (retake_fragments' search), or - no twin of its name - is ordinary again; one that ran in enough of
    them still waits, and nothing happens before the value has come round often enough, nor after a filing."""
    value = "sensor.washer=Rinse"
    twin = _sig(1, 60.0, 30.0, count=50, phases="c", name="Inkubator")
    stuck = _sig(2, 61.0, 28.0, count=1, phases="c", name="Inkubator", born_in=value, ep_seen=1.0)
    covered = _sig(3, 59.0, 33.0, count=2, phases="c", born_in=value, ep_seen=2.0)
    boiler = _sig(4, 500.0, 600.0, count=20, phases="c")
    named_apart = _sig(5, 505.0, 600.0, count=1, phases="c", name="Boiler", born_in=value, ep_seen=1.0)
    fleet = D.Fleet()
    det = fleet.subs["Hiša"] = D.Detector()
    det.signatures = [twin, stuck, covered, boiler, named_apart]
    det.input_episodes = {"sensor.washer": {"Rinse": 2.0}}
    fleet._repair(T0, T0 + 86400.0)                                   # come round twice: too soon to judge
    assert [s.id for s in det.signatures] == [1, 2, 3, 4, 5] and named_apart.born_in == value
    det.input_episodes["sensor.washer"]["Rinse"] = 3.0
    det._judge_born()                                                 # after a filing: still waiting on the lift
    assert [s.id for s in det.signatures] == [1, 2, 3, 4, 5] and named_apart.born_in == value
    fleet._repair(T0 + 86400.0, T0 + 2 * 86400.0)
    assert [s.id for s in det.signatures] == [1, 3, 4, 5]
    assert twin.count == 51 and det._current(2) == 1 and value in twin.takes_in
    assert covered.born_in == value and not covered.born_judged      # ran in 2 of 3: still the value's to judge
    assert named_apart.born_in is None and value in named_apart.takes_in and boiler.count == 20


def test_a_library_stored_before_fragments_were_retaken_is_retaken_once_on_loading():
    value = "sensor.washer=Rinse"
    det = D.Detector()
    det.signatures = [_sig(1, 60.0, 30.0, count=50, phases="c"), _sig(2, 61.0, 28.0, count=1, phases="c", born_in=value)]
    stored = det.to_dict()
    assert stored["fragments_retaken"] is True
    assert len(D.Detector.from_dict(stored).signatures) == 2          # a library that says so is left as it is
    del stored["fragments_retaken"]
    back = D.Detector.from_dict(stored)
    assert [s.id for s in back.signatures] == [1] and back.signatures[0].count == 51
    assert back.to_dict()["fragments_retaken"] is True


def test_the_floor_energy_is_the_floor_times_the_time_and_is_stored():
    """The base load as a load of its own: the floor's Wh, reading to reading,
    a gap past FLOOR_GAP_S counted that long, kept across a store."""
    st = D.PhaseState()
    st.baseline, st.noise, st.level = 50.0, 5.0, 50.0
    for k in range(61):
        st.process(T0 + 60.0 * k, 50.0)
    assert abs(st.floor_wh - 50.0) < 1e-6, st.floor_wh
    st.process(T0 + 3600.0 + 7200.0, 50.0)
    assert abs(st.floor_wh - (50.0 + 50.0 * D.FLOOR_GAP_S / 3600.0)) < 1e-6
    assert abs(D.PhaseState.from_dict(st.to_dict()).floor_wh - st.floor_wh) < 1e-3


def test_a_run_its_meter_shows_on_is_never_another_loads_close_hint():
    """Susilna's dehumidifier run, its plug flat at 320 W, was closed as
    "observed" by another load's fall the other legs corroborated (the close
    hint): a run its meter shows on is the meter's to end (_meter_holds); one
    whose meter fell by half across the fall is the hint's (2026-10-30)."""
    for plug in (320.0, 0.0):
        det, st = _scoring_phase((0.0, 320.0, 1))
        o = st.open_edges[0]
        o.meter = "Susilna"
        at = T0 + 3600.0
        det.meter_on = lambda name, ph, since, watts, ts, plug=plug: plug > 160.0
        det.meter_level = lambda name, ph, ts, since, plug=plug: 320.0 if ts < at - 5.0 else plug
        st.last_ts, st.close_hint = at, o
        closed = st._pair(at, 320.0, None, 300.0)
        assert [x.start for x in closed] == ([] if plug else [T0]), (plug, closed)


if __name__ == "__main__":
    run_main(globals())
