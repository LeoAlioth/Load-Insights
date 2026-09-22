"""Sessions, transitions, multi-phase merging, signatures, resumability."""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from datetime import timezone  # noqa: E402
from _load import load, run_main  # noqa: E402

D = load("insights.detect")
T0 = 1_789_000_000.0     # some epoch, arbitrary
DT = 5.0


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
    assert "6.0 kW on A+C" in sig.describe(None) or "5.9 kW on A+C" in sig.describe(None) or "6.1 kW on A+C" in sig.describe(None), sig.describe(None)
    assert all(s.phases == "ac" for s in closed)


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
                      now_ts=main_b[j - 1][0], agnostic={"boiler": True})
    fleet.process({}, {}, now_ts=end)
    sig = fleet.main.signatures[0]
    assert sig.phases == "b", sig.phases          # the main meter knows the phase
    assert sig.location == "boiler", sig.locations
    assert sig.locations["boiler"] >= sig.count * 0.8, (sig.locations, sig.count)


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
    assert det2.active_by_name(T0 + 1200).get("Kettle", 0) > 2800, det2.active(T0 + 1200)


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
    # naming one removes it from the pool: a named signature is settled
    det.rename(groups[0][0], "Hob")
    assert D.suggest_levels(det.signatures, det.recent) == []


def test_overlapping_signatures_are_never_suggested_as_one_device():
    sigs = [
        D.Signature(id=1, phases="a", power={"a": 1000.0}, duration_s=300, pf=None, count=5, first_seen=T0, last_seen=T0),
        D.Signature(id=2, phases="a", power={"a": 2000.0}, duration_s=300, pf=None, count=5, first_seen=T0, last_seen=T0),
    ]
    recent = [{"signature": 1, "start": T0, "end": T0 + 600}, {"signature": 2, "start": T0 + 300, "end": T0 + 900}]
    assert D.suggest_levels(sigs, recent) == []
    apart = [{"signature": 1, "start": T0, "end": T0 + 300}, {"signature": 2, "start": T0 + 600, "end": T0 + 900}]
    assert D.suggest_levels(sigs, apart) == [[1, 2]]


def test_a_load_that_starts_while_another_runs_still_closes():
    """The regression that made the whole library useless: the phase never
    returned to its idle floor, so ONE session ran for 155 hours and 778 kWh
    at Anze's home meter. Each load must close on its own step down."""
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
    at Anze's home meter eight of them had piled up on phase A, adding to
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
        D.Signature(id=i, phases="a", power={"a": 100.0 + i}, duration_s=60.0, pf=None,
                    count=1, first_seen=0.0, last_seen=i * hour)
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
        D.Signature(id=i, phases="a", power={"a": 100.0 + i}, duration_s=60.0, pf=None,
                    count=20, first_seen=0.0, last_seen=i * hour)
        for i in range(cap)
    ]
    now = cap * hour
    det2.signatures.append(D.Signature(id=9999, phases="a", power={"a": 5000.0}, duration_s=40.0,
                                       pf=None, count=1, first_seen=now, last_seen=now))
    det2._prune(now=now)
    assert any(s.id == 9999 for s in det2.signatures)
    assert len(det2.signatures) == cap + 1, "established loads are not traded for a cap"

    # an appliance that has genuinely left the house is fair game again -
    # but the horizon is over a year, because a load that runs twice a year
    # is rare, not stale (see the twice-a-year test)
    day = 86400.0
    det3 = D.Detector()
    det3.signatures = [
        D.Signature(id=i, phases="a", power={"a": 100.0 + i}, duration_s=60.0, pf=None,
                    count=20, first_seen=0.0, last_seen=500 * day + i)
        for i in range(cap + 1)
    ]
    det3.signatures[0].last_seen = 0.0               # gone for five hundred days
    det3._prune(now=500 * day + cap)
    assert not any(s.id == 0 for s in det3.signatures)
    assert D.ESTABLISHED_HORIZON_S > 365 * day, "a yearly load must survive its own year"


def test_a_reading_with_generation_in_it_is_the_one_that_goes_negative():
    """Physics, not statistics. I tried correlating the two series' changes
    first and Anze's own data threw it out: on a day when the grid meter
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


def test_a_glitch_below_zero_cannot_drag_the_floor_down():
    """A consumption reading cannot go below zero, and Anze's template dips
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


def _sig(id, watts, dur, pf, count, hours=None, loc=None, name=None):
    return D.Signature(id=id, phases="a", power={"a": watts}, duration_s=dur, pf=pf, count=count,
                       first_seen=0.0, last_seen=float(id), hour_wh=list(hours or [0.0] * 24),
                       locations=dict(loc or {}), name=name)


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
    coarse = D.Session(phases="a", start=0.0, end=40.0, levels={"a": [(0.0, 3000.0)]}, samples=4)
    fine = D.Session(phases="a", start=0.0, end=40.0, levels={"a": [(0.0, 3000.0)]}, samples=40)
    assert coarse.confidence < fine.confidence
    assert D.Session(phases="a", start=0.0, end=40.0, levels={"a": [(0.0, 3000.0)]}).confidence == 1.0

    # the coarse one is admitted at a power the fine one is not
    sig = _sig(1, 3000.0, 40.0, None, 50)
    off = lambda n: D.Session(phases="a", start=0.0, end=40.0, levels={"a": [(0.0, 3450.0)]}, samples=n)
    assert sig.matches(off(4), 50.0) is not None, "a coarsely measured run needs a wider band"
    assert sig.matches(off(40), 50.0) is None, "a well measured run does not"

    # and it pulls the running mean less far
    lax = _sig(2, 3000.0, 40.0, None, 10)
    strict = _sig(3, 3000.0, 40.0, None, 10)
    import datetime as _dt
    lax.absorb(D.Session(phases="a", start=0.0, end=40.0, levels={"a": [(0.0, 2000.0)]}, samples=4), _dt.timezone.utc)
    strict.absorb(D.Session(phases="a", start=0.0, end=40.0, levels={"a": [(0.0, 2000.0)]}, samples=40), _dt.timezone.utc)
    assert lax.power["a"] > strict.power["a"], (lax.power, strict.power)


def test_a_named_load_that_changed_points_at_what_replaced_it():
    """A named signature is never evicted, so when its load changes in a step
    the name stays on a fingerprint nothing matches while the successor sits
    unnamed. The hint is recorded; nothing is renamed without the user."""
    day = 86400.0
    det = D.Detector()
    old = _sig(1, 3000.0, 40.0, 0.95, 200, name="Kiln")
    old.last_seen = 0.0
    new = _sig(2, 2400.0, 42.0, 0.95, 60)
    new.last_seen = 9 * day
    unrelated = _sig(3, 300.0, 42.0, 0.95, 60)      # nothing like it
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
    run = lambda w: D.Session(phases="a", start=0.0, end=40.0, levels={"a": [(0.0, w)]}, samples=40)
    young, mature = _sig(1, 3000.0, 40.0, None, 10), _sig(2, 3000.0, 40.0, None, 300)
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
    sig = _sig(3, 3000.0, 40.0, None, 300)
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
        D.Signature(id=i, phases="a", power={"a": 100.0 + i}, duration_s=60.0, pf=None,
                    count=30, first_seen=0.0, last_seen=400 * day + i)
        for i in range(cap)
    ]
    rare = D.Signature(id=9999, phases="a", power={"a": 7000.0}, duration_s=3600.0, pf=0.99,
                       count=8, first_seen=0.0, last_seen=400 * day - 180 * day)
    rare.power_mad, rare.duration_mad = 40.0, 20.0        # tight: real evidence
    det.signatures.append(rare)
    assert rare.evidence >= D.ESTABLISHED_EVIDENCE
    det._prune(now=400 * day + cap)
    assert any(s.id == 9999 for s in det.signatures), "a strong twice-a-year load was evicted"

    # and it is not offered a successor merely for being rare
    rare.name = "Kiln"
    rare.interval_s = 180 * day
    other = _sig(1234, 7000.0, 3600.0, 0.99, 20)
    other.last_seen = 400 * day
    det.signatures = [rare, other]
    det._link_successors(now=400 * day)
    assert rare.successor_id is None, "a rare load was declared replaced for running rarely"

    # a load that runs every five minutes and has not for a week IS quiet
    fast = _sig(5, 2000.0, 60.0, 0.95, 200, name="Pump")
    fast.interval_s, fast.last_seen = 300.0, 0.0
    heir = _sig(6, 2100.0, 60.0, 0.95, 20)
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
    sig = D.Signature(id=1, phases="a", power={"a": 2000.0}, duration_s=60.0, pf=None, count=0,
                      first_seen=0.0, last_seen=0.0, name="Boiler")
    det.signatures = [sig]
    run = lambda i: D.Session(phases="a", start=T0 + i * 3600, end=T0 + i * 3600 + 1800.0,
                              levels={"a": [(T0 + i * 3600, 2000.0)]}, samples=20)
    seen = []
    for i in range(5):                       # half an hour at 2 kW is 1 kWh
        sig.absorb(run(i), tz)
        seen.append(det.energy_by_name()["Boiler"] / 1000.0)
    assert [round(x, 3) for x in seen] == [1.0, 2.0, 3.0, 4.0, 5.0], seen
    assert seen == sorted(seen)

    # two signatures under one name are one device, so their energy adds
    twin = D.Signature(id=2, phases="a", power={"a": 2000.0}, duration_s=60.0, pf=None, count=3,
                       first_seen=0.0, last_seen=0.0, name="Boiler")
    twin.hour_wh = [500.0] + [0.0] * 23
    det.signatures.append(twin)
    assert round(det.energy_by_name()["Boiler"] / 1000.0, 3) == 5.5

    # and a merge carries the history across rather than losing half of it
    sig.swallow(twin)
    det.signatures = [sig]
    assert round(det.energy_by_name()["Boiler"] / 1000.0, 3) == 5.5
    assert sig.name == "Boiler"

    # an unnamed signature contributes nothing to anyone's meter
    det.signatures.append(D.Signature(id=3, phases="a", power={"a": 9.0}, duration_s=1.0, pf=None,
                                      count=1, first_seen=0.0, last_seen=0.0))
    assert set(det.energy_by_name()) == {"Boiler"}


def test_average_power_comes_from_energy_that_arrived_not_from_an_open_step():
    """The instantaneous reading spoke on a step UP without waiting for the
    matching step down, was blind to any load that began and ended between
    two five-minute passes, and sat high for a day when a partner never
    came. Energy over the span it covers has none of that."""
    # 178 Wh over five minutes is 2136 W - roughly two and a half kiln runs
    got = D.mean_power({"Kiln": 1000.0}, {"Kiln": 1178.0}, 300.0)
    assert round(got["Kiln"]) == 2136, got

    # a load that did nothing reads zero, not whatever was left open
    assert D.mean_power({"Kiln": 1178.0}, {"Kiln": 1178.0}, 300.0) == {"Kiln": 0.0}

    # one reading is a total, not a rate: a name with no earlier figure waits
    assert D.mean_power({}, {"Kiln": 1178.0}, 300.0) == {}

    # a reset takes the total to zero, which is not negative power
    assert D.mean_power({"Kiln": 1178.0}, {"Kiln": 0.0}, 300.0) == {"Kiln": 0.0}

    # the denominator is the span of DATA processed: a backfill pass covering
    # six hours in a few seconds must not report megawatts
    slow = D.mean_power({"Kiln": 0.0}, {"Kiln": 12000.0}, 6 * 3600.0)
    assert round(slow["Kiln"]) == 2000, slow
    assert D.mean_power({"Kiln": 0.0}, {"Kiln": 12000.0}, 0.0) == {}

    # and it integrates back to the meter: watts x hours is the energy gained
    span = 900.0
    rate = D.mean_power({"X": 5.0}, {"X": 305.0}, span)["X"]
    assert abs(rate * span / 3600.0 - 300.0) < 1e-6


def test_stored_state_carries_only_the_precision_it_has():
    """A watt-hour written as 276.1825572400394 spends fifteen digits on a
    figure the energy meter publishes to two decimals of a kilowatt-hour, and
    the whole library is rewritten every pass. Trimming must not cost the
    energy total anything that matters: a total that steps DOWN reads as a
    meter reset."""
    import json
    det = D.Detector()
    sig = D.Signature(id=1, phases="a", power={"a": 2000.123456789}, duration_s=61.987654321,
                      pf=0.9543210987, count=40, first_seen=1_789_000_000.25,
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
    old = _sig(1, 3000.0, 40.0, 0.95, 200, name="Kiln")
    old.last_seen, old.hour_wh = 0.0, [1000.0] + [0.0] * 23
    new = _sig(2, 2400.0, 42.0, 0.95, 60)
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
    loads_backup = [(stamp(i), 900.0) for i in range(n)]
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
    clock = _sig(1, 1800.0, 70.0, 0.96, 60)
    clock.interval_s, clock.interval_mad = 840.0, 60.0      # every 14 min, tight
    clock.hour_wh = [500.0] * 24
    assert clock.regular
    assert "every 14 min" in clock.row(tz), clock.row(tz)

    # an irregular load keeps the row short - the spacing would be noise
    erratic = _sig(2, 1800.0, 70.0, 0.96, 60)
    erratic.interval_s, erratic.interval_mad = 840.0, 700.0
    erratic.hour_wh = [500.0] * 24
    assert not erratic.regular
    assert "every" not in erratic.row(tz), erratic.row(tz)
    # and a menu row stays narrow either way
    assert len(clock.row(tz)) < 72, clock.row(tz)


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


def test_signatures_that_have_become_alike_are_merged():
    """Power and duration are running MEANS, so two signatures indistinguish-
    able today need not have been when the second was created. Kozolec had
    one 1.8 kW load split five ways - 230, 136, 50, 27 and 18 sightings, all
    within 4 % of each other."""
    det = D.Detector()
    h1 = [0] * 24; h1[7] = 5
    h2 = [0] * 24; h2[8] = 3
    det.signatures = [
        _sig(1, 1784.0, 69.0, 0.97, 230, h1, {"Hiša": 100}),
        _sig(37, 1824.0, 89.0, 0.96, 136, h2, {"Hiša": 36}),
        _sig(70, 1858.0, 78.0, 0.93, 50),
        _sig(99, 900.0, 70.0, 0.97, 40),          # half the size: a different load
    ]
    det.recent = [{"start": 0.0, "end": 1.0, "phases": "a", "kwh": 0.1, "max_w": 1800,
                   "levels": 1, "signature": 37}]
    gone = det.consolidate(100.0)
    assert gone == 2, [(x.id, x.count) for x in det.signatures]
    kept = max(det.signatures, key=lambda x: x.count)
    assert kept.id == 1 and kept.count == 416, (kept.id, kept.count)
    assert 1790 < kept.power["a"] < 1815, kept.power
    assert kept.hour_wh[7] == 5 and kept.hour_wh[8] == 3, kept.hour_wh
    assert kept.locations == {"Hiša": 136}, kept.locations
    assert det.recent[0]["signature"] == 1, det.recent        # sessions follow
    assert {x.id for x in det.signatures} == {1, 99}


def test_loads_named_differently_are_never_merged():
    det = D.Detector()
    det.signatures = [_sig(1, 1800.0, 70.0, 0.97, 10, name="Kettle"),
                      _sig(2, 1810.0, 72.0, 0.97, 8, name="Toaster")]
    assert det.consolidate(100.0) == 0
    assert len(det.signatures) == 2


def test_an_unnamed_twin_joins_the_named_one_and_keeps_the_name():
    det = D.Detector()
    det.signatures = [_sig(1, 1800.0, 70.0, 0.97, 10, name="Kettle"),
                      _sig(2, 1810.0, 72.0, 0.97, 8)]
    assert det.consolidate(100.0) == 1
    assert det.signatures[0].name == "Kettle" and det.signatures[0].count == 18


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
    belongs to three hours and two days (Anze, 2026-09-17)."""
    from datetime import datetime, timezone
    det = D.Detector()
    det.tz_offset_s = 0.0
    start = datetime(2026, 9, 18, 23, 40, tzinfo=timezone.utc).timestamp()   # a Friday
    det._file(D.Session(phases="a", start=start, end=start + 6000,
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


def test_merging_two_signatures_adds_their_weeks_together():
    det = D.Detector()
    a = _sig(1, 1800.0, 70.0, 0.97, 10)
    b = _sig(2, 1810.0, 72.0, 0.97, 8)
    a.day_wh = [1.0, 2.0, 3.0, 0.0, 0.0, 0.0, 4.0]
    b.day_wh = [0.0, 1.0, 0.0, 0.0, 5.0, 0.0, 2.0]
    det.signatures = [a, b]
    assert det.consolidate(100.0) == 1
    assert det.signatures[0].day_wh == [1.0, 3.0, 3.0, 0.0, 5.0, 0.0, 6.0], det.signatures[0].day_wh


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


def test_what_is_behind_the_ac_input_is_read_off_it():
    """A utility absorbs a surplus; a generator never does and is off almost
    always; a port that has never carried anything looks unconnected."""
    import random
    rnd = random.Random(11)
    n = 400
    house = [800.0 + rnd.uniform(-50, 50) for _ in range(n)]
    pv = [1600.0 * (i % 50) / 50.0 for i in range(n)]
    # goes negative when the site exports - only the grid does that
    assert D.classify_source([(T0 + i * DT, house[i] - pv[i]) for i in range(n)]) == D.SOURCE_UTILITY
    # imports all day, never exports, never idle: still the grid
    assert D.classify_source([(T0 + i * DT, house[i]) for i in range(n)]) == D.SOURCE_UTILITY
    # off except for two short runs, and never absorbs: a generator
    gen = [(T0 + i * DT, 4200.0 if (30 <= i < 45 or 300 <= i < 312) else 0.0) for i in range(n)]
    assert D.classify_source(gen) == D.SOURCE_GENERATOR
    # a port that has never carried anything cannot be told from a generator
    # that has not run - hence the override, and hence the caller keeping the
    # better verdict rather than following this one back down
    assert D.classify_source([(T0 + i * DT, 0.0) for i in range(n)]) == D.SOURCE_NONE
    assert D.classify_source([(T0, 900.0)]) is None


def test_which_way_round_the_grid_meter_is_wired():
    """"House = meter + inverter" holds only where importing is positive.
    Anze's SolarEdge M1 is the other way up, and summing it unflipped would
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
    once. A one-sample dip already fails SUSTAIN; one that lasts two samples
    would be accepted as a real step, so on a reading that cannot go below
    zero the samples are simply not readings."""
    n = 200
    house = [(T0 + i * DT, 400.0) for i in range(n)]
    house[80] = (house[80][0], -3001.0)                  # two samples, so
    house[81] = (house[81][0], -2950.0)                  # SUSTAIN is satisfied
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



def _plain(id, watts, count, dur=100.0, mad=0.0):
    sig = D.Signature(id=id, phases="a", power={"a": watts}, duration_s=dur, pf=None,
                      count=count, first_seen=0.0, last_seen=float(id))
    sig.power_mad = mad
    return sig


def test_a_merge_owns_up_to_the_distance_it_just_closed():
    """Averaging two signatures' deviations throws away the gap between their
    MEANS, so folding two tight signatures 300 W apart produced one claiming
    its sightings sat within a few watts of each other. That figure feeds
    tightness, which feeds evidence, which feeds the confidence the user is
    shown - a merge made a signature look BETTER measured the further apart
    the things it merged."""
    keep, other = _plain(1, 1000.0, 10), _plain(2, 700.0, 10)
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
    keep = _plain(1, 400.0, 10)
    assert keep.alike(_plain(2, 330.0, 10), 115.0)          # 70 apart, inside 115
    keep.swallow(_plain(2, 330.0, 10))                      # now 365 W, spread 35
    assert keep.alike(_plain(3, 260.0, 10), 115.0)          # 105 apart, spread would be 70
    keep.swallow(_plain(3, 260.0, 10))                      # now 330 W, spread 70
    # 200 W is 130 away - outside the band on its own terms
    assert not keep.alike(_plain(4, 200.0, 10), 115.0)
    # and a pool already at the limit refuses a partner that is inside the
    # band on its own terms, because taking it would push the pool past it
    stretched = _plain(5, 330.0, 100, mad=120.0)
    assert abs(330.0 - 260.0) < 115.0                       # the pair would match
    assert not stretched.alike(_plain(6, 260.0, 10), 115.0)  # the pool would not


def test_a_three_phase_load_is_not_judged_by_a_single_phase_yardstick():
    """power_mad and the distance travelled are TOTALS across the phases;
    the tolerance that bounds them is one phase's. A three-phase load's total
    wanders about three times what one leg does, so it was refused merges an
    identical single-phase load was granted (Anze, 2026-09-22)."""
    def leg(i, per_phase, count, phases, mad=0.0):
        sig = D.Signature(id=i, phases=phases, power={p: per_phase for p in phases},
                          duration_s=60.0, pf=None, count=count,
                          first_seen=0.0, last_seen=1.0, power_mad=mad)
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


def test_consolidation_re_asks_as_the_mean_moves():
    """The merge list is chosen against the signature as it stands BEFORE any
    of them go in. Swallowing them all without re-asking carried it somewhere
    the later entries would never have been admitted to: a ladder from 400 W
    to 25 W collapsed into one signature calling itself 94 W."""
    det = D.Detector()
    det.tz_offset_s = 0.0
    for i, w in enumerate([400.0, 330.0, 260.0, 200.0, 150.0, 110.0, 80.0, 55.0, 35.0, 25.0]):
        det.signatures.append(_plain(i, w, 10))
    det.consolidate(115.0)
    got = sorted(round(sum(s.power.values())) for s in det.signatures)
    # whatever it merges, nothing may end up claiming a power that none of
    # its constituents was anywhere near
    for sig in det.signatures:
        watts = sum(sig.power.values())
        assert sig.power_mad <= max(0.10 * watts, 115.0), (got, watts, sig.power_mad)
    assert len(det.signatures) >= 2, got


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
    dashboard with it (Anze, 2026-09-22: "we dont want people loosing their
    named entities if they update integration")."""
    det = D.Detector()
    det.tz_offset_s = 0.0
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
    fresh.tz_offset_s = 0.0
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
    det.tz_offset_s = 0.0
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


def test_a_named_loads_meter_does_not_step_down_when_the_library_is_rebuilt():
    """A rebuilt library covers ten days where the old one had accumulated
    since it was installed, so the name comes back attached to far less
    energy than its meter had already published. Home Assistant reads a drop
    on a TOTAL_INCREASING sensor as a meter reset - true, but it need not
    happen: the old reading is a FLOOR, not something to add, because the two
    periods overlap and adding them would count those ten days twice."""
    det = D.Detector()
    det.tz_offset_s = 0.0
    samples, t = _session(T0, 2000.0, 600.0)
    det.process(samples, now_ts=t)
    sig = det.signatures[0]
    assert det.rename(sig.id, "Kompresor")
    sig.carried_wh = 18978.0 - sum(sig.hour_wh)          # as if it had run for months
    before = det.energy_by_name()["Kompresor"]
    assert round(before) == 18978

    carried = det.name_descriptors()
    assert round(carried[0]["energy_wh"]) == 18978

    fresh = D.Detector()
    fresh.tz_offset_s = 0.0
    fresh.carry_names(carried)
    # the meter holds its reading even before the name finds a load again
    assert round(fresh.energy_by_name().get("Kompresor", 0.0)) == 18978

    samples, t2 = _session(T0 + 100000.0, 2000.0, 600.0)
    fresh.process(samples, now_ts=t2)
    assert [s.name for s in fresh.signatures] == ["Kompresor"]
    after = fresh.energy_by_name()["Kompresor"]
    assert after >= before, (before, after)
    # and it is the floor, NOT a sum - the rebuilt days are not counted twice
    assert round(after) == 18978, after


def test_the_floor_stops_mattering_once_the_meter_passes_it():
    """It is a floor, not a constant: a rebuilt library that outgrows the old
    reading publishes its own figure."""
    det = D.Detector()
    det.tz_offset_s = 0.0
    det.carry_names([{"name": "Kettle", "phases": "a", "power": {"a": 2000.0},
                      "duration_s": 600.0, "pf": None, "energy_wh": 5.0}])
    samples, t = _session(T0, 2000.0, 600.0)
    det.process(samples, now_ts=t)
    assert [s.name for s in det.signatures] == ["Kettle"]
    own = sum(s.energy_wh for s in det.signatures if s.name == "Kettle")
    assert own > 5.0, own
    assert det.energy_by_name()["Kettle"] == own


def test_the_meter_reading_survives_a_restart_mid_rebuild():
    det = D.Detector()
    det.carry_names([{"name": "Kiln", "phases": "ac", "power": {"a": 2985.0, "c": 2937.0},
                      "duration_s": 23.0, "pf": 0.96, "energy_wh": 10436.2}])
    back = D.Detector.from_dict(det.to_dict())
    assert round(back.energy_floor["Kiln"], 1) == 10436.2
    assert [o["name"] for o in back.orphan_names] == ["Kiln"]


def test_a_generation_bump_keeps_names_and_their_meter_readings():
    """The path nobody has ever walked: DETECTOR_GENERATION moves, the whole
    stored library is discarded, and every installation in the world does this
    at once on the next update. It is simulated here against a store in the
    shape the current code writes - which is what an installation would
    actually be holding - because the alternative is discovering it went wrong
    from someone's Energy dashboard (Anze, 2026-09-22: "i just want this fixed
    for future updates/of the detection library versions/resets")."""
    det = D.Detector()
    det.tz_offset_s = 0.0
    samples, t = _session(T0, 2000.0, 600.0)
    det.process(samples, now_ts=t)
    sig = det.signatures[0]
    det.rename(sig.id, "Kiln")
    sig.carried_wh = 10436.2 - sum(sig.hour_wh)
    stored = {"generation": 5, "fleet": {"main": det.to_dict()}}

    # the bump: the store is read by a detector that has disowned its shape
    orphans = D.names_in_store(stored)
    assert [o["name"] for o in orphans] == ["Kiln"]
    assert round(orphans[0]["energy_wh"], 1) == 10436.2, orphans[0]["energy_wh"]

    fresh = D.Detector()                        # what `raw = {}` leaves behind
    fresh.tz_offset_s = 0.0
    fresh.carry_names(orphans)
    assert round(fresh.energy_by_name()["Kiln"], 1) == 10436.2

    samples, t2 = _session(T0 + 100000.0, 2000.0, 600.0)
    fresh.process(samples, now_ts=t2)
    assert [x.name for x in fresh.signatures] == ["Kiln"]
    assert round(fresh.energy_by_name()["Kiln"], 1) == 10436.2
    assert fresh.orphan_names == []


def test_the_only_paths_that_discard_the_library_both_carry_names():
    """Two ways the library goes: the reset the user asks for, and the
    generation bump they never see. Both take the same road out - a list of
    descriptors into carry_names - so neither can quietly grow a third
    behaviour."""
    det = D.Detector()
    det.tz_offset_s = 0.0
    samples, t = _session(T0, 2000.0, 600.0)
    det.process(samples, now_ts=t)
    det.rename(det.signatures[0].id, "Kiln")
    det.signatures[0].carried_wh = 9000.0

    by_reset = det.name_descriptors()
    by_bump = D.names_in_store({"generation": 4, "fleet": {"main": det.to_dict()}})
    for got in (by_reset, by_bump):
        assert [g["name"] for g in got] == ["Kiln"]
        assert round(got[0]["energy_wh"]) == round(det.energy_by_name()["Kiln"])
        assert got[0]["phases"] == "a" and got[0]["power"]
    # and both produce the same floor
    a, b = D.Detector(), D.Detector()
    a.carry_names(by_reset); b.carry_names(by_bump)
    assert round(a.energy_floor["Kiln"]) == round(b.energy_floor["Kiln"])


def test_a_row_says_whether_the_load_is_on_now_or_when_it_last_ran():
    """What someone naming a load actually has to go on is their own memory of
    the last hour: the dishwasher went on after dinner, nothing has run in the
    workshop since Tuesday. A row that says a load is on RIGHT NOW turns
    naming into walking over and looking at it."""
    now = 1_000_000.0
    sig = D.Signature(id=1, phases="a", power={"a": 2000.0}, duration_s=600.0, pf=0.99,
                      count=9, first_seen=now - 86400.0, last_seen=now - 600.0)
    assert "last ran 10 min ago" in sig.row(timezone.utc, now)
    assert "last ran 10 min ago" in sig.describe(timezone.utc, now)
    assert "running now" in sig.row(timezone.utc, now, running=True)
    assert "last ran" not in sig.row(timezone.utc, now, running=True)
    # a run that has only just stopped reads better as that
    sig.last_seen = now - 30.0
    assert "just finished" in sig.row(timezone.utc, now)
    # and without a clock the row is exactly what it always was
    assert "ran" not in sig.row(timezone.utc)
    assert "running" not in sig.row(timezone.utc)


def test_running_now_names_the_signatures_that_are_on():
    """A signature only exists once a run has FINISHED - the session is the
    step up paired with the step down that undoes it - so the first time a
    load ever runs there is nothing to say it is on. From the second time,
    the open edge is matched on its size and the row can say so."""
    det = D.Detector()
    det.tz_offset_s = 0.0
    rows, t = [], T0
    for _ in range(40):
        rows.append((t, 200.0)); t += 10.0
    for _ in range(40):                     # a complete run, so a signature exists
        rows.append((t, 2200.0)); t += 10.0
    for _ in range(40):
        rows.append((t, 200.0)); t += 10.0
    det.process({"a": rows}, now_ts=t)
    assert det.signatures, "a finished run should have made a signature"
    assert det.running_now(t) == set(), "nothing is on between runs"

    rows2 = []
    for _ in range(20):                     # it starts again, and stays on
        rows2.append((t, 2200.0)); t += 10.0
    det.process({"a": rows2}, now_ts=t)
    on = det.running_now(t)
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
    Anze's twenty-four rows, which distinguished nothing from nothing."""
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


def test_the_row_drops_the_sparkline_when_it_has_words_for_the_week():
    """Seven characters of bars and the word "weekdays" are the same fact, and
    a menu row is too narrow to spend on both."""
    now = 1_700_000_000.0
    sig = D.Signature(id=1, phases="a", power={"a": 2000.0}, duration_s=600.0, pf=0.99,
                      count=20, first_seen=now - 20 * 86400.0, last_seen=now - 900.0)
    sig.hour_wh = [50.0] * 24
    sig.day_wh = [10.0] * 7
    plain = sig.row(timezone.utc, now)
    assert sig.when == "" and any(b in plain for b in "▁▂▃▄▅▆▇█")

    sig.day_wh = [10.0, 10.0, 10.0, 10.0, 10.0, 0.0, 0.0]
    worded = sig.row(timezone.utc, now)
    assert "weekdays" in worded
    assert not any(b in worded for b in "▁▂▃▄▅▆▇█"), worded


def _lvl(id, watts, dur=100.0, pf=0.96, phases="a", count=5):
    return D.Signature(id=id, phases=phases, power={p: watts / len(phases) for p in phases},
                       duration_s=dur, pf=pf, count=count, first_seen=0.0, last_seen=1.0)


def test_two_loads_are_not_one_device_just_because_nothing_was_recorded():
    """"Never two of them at once" has to be OBSERVED. The session list is
    finite - two hundred entries against a library several times that at a
    busy house - so for most pairs there is nothing recorded either way, and
    reading that silence as "they never overlap" offered a 149 W load and a
    2.7 kW one as one device (Anze's house, 2026-09-22)."""
    a, b = _lvl(1, 150.0), _lvl(2, 2700.0)
    assert D.suggest_levels([a, b], []) == []               # nothing seen of either
    seen_a = [{"signature": 1, "start": 0.0, "end": 50.0}]
    assert D.suggest_levels([a, b], seen_a) == []           # only one side seen
    both = seen_a + [{"signature": 2, "start": 500.0, "end": 550.0}]
    assert D.suggest_levels([a, b], both) == [[1, 2]]       # both seen, never together


def test_levels_of_one_device_run_for_about_as_long_each_time():
    """Sizes are not compared - a setting can be any fraction of another - but
    duration is a different question, and leaving it out was most of what let
    unrelated loads group. A hob on three settings boils the same pan for
    about as long each time; what differs is the power."""
    seen = [{"signature": 1, "start": 0.0, "end": 30.0},
            {"signature": 2, "start": 500.0, "end": 530.0},
            {"signature": 3, "start": 1000.0, "end": 1600.0}]
    brief_a, brief_b = _lvl(1, 3000.0, dur=25.0), _lvl(2, 5900.0, dur=23.0)
    lengthy = _lvl(3, 4100.0, dur=600.0)
    groups = D.suggest_levels([brief_a, brief_b, lengthy], seen)
    assert groups == [[1, 2]], groups


def test_a_load_that_overlaps_another_is_never_the_same_device():
    """The whole test: one appliance cannot run two of its own settings at
    once, so an observed overlap rules the pair out however well they match."""
    a, b = _lvl(1, 1000.0), _lvl(2, 2000.0)
    together = [{"signature": 1, "start": 0.0, "end": 100.0},
                {"signature": 2, "start": 50.0, "end": 150.0}]
    assert D.suggest_levels([a, b], together) == []


def test_duration_identifies_some_loads_and_not_others():
    """Anze, 2026-09-22: "duration can be a part of the fingerprint, but it is
    not necessarily one." A kettle boils the same volume every time and takes
    about two minutes; a thermostat runs for twenty seconds or twenty minutes
    depending how cold the tank is. Measured against Kozolec's submeters, the
    boiler's runs spread by 0.12 of their median and the pressure pump's by
    0.48. So the load says which it is, and the library already writes it
    down."""
    kettle = D.Signature(id=1, phases="a", power={"a": 2000.0}, duration_s=120.0,
                         pf=0.99, count=20, first_seen=0.0, last_seen=1.0)
    kettle.duration_mad = 8.0                      # 0.07 of its length
    assert kettle.keeps_time
    assert kettle.duration_factor == D.MATCH_DURATION_FACTOR

    thermostat = D.Signature(id=2, phases="a", power={"a": 1800.0}, duration_s=67.0,
                             pf=0.99, count=489, first_seen=0.0, last_seen=1.0)
    thermostat.duration_mad = 40.0                 # 0.6 of its length
    assert not thermostat.keeps_time
    assert thermostat.duration_factor == D.LOOSE_DURATION_FACTOR


def test_a_young_signature_does_not_enforce_a_duration_it_has_not_earned():
    """The bootstrapping trap: judged on one or two sightings, a signature
    freezes whatever its first runs happened to be and then never absorbs the
    ones that would have taught it otherwise."""
    young = D.Signature(id=1, phases="a", power={"a": 2000.0}, duration_s=120.0,
                        pf=0.99, count=2, first_seen=0.0, last_seen=1.0)
    young.duration_mad = 0.0                       # perfectly consistent, so far
    assert not young.keeps_time, "two sightings say nothing about keeping time"
    assert young.duration_factor == D.LOOSE_DURATION_FACTOR


def test_a_thermostat_absorbs_its_own_short_and_long_runs():
    """The whole point, end to end: the same tank reheating from nearly hot and
    from stone cold is one load, and was two."""
    det = D.Detector()
    det.tz_offset_s = 0.0
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
    much work the person has already done (Anze, 2026-09-22)."""
    sigs = []
    for i in range(40):
        sig = D.Signature(id=i, phases="a", power={"a": 1000.0 + i * 50}, duration_s=60.0,
                          pf=0.95, count=9, first_seen=0.0, last_seen=1.0)
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
    weak = [D.Signature(id=i, phases="a", power={"a": 500.0}, duration_s=60.0, pf=0.9,
                        count=2, first_seen=0.0, last_seen=1.0) for i in range(9)]
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
    Kozolec showing 6 of 12, Home 14 of 60 (Anze's screenshots, 2026-09-22)."""
    sigs = []
    for i in range(30):
        sig = D.Signature(id=i, phases="a", power={"a": 1000.0 + i * 50}, duration_s=60.0,
                          pf=0.95, count=9, first_seen=0.0, last_seen=1.0)
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


def test_a_power_factor_carries_how_far_wrong_it_could_be():
    """A factor from coarse amps is not thrown away - it is given its error
    bar, and the bar is what stops it constraining anything.

    Anze asked for this rather than the outright gate it replaces: one
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
    configured for (Anze, 2026-09-22)."""
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
    wins. At Anze's house that was the grid meter read as the house - sign
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
    wrong (Anze, 2026-09-22)."""
    def row(watts):
        sig = D.Signature(id=1, phases="a", power={"a": float(watts)}, duration_s=180.0,
                          pf=0.95, count=50, first_seen=0.0, last_seen=9 * 86400.0)
        return sig.describe(timezone.utc)
    assert row(28).startswith("28 W on A")
    assert row(92).startswith("92 W on A")
    assert row(345).startswith("345 W on A")
    # and a kilowatt is still a kilowatt
    assert row(4400).startswith("4.4 kW on A")
    assert row(5918).startswith("5.9 kW on A")
    # the boundary belongs to kW, not to 1000 W
    assert row(999).startswith("999 W")
    assert row(1000).startswith("1.0 kW")


def test_a_motors_starting_surge_is_not_a_load_of_its_own():
    """Anze's pressure pump reads 8886 W in one sample and 830 W in every
    sample after, four times over in a day. Held for the length of a sample
    that single reading dominates the run - a 40 s session came out at 2.8 kW
    for an 830 W pump - and the power it recorded depended on how long the
    session happened to last, so one pump arrived as several loads."""
    t = T0
    pump = D.Session(phases="a", start=t, end=t + 40.0, samples=5,
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
    washer = D.Session(phases="a", start=t, end=t + 3000.0, samples=200,
                       levels={"a": [(t, 2000.0), (t + 600, 200.0)]})
    assert round(sum(washer.power_by_phase().values())) == 560
    assert washer.inrush_w == 0.0

    # and a run with a single level has nothing to strip
    flat = D.Session(phases="a", start=t, end=t + 60.0, samples=12,
                     levels={"a": [(t, 1800.0)]})
    assert round(sum(flat.power_by_phase().values())) == 1800
    assert flat.inrush_w == 0.0


def test_the_surge_is_kept_as_evidence_and_survives_a_restart():
    """It is the most diagnostic thing a house produces - only a motor does it
    - so once it is out of the power it is worth keeping as a feature (Anze,
    2026-09-22: "that spike is a very good device signature, but it has to be
    taken into account properly to not show as separate loads")."""
    det = D.Detector()
    det.tz_offset_s = 0.0
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


if __name__ == "__main__":
    run_main(globals())
