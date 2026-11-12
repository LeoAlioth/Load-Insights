"""Streaming (step 2): the runner's one-second windows from the state changes (_Stream) give the detector what one
recorder window gives it. Three hours of a site - a grid meter's three phases and an inverter on one poll every
2 s, a fridge on a, a kettle on b, a heater on all three - assembled by the runner's own code (_assemble, through
_read_raw and _derive_load) and run two ways: A, the whole span as one recorder window through Fleet.process; B,
a window a second from the stream, ingest and advance every second and settle every minute, as the live runner
does. The stored libraries must be the same.

Needs Home Assistant installed (detection.py imports it); run with Load Juggler's venv:
    ../Dynamic_OCPP_EVSE/.venv/bin/python tests/ha/test_stream_windows.py
"""
import json
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from homeassistant.const import COMPRESSED_STATE_LAST_UPDATED, COMPRESSED_STATE_STATE  # noqa: E402

from _load import run_main  # noqa: E402
import custom_components.load_insights.detection as D  # noqa: E402
from custom_components.load_insights.const import CONF_DETECTION, CONF_INVERTERS, GRID_PREFIX  # noqa: E402
from custom_components.load_insights.insights.detect import Fleet  # noqa: E402

T0 = 1_762_840_800.37                     # 2025-11-11 06:00 UTC, off the whole second the windows end on
HOURS = 3
GRID = {p: f"sensor.m1_ac_power_{p}" for p in "abc"}
INV = "sensor.inverter_ac_power"


def _site_rows():
    """entity -> [(ts, state string)], change-only as the recorder keeps them: every 2 s the meter's three phases
    and the inverter in one poll (the inverter 14 ms behind), import positive."""
    rng = random.Random(7)
    out = {e: [] for e in [*GRID.values(), INV]}
    for k in range(int(HOURS * 3600 / 2)):
        t = T0 + 2.0 * k
        pv = max(0.0, 1500.0 * math.sin(math.pi * k / (HOURS * 1800)))
        load = {"a": 150.0, "b": 160.0, "c": 140.0}
        if (t - T0) % 1800 < 600:
            load["a"] += 110.0                       # fridge: 10 min on, 20 off
        if (t - T0) % 2400 < 180:
            load["b"] += 2000.0                      # kettle: 3 min every 40
        if 3600 <= (t - T0) % 7200 < 4500:
            for p in "abc":
                load[p] += 900.0                     # a three-phase heater, 15 min every 2 h
        for p in "abc":
            w = round(load[p] - pv / 3.0 + rng.uniform(-4, 4))
            rows = out[GRID[p]]
            if not rows or rows[-1][1] != str(w):
                rows.append((t + 0.001 * "abc".index(p), str(w)))
        w = round(pv)
        if not out[INV] or out[INV][-1][1] != str(w):
            out[INV].append((t + 0.014, str(w)))
    return out


def _runner():
    r = object.__new__(D.DetectionRunner)
    r.hass = SimpleNamespace(states=SimpleNamespace(get=lambda eid: SimpleNamespace(attributes={"unit_of_measurement": "W"})))
    # a list among the detection options, as Home has (ignore_meters): the triple memo once keyed on the whole
    # configuration and failed every step on it (2026-11-12)
    r.entry = SimpleNamespace(options={CONF_DETECTION: {**{f"{GRID_PREFIX}power_{p}": GRID[p] for p in "abc"},
                                                        "ignore_meters": ["Delavnica"]},
                                       CONF_INVERTERS: [{"power": INV}]})
    r.fleet = Fleet()
    r.fleet.main.tz_offset_s = 0.0
    r.array_in_house, r.pv_visible, r._exports_positive = {}, {}, None
    r.volts, r.q_quantum, r.sub_q_quantum, r._amp_steps, r.reactive_from = {}, {}, {}, {}, {}
    r.solar, r.submeters, r.all_submeters, r.samples_read = [], {}, {}, 0
    r._wanted, r._missing, r._pass_rows, r._site = set(), set(), None, None
    r._live, r._lags, r._lag_seen, r.step_latency = False, [], {}, {}
    r._livepower, r._live_due = D.LivePower(), {}
    return r


def _stub_registry():
    D.er = SimpleNamespace(async_get=lambda hass: SimpleNamespace(async_get=lambda eid: None))


def _at(ts):
    return datetime.fromtimestamp(ts, timezone.utc)


def _window_from_recorder(rows, start, end):
    """What _fetch returns: rows strictly inside, the newest before stamped at start (no row here is stamped
    on a boundary, where the stream's (start, end] and the recorder's (start, end) differ)."""
    out = {}
    for eid, rr in rows.items():
        before = [x for x in rr if x[0] < start][-1:]
        got = [{COMPRESSED_STATE_LAST_UPDATED: start, COMPRESSED_STATE_STATE: before[0][1]}] if before else []
        got += [{COMPRESSED_STATE_LAST_UPDATED: t, COMPRESSED_STATE_STATE: v} for t, v in rr if start < t < end]
        out[eid] = got
    return out


def _library(fleet):
    return json.dumps(fleet.to_dict(), sort_keys=True, default=str)


def _decisions(fleet):
    """The stored library less the kept-rows memory (Fleet.sub_rows, sub_from): what is kept of the readings
    for the hooks depends on how often a window trims it, not on any decision."""
    lib = json.loads(json.dumps(fleet.to_dict(), sort_keys=True, default=str))
    lib.pop("sub_rows", None)
    lib.pop("sub_from", None)
    return json.dumps(lib, sort_keys=True)


def test_one_second_windows_from_the_stream_build_what_one_recorder_window_builds():
    """B is production's shape: a gap the recorder fills (the first half hour, as after a restart), then the
    stream. The verdicts a window needs 200 readings for (carries_generation: whether the reading goes below
    zero) come from gap windows - a live minute pass, ~30 readings, could not give them either."""
    _stub_registry()
    rows = _site_rows()
    end = T0 + HOURS * 3600.0
    gap = T0 + 1800.0
    # A: one recorder window, a pass
    a = _runner()
    a._pass_rows = (_at(T0 - 1.0), _at(end), _window_from_recorder(rows, T0 - 1.0, end))
    assert a._hot(_at(T0 - 1.0), _at(end), None, None, None, settle=True)
    # B: the gap from the recorder, then every change added as it arrives, a window a second, settled each minute
    b = _runner()
    b._pass_rows = (_at(T0 - 1.0), _at(gap), _window_from_recorder(rows, T0 - 1.0, gap))
    assert b._hot(_at(T0 - 1.0), _at(gap), None, None, None, settle=True)
    stream = D._Stream()
    ids = sorted(rows)
    for e in ids:
        before = [x for x in rows[e] if x[0] <= gap][-1:]
        if before:
            stream.seed(e, *before[0])
    stream.ran_to = gap
    feed = sorted((t, e, v) for e, rr in rows.items() for t, v in rr if t > gap)
    i, prev, now = 0, gap, gap
    while prev < end:
        now += 1.0
        while i < len(feed) and feed[i][0] <= now + D.STREAM_SETTLE_S:   # what has arrived by the tick
            stream.add(feed[i][1], feed[i][0], feed[i][2])
            i += 1
        until = min(now, end)
        if until < end:
            # as _step: never inside one update, and through a datetime, as last_processed is
            until = _at(stream.quiet_end(prev, until, ids, D.COMBINE_SETTLE_S)).timestamp()
        if until <= prev:
            continue
        b._pass_rows = (_at(prev), _at(until), stream.window(prev, until, ids))
        b._missing = set()
        assert b._hot(_at(prev), _at(until), None, None, None, settle=False)
        prev = until
        if int(now) % 60 == 0:
            b.fleet.settle(until)
    b.fleet.settle(end)
    assert sum(stream.late.values()) == 0
    assert a.samples_read == b.samples_read, (a.samples_read, b.samples_read)
    assert len(a.fleet.main.recent) > 5, len(a.fleet.main.recent)        # the loads were found at all
    assert _decisions(a.fleet) == _decisions(b.fleet)


def test_the_stream_answers_a_window_as_the_recorder_does():
    rows = _site_rows()
    stream = D._Stream()
    for e, rr in rows.items():
        for t, v in rr:
            stream.add(e, t, v)
    ids = sorted(rows)
    for start, end in ((T0 + 100.5, T0 + 101.7), (T0 + 101.7, T0 + 2000.5), (T0 + 2000.5, T0 + 2000.5 + 7.3)):
        assert stream.window(start, end, ids) == _window_from_recorder(rows, start, end), (start, end)


def test_a_change_behind_its_window_is_dropped_and_counted():
    stream = D._Stream()
    stream.add("sensor.x", T0 + 1.0, "5")
    stream.window(T0, T0 + 10.0, ["sensor.x"])
    stream.add("sensor.x", T0 + 9.0, "6")             # stamped inside a window already run
    assert stream.late == {"sensor.x": 1}
    assert stream.window(T0 + 10.0, T0 + 20.0, ["sensor.x"])["sensor.x"] == [
        {COMPRESSED_STATE_LAST_UPDATED: T0 + 10.0, COMPRESSED_STATE_STATE: "5"}]


def test_the_amps_quantum_is_worked_out_again_only_when_the_amps_stepped():
    r = _runner()
    calls = []
    real = D.quantum_of_steps
    D.quantum_of_steps = lambda steps: calls.append(len(steps)) or real(steps)
    try:
        amps = [(T0 + k, 5.0 + 0.01 * (k % 7)) for k in range(400)]
        volts = [(T0 + k, 230.0) for k in range(400)]
        first = r._learn_q_quantum("a", {("current", "a"): amps, ("voltage", "a"): volts}, "a")
        still = [(T0 + 500.0, amps[-1][1])]                 # a window with no amp step in it
        again = r._learn_q_quantum("a", {("current", "a"): still, ("voltage", "a"): [(T0 + 500.0, 230.0)]}, "a")
    finally:
        D.quantum_of_steps = real
    assert first is not None and again == first, (first, again)
    assert len(calls) == 1, calls


if __name__ == "__main__":
    run_main(globals())
