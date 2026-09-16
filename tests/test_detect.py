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


if __name__ == "__main__":
    run_main(globals())
