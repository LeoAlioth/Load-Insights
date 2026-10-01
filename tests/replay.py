"""Run load detection over an exported history, with no Home Assistant.

Shipping a build to a live site to see what the detector makes of it is a
slow way to learn anything (Anze, 2026-09-18). This takes the CSV that
Home Assistant's History panel downloads - the one with entity_id, state
and last_changed - and feeds it through exactly the code that runs there,
then prints the library the naming page would show.

    python3 tests/replay.py data/history/home --rates
    python3 tests/replay.py data/history/home --pv sensor.inverter_ac_power
    python3 tests/replay.py hist.csv --role power_a=sensor.my_phase_a
    python3 tests/replay.py hist.csv --sub "Boiler=sensor.boiler_power"

Exports live in ``data/``, which .gitignore holds twice over - the whole
folder, and *.csv anywhere - so a site's history never reaches the remote.
One folder per site, one file per day.

Entities are matched to their roles by name, the same way the config flow
matches a device's sensors, and anything it gets wrong can be pinned with
--role. Everything is optional except at least one phase of power.
"""
import argparse
import bisect
import math
import csv
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load  # noqa: E402
from export_urls import RENAMED  # noqa: E402  - ids renamed since some history was fetched

D = load("insights.detect")
DISCOVERY = load("insights.discovery")

KIND_HINTS = (
    ("power_factor", "power_factor"), ("_pf", "power_factor"), ("_var_", "reactive_power"),
    ("apparent", "apparent_power"),
    ("voltage", "voltage"), ("current", "current"), ("power", "power"),
)


def device_class_of(entity_id: str):
    """The CSV carries no device class, so read it off the name."""
    lowered = entity_id.lower()
    for token, kind in KIND_HINTS:
        if token in lowered:
            return kind
    return None


def drop_aggregates(rows):
    """Remove hourly statistics, keeping the raw state changes.

    An export reaching past the recorder's retention comes back at two
    resolutions: state changes for the recent part and HOURLY MEANS for
    everything older. An hour's mean cannot show a 44-second burst, and fed
    to the detector each one looks like a step, so a month of them would
    invent a load an hour. They give themselves away by landing on the hour
    after a long gap (Anze exporting 30 days, 2026-09-18)."""
    kept, dropped, previous = [], 0, None
    for ts, value in rows:
        on_the_hour = abs((ts + 30) % 3600.0 - 30) < 5.0
        gap = None if previous is None else ts - previous
        if on_the_hour and (gap is None or gap >= 1800.0):
            dropped += 1
        else:
            kept.append((ts, value))
        previous = ts
    return kept, dropped


def expand(paths):
    """Files, or every .csv inside a directory - a long window has to come
    out of the History panel a day at a time (Anze, 2026-09-18), so a folder
    of them is the normal case."""
    out = []
    for raw in paths:
        path = Path(raw)
        out.extend(sorted(path.glob("*.csv")) if path.is_dir() else [path])
    return out


ON_STATES = {"on", "heating", "cooling", "drying"}


def read_states(paths, entity_id):
    """An entity's TEXT states over time [(ts, state)] - a washer's cycle
    phase, a fan's speed exported as its own series."""
    rows = []
    for path in expand(paths):
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if (row.get("entity_id") or "").strip() != entity_id:
                    continue
                when = (row.get("last_changed") or row.get("last_updated") or "").strip()
                try:
                    moment = datetime.fromisoformat(when.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                state = (row.get("state") or "").strip()
                if state and state not in ("unknown", "unavailable"):
                    rows.append((moment.timestamp(), state))
    return sorted(set(rows))


def read_switch(paths, entity_id):
    """An entity's on-periods [(on, off)] from the exports' TEXT states - on,
    or a thermostat's heating (its hvac_action exported as its own series)."""
    rows = []
    for path in expand(paths):
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if (row.get("entity_id") or "").strip() != entity_id:
                    continue
                when = (row.get("last_changed") or row.get("last_updated") or "").strip()
                try:
                    moment = datetime.fromisoformat(when.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                rows.append((moment.timestamp(), (row.get("state") or "").strip().lower() in ON_STATES))
    rows.sort()
    spans, on = [], None
    for t, is_on in rows:
        if is_on and on is None:
            on = t
        elif not is_on and on is not None:
            spans.append((on, t))
            on = None
    if on is not None:
        spans.append((on, None))
    return spans


def read_csv(paths, keep_coarse=False):
    """entity_id -> [(epoch seconds, value)], numbers only, in time order.

    Order across files does not matter, and the overlap between one day's
    export and the next is harmless: rows are sorted and de-duplicated."""
    series = defaultdict(list)
    paths = expand(paths)
    for path in paths:
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                eid = (row.get("entity_id") or "").strip()
                eid = RENAMED.get(eid, eid)       # history from before a rename
                raw = (row.get("state") or "").strip()
                when = (row.get("last_changed") or row.get("last_updated") or "").strip()
                if not eid or not when:
                    continue
                try:
                    value = float(raw)
                except ValueError:
                    continue                      # unavailable, unknown, a text state
                try:
                    moment = datetime.fromisoformat(when.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                series[eid].append((moment.timestamp(), value))
    out, coarse = {}, 0
    for eid, rows in series.items():
        rows.sort()
        rows = [row for i, row in enumerate(rows) if i == 0 or row[0] != rows[i - 1][0]]
        if not keep_coarse:
            rows, gone = drop_aggregates(rows)
            coarse += gone
        out[eid] = rows
    if coarse:
        print(f"ignored {coarse} hourly rows - too coarse for a load that lasts seconds")
    print(f"read {len(paths)} file(s)")
    return out


def guess_roles(series):
    """Which entity is which per-phase reading, by the live matcher.

    With one correction the live flow does not need. There, the user picks a
    DEVICE and the matcher only ever sees that device's entities; here it is
    handed every entity in the export at once, so it can pick a grid meter
    over a house-consumption template on the strength of the meter having
    volts and amps beside it. The harness has what the config flow does not -
    the actual data - so it settles it the way physics does: a reading that
    goes below zero contains the site's generation and is not what the house
    draws (Anze's home, 2026-09-18, where that mistake put the idle floor at
    -8 kW)."""
    rows = [{"entity_id": eid, "device_class": device_class_of(eid), "name": eid,
             "device_id": pseudo_device(eid)}
            for eid in series]
    fields = DISCOVERY.match_meter_entities(rows, "load")
    for p in D.PHASES:
        chosen = fields.get(f"power_{p}")
        if not chosen or D.carries_generation(series[chosen]) is not True:
            continue
        better = [r["entity_id"] for r in rows
                  if r["device_class"] == "power"
                  and DISCOVERY.phase_of(r["entity_id"]) == p
                  and D.carries_generation(series[r["entity_id"]]) is False]
        if better:
            pick = max(better, key=lambda e: DISCOVERY._score(e, e, "load") or 0)
            print(f"   ({chosen} carries generation - using {pick} for phase {p.upper()})")
            fields[f"power_{p}"] = pick
    return fields


def align(source, target_rows):
    """``source`` read as of each of ``target_rows``' moments."""
    out, i = {}, 0
    for ts, _ in target_rows:
        while i + 1 < len(source) and source[i + 1][0] <= ts:
            i += 1
        if source and source[0][0] <= ts:
            out[ts] = source[i][1]
    return out


def pseudo_device(entity_id: str) -> str:
    """The CSV carries no device, so stand one in from the name.

    Everything a meter publishes shares a prefix and differs only in the
    trailing kind and phase - sensor.solaredge_se17k_m1_ac_power_a beside
    sensor.solaredge_se17k_m1_ac_current_a - so stripping those two tokens
    groups a meter's readings the way the entity registry does live."""
    parts = entity_id.split("_")
    while parts and (parts[-1] in ("a", "b", "c", "1", "2", "3", "an", "bn", "cn",
                                   "l1", "l2", "l3")
                     or parts[-1] in ("power", "current", "voltage", "pf", "factor", "var",
                                      "active", "apparent", "reactive")):   # ..._ac_var_a beside ..._ac_power_a   # a 3EM's phase_a_active_power beside phase_a_power_factor
        parts.pop()
    return "_".join(parts)


def coherent_triples(series, fields, phases):
    """Per phase, one meter's power + voltage + current, all from the SAME
    pseudo-device. The load role's own first - that is the circuit the loads
    are in - then any other meter that offers a complete set, which at home
    is the grid meter and is the right answer: every household watt flows
    through it, so its reactive power steps when a load switches."""
    by_device = {}
    for eid in series:
        by_device.setdefault(pseudo_device(eid), {})[device_kind_phase(eid)] = eid
    out = {}
    for p in phases:
        own = pseudo_device(fields.get(f"power_{p}", ""))
        order = [own] + [d for d in sorted(by_device) if d != own]
        for dev in order:
            got = by_device.get(dev) or {}
            trio = (got.get(("power", p)), got.get(("voltage", p)), got.get(("current", p)))
            if all(trio) and D.carries_load(series[trio[0]]):
                out[p] = trio
                break
    return out


def device_kind_phase(entity_id: str):
    lowered = entity_id.lower()
    kind = device_class_of(entity_id)
    phase = DISCOVERY.phase_of(lowered)
    return (kind, phase)


def reactive(power_rows, volts, amps, pfs, signed=None, vas=None):
    """As production's _reactive: the meter's own signed reactive power where
    it covers the window, else the root of S squared minus P squared - S the
    meter's own apparent power, then V x I, then P over the power factor."""
    import math
    if signed and power_rows:
        # production decides this per pass, minutes at a time: the passes
        # after the meter's own VAr began read it, the ones before do not.
        # Decided once for the whole replay, a VAr enabled on the last day
        # (Home's grid meter, 30.09 07:57) was never read at all.
        start = signed[0][0]
        before = [r for r in power_rows if r[0] < start]
        out = reactive(before, volts, amps, pfs, None, vas) if before else {}
        si = 0
        for ts, _ in power_rows:
            if ts < start:
                continue
            while si + 1 < len(signed) and signed[si + 1][0] <= ts:
                si += 1
            out[ts] = signed[si][1]
        return out
    out = {}
    vi = ai = fi = si = 0
    volts, amps, pfs, vas = volts or [], amps or [], pfs or [], vas or []

    def walk(rows, ts, i):
        while i + 1 < len(rows) and rows[i + 1][0] <= ts:
            i += 1
        return i if rows and rows[0][0] <= ts else -1

    for ts, p in power_rows:
        vi, ai, fi = walk(volts, ts, max(vi, 0)), walk(amps, ts, max(ai, 0)), walk(pfs, ts, max(fi, 0))
        si = walk(vas, ts, max(si, 0))
        same = lambda rows, i: i + 1 if i + 1 < len(rows) and rows[i + 1][0] - ts <= 1.0 else i   # production's _with_update
        v, a, f, s = same(volts, vi), same(amps, ai), same(pfs, fi), same(vas, si)
        apparent = None
        if s >= 0:
            apparent = vas[s][1]
        elif v >= 0 and a >= 0:
            apparent = volts[v][1] * amps[a][1]
        elif f >= 0 and pfs[f][1]:
            apparent = abs(p) / abs(pfs[f][1])
        if apparent is not None:
            out[ts] = math.sqrt(max(0.0, apparent * apparent - p * p))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", nargs="+", help="History panel exports")
    parser.add_argument("--role", action="append", default=[],
                        metavar="power_a=sensor.x", help="pin one field to an entity")
    parser.add_argument("--sub", action="append", default=[],
                        metavar="Name=sensor.x", help="a device's own meter")
    parser.add_argument("--pv", action="append", default=[], help="an array's power")
    parser.add_argument("--switch", action="append", default=[],
                        help="NAME=ENTITY: an entity whose on/off (or heating) says when a load runs")
    parser.add_argument("--input", action="append", default=[],
                        help="ENTITY: a setting a device reports - a washer's cycle phase")
    parser.add_argument("--driver", action="append", default=[],
                        help="ENTITY: a number a load's runs may follow - a room's temperature")
    parser.add_argument("--parent", action="append", default=[],
                        help="METER=PARENT: the meter hangs under PARENT, as the Energy dashboard nests them")
    parser.add_argument("--single", action="append", default=[],
                        help="a sub-meter declared to hold one device, as production's options say")
    parser.add_argument("--sub-phases", action="append", default=[],
                        metavar="Name=sensor.a,sensor.b,sensor.c", help="a three-phase meter, per phase")
    parser.add_argument("--top", type=int, default=25, help="rows to print")
    parser.add_argument("--no-start-state", action="store_true",
                        help="slice without the recorder's start-of-window row")
    parser.add_argument("--slice-hours", type=float, default=0.0,
                        help="feed the detector in slices this long, as production's "
                             "backfill does (6); 0 feeds everything in one call")
    parser.add_argument("--keep-coarse", action="store_true",
                        help="do not drop hourly statistics rows")
    parser.add_argument("--no-q", action="store_true",
                        help="ignore reactive power entirely")
    parser.add_argument("--rates", action="store_true",
                        help="print each entity's sampling rate and stop")
    args = parser.parse_args()

    series = read_csv(args.csv, args.keep_coarse)
    if args.rates:
        for eid, rows in sorted(series.items()):
            if len(rows) < 3:
                print(f"  {eid:62} {len(rows)} rows")
                continue
            gaps = sorted(b[0] - a[0] for a, b in zip(rows, rows[1:]))
            span = (rows[-1][0] - rows[0][0]) / 86400.0
            print(f"  {eid:62} {len(rows):>8} rows, {span:5.1f} days, "
                  f"median gap {gaps[len(gaps) // 2]:6.1f} s")
        return 0
    if not series:
        print("no numeric rows found - is this a History panel export?")
        return 1
    fields = guess_roles(series)
    for pin in args.role:
        key, _, eid = pin.partition("=")
        fields[key.strip()] = eid.strip()
    fields = {k: v for k, v in fields.items() if v in series}

    span = [t for rows in series.values() for t, _ in rows]
    print(f"{len(series)} entities, {sum(len(r) for r in series.values())} rows, "
          f"{(max(span) - min(span)) / 86400:.1f} days")
    print("matched:")
    for key in sorted(fields):
        print(f"   {key:12} {fields[key]}")
    phases = [p for p in D.PHASES if fields.get(f"power_{p}")]
    if not phases:
        print("no per-phase power found - pin one with --role power_a=sensor.x")
        return 1

    samples = {p: series[fields[f"power_{p}"]] for p in phases}
    q = {}
    q_quantum = {}
    if not args.no_q:
        trios = coherent_triples(series, fields, phases)
        print("reactive power from:")
        for p in phases:
            if p not in trios:
                print(f"   {p.upper()}: no meter publishes power, voltage and current together")
                continue
            pw, v, i = trios[p]
            signed = next((e for e in series if device_kind_phase(e) == ("reactive_power", p)
                           and pseudo_device(e) == pseudo_device(pw)), None)
            print(f"   {p.upper()}: {pw}" + (f" (signed: {signed})" if signed else ""))
            var = reactive(series[pw], series.get(v), series.get(i), None, series.get(signed) if signed else None)
            if var:
                # held forward onto the load reading's own sample times
                q[p] = align(sorted(var.items()), samples[p])
            # ...and what those amps can resolve, the same way production
            # measures it, so the preview is not kinder than the real thing
            amps, volts = series.get(i) or [], series.get(v) or []
            if amps and volts:
                dq = D.measure_quantum([x for _, x in amps])
                if dq:
                    lvl = sorted(x for _, x in volts)[len(volts) // 2]
                    q_quantum[p] = dq * lvl
                    print(f"      amps resolve {dq:g} A -> {dq * lvl:.1f} VA per quantum; "
                          f"no power factor under {D.PF_MIN_QUANTA * dq * lvl:.0f} W")
    pv = {}
    for eid in args.pv:
        rows = series.get(eid)
        if not rows:
            print(f"   (no rows for {eid})")
            continue
        for p in phases:
            bucket = pv.setdefault(p, {})
            for ts, watts in align(rows, samples[p]).items():
                bucket[ts] = bucket.get(ts, 0.0) + watts
    for p in list(pv):
        verdict = D.carries_generation(samples[p])
        print(f"   array shows in phase {p.upper()}: {verdict}")
        if verdict is False:
            pv.pop(p)

    subs, agnostic = {}, {}
    for pin in args.sub:
        name, _, eid = pin.partition("=")
        if eid.strip() in series:
            subs[name.strip()] = {"a": series[eid.strip()]}
            agnostic[name.strip()] = True
    # A three-phase meter, as production reads one: a reading per phase, under
    # the phase letters the METER gives them - which need not be the house's.
    sub_q = {}
    for pin in args.sub_phases:
        name, _, eids = pin.partition("=")
        rows = {p: series[e.strip()] for p, e in zip("abc", eids.split(",")) if e.strip() in series}
        if rows:
            subs[name.strip()] = rows
            agnostic[name.strip()] = False
            if not args.no_q:
                # its reactive power from what the meter publishes beside the
                # power - a 3EM its apparent power and a power factor - as production
                # derives it for every sub-meter
                by_kind = {device_kind_phase(e): e for e in series
                           if pseudo_device(e) == pseudo_device(eids.split(",")[0].strip())}
                for p, prow in rows.items():
                    pf = series.get(by_kind.get(("power_factor", p)))
                    va = series.get(by_kind.get(("apparent_power", p)))
                    if pf or va:
                        var = reactive(prow, None, None, pf, None, va)
                        if var:
                            sub_q.setdefault(name.strip(), {})[p] = align(sorted(var.items()), prow)
                if name.strip() in sub_q:
                    print(f"   {name.strip()}: reactive power from its apparent power or power factor on {''.join(sorted(sub_q[name.strip()]))}")

    # entities that say when a load is on, read as text: on-periods
    switch_spans = {}
    for pin in args.switch:
        name, _, eid = pin.partition("=")
        switch_spans[name.strip()] = read_switch(args.csv, eid.strip())
    drivers = {eid.strip(): sorted(series.get(eid.strip()) or []) for eid in args.driver}
    inputs = {eid.strip(): read_states(args.csv, eid.strip()) for eid in args.input}
    for eid, rows in inputs.items():
        print(f"input {eid}: {len(rows)} changes")
    for eid, rows in drivers.items():
        print(f"driver {eid}: {len(rows)} readings")

    def held(rows, a, b):
        """[a - SWITCH_MEMORY_S, b) as production reads it: with the value in
        force at the start of that window."""
        ts = [r[0] for r in rows]              # by time alone: a setting's values are words
        i = bisect.bisect_left(ts, a - D.SWITCH_MEMORY_S)
        return rows[max(i - 1, 0):bisect.bisect_left(ts, b)]
    fleet = D.Fleet()
    fleet.wait_cap_s = D.METER_WAIT_CAP_S          # as production's default; METER_WAIT_CAP_S=0 to judge at once
    fleet.main.tz_offset_s = 0.0
    for p in phases:
        fleet.main.phases[p].floor_zero = D.carries_generation(samples[p]) is False
    latest = max(t for rows in samples.values() for t, _ in rows)
    # Production reads the recorder six hours at a time and files what each
    # slice closed before reading the next, so the library GROWS through a
    # backfill. Fed in one call, nothing is filed until the end, and anything
    # that consults the library on the way - how long a load of some size is
    # known to run, say - finds it empty.
    first = min(t for rows in samples.values() for t, _ in rows)
    step = args.slice_hours * 3600.0 if args.slice_hours else (latest - first + 1.0)
    def cut(rows, a, b):
        """The rows in [a, b) the way the recorder answers for that window:
        with include_start_time_state, which production asks for, the first
        row is the state AS OF a, stamped a - a copy of the last reading,
        repeated at every slice start (checked on Anze's home, 2026-09-23). At
        one-minute ticks that is one extra reading a minute on every phase."""
        i, j = bisect.bisect_left(rows, (a, -math.inf)), bisect.bisect_left(rows, (b, -math.inf))
        part = rows[i:j]
        if args.slice_hours and not args.no_start_state and i > 0 and (not part or part[0][0] > a):
            part = [(a, rows[i - 1][1])] + part
        return part
    t = first
    fleet.parents = dict(p.split("=", 1) for p in args.parent)
    while t <= latest:
        e = min(t + step, latest + 1e-6)
        # sliced the way the recorder answers, then cleaned the way production
        # cleans it, so both paths are the same code
        fleet.process(D.without_window_start({p: cut(rows, t, e) for p, rows in samples.items()}, t),
                      {n: D.without_window_start({p: cut(rows, t, e) for p, rows in byp.items()}, t)
                       for n, byp in subs.items()},
                      q, sub_q or None, e, agnostic, pv or None, q_quantum,
                      single={n: n in args.single for n in subs} if args.single else None,
                      switches={n: [(a, b if b is not None and b <= e else None) for a, b in spans
                                    if a < e and (b is None or b > t - D.SWITCH_MEMORY_S)]
                                for n, spans in switch_spans.items()} or None,
                      drivers={n: held(rows, t, e) for n, rows in drivers.items()} or None,
                      inputs={n: held(rows, t, e) for n, rows in inputs.items()} or None)
        t = e
    detector = fleet.main
    # The detector's OWN measured noise, which is what production passes.
    print()
    print(f"{len(detector.signatures)} signatures from "
          f"{sum(s.count for s in detector.signatures)} sessions")
    for phase, state in detector.phases.items():
        if state.baseline is not None:
            print(f"   phase {phase.upper()}: idle {state.baseline:.0f} W, "
                  f"noise {state.noise:.0f} W, {len(state.open_edges)} still open")
    print()
    for sig in sorted(detector.signatures, key=lambda s: -s.energy_wh)[:args.top]:
        print(f"  {sig.row(timezone.utc)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
