"""The scorecard: per MEASURED load, how much of its energy the detector puts
in that load's signatures, and how much of those signatures' energy is really
its own - in watt-hours, the way a named load's energy statistics will be
right or wrong. Loads can also be PLANTED into the house reading, a square
wave of known size and timing, to score a load no meter watches.

    python3 tests/energy_bench.py card [SITE ...] [DIAL=value ...]
    python3 tests/energy_bench.py invariance SITE FOLDER [DIAL=value ...]
    python3 tests/energy_bench.py diff A.json B.json
    python3 tests/energy_bench.py SITE FOLDER [DIAL=value ...] [PLANT=...] [QGATE=x] [OUT=run.json]
    python3 tests/energy_bench.py check

Two numbers per measured load L (Anze, 2026-10-02; a boiler that used 10 kWh,
9.5 kWh attributed to it, 9 kWh of that rightly):
    capture  = energy rightly attributed to L / L's measured energy    9 / 10    = 90 %
    impurity = energy wrongly attributed to L / all attributed to L    0.5 / 9.5 = 5.3 %
L's measured energy is its meter's above its idle floor - the level it holds a
tenth of the TIME, by time, since a meter that reports on change reads mostly
while running; the floor's kWh is printed beside it. Attributed to L: every
session in a signature L owns, one holding at least half of its energy - the
signature that would be named after L. A session's energy is L's where L's
meter switched on with it (_started_with), as far as the meter drew above its
floor while it ran (_above). Over a site: capture weighted by energy, impurity
pooled (all wrong / all attributed).

Each site is scored against two truth sets (_tables), each one owner per
signature: its DEVICES - every meter that holds no other: Home's seven plugs
under the grid connection plus Blaževa Soba (inside Hiša) and the office
(inside Mansarda), a session credited to a device inside a circuit and never
to the circuit - and its PARTITION: the meters directly under the main meter
and, named in REST, what is left of the main without them (Home's Delavnica,
Kozolec's Rest) - everything the main meter reads, once.

Modes (bench.py's SUBS): hidden (SUBS=none - no meter fed, the main-meter
estimate), circuits (SUBS=circuits - only the meters others hang under, Home's
two 3EMs) and fed (SUBS=prod - every meter production reads). The meters are
the truth in every mode, read from the history.

card        every site (default home, kozolec and the circuits home-hisa and
            home-mansarda) in every mode at SLICE=0, 6 and LIVE=1, PARALLEL
            replays at once: one figure where the slicings agree, each one's
            (0|6|L1) where they do not; the slicing invariance, and how runs
            were closed (the pairing). Written to data/scorecard/<commit>-<utc>.json.
invariance  SLICE=0 - one call, the reference - against 6 and LIVE=1, fed
            unless a SUBS= dial says otherwise: every filed session that
            differs, with how far it lies from the nearest pass boundary;
            also written to data/scorecard/invariance-<site>-<mode>-<commit>-<utc>.txt.
diff        two card files, capture and impurity load by load.

PLANT=set1 plants three loads at Home: 1.2 kW for 8 min every 5 h on A,
150 W for 20 min every 3 h on B, 40 W for 45 min every 2 h on B. A plant is
added to the house reading, and to the grid meter's power and current so the
power factor stays consistent (a resistive load); it is a load of every truth
set. QGATE=x: only runs of at least that quality have their energy attributed.
"""
from __future__ import annotations

import bisect
import collections
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench as B  # noqa: E402

D, R = B.D, B.R
ROOT = Path(__file__).resolve().parents[1]
PLANTS: list = []          # (name, watts, phase, [(on, off), ...])
GRID = "sensor.solaredge_se17k_m1_ac_"
SETS = {"set1": ["1200:480:18000:a", "150:1200:10800:b", "40:2700:7200:b"]}
REST = {"home": "Delavnica", "kozolec": "Rest"}    # what the main meter reads and no meter below it
MODES = {"hidden": "SUBS=none", "circuits": "SUBS=circuits", "fed": "SUBS=prod"}
SLICINGS = {"0": ["SLICE=0"], "6": ["SLICE=6"], "L1": ["LIVE=1"]}
PARALLEL = 8               # replays at once: each is one core and ~0.1 GB
MOVED_S = 60.0             # a run starting this near one of the other slicing's, same phases, is that run moved
# what closed a run, by the function that closed it - see _instrument
KINDS = {"_unseen_stop": "unseen stop", "<genexpr>": "multi-close", "_joint_stop": "joint stop",
         "end_older": "started again", "_input_ended": "input ended", "_pair": "leg split off"}


def _intervals(t0: float, t1: float, on_s: float, every_s: float, seed: int) -> list:
    rnd = random.Random(seed)
    out, t = [], t0 + rnd.uniform(0.2, 0.8) * every_s
    while t + on_s < t1:
        out.append((t, t + on_s))
        t += every_s * rnd.uniform(0.8, 1.2)
    return out


def _square(rows: list, ivs: list, amp) -> list:
    """rows plus a square wave: ``amp(ts, level_change)`` says what a change of
    level adds to the reading at that instant; a row is inserted at each edge
    so the step shows when it happened, not at the next reading."""
    edges = sorted([(a, 1) for a, b in ivs] + [(b, -1) for a, b in ivs])
    out, level, k, held = [], 0, 0, None
    for ts, w in rows:
        while k < len(edges) and edges[k][0] <= ts:
            level += edges[k][1]
            if held is not None and (not out or out[-1][0] < edges[k][0]):
                out.append((edges[k][0], held + amp(edges[k][0], level)))
            k += 1
        held = w
        out.append((ts, w + amp(ts, level)))
    return out


def planted(plants: list):
    """The series with the loads planted, for the bench to build its house
    from - or None, nothing to plant."""
    specs = []
    for p in plants:
        specs += SETS.get(p, [p])
    if not specs:
        return None

    def plant(s):
        for k, spec in enumerate(specs):
            watts, on_s, every_s, ph = spec.split(":")
            watts, on_s, every_s = float(watts), float(on_s), float(every_s)
            house = s.get(B.HOUSE_IDS[ph])
            if not house:
                continue
            name = f"plant {watts:g} W {ph.upper()}"
            ivs = _intervals(house[0][0], house[-1][0], on_s, every_s, seed=11 + k)
            if not any(x[0] == name for x in PLANTS):
                PLANTS.append((name, watts, ph, ivs))
            s[B.HOUSE_IDS[ph]] = _square(house, ivs, lambda ts, lv: lv * watts)
            grid, amps = s.get(f"{GRID}power_{ph}"), s.get(f"{GRID}current_{ph}")
            volts = s.get(f"{GRID}voltage_{ph}n") or []
            if grid:
                raw = list(grid)
                s[f"{GRID}power_{ph}"] = _square(grid, ivs, lambda ts, lv: -lv * watts)
                if amps:
                    # the meter's current is unsigned: what the extra import does to it
                    def more(ts, lv, raw=raw, volts=volts):
                        g = B._at(raw, ts, 0.0)
                        return (abs(g - lv * watts) - abs(g)) / max(B._at(volts, ts, 230.0), 100.0)
                    s[f"{GRID}current_{ph}"] = _square(amps, ivs, more)
        return s
    return plant


def _above(rows: list, times: list, a: float, b: float, floor: float) -> float:
    """The device's energy above its idle floor over [a, b], its readings held
    until the next - the same measure as its truth. Not what its meter ROSE
    by against the minutes before, as bench.label once read it: a charge
    that follows another has the last one in its "before", and read as
    nothing - 13.8 kWh of Kozolec's charger scored as no one's (2026-10-01)."""
    i = max(bisect.bisect_right(times, a) - 1, 0)
    wh, t = 0.0, a
    while i < len(rows) and t < b:
        nxt = rows[i + 1][0] if i + 1 < len(rows) else b
        end = min(b, nxt)
        if end > t and rows[i][0] <= t:
            wh += max(0.0, rows[i][1] - floor) * (end - t)
        t = max(t, end)
        i += 1
    return wh / 3600.0


def _started_with(rows: list, times: list, s) -> bool:
    """Did the device's meter switch on with the run - rise, at any moment
    from just before its start to a minute after, by at least half of what the
    run started at? Only then is the run the device's. Energy drawn DURING it
    is not enough: that credits every long run a device merely ran beside, and
    each of the overlapping ones again - Susilna scored 86 kWh right of 63
    (2026-10-01). And the peak, not the reading a minute on: a pump runs 60-70
    s, was off again by then, and a third of its own runs scored as nobody's -
    its group under half its own, and all of it "not found" (2026-10-01)."""
    first = sum(rows_[0][1] for rows_ in s.levels.values() if rows_)
    before = B._at(rows, s.start - 10.0, 0.0)
    i, j = bisect.bisect_right(times, s.start - 10.0), bisect.bisect_right(times, s.start + 60.0)
    peak = max([before] + [r[1] for r in rows[i:j]])
    return first > 0 and peak - before >= 0.5 * first


def _overlap(ivs: list, a: float, b: float) -> float:
    return sum(max(0.0, min(b, y) - max(a, x)) for x, y in ivs)


def _tables(site: str) -> dict:
    """The two truth sets a site is scored against - see the docstring - or
    one, where they are the same loads."""
    subs, parents = B.PROD_SUBS[site], B.PROD_PARENTS.get(site, {})
    circuits = set(parents.values())
    out = {"devices": [n for n in subs if n not in circuits],
           "partition": [n for n in subs if n not in parents] + ([REST[site]] if site in REST else [])}
    return out if out["partition"] != out["devices"] else {"devices": out["devices"]}


def _remainder(main: list, meters: list) -> list:
    """What the main meter reads that no meter below it does: the main's
    readings less every meter's, each held at its last reading. A meter not
    heard from yet counts as nothing: a plug reporting on change is silent
    while off (Kozolec's washing machine wrote one reading in ten days, Home's
    workshop charger four), and waiting for it would start the rest days late."""
    t0 = min(r[0][0] for r in main)
    return D.combine([(r, 1.0) for r in main] + [(r if r[0][0] <= t0 else [(t0, 0.0)] + r, -1.0) for r in meters])


def _held_wh(rows: list, a: float, b: float) -> float:
    """Watt-hours of ``rows`` over [a, b], each reading held until the next
    and the last until b; nothing before the first."""
    wh = 0.0
    for (t, w), nxt in zip(rows, [r[0] for r in rows[1:]] + [b]):
        lo, hi = max(t, a), min(nxt, b)
        if hi > lo:
            wh += w * (hi - lo)
    return wh / 3600.0


def measured(folder: str, site: str):
    """Every load of the site's truth sets, name -> [(ts, watts)], read from
    the history whatever the Fleet was fed - a three-phase meter's phases
    summed - and the remainder's check: (main, meters, remainder) kWh over the
    remainder's span, or None where the site has none."""
    s = R.read_csv([folder], False, say=lambda *a, **k: None)
    if site == "home":
        B._prod_house(s)          # the house as production builds it: the delavnica's solar added back
    parts = {n: [s[e] for e in (e if isinstance(e, list) else [e]) if s.get(e)] for n, e in B.PROD_SUBS[site].items()}
    out = {}
    for n, rows in parts.items():
        total: list = []
        for r in rows:
            total = D._sum_series(total, r)
        if total:
            out[n] = total
    if site not in REST:
        return out, None
    main = [s[e] for e in B.SITES[site]["main"].values() if s.get(e)]
    meters = [r for n in _tables(site)["partition"] for r in parts.get(n, [])]
    rest = out[REST[site]] = _remainder(main, meters)
    a, b = rest[0][0], rest[-1][0]
    kwh = lambda rows: sum(_held_wh(r, a, b) for r in rows) / 1000.0   # noqa: E731
    return out, (kwh(main), kwh(meters), kwh([rest]))


def _truth(rows: list) -> tuple:
    """(Wh above the meter's idle floor, the floor in W, the floor's Wh): the
    floor the level it holds a tenth of the TIME - by time, since a meter
    that reports on change reads mostly while running."""
    held = sorted((w, t2 - t1) for (t1, w), (t2, _) in zip(rows, rows[1:]))
    span, acc, floor = sum(d for _, d in held), 0.0, 0.0
    for w, d in held:
        acc += d
        if acc >= 0.1 * span:
            floor = w
            break
    return sum(max(0.0, w - floor) * d for w, d in held) / 3600.0, floor, floor * span / 3600.0


def _own(per_sig: dict, names) -> dict:
    """signature -> the load among ``names`` holding the most of its energy,
    where that is at least half of it."""
    out = {}
    for sid, row in per_sig.items():
        best = max((n for n in names if n in row), key=row.get, default=None)
        if best is not None and row[best] >= 0.5 * row["total"]:
            out[sid] = best
    return out


def _rates(truth_wh: float, attributed_wh: float, correct_wh: float) -> tuple:
    """(capture, impurity) - impurity None where nothing was attributed."""
    return correct_wh / truth_wh, ((attributed_wh - correct_wh) / attributed_wh if attributed_wh else None)


def _table(per_sig: dict, truth: dict, floors: dict, names: list) -> tuple:
    """One truth set's scorecard, and which load owns which signature."""
    owner = _own(per_sig, names)
    got = {n: {"in": 0.0, "of": 0.0, "sigs": []} for n in names if n in truth}
    for sid, n in owner.items():
        row = per_sig[sid]
        got[n]["in"] += row[n]
        got[n]["of"] += row["total"]
        got[n]["sigs"].append([sid, row["n"], round(row["total"] / 1000.0, 2), round(row[n] / row["total"], 2)])
    loads, tot = {}, {"truth": 0.0, "of": 0.0, "in": 0.0}
    for n, g in got.items():
        if truth[n] < 50.0:                   # under 50 Wh over the replay: nothing to score
            continue
        cap, imp = _rates(truth[n], g["of"], g["in"])
        floor = floors.get(n, (0.0, 0.0))
        loads[n] = {"truth_kwh": truth[n] / 1000.0, "floor_w": floor[0], "floor_kwh": floor[1],
                    "attributed_kwh": g["of"] / 1000.0, "correct_kwh": g["in"] / 1000.0,
                    "capture": cap, "impurity": imp, "sigs": sorted(g["sigs"], key=lambda x: -x[2])}
        tot["truth"] += truth[n]
        tot["of"] += g["of"]
        tot["in"] += g["in"]
    cap, imp = _rates(tot["truth"], tot["of"], tot["in"]) if tot["truth"] else (None, None)
    return {"loads": loads, "capture": cap, "impurity": imp, "truth_kwh": tot["truth"] / 1000.0,
            "attributed_kwh": tot["of"] / 1000.0, "wrong_kwh": (tot["of"] - tot["in"]) / 1000.0}, owner


def _instrument():
    """Watch the house detector's pairing without changing it, the way replay
    watches what it files. Every run ends in PhaseState._close: ``direct`` when
    the fall being paired is its own observed stop, otherwise inferred, the
    kind being the function that closed it (KINDS). Every fall is paired in
    PhaseState._pair: one that closes nothing and steps no run down to a lower
    level is a stop that closed nothing. Every rise opens a run in
    PhaseState._declare: one that never reaches _close is a start never
    closed - still open at the end, or given up on. Each closed run carries
    its close ("closes": kind, start and stop watts), through Detector._combine
    for a run merged across phases. Each Fleet.process is a pass, its end a
    pass boundary. Returns (log, undo)."""
    log: dict = {"closes": [], "rises": [], "falls": [], "bounds": [], "fleet": None}
    P, Det, F = D.PhaseState, D.Detector, D.Fleet
    saved = [(c, n, c.__dict__[n]) for c, n in ((P, "_close"), (P, "_pair"), (P, "_declare"),
                                                 (Det, "_combine"), (F, "process"))]
    close0, pair0, declare0, combine0, process0 = P._close, P._pair, P._declare, Det._combine, F.process

    def close(st, o, at, watts, var=None, direct=False):
        s = close0(st, o, at, watts, var, direct)
        f = sys._getframe(1)
        name = f.f_code.co_name
        kind = ("observed" if direct else "too big" if name == "_unseen_stop" and "out" in f.f_locals
                else KINDS.get(name, name))
        s.closes = [(kind, o.watts, watts)]
        log["closes"].append((st, o, kind, o.watts, watts))
        return s

    def pair(st, at, watts, var, new_level):
        out = pair0(st, at, watts, var, new_level)
        lost = not out and (not st.open_edges or bool(st.held_drops) and st.held_drops[-1][:2] == (at, watts))
        log["falls"].append((st, lost))
        return out

    def declare(st, since, step, *a, **kw):
        out = declare0(st, since, step, *a, **kw)
        if step > 0:
            log["rises"].append((st, st.open_edges[-1]))
        return out

    def combine(g):
        s = combine0(g)
        if len(g) > 1:
            s.closes = [c for m in g for c in getattr(m, "closes", ())]
        return s

    def process(fleet, *a, **kw):
        log["fleet"] = fleet
        log["bounds"].append(kw["now_ts"] if "now_ts" in kw else a[4])
        return process0(fleet, *a, **kw)

    P._close, P._pair, P._declare, F.process = close, pair, declare, process
    Det._combine = staticmethod(combine)

    def undo():
        for c, n, v in saved:
            setattr(c, n, v)
    return log, undo


def _close_stats(closes: list) -> dict:
    """Of these closes (kind, start W, stop W): the share each kind closed,
    and how near each OBSERVED stop came to its start's size - an inferred one
    is handed a size, so it measures nothing."""
    n = len(closes)
    err = sorted(abs(b - a) / max(abs(a), 1e-9) for k, a, b in closes if k == "observed")
    share = (lambda x: x / len(err)) if err else (lambda x: None)
    return {"closes": n, "kinds": {k: c / n for k, c in collections.Counter(k for k, _, _ in closes).most_common()},
            "within10": share(sum(e <= 0.1 for e in err)), "within20": share(sum(e <= 0.2 for e in err)),
            "median_err": err[len(err) // 2] if err else None}


def _pairing(log: dict, credited: dict) -> dict:
    """The house detector's pairing, site-wide and over each load's credited
    sessions - see _instrument. Sub-meters' detectors are left out."""
    main = {id(st) for st in log["fleet"].main.phases.values()} if log["fleet"] else set()
    closes = [c for c in log["closes"] if id(c[0]) in main]
    rises = [o for st, o in log["rises"] if id(st) in main]
    falls = [lost for st, lost in log["falls"] if id(st) in main]
    shut = {id(c[1]) for c in closes}
    site = _close_stats([c[2:] for c in closes])
    steps = len(rises) + len(falls)
    site.update(starts=len(rises), stops=len(falls), unpaired_starts=sum(id(o) not in shut for o in rises),
                unpaired_stops=sum(falls), steps=steps)
    return {"site": site,
            "loads": {n: _close_stats([c for s in ss for c in getattr(s, "closes", ())]) for n, ss in credited.items()}}


def energy(site: str, folder: str, dials: list) -> dict:
    """One replay scored: printed, written to OUT= if given, and returned."""
    plants = [d[6:] for d in dials if d.startswith("PLANT=")]
    gate = next((float(d[6:]) for d in dials if d.startswith("QGATE=")), 0.0)
    out_path = next((d[4:] for d in dials if d.startswith("OUT=")), None)
    tag = B._apply([d for d in dials if not d.startswith(("PLANT=", "QGATE=", "OUT="))]) + (f" QGATE={gate:g}" if gate else "")
    log, undo = _instrument()
    try:
        fleet, filed, _ = B._run(folder, site, planted(plants))
    finally:
        undo()
    det = fleet.main
    devices, rest = measured(folder, site)
    tables = _tables(site)
    truth, floors = {}, {}
    for name, rows in devices.items():
        truth[name], w, wh = _truth(rows)
        floors[name] = (w, wh / 1000.0)
    times = {name: [r[0] for r in rows] for name, rows in devices.items()}
    for name, watts, _, ivs in PLANTS:
        truth[name] = watts * sum(b - a for a, b in ivs) / 3600.0
        floors[name] = (0.0, 0.0)
        for names in tables.values():
            names.append(name)
    by_id = {x.id: x for x in det.signatures}

    def sid_of(s):
        # the signature it stands in now, merges followed; one since evicted
        # still counts - its runs were detected and their energy written
        cur = det._current(s.signature_id) if s.signature_id is not None else None
        return cur if cur in by_id else f"gone {s.signature_id}"
    sids = [sid_of(s) for s in filed]
    per_sig: dict = {}
    credited = collections.defaultdict(list)
    gated = 0.0
    for s, sid in zip(filed, sids):
        if s.energy_wh <= 0:
            continue
        if s.quality < gate:
            gated += s.energy_wh
            continue
        row = per_sig.setdefault(sid, {"total": 0.0, "n": 0})
        row["total"] += s.energy_wh
        row["n"] += 1
        for name, rows in devices.items():
            if not _started_with(rows, times[name], s):
                continue
            got_wh = _above(rows, times[name], s.start, s.end, floors[name][0])
            if got_wh > 0:
                row[name] = row.get(name, 0.0) + min(got_wh, s.energy_wh)
                credited[name].append(s)
        for name, watts, _, ivs in PLANTS:
            ov = _overlap(ivs, s.start, s.end)
            if ov > 0:
                row[name] = row.get(name, 0.0) + min(watts * ov / 3600.0, s.energy_wh)
                credited[name].append(s)
    res = {"site": site, "folder": folder, "dials": tag, "mode": B.SUBS,
           "detected_kwh": sum(r["total"] for r in per_sig.values()) / 1000.0, "gated_kwh": gated / 1000.0,
           "signatures": len(per_sig), "tables": {}, "owners": {},
           "rest": None if rest is None else dict(zip(("main_kwh", "meters_kwh", "rest_kwh"), rest)),
           "pairing": _pairing(log, credited)}
    for k, names in tables.items():
        res["tables"][k], res["owners"][k] = _table(per_sig, truth, floors, names)
    own = res["owners"]["devices"]
    res["sessions"] = [[s.phases, round(s.start, 3), round(s.end, 3), round(s.energy_wh, 3), sid, own.get(sid)]
                       for s, sid in zip(filed, sids)]
    res["bounds"] = sorted(log["bounds"])
    _print_run(res, per_sig, by_id)
    if out_path:
        res["owners"] = {k: {str(sid): n for sid, n in v.items()} for k, v in res["owners"].items()}
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(res, f)
    return res


def _pct(v, signed: bool = False) -> str:
    return "-" if v is None else (f"{100 * v:+.1f}" if signed else f"{100 * v:.1f}")


def _kinds(st: dict) -> str:
    return ", ".join(f"{k} {100 * v:.1f} %" for k, v in st["kinds"].items())


def _print_run(res: dict, per_sig: dict, by_id: dict) -> None:
    total = res["detected_kwh"] * 1000.0
    print(f"  {res['dials']}   {res['signatures']} signatures with energy, {res['detected_kwh']:.0f} kWh detected in all"
          + (f", {res['gated_kwh']:.0f} kWh more left unattributed below the quality gate" if res["gated_kwh"] else ""))
    # the signatures holding most of the detected energy: a blob here is
    # energy no name will ever fit
    for sid, row in sorted(per_sig.items(), key=lambda kv: -kv[1]["total"])[:5]:
        sig = by_id.get(sid)
        what = (f"{sig.phases.upper()} {sum(sig.power.values()):.0f} W ~{sig.duration_s/60:.0f} min" if sig is not None else "evicted")
        devs = " ".join(f"{k}:{v/row['total']:.0%}" for k, v in sorted(row.items(), key=lambda kv: -kv[1] if isinstance(kv[1], float) else 0)
                        if k not in ("total", "n") and v >= 0.02 * row["total"])
        print(f"     #{sid}: {row['n']} runs, {row['total']/1000:.1f} kWh ({row['total']/max(total, 1e-9):.0%}), {what}  {devs}")
    for k, tab in res["tables"].items():
        print(f"  {k}: {'load':20s} {'truth kWh':>9s} {'floor kWh':>9s} {'attrib kWh':>10s} {'right kWh':>9s} "
              f"{'capture':>7s} {'impurity':>8s}  signatures (id, runs, kWh, share)   holds most")
        for name, g in sorted(tab["loads"].items(), key=lambda kv: -kv[1]["truth_kwh"]):
            sigs = " ".join(f"#{sid}:{n}x{kwh}kWh@{sh:.0%}" for sid, n, kwh, sh in g["sigs"][:4])
            # ...and where the rest of its energy went: the signatures holding
            # most of it, whether or not it is their majority
            went = sorted(((row.get(name, 0.0), sid, row) for sid, row in per_sig.items() if row.get(name, 0.0) > 0),
                          key=lambda x: x[0], reverse=True)[:3]   # ids mix numbers and "gone N"
            went_s = " ".join(f"#{sid}:{e/1000:.2f}kWh={e/row['total']:.0%}of{row['n']}" for e, sid, row in went)
            print(f"  {'':{len(k) + 1}s} {name:20s} {g['truth_kwh']:9.2f} {g['floor_kwh']:9.2f} {g['attributed_kwh']:10.2f} "
                  f"{g['correct_kwh']:9.2f} {_pct(g['capture']):>7s} {_pct(g['impurity']):>8s}  {sigs}   {went_s}")
        print(f"  {'':{len(k) + 1}s} {'all':20s} {tab['truth_kwh']:9.2f} {'':9s} {tab['attributed_kwh']:10.2f} "
              f"{tab['attributed_kwh'] - tab['wrong_kwh']:9.2f} {_pct(tab['capture']):>7s} {_pct(tab['impurity']):>8s}"
              f"   capture weighted by energy, impurity pooled")
    if res["rest"]:
        r = res["rest"]
        print(f"  {REST[res['site']]} = main {r['main_kwh']:.2f} kWh - meters {r['meters_kwh']:.2f} kWh = "
              f"{r['main_kwh'] - r['meters_kwh']:.2f} kWh; its series integrates to {r['rest_kwh']:.2f} kWh")
    p = res["pairing"]["site"]
    print(f"  pairing: {p['closes']} runs closed - {_kinds(p)}")
    print(f"           observed stops within 10 % of their start's size {_pct(p['within10'])} %, within 20 % "
          f"{_pct(p['within20'])} %, median off {_pct(p['median_err'])} %; never closed: {p['unpaired_starts']} of "
          f"{p['starts']} starts, closed nothing: {p['unpaired_stops']} of {p['stops']} stops "
          f"({_pct(p['unpaired_starts'] / max(p['steps'], 1))} / {_pct(p['unpaired_stops'] / max(p['steps'], 1))} % of all steps)")
    for name, st in sorted(res["pairing"]["loads"].items(), key=lambda kv: -kv[1]["closes"]):
        print(f"    {name:20s} {st['closes']:6d} closes, within 10/20 % {_pct(st['within10'])}/{_pct(st['within20'])}, "
              f"median off {_pct(st['median_err'])} % - {_kinds(st)}")


# ------------------------------------------------------------- invariance
def _near(t: float, bounds: list):
    """t less the nearest pass boundary (positive: after it), or None."""
    i = bisect.bisect_left(bounds, t)
    return min((t - b for b in bounds[max(i - 1, 0):i + 1]), key=abs, default=None)


def compare(ref: list, other: list, bounds: list) -> list:
    """Every session (phases, start, end, Wh, signature, its owner) of ``ref``
    - the reference, one call - and of ``other``, a slicing of the same
    history, that differs: missing from other, extra in it, or matched by its
    start (the same, or within MOVED_S on the same phases) with another start,
    end, size or signature. A signature of ``ref`` is "the same" as the one of
    ``other`` most of its matched sessions went to. Biggest first, each with
    how far it lies from the nearest of ``other``'s pass boundaries."""
    by_key = collections.defaultdict(list)
    for y in other:
        by_key[(y[0], y[1])].append(y)
    pairs, missing = [], []
    for x in ref:
        got = by_key.get((x[0], x[1]))
        if got:
            pairs.append((x, got.pop(0)))
        else:
            missing.append(x)
    pool = collections.defaultdict(list)
    for ys in by_key.values():
        for y in ys:
            pool[y[0]].append(y)
    still = []
    for x in missing:
        ys = pool.get(x[0], [])
        y = min((y for y in ys if abs(y[1] - x[1]) <= MOVED_S), key=lambda y: abs(y[1] - x[1]), default=None)
        if y is None:
            still.append(x)
        else:
            ys.remove(y)
            pairs.append((x, y))
    votes = collections.defaultdict(collections.Counter)
    for x, y in pairs:
        votes[x[4]][y[4]] += 1
    twin = {sid: c.most_common(1)[0][0] for sid, c in votes.items()}

    def diff(x, y, what):
        ts = [t for s in (x, y) if s for t in (s[1], s[2])]
        near = min((d for d in (_near(t, bounds) for t in ts) if d is not None), key=abs, default=None)
        return {"kind": what[0][0], "what": "; ".join(w for _, w in what), "ref": x, "other": y,
                "kwh": max(s[3] for s in (x, y) if s) / 1000.0, "near_s": near}
    out = []
    for x, y in pairs:
        what = []                                  # (kind, what), the first the session's kind
        if x[1] != y[1]:
            what.append(("start", f"start {y[1] - x[1]:+.1f} s"))
        if x[2] != y[2]:
            what.append(("end", f"end {y[2] - x[2]:+.1f} s"))
        if abs(x[3] - y[3]) > 0.01 + 1e-4 * abs(x[3]):
            what.append(("size", f"{x[3]:.1f} -> {y[3]:.1f} Wh"))
        if twin[x[4]] != y[4]:
            what.append(("signature", f"signature -> #{y[4]} ({y[5] or '-'}), where most of #{x[4]}'s went to #{twin[x[4]]}"))
        if what:
            out.append(diff(x, y, what))
    out += [diff(x, None, [("missing", "")]) for x in still]
    out += [diff(None, y, [("extra", "")]) for ys in pool.values() for y in ys]
    return sorted(out, key=lambda d: -d["kwh"])


def _summary(diffs: list, n: int) -> dict:
    kinds = collections.Counter(d["kind"] for d in diffs)
    return {"sessions": n, "differ": len(diffs), "kwh": sum(d["kwh"] for d in diffs),
            "kinds": {k: [c, sum(d["kwh"] for d in diffs if d["kind"] == k)] for k, c in kinds.most_common()}}


def _worklist(title: str, diffs: list, n: int) -> list:
    sm = _summary(diffs, n)
    out = [f"{title}: {sm['differ']} of {n} sessions differ, {sm['kwh']:.2f} kWh",
           "   " + ", ".join(f"{k} {c} ({e:.2f} kWh)" for k, (c, e) in sm["kinds"].items()),
           f"   {'kind':9s} {'start (local)':14s} {'ph':3s} {'kWh':>7s} {'W':>6s} {'min':>6s}  "
           f"{'signature (owner)':26s} {'pass boundary':>13s}  what differs"]
    for d in diffs:
        s = d["ref"] or d["other"]
        when = datetime.fromtimestamp(s[1]).strftime("%m-%d %H:%M:%S")
        dur = s[2] - s[1]
        sig = f"#{s[4]}" + (f" ({s[5]})" if s[5] else "")
        near = "-" if d["near_s"] is None else f"{d['near_s']:+.0f} s"
        out.append(f"   {d['kind']:9s} {when:14s} {s[0].upper():3s} {d['kwh']:7.3f} {s[3] * 3600 / max(dur, 1e-9):6.0f} "
                   f"{dur / 60:6.1f}  {sig:26s} {near:>13s}  {d['what']}")
    return out


# ------------------------------------------------------------- card
def _stamp() -> tuple:
    """(commit, UTC time) to name a result by."""
    commit = subprocess.run(["git", "describe", "--always", "--dirty"], cwd=ROOT, capture_output=True,
                            text=True).stdout.strip() or "nogit"
    return commit, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _folder(site: str) -> str:
    return str(ROOT / "data" / "history" / site.split("-")[0])


def _parallel(jobs: list) -> list:
    """Each job (site, folder, dials) replayed by this file in a process of
    its own, PARALLEL at once, one hash seed for all - a difference between
    them is never the seed's; their results, in order."""
    with tempfile.TemporaryDirectory() as tmp:
        def one(k):
            site, folder, dials = jobs[k]
            out = f"{tmp}/{k}.json"
            p = subprocess.run([sys.executable, __file__, site, folder, *dials, f"OUT={out}"],
                               capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": "0"})
            if p.returncode:
                raise RuntimeError(f"{site} {' '.join(dials)} failed:\n{p.stdout[-2000:]}{p.stderr[-2000:]}")
            with open(out, encoding="utf-8") as f:
                return json.load(f)
        with ThreadPoolExecutor(PARALLEL) as pool:
            return list(pool.map(one, range(len(jobs))))


def _site_dials(site: str) -> list:
    return ["HOUSE=prod"] if site == "home" else []   # always at Home - see bench.py


def _fig(vals: list, signed: bool = False) -> str:
    """One figure where the slicings agree, each one's (0|6|L1) where not."""
    s = [_pct(v, signed) for v in vals]
    return s[0] if len(set(s)) == 1 else "|".join(s)


def card(args: list) -> None:
    sites = [a for a in args if "=" not in a] or ["home", "kozolec", "home-hisa", "home-mansarda"]
    dials = [a for a in args if "=" in a]
    keys = sorted([(site, mode, sl) for site in sites for mode in MODES
                   if mode != "circuits" or B.PROD_PARENTS.get(site) for sl in SLICINGS],
                  key=lambda k: (k[2] != "0", k[0] != "home"))   # the slowest first: Home in one call, ~6 min
    t0 = time.time()
    got = dict(zip(keys, _parallel([(site, _folder(site), [MODES[mode]] + SLICINGS[sl] + _site_dials(site) + dials)
                                    for site, mode, sl in keys])))
    took = time.time() - t0
    commit, utc = _stamp()
    inv: dict = {}
    for site, mode, sl in keys:
        if sl != "0":
            ref, res = got[(site, mode, "0")], got[(site, mode, sl)]
            inv.setdefault(f"{site}|{mode}", {})[sl] = _summary(compare(ref["sessions"], res["sessions"], res["bounds"]),
                                                               len(ref["sessions"]))
    lines = [f"scorecard {commit} {utc}  {' '.join(dials) or 'defaults'}  - {len(keys)} replays in {took:.0f} s",
             "capture / impurity %, one figure where SLICE=0, 6 and LIVE=1 agree, each one's (0|6|L1) where not"]
    for site in sites:
        modes = [m for m in MODES if (site, m, "0") in got]
        first = got[(site, modes[0], "0")]
        for tname in first["tables"]:
            names = sorted(first["tables"][tname]["loads"], key=lambda n: -first["tables"][tname]["loads"][n]["truth_kwh"])
            lines.append("")
            lines.append(f"{site} - {tname} (truth, floor kWh)".ljust(48) + "".join(f" {m + ' capture':>17s} {'impurity':>15s}" for m in modes))
            for name in names + ["all"]:
                t = first["tables"][tname]["loads"].get(name)
                row = (f"  {name:20s} {(t or first['tables'][tname])['truth_kwh']:7.2f} kWh"
                       + (f", floor {t['floor_kwh']:6.2f}" if t else " " * 14))
                for m in modes:
                    vals = []
                    for metric in ("capture", "impurity"):
                        v = [(got[(site, m, sl)]["tables"][tname]["loads"].get(name) or {}).get(metric)
                             if t else got[(site, m, sl)]["tables"][tname][metric] for sl in SLICINGS]
                        vals.append(_fig(v))
                    row += f" {vals[0]:>17s} {vals[1]:>15s}"
                lines.append(row)
        if first["rest"]:
            r = first["rest"]
            lines.append(f"  {REST[site]} = main {r['main_kwh']:.2f} - meters {r['meters_kwh']:.2f} = "
                         f"{r['main_kwh'] - r['meters_kwh']:.2f} kWh; its series integrates to {r['rest_kwh']:.2f} kWh")
        lines.append("  invariance (sessions / kWh differing from SLICE=0): " + "   ".join(
            f"{m}: " + ", ".join(f"{sl} {inv[f'{site}|{m}'][sl]['differ']}/{inv[f'{site}|{m}'][sl]['kwh']:.1f}"
                                 for sl in SLICINGS if sl != "0") for m in modes))
        lines.append("  pairing at SLICE=6 - closes, observed stops within 10/20 % of their start, median off; never closed / closed nothing, % of steps")
        for m in modes:
            p = got[(site, m, "6")]["pairing"]
            s = p["site"]
            lines.append(f"    {m:9s} {s['closes']:6d} closes, {_pct(s['within10'])}/{_pct(s['within20'])}, "
                         f"{_pct(s['median_err'])} %; starts {_pct(s['unpaired_starts'] / max(s['steps'], 1))}, stops "
                         f"{_pct(s['unpaired_stops'] / max(s['steps'], 1))}  - {_kinds(s)}")
            for name in sorted(p["loads"], key=lambda n: -p["loads"][n]["closes"]):
                st = p["loads"][name]
                lines.append(f"      {name:20s} {st['closes']:6d}, {_pct(st['within10'])}/{_pct(st['within20'])}, "
                             f"{_pct(st['median_err'])} % - {_kinds(st)}")
    print("\n".join(lines))
    out = ROOT / "data" / "scorecard" / f"{commit}-{utc}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    keep = {f"{s}|{m}|{sl}": {k: v for k, v in r.items() if k not in ("sessions", "bounds", "owners")}
            for (s, m, sl), r in got.items()}
    out.write_text(json.dumps({"commit": commit, "utc": utc, "dials": dials, "seconds": took,
                               "runs": keep, "invariance": inv}, indent=1), encoding="utf-8")
    print(f"\nwritten {out}")


def invariance(site: str, folder: str, dials: list) -> None:
    subs = next((d for d in dials if d.startswith("SUBS=")), MODES["fed"])
    mode = next((m for m, d in MODES.items() if d == subs), subs)
    rest = [d for d in dials if not d.startswith("SUBS=")]
    t0 = time.time()
    runs = _parallel([(site, folder, [subs] + SLICINGS[sl] + _site_dials(site) + rest) for sl in SLICINGS])
    commit, utc = _stamp()
    ref = runs[0]
    lines = [f"invariance {site} {mode} {commit} {utc}  {' '.join(dials) or 'defaults'}  - {time.time() - t0:.0f} s; "
             "pass boundary = the session's start or end less the nearest of the slicing's pass ends"]
    for sl, res in zip(list(SLICINGS)[1:], runs[1:]):
        lines.append("")
        lines += _worklist(f"SLICE=0 (one call, the reference) vs {' '.join(SLICINGS[sl])}",
                           compare(ref["sessions"], res["sessions"], res["bounds"]), len(ref["sessions"]))
    print("\n".join(lines))
    out = ROOT / "data" / "scorecard" / f"invariance-{site}-{mode}-{commit}-{utc}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwritten {out}")


def diff(a_path: str, b_path: str) -> None:
    """Capture and impurity load by load, A -> B and the change."""
    A, Bj = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (a_path, b_path))
    print(f"{A['commit']} {A['utc']} -> {Bj['commit']} {Bj['utc']}   capture / impurity %, (0|6|L1) where the slicings differ")
    groups = sorted({k.rsplit("|", 1)[0] for k in A["runs"]} & {k.rsplit("|", 1)[0] for k in Bj["runs"]})
    for g in groups:
        sls = [sl for sl in SLICINGS if f"{g}|{sl}" in A["runs"] and f"{g}|{sl}" in Bj["runs"]]
        for tname in A["runs"][f"{g}|{sls[0]}"]["tables"]:
            print(f"{g} - {tname}")
            names = sorted(set(A["runs"][f"{g}|{sls[0]}"]["tables"][tname]["loads"])
                           | set(Bj["runs"][f"{g}|{sls[0]}"]["tables"][tname]["loads"]))
            for name in names + ["all"]:
                row = f"  {name:20s}"
                for metric in ("capture", "impurity"):
                    va, vb = [[(r["runs"][f"{g}|{sl}"]["tables"][tname]["loads"].get(name) or {}).get(metric)
                               if name != "all" else r["runs"][f"{g}|{sl}"]["tables"][tname][metric] for sl in sls]
                              for r in (A, Bj)]
                    dv = [None if a is None or b is None else b - a for a, b in zip(va, vb)]
                    row += f"  {metric} {_fig(va):>9s} -> {_fig(vb):>9s} ({_fig(dv, True)})"
                print(row)
        ia, ib = A["invariance"].get(g, {}), Bj["invariance"].get(g, {})
        print("  invariance " + ", ".join(f"{sl}: {ia[sl]['differ']}/{ia[sl]['kwh']:.1f} -> {ib[sl]['differ']}/{ib[sl]['kwh']:.1f}"
                                          for sl in ia if sl in ib) + " sessions/kWh")


def check() -> None:
    """python3 tests/energy_bench.py check"""
    rows = [(0.0, 5.0), (100.0, 3005.0), (200.0, 5.0)]
    t = [r[0] for r in rows]
    assert round(_above(rows, t, 0, 300, 5.0), 2) == 83.33          # 3 kW above a 5 W floor for 100 s
    assert round(_above(rows, t, 120, 140, 5.0), 2) == 16.67        # ...of which 20 s
    assert _above(rows, t, 250, 300, 5.0) == 0.0                     # idle: nothing
    class S:                                                          # a run of 3 kW from 100 s
        start, levels = 100.0, {"a": [(100.0, 3000.0)]}
    assert _started_with(rows, t, S)
    S.start = 150.0                                                   # a run beside it, started later
    assert not _started_with(rows, t, S)
    pump = [(0.0, 0.5), (100.0, 1100.0), (110.0, 880.0), (150.0, 0.5)]  # on 50 s: off again a minute on
    S.start, S.levels = 101.0, {"a": [(101.0, 900.0)]}
    assert _started_with(pump, [r[0] for r in pump], S)
    # the floor: 5 W for 900 s, 1005 W for 100 s - the level of a tenth of the time
    wh, floor, floor_wh = _truth([(0.0, 5.0), (900.0, 1005.0), (1000.0, 5.0)])
    assert floor == 5.0 and round(wh, 2) == 27.78 and round(floor_wh, 2) == 1.39
    # Anze's boiler: 10 kWh used, its signature 9.5 kWh of which 9 its own -
    # capture 90 %, impurity 5.3 %; a signature nobody holds half of is nobody's
    per_sig = {1: {"total": 9500.0, "n": 40, "Boiler": 9000.0, "Pump": 300.0},
               2: {"total": 1000.0, "n": 5, "Boiler": 400.0, "Pump": 450.0}}
    tab, owner = _table(per_sig, {"Boiler": 10000.0, "Pump": 2000.0}, {}, ["Boiler", "Pump"])
    b = tab["loads"]["Boiler"]
    assert owner == {1: "Boiler"} and round(b["capture"], 3) == 0.9 and round(b["impurity"], 3) == 0.053
    assert tab["loads"]["Pump"]["capture"] == 0.0 and tab["loads"]["Pump"]["impurity"] is None
    assert round(tab["capture"], 2) == 0.75 and round(tab["impurity"], 3) == 0.053   # 9 of 12; 0.5 of 9.5
    assert _own(per_sig, ["Pump"]) == {}          # a truth set without the boiler: still nobody's
    # the remainder: the main less its meters, each held; one not heard from yet is nothing
    rest = _remainder([[(0.0, 100.0), (10.0, 300.0), (20.0, 100.0), (30.0, 100.0)]],
                      [[(0.0, 50.0), (15.0, 150.0)], [(5.0, 10.0)]])
    assert rest == [(0.0, 50.0), (5.0, 40.0), (10.0, 240.0), (15.0, 140.0), (20.0, -60.0), (30.0, -60.0)]
    assert round(_held_wh([(0.0, 100.0), (10.0, 300.0)], 5.0, 20.0) * 3600) == 3500
    # invariance: one the same (its signature renumbered), another end, one
    # missing, one moved, one extra; the boundary 10 s after the second start
    ref = [["a", 0.0, 60.0, 10.0, 1, None], ["a", 100.0, 160.0, 10.0, 1, None], ["b", 200.0, 260.0, 5.0, 2, None],
           ["a", 300.0, 360.0, 10.0, 1, None]]
    oth = [["a", 0.0, 60.0, 10.0, 7, None], ["a", 100.0, 166.0, 10.0, 7, None], ["a", 306.0, 360.0, 10.0, 7, None],
           ["c", 400.0, 460.0, 3.0, 8, None]]
    got = {d["kind"]: d for d in compare(ref, oth, [110.0])}
    assert set(got) == {"end", "missing", "start", "extra"} and got["end"]["near_s"] == -10.0
    print("ok")


if __name__ == "__main__":
    cmd = sys.argv[1:2]
    if cmd == ["check"]:
        check()
    elif cmd == ["card"]:
        card(sys.argv[2:])
    elif cmd == ["invariance"] and len(sys.argv) >= 4:
        invariance(sys.argv[2], sys.argv[3], sys.argv[4:])
    elif cmd == ["diff"] and len(sys.argv) == 4:
        diff(sys.argv[2], sys.argv[3])
    elif len(sys.argv) >= 3 and cmd[0] not in ("card", "invariance", "diff"):
        energy(sys.argv[1], sys.argv[2], sys.argv[3:])
    else:
        print(__doc__)
        raise SystemExit(1)
