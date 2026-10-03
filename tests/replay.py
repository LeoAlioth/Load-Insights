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
import csv
import functools
import hashlib
import math
import os
import pickle
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load  # noqa: E402
from fetch_history import RENAMED  # noqa: E402  - ids renamed since some history was fetched

D = load("insights.detect")
DISCOVERY = load("insights.phases")

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


CACHE = Path.home() / ".cache" / "load_insights_bench"
SOURCE = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()   # a change here re-parses


def _cached(key, build):
    """build()'s result, kept as a pickle in CACHE under ``key``: every replay
    of a scorecard parsed the same half a gigabyte of CSV again, most of a
    Kozolec replay's time. Entries a week old go when a new one is written."""
    path = CACHE / (hashlib.sha1(repr((SOURCE, sorted(RENAMED.items()), key)).encode()).hexdigest() + ".pickle")
    try:
        with open(path, "rb") as handle:
            return pickle.load(handle)
    except (OSError, EOFError, pickle.UnpicklingError):
        pass
    value = build()
    CACHE.mkdir(parents=True, exist_ok=True)
    for old in CACHE.glob("*.pickle"):
        if old.stat().st_mtime < datetime.now().timestamp() - 7 * 86400:
            old.unlink(missing_ok=True)
    part = path.with_suffix(f".{os.getpid()}.part")    # replays started together each write their own
    with open(part, "wb") as handle:
        pickle.dump(value, handle, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(part, path)
    return value


def _stamp(path) -> tuple:
    st = os.stat(path)
    return str(Path(path).resolve()), st.st_size, st.st_mtime_ns


def _parse(path) -> dict:
    """One export as {entity_id: [(epoch seconds, state)]}."""
    out = defaultdict(list)
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            eid = (row.get("entity_id") or "").strip()
            eid = RENAMED.get(eid, eid)       # history from before a rename
            if not eid:
                continue
            when = (row.get("last_changed") or row.get("last_updated") or "").strip()
            try:
                moment = datetime.fromisoformat(when.replace("Z", "+00:00"))
            except ValueError:
                continue
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            out[eid].append((moment.timestamp(), (row.get("state") or "").strip()))
    return dict(out)


@functools.lru_cache(maxsize=None)
def _parsed(stamp: tuple) -> dict:
    return _cached(("file", stamp), lambda: _parse(stamp[0]))


def _rows(paths, entity_id=None):
    """(entity_id, epoch seconds, state) for every row of the exports, or of
    one entity's; an id renamed since is read as its new one."""
    for path in expand(paths):
        parsed = _parsed(_stamp(path))
        for eid in ([entity_id] if entity_id else parsed):
            for ts, state in parsed.get(eid, ()):
                yield eid, ts, state


def read_states(paths, entity_id):
    """An entity's TEXT states over time [(ts, state)] - a washer's cycle
    phase, a fan's speed exported as its own series."""
    return sorted({(ts, state) for _, ts, state in _rows(paths, entity_id)
                   if state and state not in ("unknown", "unavailable")})


def read_switch(paths, entity_id):
    """An entity's on-periods [(on, off)] from the exports' TEXT states - on,
    or a thermostat's heating (its hvac_action exported as its own series).
    A drop-out is not an off: Tuya Local's dehumidifier switch went
    unavailable ~60 times in ten days, for under a second each."""
    rows = sorted((ts, state.lower() in ON_STATES) for _, ts, state in _rows(paths, entity_id)
                  if state not in ("unknown", "unavailable"))
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


def read_csv(paths, keep_coarse=False, say=print):
    """entity_id -> [(epoch seconds, value)], numbers only, in time order.

    Order across files does not matter, and the overlap between one day's
    export and the next is harmless: rows are sorted and de-duplicated. Kept
    in CACHE, one file for the whole folder, until a file in it changes."""
    files = expand(paths)
    out, coarse = _cached(("series", [_stamp(f) for f in files], keep_coarse),
                          lambda: _series(paths, keep_coarse))
    if coarse:
        say(f"ignored {coarse} hourly rows - too coarse for a load that lasts seconds")
    say(f"read {len(files)} file(s)")
    return out


def _series(paths, keep_coarse):
    series = defaultdict(list)
    for eid, ts, raw in _rows(paths):
        try:
            value = float(raw)
        except ValueError:
            continue                              # unavailable, unknown, a text state
        series[eid].append((ts, value))
    out, coarse = {}, 0
    for eid, rows in series.items():
        rows.sort()
        rows = [row for i, row in enumerate(rows) if i == 0 or row[0] != rows[i - 1][0]]
        if not keep_coarse:
            rows, gone = drop_aggregates(rows)
            coarse += gone
        out[eid] = rows
    return out, coarse


def guess_roles(series, say=print):
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
            say(f"   ({chosen} carries generation - using {pick} for phase {p.upper()})")
            fields[f"power_{p}"] = pick
    return fields


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
    """Production's _reactive, with the meter's own signed reactive power
    taken from its first reading on. Production decides per pass, minutes at
    a time: the passes after the meter's own VAr began read it, the ones
    before do not. Decided once for the whole replay, a VAr enabled on the
    last day (Home's grid meter, 30.09 07:57) was never read at all."""
    if not (signed and power_rows):
        return D._reactive(power_rows, volts, amps, pfs, None, vas)
    start = signed[0][0]
    before = [r for r in power_rows if r[0] < start]
    out = D._reactive(before, volts, amps, pfs, None, vas) if before else {}
    out.update(D._reactive([r for r in power_rows if r[0] >= start], volts, amps, pfs, signed, vas))
    return out


def parse(argv):
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
    parser.add_argument("--live-days", type=float, default=0.0,
                        help="feed the last this many days in --live-pass-s passes, as a site runs "
                             "once it has caught up; the days before in --slice-hours slices, as its backfill")
    parser.add_argument("--live-pass-s", type=float, default=60.0,
                        help="how long each live pass is (production's default: one minute)")
    parser.add_argument("--keep-coarse", action="store_true",
                        help="do not drop hourly statistics rows")
    parser.add_argument("--no-q", action="store_true",
                        help="ignore reactive power entirely")
    parser.add_argument("--rates", action="store_true",
                        help="print each entity's sampling rate and stop")
    return parser.parse_args(argv)


def run(args, transform=None, say=print):
    """Replay the exports ``args`` (see parse) through a Fleet, as production
    would have read them; ``transform`` may rebuild the series first.
    Returns the Fleet, every session its house detector filed (in order),
    and each sub-meter's series as the Fleet was fed it, its phases summed."""
    series = read_csv(args.csv, args.keep_coarse, say)
    if transform:
        series = transform(series)
    if not series:
        raise SystemExit("no numeric rows found - is this a History panel export?")
    fields = guess_roles(series, say)
    for pin in args.role:
        key, _, eid = pin.partition("=")
        fields[key.strip()] = eid.strip()
    fields = {k: v for k, v in fields.items() if v in series}

    span = [t for rows in series.values() for t, _ in rows]
    say(f"{len(series)} entities, {sum(len(r) for r in series.values())} rows, "
        f"{(max(span) - min(span)) / 86400:.1f} days")
    say("matched:")
    for key in sorted(fields):
        say(f"   {key:12} {fields[key]}")
    phases = [p for p in D.PHASES if fields.get(f"power_{p}")]
    if not phases:
        raise SystemExit("no per-phase power found - pin one with --role power_a=sensor.x")

    samples = {p: series[fields[f"power_{p}"]] for p in phases}
    q = {}
    q_quantum = {}
    if not args.no_q:
        trios = coherent_triples(series, fields, phases)
        say("reactive power from:")
        for p in phases:
            if p not in trios:
                say(f"   {p.upper()}: no meter publishes power, voltage and current together")
                continue
            pw, v, i = trios[p]
            signed = next((e for e in series if device_kind_phase(e) == ("reactive_power", p)
                           and pseudo_device(e) == pseudo_device(pw)), None)
            say(f"   {p.upper()}: {pw}" + (f" (signed: {signed})" if signed else ""))
            var = reactive(series[pw], series.get(v), series.get(i), None, series.get(signed) if signed else None)
            if var:
                # held forward onto the load reading's own sample times
                q[p] = D._align(sorted(var.items()), samples[p])
            # ...and what those amps can resolve, the same way production
            # measures it, so the preview is not kinder than the real thing
            amps, volts = series.get(i) or [], series.get(v) or []
            if amps and volts:
                dq = D.measure_quantum([x for _, x in amps])
                if dq:
                    lvl = sorted(x for _, x in volts)[len(volts) // 2]
                    q_quantum[p] = dq * lvl
                    say(f"      amps resolve {dq:g} A -> {dq * lvl:.1f} VA per quantum; "
                        f"no power factor under {D.PF_MIN_QUANTA * dq * lvl:.0f} W")
    pv = {}
    for eid in args.pv:
        rows = series.get(eid)
        if not rows:
            say(f"   (no rows for {eid})")
            continue
        for p in phases:
            bucket = pv.setdefault(p, {})
            for ts, watts in D._align(rows, samples[p]).items():
                bucket[ts] = bucket.get(ts, 0.0) + watts
    for p in list(pv):
        verdict = D.carries_generation(samples[p])
        say(f"   array shows in phase {p.upper()}: {verdict}")
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
                            sub_q.setdefault(name.strip(), {})[p] = D._align(sorted(var.items()), prow)
                if name.strip() in sub_q:
                    say(f"   {name.strip()}: reactive power from its apparent power or power factor on {''.join(sorted(sub_q[name.strip()]))}")

    # entities that say when a load is on, read as text: on-periods
    switch_spans = {}
    for pin in args.switch:
        name, _, eid = pin.partition("=")
        switch_spans[name.strip()] = read_switch(args.csv, eid.strip())
    drivers = {eid.strip(): sorted(series.get(eid.strip()) or []) for eid in args.driver}
    inputs = {eid.strip(): read_states(args.csv, eid.strip()) for eid in args.input}
    for eid, rows in inputs.items():
        say(f"input {eid}: {len(rows)} changes")
    for eid, rows in drivers.items():
        say(f"driver {eid}: {len(rows)} readings")

    def held(rows, a, b):
        """[a - SWITCH_MEMORY_S, b) as production reads it: with the value in
        force at the start of that window."""
        ts = [r[0] for r in rows]              # by time alone: a setting's values are words
        i = bisect.bisect_left(ts, a - D.SWITCH_MEMORY_S)
        return rows[max(i - 1, 0):bisect.bisect_left(ts, b)]
    fleet = D.Fleet()
    fleet.wait_cap_s = D.METER_WAIT_CAP_S          # as production's default; METER_WAIT_CAP_S=0 to judge at once
    filed, fed, file = [], {}, fleet.main._file

    def keep(s, *a, **kw):                          # every session the house files, for the bench
        file(s, *a, **kw)
        filed.append(s)
    fleet.main._file = keep
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
    fed_last: dict = {}
    fleet.parents = dict(p.split("=", 1) for p in args.parent)
    live_from = latest - args.live_days * 86400.0 if args.live_days else math.inf
    while t <= latest:
        if t < live_from:
            e = min(t + step, live_from, latest + 1e-6)          # the backfill's slices, up to where live begins
        else:
            e = min(t + args.live_pass_s, latest + 1e-6)         # then pass by pass, as a caught-up site
        # sliced the way the recorder answers, then cleaned the way production
        # cleans it, so both paths are the same code
        sub_slice = {n: D.without_window_start({p: cut(rows, t, e) for p, rows in byp.items()}, t)
                     for n, byp in subs.items()}
        for n, byp in sub_slice.items():
            last = fed_last.setdefault(n, {})           # each channel held at its last value...
            at = {p: 0 for p in byp}
            for ts in sorted({ts for rows in byp.values() for ts, _ in rows}):
                for p, rows in byp.items():
                    while at[p] < len(rows) and rows[at[p]][0] <= ts:
                        last[p] = rows[at[p]][1]
                        at[p] += 1
                fed.setdefault(n, []).append((ts, sum(last.values())))   # ...across slices, as the Fleet keeps them
        fleet.process(D.without_window_start({p: cut(rows, t, e) for p, rows in samples.items()}, t),
                      sub_slice, q, sub_q or None, e, agnostic, pv or None, q_quantum,
                      single={n: n in args.single for n in subs},     # every meter declared, as the runner does
                      switches={n: [(a, b if b is not None and b <= e else None) for a, b in spans
                                    if a < e and (b is None or b > t - D.SWITCH_MEMORY_S)]
                                for n, spans in switch_spans.items()} or None,
                      drivers={n: held(rows, t, e) for n, rows in drivers.items()} or None,
                      inputs={n: held(rows, t, e) for n, rows in inputs.items()} or None)
        t = e
    return fleet, filed, {k: sorted(v) for k, v in fed.items() if v}


def main() -> int:
    args = parse(sys.argv[1:])
    if args.rates:
        for eid, rows in sorted(read_csv(args.csv, args.keep_coarse).items()):
            if len(rows) < 3:
                print(f"  {eid:62} {len(rows)} rows")
                continue
            gaps = sorted(b[0] - a[0] for a, b in zip(rows, rows[1:]))
            span = (rows[-1][0] - rows[0][0]) / 86400.0
            print(f"  {eid:62} {len(rows):>8} rows, {span:5.1f} days, "
                  f"median gap {gaps[len(gaps) // 2]:6.1f} s")
        return 0
    detector = run(args)[0].main
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
        print(f"  {sig.describe(timezone.utc)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
