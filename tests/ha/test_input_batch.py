"""The inputs read in one recorder query a pass (DetectionRunner._batch_inputs) give every pass exactly what one
query an input gave: switches, numbers and settings, a climate read by its attribute, a reading committed late
(found again through INPUT_OVERLAP_S), an input added later (read whole beside the others read on).

Needs Home Assistant installed (detection.py imports it), so it lives outside the pure tier's tests/test_*.py:
run it with an environment that has it, e.g. Load Juggler's venv:
    ../Dynamic_OCPP_EVSE/.venv/bin/python tests/ha/test_input_batch.py
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from homeassistant.core import State  # noqa: E402

from _load import run_main  # noqa: E402
import custom_components.load_insights.detection as D  # noqa: E402
from custom_components.load_insights.const import CONF_INPUT_ENTITIES  # noqa: E402

T0 = datetime(2026, 11, 11, 6, 0, tzinfo=timezone.utc)


def _history():
    """Six hours of four inputs, (visible from, State); a reading of the boiler sensor stamped 40 s before a
    pass's end but committed 30 s after it."""
    rows = []

    def add(eid, state, t, attrs=None, seen=None):
        at = T0 + timedelta(seconds=t)
        rows.append((seen if seen is not None else at, State(eid, state, attrs or {}, last_changed=at, last_updated=at)))

    for k in range(0, 6 * 3600, 700):
        add("switch.pump", "on" if (k // 700) % 2 else "off", k + 3)
    for k in range(0, 6 * 3600, 170):
        add("sensor.boiler_temperature", f"{40 + (k % 7)}", k + 1)
    add("sensor.boiler_temperature", "48", 4 * 3600 + 40 * 60 - 40,
        seen=T0 + timedelta(seconds=4 * 3600 + 40 * 60 + 30))
    for k in range(0, 6 * 3600, 900):
        # the thermostat stays "heat"; only hvac_action moves (an attribute-only update: last_changed stays)
        first = T0 + timedelta(seconds=11)
        at = T0 + timedelta(seconds=k + 11)
        rows.append((at, State("climate.floor", "heat", {"hvac_action": "heating" if (k // 900) % 2 else "idle"},
                               last_changed=first, last_updated=at)))
    for k in range(0, 6 * 3600, 2500):
        add("select.washer_program", ["Cotton", "Eco", "Quick"][(k // 2500) % 3], k + 7)
    for k in range(0, 6 * 3600, 400):
        add("switch.late", "on" if (k // 400) % 2 else "off", k + 5)
    rows.sort(key=lambda r: r[1].last_updated)
    return rows


class _Recorder:
    """get_significant_states over the rows visible at ``now``: the rows inside (start, end) of each entity, with
    the newest one before start when asked - one call counted for every query."""
    def __init__(self, rows):
        self.rows, self.now, self.calls = rows, T0, 0

    def query(self, hass, start, end, ids, include_start_time_state=False, significant_changes_only=True, **_):
        self.calls += 1
        out = {}
        for eid in ids:
            mine = [st for seen, st in self.rows if st.entity_id == eid and seen <= self.now]
            inside = [st for st in mine if start < st.last_updated < end]
            before = [st for st in mine if st.last_updated < start][-1:] if include_start_time_state else []
            if before or inside:
                out[eid] = before + inside
        return out

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _runner(rec, inputs):
    r = object.__new__(D.DetectionRunner)
    r.hass = None
    r.entry = SimpleNamespace(options={CONF_INPUT_ENTITIES: inputs})
    r.fleet = SimpleNamespace(_now=None)
    r._inputs_primed = True
    r._input_cache, r._input_starts, r._input_rows, r._input_passes, r.input_cache_check = {}, {}, {}, {}, {}
    r._live, r._input_buf = False, {}
    return r


def _passes(batch: bool, live: bool = False, listen_late_s: float = 0.0):
    """60 one-minute passes of _read_inputs: one query an input (batch False), one a pass (batch True), or -
    live - the first from the recorder and every later one from the state changes as they arrive (_input_buf),
    as the streaming runner reads them; ``listen_late_s`` after the first pass's end listening begins."""
    rec = _Recorder(_history())
    D.history = SimpleNamespace(get_significant_states=rec.query,
                                state_changes_during_period=lambda *a, **k: {})
    D.get_instance = lambda hass: rec
    D.INPUT_AUDIT_PASSES = 10 ** 9            # the daily full-read audit is not what is compared here
    inputs = ["switch.pump", "sensor.boiler_temperature", "climate.floor", "select.washer_program"]
    r = _runner(rec, inputs)
    if not batch:
        async def none(*a):
            return None
        r._batch_inputs = none
    out, arrived = [], T0 + timedelta(seconds=4 * 3600 - 60)
    for k in range(60):
        end = T0 + timedelta(seconds=4 * 3600 + k * 60)
        rec.now = end
        if live and k:
            listened = T0 + timedelta(seconds=4 * 3600 + listen_late_s)   # the first pass ended at T0 + 4 h
            # live once a window starts after listening began (_step's rule); before, a gap pass from the recorder
            r._live = end - timedelta(seconds=60) >= listened
            for seen, st in rec.rows:                # what HA's state_changed events delivered since the last pass
                if arrived < seen <= end and seen > listened:
                    r._input_buf.setdefault(st.entity_id, []).append(st)
        arrived = end
        if k == 20:
            r.entry.options[CONF_INPUT_ENTITIES] = inputs + ["switch.late"]
        out.append(asyncio.run(r._read_inputs(end - timedelta(seconds=60), end)))
    return out, rec.calls


def test_one_query_a_pass_reads_what_one_an_input_read():
    one_each, calls_each = _passes(batch=False)
    together, calls_together = _passes(batch=True)
    assert any(sw.get("switch.pump") for sw, _, _ in together)
    assert any(sw.get("climate.floor") for sw, _, _ in together)
    assert any(n.get("sensor.boiler_temperature") for _, n, _ in together)
    assert any(i.get("select.washer_program") for _, _, i in together)
    assert any(sw.get("switch.late") for sw, _, _ in together[20:])
    assert together == one_each
    # the start rows plus one an input (four inputs, five from pass 20), against the start rows plus one (two on
    # the pass an input is added: it is read whole, the others on)
    assert calls_each == 20 * 5 + 40 * 6, calls_each
    assert calls_together == 60 * 2 + 1, calls_together


def test_live_inputs_from_the_state_changes_read_what_the_recorder_read():
    """Streaming (step 2): after the first pass, the inputs' new rows come from their state changes and their
    start rows from the kept rows - no recorder query - and every pass returns exactly what one query an input
    returned; only an input added later (switch.late, pass 20) is read whole from the recorder once."""
    one_each, _ = _passes(batch=False)
    live, calls = _passes(batch=True, live=True)
    assert live == one_each
    assert calls == 2 + 1, calls          # pass 0: the start rows and the batch; pass 20: the new input, whole



def test_listening_that_begins_after_the_last_gap_read_loses_nothing():
    """The first live read's overlap edge lies before listening began: what changed between is in the kept rows
    the last recorder read handed over, and is read from there."""
    one_each, _ = _passes(batch=False)
    live, calls = _passes(batch=True, live=True, listen_late_s=30.0)
    assert live == one_each
    assert calls == 2 + 2 + 1, calls      # pass 0 and the gap pass 1 from the recorder, then live; pass 20's input


if __name__ == "__main__":
    run_main(globals())
