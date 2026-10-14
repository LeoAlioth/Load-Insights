"""The cross-meter edges' scorecard (docs/specs/2026-10-14-cross-meter-edges.md,
insights/edges.py): no devices, every fed meter's own steps assembled into
edges beside the detector, which is unchanged.

    python3.14 tests/edge_bench.py card [SITE ...] [DIAL=value ...] [LABEL=x]
    python3.14 tests/edge_bench.py diff A.json B.json
    python3.14 tests/edge_bench.py SITE FOLDER [DIAL=value ...] [OUT=run.json]

card    Home, Kozolec and Andrej's site (or the SITEs given) with every meter
        production reads fed (SUBS=prod, and HOUSE=prod where the house is
        built), each replayed in one call (SLICE=0), in 6-hour slices (SLICE=6)
        and with the last day in 1-minute passes (LIVE=1), BENCH_PARALLEL at
        once (8). Printed, and written to
        data/scorecard/edges-<commit>[-<label>]-<utc>.json with each site's
        worklists in edges-...-worklist.txt beside it. The figures are the
        SLICE=6 replay's:
          1 conservation - per meter pair parent>child, the share of moments
            where the parent's step is its children's (within both meters'
            noise and METER_CAL_SLACK), a parent that read nothing counted
            as 0 - of the moments whose children's step is clear of the
            meters' idle noises added, the noise-level ones counted apart; the worst ones
            are the worklist
          2 coverage - per meter, its entries unknown (not read yet) and 0 on
            silence alone; per place, its edges where a meter above read 0
            though the step was clear of its idle noise and the place's
            added ("blind")
          3 invariance - edges SLICE=0 and LIVE=1 assemble differently from
            SLICE=6 (place, direction, pattern within 2 s, non-zero entries)
          4-8 the groups' tightness, mixed, duplicates, count and mirror, beside
            the same figures for today's grid edges - the main detector's
            classified steps by their EdgeCluster, sized as today (raw watts)
diff    two card files, figure by figure.

The steps are each detector's as it classified them (Detector._classify_step:
falls at once, rises when their event formed), whole as the channel declared
them (PhaseState.declared - a fall split between meters is one step), each
with its meter's gain against the grid as the fleet had learned it then
(Fleet.gain: as of its own moment on the fleet's one clock, so a gain learned
later never reaches an edge already decided, whatever the slicing); the
readings, reactive powers, voltages, each meter's phase map, latency, noise and how far it has
read are handed over after every pass (Fleet.process).
"""
from __future__ import annotations

import bisect
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench as B  # noqa: E402
import energy_bench as EB  # noqa: E402
from _load import load  # noqa: E402

D = B.D
E = load("insights.edges")
ROOT = EB.ROOT
SLICINGS = {"0": ["SLICE=0"], "6": ["SLICE=6"], "L1": ["LIVE=1"]}
PAIRS = [("6", "0"), ("6", "L1")]
PARALLEL = EB.PARALLEL
WORST = 15                     # worklist lines per meter pair (conservation) or pair of slicings (invariance)
TOP_GROUPS = 40                # group summaries kept per site, the largest


def _frontier(det, name: str, asm) -> float:
    """The earliest step ``det`` may still declare: a rise waiting for its
    event, a reading pending on a channel, else past its last reading - and a
    channel fed but not read yet (the grid's held behind its horizon) not
    before its first row."""
    ts = [p["since"] for p in det._pending]
    for c, st in det.phases.items():
        if st.pending:
            ts.append(st.pending[0][0])
        elif st.last_ts is not None:
            ts.append(st.last_ts)
        elif (name, c) in asm.first:
            ts.append(asm.first[(name, c)])
    return min(ts, default=math.inf)


def _instrument(asm):
    """Capture every detector's classified steps and hand each pass to the
    assembler; returns (today's grid steps, undo)."""
    st = {"fleet": None, "names": {}, "pass": [], "seen": {}, "calls": 0}
    today, cur = [], {}
    process, classify, density = D.Fleet.process, D.Detector._classify_step, D.Detector._by_density

    def by_density(self, ph, up, since, size, keyed, kind, angle=None, where=""):
        cur.update(size=size, angle=angle)
        return density(self, ph, up, since, size, keyed, kind, angle, where)

    def classify_step(self, ph, since, watts, var, surge, window_ok=False, legs=None):
        cur.clear()
        c = classify(self, ph, since, watts, var, surge, window_ok, legs)
        fleet = st["fleet"]
        name = st["names"].get(id(self))
        if name is None:
            st["names"] = {id(d): n for n, d in fleet.detectors()}
            name = st["names"].get(id(self))
        if name is None:
            return c
        st["calls"] += 1
        mp = fleet.phase_map(name) if name else {}
        for lph, lsince, lw in legs or [(ph, since, watts)]:
            key = (name, lph, round(lsince, 3))
            if key in st["seen"]:
                if legs:
                    st["seen"][key].event = st["calls"]      # a rise's split share first, its event after
                continue
            p = self.phases[lph]
            d = p.declared_at(lsince)
            dp, dq, a, b = (d[1], d[2], d[3], d[4]) if d else (lw, var if len(ph) == 1 else None, lsince, lsince)
            b += fleet._lead(p) if name else 0.0         # its span on the grid's clock - see LAG_PERCENTILE
            s = E.Step(meter=name, ch=lph, t=lsince, dp=dp, dq=dq, a=a, b=b,
                       phase=mp.get(lph) if name else lph, window=self.event_window(), interval=p.interval,
                       noise=p.noise_at(), floor=p.noise, event=st["calls"] if legs else None,
                       gain=fleet.gain(name, "p") if name else 1.0)
            st["seen"][key] = s
            st["pass"].append(s)
        if not name:
            today.append({"cid": c.id, "pattern": ph, "up": watts > 0, "where": c.where, "t": since,
                          "size": cur.get("size", abs(watts)), "angle": cur.get("angle"),
                          "legs": [(lph, round(ls, 3), abs(lw)) for lph, ls, lw in legs or [(ph, since, watts)]]})
        return c

    def fleet_process(self, main_samples, sub_samples, *a, **kw):
        if st["fleet"] is None:
            st["fleet"] = self
            asm.parents = {n: p for n, p in self.parents.items() if p}
        out = process(self, main_samples, sub_samples, *a, **kw)
        if self is not st["fleet"]:
            return out
        now = a[2] if len(a) > 2 else kw.get("now_ts")
        rows = {("", p): list(r) for p, r in (main_samples or {}).items()}
        rows.update({(n, c): list(r) for n, byc in (sub_samples or {}).items() for c, r in byc.items()})
        volts = {(n, c): list(r) for n, byc in (kw.get("volts") or {}).items() for c, r in byc.items()}
        # the reactive power the replay hands over whole every pass ({channel: {t: var}}): this pass's part
        main_q, sub_q = (a[0] if a else kw.get("main_q")), (a[1] if len(a) > 1 else kw.get("sub_q"))
        lo, hi = st.get("qnow", -math.inf), now if now is not None else math.inf
        qrows = {k: sorted((t, v) for t, v in byt.items() if lo < t <= hi)
                 for k, byt in [(("", p), d) for p, d in (main_q or {}).items()]
                 + [((n, c), d) for n, byc in (sub_q or {}).items() for c, d in byc.items()]}
        st["qnow"] = hi
        dets = self.detectors()
        read = {n: [c for c, p in d.phases.items() if p.last_ts is not None] for n, d in dets}
        asm.feed(st["pass"], rows, volts, qrows=qrows,
                 frontier={n: _frontier(d, n, asm) for n, d in dets},
                 reach={n: self._latency(n) + d.event_window() for n, d in dets}, now=now,
                 chans={n: {c: (c if not n else self.phase_map(n).get(c)) for c in read[n]} for n, _ in dets},
                 noise={(n, c): d.phases[c].noise_at() for n, d in dets for c in read[n]},
                 floor={(n, c): d.phases[c].noise for n, d in dets for c in read[n]},
                 latency={n: self._latency(n) for n, _ in dets})
        st["pass"] = []
        st["undecided"] = max(st.get("undecided", 0), len(asm.steps))
        return out

    D.Fleet.process, D.Detector._classify_step, D.Detector._by_density = fleet_process, classify_step, by_density

    def undo():
        D.Fleet.process, D.Detector._classify_step, D.Detector._by_density = process, classify, density
    return today, st, undo


def _today_groups(today: list, edges: list, fleet) -> list:
    """Today's grid edges as groups: each classified step in its EdgeCluster,
    its size as the detector sized it, its angle a load's (a fall's sign
    flipped), its properties its legs' (the grid's entries), by size."""
    props = {}
    for e in edges:
        for k, v in e["entries"].items():
            if isinstance(v, dict) and k.startswith("|"):
                props[(k[1:], round(v["t"], 3))] = v
    by = {}
    for s in today:
        item = {"size": s["size"], "t": s["t"],
                "err": sum((props.get((ph, ls)) or {}).get("noise") or 0.0 for ph, ls, _ in s["legs"]),
                "angle": None if s["angle"] is None else (s["angle"] if s["up"] else -s["angle"])}
        for p in E.PROPS:
            if p == "angle":
                continue
            got = [(w, (props.get((ph, ls)) or {}).get(p)) for ph, ls, w in s["legs"]]
            got = [(w, v) for w, v in got if v is not None]
            item[p] = sum(w * v for w, v in got) / sum(w for w, _ in got) if got else None
        by.setdefault(s["cid"], (s, []))[1].append(item)
    out = []
    for cid, (s, items) in sorted(by.items()):
        unit = fleet.main.edge_unit.get(D._group(s["pattern"], s["up"], s["where"])) or D.EDGE_NOISE_SHARE * D.MIN_NOISE_W
        out.append({"key": (s["where"], 1 if s["up"] else -1, s["pattern"]), "unit": unit, "items": items})
    return out


OFF_REACH_S = 120.0            # how far from a failing moment the parent's own step of the children's size is looked for


def _lateness(cons: list, steps) -> dict:
    """Of the moments that do not add up, those where the parent did declare a
    step of the children's size (within the tolerance) on their phases, only
    not over their moment: its offset ("off", parent less child, seconds) set
    on the record - a meter's clock against its parent's, not a meter at
    fault. Per meter pair [failing, of them so found, the median offset, and
    the median ratio parent / children over every moment both stepped the
    same way], both in the grid's terms (Step.gain) - the ratio the
    calibration the learned gains leave."""
    by = {}
    for s in steps:
        by.setdefault(s.meter, []).append(s)
    for v in by.values():
        v.sort(key=lambda s: s.t)
    out = {}
    for c in cons:
        if c["ok"] or not c["clear"]:
            continue
        mine = by.get(c["parent"], [])
        i = bisect.bisect_left([s.t for s in mine], c["at"] - OFF_REACH_S) if mine else 0
        near = [s for s in mine[i:] if s.t <= c["at"] + OFF_REACH_S and abs(s.grid - c["c"]) <= c["tol"]
                and (c["phases"] is None or s.phase is None or set(s.phase) & set(c["phases"]))]
        if near:
            c["off"] = min((s.t - c["at"] for s in near), key=abs)
        for ch in c["children"]:
            row = out.setdefault(f"{c['parent']}>{ch}", [0, 0, []])
            row[0] += 1
            if near:
                row[1] += 1
                row[2].append(c["off"])
    ratio = {}
    for c in cons:
        if c["p"] and c["c"] and (c["p"] > 0) == (c["c"] > 0):
            for ch in c["children"]:
                ratio.setdefault(f"{c['parent']}>{ch}", []).append(c["p"] / c["c"])
    return {k: [n, f, statistics.median(o) if o else None, statistics.median(ratio[k]) if k in ratio else None]
            for k, (n, f, o) in out.items()}


def _when(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%m-%d %H:%M:%S")


def replay(site: str, folder: str, dials: list) -> dict:
    """One replay assembled and scored; written to OUT= if given."""
    out_path = next((d[4:] for d in dials if d.startswith("OUT=")), None)
    tag = B._apply([d for d in dials if not d.startswith("OUT=")])
    asm = E.Assembler()
    today, st, undo = _instrument(asm)
    t0 = time.time()
    try:
        fleet, _, _ = B._run(folder, site)
    finally:
        undo()
    asm.flush()
    edges = asm.edges
    groups = E.group(edges)
    # the worklist: the largest children's steps their parent did not show as theirs first - a
    # coincident load beside a wobble of a plug's is the parent's rest, not a meter at fault
    cons = {}
    for c in sorted((c for c in asm.cons if not c["ok"] and c["clear"]), key=lambda c: (-abs(c["c"]), c["t"])):
        pair = cons.setdefault(f"{c['parent']}>{','.join(c['children'])}", [])
        if len(pair) < WORST:
            pair.append(c)
    late = _lateness(asm.cons, st["seen"].values())
    res = {"site": site, "dials": tag, "seconds": time.time() - t0, "steps": len(st["seen"]),
           "edges": len(edges), "late": asm.late,
           "gains": {n: [fleet.gain(n, "p"), (fleet.meter_gain.get(n) or {}).get("p", [0.0, 0.0])[1]] for n in sorted(fleet.subs)}, "undecided_max": st.get("undecided", 0),
           "places": {m: sum(1 for e in edges if e["place"] == m) for m in sorted({e["place"] for e in edges})},
           "shared": sum(1 for e in edges if e["shared"]),
           "conservation": E.conservation(asm.cons), "lateness": late, "coverage": E.coverage(edges),
           "groups": E.scorecard(groups), "today": E.scorecard(_today_groups(today, edges, fleet)),
           "summaries": [E.summary(g) for g in sorted(groups, key=lambda g: -len(g["items"]))[:TOP_GROUPS]],
           "worst": {pair: [{k: c.get(k) for k in ("t", "parent", "children", "p", "c", "tol", "keys", "off")} for c in cs]
                     for pair, cs in cons.items()},
           "brief": [E.brief(e) for e in edges]}
    if out_path:
        Path(out_path).write_text(json.dumps(res, default=list), encoding="utf-8")
    return res


def _parallel(jobs: list, n: int = PARALLEL) -> list:
    """Each job (site, folder, dials) replayed by this file in a process of
    its own, ``n`` at once, one hash seed for all; their results, in order."""
    with tempfile.TemporaryDirectory() as tmp:
        def one(k):
            site, folder, dials = jobs[k]
            out = f"{tmp}/{k}.json"
            p = subprocess.run([sys.executable, __file__, site, folder, *dials, f"OUT={out}"],
                               capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": "0"})
            if p.returncode:
                raise RuntimeError(f"{site} {' '.join(dials)} failed:\n{p.stdout[-2000:]}{p.stderr[-2000:]}")
            return json.loads(Path(out).read_text(encoding="utf-8"))
        with ThreadPoolExecutor(n) as pool:
            return list(pool.map(one, range(len(jobs))))


def _pct(v) -> str:
    return "  -  " if v is None else f"{100 * v:5.1f}"


FIGS = [("tightness", "tightness (errors)"), ("mixed", "mixed %"), ("duplicates", "duplicates %"),
        ("groups", "groups"), ("groups_min", f"groups >= {E.GROUP_MIN} edges"), ("small_share", "edges in smaller %"),
        ("groups_days", f"groups on >= {E.GROUP_DAYS} days"), ("mirror", "mirror %"), ("edges", "edges")]


def _fig(k: str, v) -> str:
    if v is None:
        return "-"
    return f"{v:.2f}" if k == "tightness" else f"{100 * v:.1f}" if isinstance(v, float) else f"{v:,d}"


def _site_lines(site: str, runs: dict, inv: dict) -> tuple:
    r = runs["6"]
    lines = [f"{site}: {r['steps']:,d} steps -> {r['edges']:,d} edges (" +
             ", ".join(f"{m or 'grid'} {n:,d}" for m, n in sorted(r["places"].items(), key=lambda x: -x[1])) +
             f"); {r['shared']:,d} with a shared entry; late {r['late']}; {r['seconds']:.0f} s"]
    lines.append("  gains at the end (Fleet.gain against the grid, the steps it learned from): " +
                 ", ".join(f"{m} {g:.3f} ({n:.0f})" for m, (g, n) in r.get("gains", {}).items()))
    lines.append("  1 conservation, parent > child: adds up % of the moments clear of the meters' idle noises added; the rest as "
                 + ", ".join(E.KINDS) + " %; the noise-level moments; median parent / children; failing ones whose "
                 "parent stepped so off their moment")
    for k, (n, ok, *how, quiet) in sorted(r["conservation"].items(), key=lambda x: -x[1][0]):
        parent, child = k.split(">", 1)
        n_f, found, off, ratio = r["lateness"].get(k, [0, 0, None, None])
        lines.append(f"    {(parent or 'grid') + ' > ' + child:34s} {_pct(ok / n if n else None)} % of {n:7,d}   "
                     + " ".join(_pct(h / n if n else None) for h in how) + f"   noise-level {quiet:6,d}"
                     + (f"   ratio {ratio:.3f}" if ratio else "")
                     + (f"   off: {found / n_f:.0%} of the failing, median {off:+.1f} s" if found else ""))
    lines.append("  2 coverage, per meter: entries, unknown %, 0 on silence %; as the place: edges, blind %")
    for m, c in sorted(r["coverage"].items(), key=lambda x: -x[1]["entries"]):
        lines.append(f"    {m or 'grid':20s} {c['entries']:8,d} {_pct(c['unknown'] / max(c['entries'], 1))} "
                     f"{_pct(c['silent'] / max(c['entries'], 1))}   {c['edges']:7,d} {_pct(c['blind'] / c['edges']) if c['edges'] else '  -  '}")
    lines.append("  3 invariance, edges differing from SLICE=6: " + "   ".join(
        f"{b}: {v['differ']} of {v['n']} (only 6 {v['only A']}, only {b} {v['only B']}, entries {v['entries']})"
        for (_, b), v in inv.items()))
    lines.append(f"  4-8 {'':26s} {'edges':>10s} {'today':>10s}")
    for k, label in FIGS:
        lines.append(f"    {label:28s} {_fig(k, r['groups'][k]):>10s} {_fig(k, r['today'][k]):>10s}")
    lines.append("    mixed by property %: " + ", ".join(f"{p} {100 * r['groups']['mixed_by'][p]:.1f}/{100 * r['today']['mixed_by'][p]:.1f}"
                                                         for p in E.PROPS) + "  (edges/today)")
    return lines


def _worklist(site: str, runs: dict, diffs: dict) -> list:
    r = runs["6"]
    out = [f"{site} - conservation, per parent > children the largest children's steps that do not add up (SLICE=6): "
           "when, how, parent's step, children's, tolerance, entries"]
    for c in [c for pair in sorted(r["worst"], key=lambda k: -len(r["worst"][k])) for c in r["worst"][pair][:3]]:
        out.append(f"  {_when(c['t'])}  {E.miss(c):8s} {c['parent'] or 'grid'} > {', '.join(c['children'])}  {c['p']:+8.0f} vs {c['c']:+8.0f} "
                   f"(tol {c['tol']:.0f})  {' '.join(c['keys'])}"
                   + (f"  - the parent's own step {c['off']:+.1f} s off" if c.get("off") is not None else ""))
    for pair, ds in diffs.items():
        out.append(f"{site} - invariance {pair[0]} vs {pair[1]}: kind, A, B (time, place, dir, pattern, non-zero entries)")
        for d in ds[:WORST]:
            x = d["a"] or d["b"]
            out.append(f"  {d['kind']:8s} {_when(x[0])}  A {d['a'][1:] if d['a'] else '-'}  B {d['b'][1:] if d['b'] else '-'}")
    return out


def card(args: list) -> None:
    sites = [a for a in args if "=" not in a] or ["home", "kozolec", "andrejg"]
    label = next((a[6:] for a in args if a.startswith("LABEL=")), None)
    dials = [a for a in args if "=" in a and not a.startswith("LABEL=")]
    keys = sorted([(site, sl) for site in sites for sl in SLICINGS], key=lambda k: (k[0] != "home", k[1] != "0"))
    t0 = time.time()
    got = dict(zip(keys, _parallel([(site, EB._folder(site), ["SUBS=prod"] + SLICINGS[sl] + EB._site_dials(site) + dials)
                                    for site, sl in keys])))
    took = time.time() - t0
    commit, utc = EB._stamp()
    name = f"edges-{commit}" + (f"-{label}" if label else "") + f"-{utc}"
    lines = [f"edge scorecard {commit} {utc}  {' '.join(dials) or 'defaults'}  - {len(keys)} replays in {took:.0f} s"]
    work, inv = [], {}
    for site in sites:
        runs = {sl: got[(site, sl)] for sl in SLICINGS}
        diffs, inv[site] = {}, {}
        for a, b in PAIRS:
            ds = E.compare([tuple(x) for x in runs[a]["brief"]], [tuple(x) for x in runs[b]["brief"]])
            diffs[(a, b)] = ds
            inv[site][f"{a}~{b}"] = {"n": len(runs[a]["brief"]), "differ": len(ds),
                                     **{k: sum(1 for d in ds if d["kind"] == k) for k in ("only A", "only B", "entries")}}
        lines += [""] + _site_lines(site, runs, {pair: inv[site][f"{pair[0]}~{pair[1]}"] for pair in PAIRS})
        work += _worklist(site, runs, diffs) + [""]
    print("\n".join(lines))
    out = ROOT / "data" / "scorecard" / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    keep = {f"{s}|{sl}": {k: v for k, v in r.items() if k != "brief"} for (s, sl), r in got.items()}
    out.write_text(json.dumps({"commit": commit, "utc": utc, "dials": dials, "seconds": took, "runs": keep,
                               "invariance": inv}, indent=1, default=list), encoding="utf-8")
    wl = out.with_name(f"{name}-worklist.txt")
    wl.write_text("\n".join(work) + "\n", encoding="utf-8")
    print(f"\nwritten {out}\n        {wl}")


def diff(a_path: str, b_path: str) -> None:
    A, Bj = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (a_path, b_path))
    print(f"{A['commit']} {A['utc']} -> {Bj['commit']} {Bj['utc']}  (SLICE=6)")
    for site in sorted({k.split("|")[0] for k in A["runs"]} & {k.split("|")[0] for k in Bj["runs"]}):
        ra, rb = A["runs"][f"{site}|6"], Bj["runs"][f"{site}|6"]
        print(f"{site}: edges {ra['edges']:,d} -> {rb['edges']:,d}")
        for k, label in FIGS:
            print(f"  {label:28s} {_fig(k, ra['groups'][k]):>9s} -> {_fig(k, rb['groups'][k]):>9s}"
                  f"   today {_fig(k, ra['today'][k]):>9s} -> {_fig(k, rb['today'][k]):>9s}")
        for pair in sorted(set(ra["conservation"]) | set(rb["conservation"])):
            (na, oka), qa = ra["conservation"].get(pair, [0, 0])[:2], ra["conservation"].get(pair, [0])[-1]
            (nb, okb), qb = rb["conservation"].get(pair, [0, 0])[:2], rb["conservation"].get(pair, [0])[-1]
            print(f"  conservation {pair:34s} {_pct(oka / na if na else None)} % of {na:6,d} -> "
                  f"{_pct(okb / nb if nb else None)} % of {nb:6,d}   noise-level {qa:6,d} -> {qb:6,d}")
        ia, ib = A["invariance"].get(site, {}), Bj["invariance"].get(site, {})
        print("  invariance " + ", ".join(f"{p}: {ia[p]['differ']} -> {ib[p]['differ']}" for p in ia if p in ib))


if __name__ == "__main__":
    cmd = sys.argv[1:2]
    if cmd == ["card"]:
        card(sys.argv[2:])
    elif cmd == ["diff"] and len(sys.argv) == 4:
        diff(*sys.argv[2:])
    elif len(sys.argv) >= 3 and cmd[0] not in ("card", "diff"):
        r = replay(sys.argv[1], sys.argv[2], sys.argv[3:])
        print(json.dumps({k: v for k, v in r.items() if k not in ("brief", "summaries", "worst")}, indent=1, default=list))
    else:
        print(__doc__)
        raise SystemExit(1)
