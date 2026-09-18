"""Sessions, transitions, multi-phase merging, signatures, resumability."""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
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


if __name__ == "__main__":
    run_main(globals())
