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
import csv
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load  # noqa: E402

D = load("insights.detect")
DISCOVERY = load("insights.discovery")

KIND_HINTS = (
    ("power_factor", "power_factor"), ("_pf", "power_factor"),
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
    """Which entity is which per-phase reading, by the live matcher."""
    rows = [{"entity_id": eid, "device_class": device_class_of(eid), "name": eid,
             "device_id": pseudo_device(eid)}
            for eid in series]
    return DISCOVERY.match_meter_entities(rows, "load")


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
                     or parts[-1] in ("power", "current", "voltage", "pf", "factor")):
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


def reactive(power_rows, volts, amps, pfs):
    import math
    out = {}
    vi = ai = fi = 0
    volts, amps, pfs = volts or [], amps or [], pfs or []

    def walk(rows, ts, i):
        while i + 1 < len(rows) and rows[i + 1][0] <= ts:
            i += 1
        return i if rows and rows[0][0] <= ts else -1

    for ts, p in power_rows:
        vi, ai, fi = walk(volts, ts, max(vi, 0)), walk(amps, ts, max(ai, 0)), walk(pfs, ts, max(fi, 0))
        apparent = None
        if vi >= 0 and ai >= 0:
            apparent = volts[vi][1] * amps[ai][1]
        elif fi >= 0 and pfs[fi][1]:
            apparent = abs(p) / abs(pfs[fi][1])
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
    parser.add_argument("--top", type=int, default=25, help="rows to print")
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
    if not args.no_q:
        trios = coherent_triples(series, fields, phases)
        print("reactive power from:")
        for p in phases:
            if p not in trios:
                print(f"   {p.upper()}: no meter publishes power, voltage and current together")
                continue
            pw, v, i = trios[p]
            print(f"   {p.upper()}: {pw}")
            var = reactive(series[pw], series.get(v), series.get(i), None)
            if var:
                # held forward onto the load reading's own sample times
                q[p] = align(sorted(var.items()), samples[p])
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

    subs = {}
    for pin in args.sub:
        name, _, eid = pin.partition("=")
        if eid.strip() in series:
            subs[name.strip()] = {"a": series[eid.strip()]}

    fleet = D.Fleet()
    fleet.main.tz_offset_s = 0.0
    for p in phases:
        fleet.main.phases[p].floor_zero = D.carries_generation(samples[p]) is False
    latest = max(t for rows in samples.values() for t, _ in rows)
    fleet.process(samples, subs, q, None, latest,
                  {name: True for name in subs}, pv or None)
    detector = fleet.main
    merged = detector.consolidate(100.0)

    print()
    print(f"{len(detector.signatures)} signatures from "
          f"{sum(s.count for s in detector.signatures)} sessions, {merged} merged on the way")
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
