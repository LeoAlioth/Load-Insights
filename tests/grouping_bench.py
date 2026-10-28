"""How well step groups hold one device each - grouping on its own, not through
the energy card (run lifecycle, Task 8b; the owner, 2026-10-28: "the benches
only tell us the final result, not how good a single step in the pipeline is").

    python3.14 tests/grouping_bench.py SITE [DIAL=V ...]          # both replays, in parallel
    python3.14 tests/grouping_bench.py SITE MODE OUT=x.json [DIAL=V ...]   (one replay; MODE hidden|fed)

hidden  the grid alone (SUBS=none, SLICE=6): every step its detector groups -
        time, phase pattern, signed W, var, group id, and the grid voltage
        either side of it (Home: the SolarEdge M1's phase voltages).
fed     every meter (SUBS=prod): each plug's own steps, its truth.
Each plug step labels the hidden grid step of its direction on its grid phase
(the fed replay's phase map) within LATENCY_S, its size within SIZE_REL (or
SIZE_W), nearest first, one to one. Per plug and direction: labelled steps,
the groups they fall in (count, the main group's share - fragmentation), the
share of those groups' steps that are other plugs' and that no plug claims
(merging), the pile width (standard deviation of its sizes, W and % of the
mean) and, where the voltage moves, the plug's exponent k (P ~ V^k,
edges.volt_slope on the on-side voltage) and the width with every step
corrected to 230 V by it. A plug is a meter, not a device: one holding several
(Susilna: fans, compressor, heater) has several groups by right.
"""
from __future__ import annotations

import collections
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench as B  # noqa: E402
import energy_bench as EB  # noqa: E402
from _load import load  # noqa: E402

D = B.D
E = load("insights.edges")
VOLTS = {"home": {p: f"sensor.solaredge_se17k_m1_ac_voltage_{p}n" for p in "abc"}}
LATENCY_S, SIZE_REL, SIZE_W, V_WIN_S = 20.0, 0.12, 15.0, 30.0


def _plugs(site: str) -> list:
    return [n for n in B.PROD_SUBS[site] if n not in set(B.PROD_PARENTS.get(site, {}).values())]


def _side(rows: list, t: float, before: bool):
    """Median of the readings V_WIN_S before ``t`` (or after), and their spread."""
    xs = [v for s, v in rows if (t - V_WIN_S <= s < t if before else t < s <= t + V_WIN_S)]
    if not xs:
        return None, None
    return statistics.median(xs), (statistics.pstdev(xs) if len(xs) > 1 else 0.0)


def replay(site: str, mode: str, dials: list) -> dict:
    out = next((d[4:] for d in dials if d.startswith("OUT=")), None)
    dials = [d for d in dials if not d.startswith(("OUT=", "LABEL="))]
    volts = [f"VOLTS={p}:{e}" for p, e in VOLTS.get(site, {}).items()] if mode == "hidden" else []
    B._apply([f"SUBS={'none' if mode == 'hidden' else 'prod'}", "SLICE=6"] + EB._site_dials(site) + volts + dials)
    rec, ref = [], {}
    proc0, cls0 = D.Fleet.process, D.Detector._classify_step

    def proc(self, *a, **kw):
        ref["f"] = self
        return proc0(self, *a, **kw)

    def cls(det, ph, since, watts, var, surge, *a, **kw):
        g = cls0(det, ph, since, watts, var, surge, *a, **kw)
        f = ref.get("f")
        if f is None:
            return g
        name = next((n for n, d in f.detectors() if d is det), None)
        if mode == "hidden" and name == "":
            vs = {}
            for p in ph:
                rows = (f.volts.get("") or {}).get(p) or []
                vs[p] = (_side(rows, since, True), _side(rows, since, False))
            rec.append({"t": since, "ph": ph, "w": watts, "var": var, "g": g.id, "v": vs})
        elif mode == "fed" and name in _plugs(site) and len(ph) == 1:
            rec.append({"m": name, "t": since, "ph": ph, "w": watts})
        return g

    D.Fleet.process, D.Detector._classify_step = proc, cls
    try:
        B._run(EB._folder(site), site)
    finally:
        D.Fleet.process, D.Detector._classify_step = proc0, cls0
    f = ref["f"]
    res = {"site": site, "mode": mode, "steps": rec,
           "maps": {n: f.phase_map(n) for n in _plugs(site)} if mode == "fed" else {}}
    if out:
        Path(out).write_text(json.dumps(res), encoding="utf-8")
    return res


def label(hidden: dict, fed: dict) -> dict:
    """{hidden step index: plug} - one to one, nearest in time first."""
    grid = [(i, s) for i, s in enumerate(hidden["steps"]) if len(s["ph"]) == 1]
    grid.sort(key=lambda x: x[1]["t"])
    ts = [s["t"] for _, s in grid]
    import bisect
    pairs = []
    for k, p in enumerate(fed["steps"]):
        gph = (fed["maps"].get(p["m"]) or {}).get(p["ph"])
        lo, hi = bisect.bisect_left(ts, p["t"] - LATENCY_S), bisect.bisect_right(ts, p["t"] + LATENCY_S)
        for j in range(lo, hi):
            i, s = grid[j]
            if (s["w"] > 0) != (p["w"] > 0) or (gph and s["ph"] != gph):
                continue
            if abs(abs(s["w"]) - abs(p["w"])) > max(SIZE_W, SIZE_REL * abs(p["w"])):
                continue
            pairs.append((abs(s["t"] - p["t"]), k, i))
    lab, used = {}, set()
    for _, k, i in sorted(pairs):
        if k in used or i in lab:
            continue
        used.add(k)
        lab[i] = fed["steps"][k]["m"]
    return lab


def _width(xs: list) -> tuple:
    if len(xs) < 2:
        return None, None
    m, sd = statistics.mean(xs), statistics.pstdev(xs)
    return sd, 100.0 * sd / m if m else None


def score(hidden: dict, fed: dict) -> dict:
    steps = hidden["steps"]
    lab = label(hidden, fed)
    by_group = collections.defaultdict(collections.Counter)
    for i, s in enumerate(steps):
        by_group[(s["g"], s["w"] > 0)][lab.get(i, "")] += 1
    out = {}
    for m in sorted({p["m"] for p in fed["steps"]}):
        for up in (True, False):
            mine = [i for i, n in lab.items() if n == m and (steps[i]["w"] > 0) == up]
            truth = [abs(p["w"]) for p in fed["steps"] if p["m"] == m and (p["w"] > 0) == up]
            if not mine:
                out[f"{m}|{'rise' if up else 'fall'}"] = {"truth": len(truth), "labelled": 0}
                continue
            groups = collections.Counter(steps[i]["g"] for i in mine)
            main = groups.most_common(1)[0][1] / len(mine)
            tot = sum(sum(by_group[(g, up)].values()) for g in groups)
            other = sum(sum(v for k, v in by_group[(g, up)].items() if k not in (m, "")) for g in groups)
            unl = sum(by_group[(g, up)][""] for g in groups)
            sizes = [abs(steps[i]["w"]) for i in mine]
            sd, cv = _width(sizes)
            row = {"truth": len(truth), "labelled": len(mine), "groups": len(groups), "main": main,
                   "top3": [n for _, n in groups.most_common(3)],
                   "other": other / tot if tot else 0.0, "unlabelled": unl / tot if tot else 0.0,
                   "width_w": sd, "width_pct": cv, "plug_width_pct": _width(truth)[1]}
            items = []
            for i in mine:
                s = steps[i]
                side = (s["v"].get(s["ph"]) or [None, None])[1 if up else 0]
                if side and side[0]:
                    items.append({"v": side[0], "ve": side[1] or 0.0, "size": abs(s["w"])})
            ks = E.volt_slope(items) if items else None
            if ks:
                k = ks[0]
                corr = [it["size"] * (230.0 / it["v"]) ** k for it in items]
                row.update({"k": k, "k_err": ks[1], "k_n": ks[2], "v_spread": statistics.pstdev([it["v"] for it in items]),
                            "width_k_pct": _width(corr)[1]})
            elif items:
                row["v_spread"] = statistics.pstdev([it["v"] for it in items]) if len(items) > 1 else 0.0
            out[f"{m}|{'rise' if up else 'fall'}"] = row
    groups_used = len({s["g"] for s in steps})
    return {"loads": out, "groups": groups_used, "steps": len(steps), "labelled": len(lab)}


def _fmt(v, f="{:.1f}"):
    return "-" if v is None else f.format(v)


def main(argv: list) -> None:
    site = argv[0]
    if len(argv) > 1 and argv[1] in ("hidden", "fed"):
        replay(site, argv[1], argv[2:])
        return
    dials = argv[1:]
    with tempfile.TemporaryDirectory() as tmp:
        def one(mode):
            out = f"{tmp}/{mode}.json"
            p = subprocess.run([sys.executable, __file__, site, mode, f"OUT={out}", *dials],
                               capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": "0"})
            if p.returncode:
                raise RuntimeError(p.stdout[-2000:] + p.stderr[-3000:])
            return json.loads(Path(out).read_text(encoding="utf-8"))
        with ThreadPoolExecutor(2) as pool:
            hidden, fed = pool.map(one, ("hidden", "fed"))
    res = score(hidden, fed)
    print(f"{site} {' '.join(dials) or 'defaults'}: {res['steps']} grid steps in {res['groups']} groups, "
          f"{res['labelled']} labelled by a plug")
    print(f"  {'plug|dir':34s} {'truth':>6s} {'lab':>5s} {'grp':>4s} {'main%':>6s} {'oth%':>5s} {'unl%':>5s} "
          f"{'wid W':>6s} {'wid%':>5s} {'plug%':>5s} {'k':>5s} {'±':>4s} {'Vsd':>4s} {'k-wid%':>6s}")
    for key, r in res["loads"].items():
        if not r.get("labelled"):
            print(f"  {key:34s} {r['truth']:6d} {0:5d}")
            continue
        print(f"  {key:34s} {r['truth']:6d} {r['labelled']:5d} {r['groups']:4d} {100 * r['main']:6.1f} "
              f"{100 * r['other']:5.1f} {100 * r['unlabelled']:5.1f} {_fmt(r['width_w']):>6s} {_fmt(r['width_pct']):>5s} "
              f"{_fmt(r['plug_width_pct']):>5s} {_fmt(r.get('k'), '{:.2f}'):>5s} {_fmt(r.get('k_err'), '{:.2f}'):>4s} "
              f"{_fmt(r.get('v_spread')):>4s} {_fmt(r.get('width_k_pct')):>6s}")
    lab = next((d[6:] for d in dials if d.startswith("LABEL=")), "")
    path = Path(__file__).resolve().parent.parent / "data" / "scorecard" / f"grouping-{site}{'-' + lab if lab else ''}.json"
    path.write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(f"written {path}")


if __name__ == "__main__":
    main(sys.argv[1:])
