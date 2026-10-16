"""Known truths for the cross-meter edges (2026-10-15: "judge the
groupings against known truths from now on"). Beside tests/edge_bench.py,
whose replay and edge machinery it reuses (_instrument, the Assembler,
edges.group); no devices, the detector unchanged.

    python3.14 tests/truth_bench.py card [SITE ...] [DIAL=value ...] [LABEL=x]
    python3.14 tests/truth_bench.py diff A.json B.json
    python3.14 tests/truth_bench.py fetch [DAYS=12]
    python3.14 tests/truth_bench.py SITE MODE [DIAL=value ...] OUT=run.json   (one replay; card runs these)

card    Home and Kozolec (or the SITEs given), each replayed twice at SLICE=6:
        fed (SUBS=prod, every meter production reads) and hidden (SUBS=circuits:
        Home's grid and its two 3EMs, Kozolec's grid alone - no plug fed).
        The truths:
          oven   - Le Hottie on Mansarda C: the channel's own declared steps
                   inside its HA "Running" windows (data/truth/home-hottie.json),
                   classed by program and size (OVEN): MW, E165, E100, E265
          kettle - Mansarda B: a rise and a fall of the same size 90-240 s
                   apart (KETTLE_*), and the panini press's run of 12 Oct
          plugs  - hidden: each plug's own step (its detector's, from the fed
                   replay), clear of its parent's idle noise and its own,
                   labels the hidden replay's edge within the plug's latency
                   of it (Fleet._latency) - same direction, its phase, sizes
                   agreeing in the grid's terms as conservation judges them
                   (one to one, nearest in time first)
        Per class: truth steps, the share found as an edge (the step a place
        leg of one; for plugs the edge it labels); per grouping the share of
        the class's edges in a group dominated (most edges) by another class
        or by unlabelled edges, and the groups in the labelled keys; for the
        oven and kettle sets leave-one-out 1-NN accuracy of each property
        alone (rises, falls, both). Groupings: edges.group (m0), and with
        TRUTH_CLUSTER=path/to/cluster.py in the environment and numpy and
        scikit-learn importable, that experiment's m4e and m4b - native and
        cut at m0's group count per key (m4e=, m4b=). Written to
        data/scorecard/truth-<commit or LABEL>-<utc>.json.
diff    two truth files, class by class.
fetch   refresh data/truth/home-hottie.json from Home's recorder (token in
        data/<host>, read here only), the last DAYS days merged in.
"""
from __future__ import annotations

import bisect
import collections
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench as B  # noqa: E402
import edge_bench as EG  # noqa: E402
import energy_bench as EB  # noqa: E402

E, D = EG.E, B.D
ROOT = EB.ROOT
TRUTH = ROOT / "data" / "truth"
HOTTIE = TRUTH / "home-hottie.json"
HOTTIE_IDS = ["sensor.aeg_le_hottie_appliancestate", "select.aeg_le_hottie_program"]
MODES = {"fed": "SUBS=prod", "hidden": "SUBS=circuits"}
PARALLEL = min(EB.PARALLEL, 6)

# the oven: a step of Mansarda C inside a Running window (5 s before it, 60 s after) of a program, sized
MW_PROGRAMS = {"Microwave Full", "Microwave Defrost", "Microwave Reheat"}   # Grill Fan left out: grill + microwave
OVEN = {"MW": (True, 1500, 1950), "E165": (False, 1500, 1800), "E100": (False, 850, 1200), "E265": (False, 2400, 2900)}
# the kettle and the press, Mansarda B (the pattern spike's, 2026-10-14)
KETTLE_W, KETTLE_RUN_S, KETTLE_MATCH = (1300, 1750), (90, 240), 0.1
_utc = lambda *a: datetime(*a, tzinfo=timezone.utc).timestamp()  # noqa: E731
PRESS = (_utc(2026, 10, 12, 18, 50, 56), _utc(2026, 10, 12, 18, 55, 54))   # 20:50:56-20:55:54 CEST
PRESS_S = 10
KETTLE_SKIP = [(_utc(2026, 10, 5, 11, 0), _utc(2026, 10, 5, 11, 45)), (_utc(2026, 10, 9, 13, 0), _utc(2026, 10, 9, 13, 20)),
               (PRESS[0] - 60, PRESS[1] + 60)]
NN_PROPS = ("size", "angle", "r", "surge", "settle", "wobble", "ramp_n", "ramp_s")


def plugs(site: str) -> list:
    """The site's meters that are no circuit: production's plugs."""
    return [n for n in B.PROD_SUBS[site] if n not in set(B.PROD_PARENTS.get(site, {}).values())]


# ------------------------------------------------------------- one replay
def _size0(x) -> float:
    """A step as its detector declared it, before the assembler measured it again."""
    d = getattr(x, "declared", None)
    return d[0] if d else x.dp


def _step(x) -> dict:
    return {"meter": x.meter, "ch": x.ch, "t": x.t, "a": x.a, "b": x.b, "dp": x.dp, "dp0": _size0(x),
            "g": x.dp * x.gain, "noise": x.noise, "floor": x.floor or x.noise, "phase": x.phase}


def _r(legs: list):
    """The load's resistance, V on^2 / |dP| per leg, averaged."""
    rs = [(e["v1"] if e["dp"] > 0 else e["v0"]) ** 2 / abs(e["dp"]) for e in legs
          if e.get("v0") is not None and e.get("v1") is not None and e["dp"]]
    return sum(rs) / len(rs) if rs else None


def replay(site: str, mode: str, dials: list) -> dict:
    out_path = next((d[4:] for d in dials if d.startswith("OUT=")), None)
    B._apply([MODES[mode], "SLICE=6"] + EB._site_dials(site) + [d for d in dials if not d.startswith("OUT=")])
    asm = E.Assembler()
    _, st, undo = EG._instrument(asm)
    t0 = time.time()
    try:
        B._run(EB._folder(site), site)
    finally:
        undo()
    asm.flush()
    seen = {(x.meter, x.ch, round(x.t, 3)): x for x in st["seen"].values()}
    gid = {}
    for k, g in enumerate(E.group(asm.edges)):
        for e in g["items"]:
            gid[e["id"]] = k
    edges = []
    for e in asm.edges:
        legs = [seen.get((*k.split("|", 1), round(e["entries"][k]["t"], 3))) for k in e["legs"]]
        legs = [x for x in legs if x is not None]
        ent = {k: {f: e["entries"][k].get(f) for f in ("t", "dp", "dq", "v0", "v1")} for k in e["legs"]}
        ph = E._phases(legs) if legs else None
        edges.append({"id": e["id"], "place": e["place"], "dir": e["dir"], "pattern": e["pattern"], "t": e["t"],
                      "A": min((x.a for x in legs), default=e["t"]), "B": max((x.b for x in legs), default=e["t"]),
                      "size": e["size"], "g": abs(sum(x.dp * x.gain for x in legs)) if legs else e["size"],
                      "angle": e["angle"], "noise": e["noise"], "err": e["err"], "r": _r(list(ent.values())),
                      **{p: e.get(p) for p in ("surge", "settle", "wobble", "ramp_n", "ramp_s")},
                      "phases": None if ph is None else sorted(ph), "legs": e["legs"], "entries": ent,
                      "m0": gid.get(e["id"], -1)})
    keep = set(plugs(site)) | ({"Mansarda"} if site == "home" else set())
    res = {"site": site, "mode": mode, "seconds": time.time() - t0, "edges": edges,
           "steps": [_step(x) for x in st["seen"].values() if x.meter in keep] if mode == "fed" else [],
           "floor": {f"{m}|{c}": v for (m, c), v in asm.floor.items()}, "chans": asm.chans, "latency": asm.latency}
    if out_path:
        Path(out_path).write_text(json.dumps(res, default=list), encoding="utf-8")
    return res


# ------------------------------------------------------------- truths
def _hottie_runs(path: Path = HOTTIE) -> list:
    """[(start, end, program)] of the oven's Running windows."""
    ts = lambda s: datetime.fromisoformat(s).timestamp()  # noqa: E731
    H = {x[0]["entity_id"]: sorted((ts(s["last_changed"]), s["state"]) for s in x) for x in json.loads(path.read_text(encoding="utf-8"))}
    state, prog = H[HOTTIE_IDS[0]], H[HOTTIE_IDS[1]]
    runs = []
    for i, (t, s) in enumerate(state):
        if s == "Running":
            end = state[i + 1][0] if i + 1 < len(state) else t + 3600
            pr = [p for tp, p in prog if tp <= t + 5]
            runs.append((t, end, pr[-1] if pr else None))
    return runs


def _hottie_since(path: Path = HOTTIE) -> float:
    return min(datetime.fromisoformat(x[0]["last_changed"]).timestamp() for x in json.loads(path.read_text(encoding="utf-8")))


def oven_steps(steps: list, runs: list) -> dict:
    """{step index: class} for Mansarda C's steps inside a Running window, by program and size."""
    out = {}
    for i, s in enumerate(steps):
        if s["meter"] != "Mansarda" or s["ch"] != "c":
            continue
        prog = next((p for a, b, p in runs if a - 5 <= s["t"] <= b + 60), None)
        if not prog:
            continue
        for cls, (mw, lo, hi) in OVEN.items():
            if lo <= abs(s["dp0"]) <= hi and (prog in MW_PROGRAMS if mw else not prog.startswith("Microwave")):
                out[i] = cls
    return out


def kettle_steps(steps: list) -> dict:
    """{step index: "kettle" | "press"} on Mansarda B."""
    cand = [i for i, s in enumerate(steps) if s["meter"] == "Mansarda" and s["ch"] == "b"
            and KETTLE_W[0] <= abs(s["dp0"]) <= KETTLE_W[1]]
    out = {}
    for i in cand:
        s = steps[i]
        for tp, up in zip(PRESS, (True, False)):
            if abs(s["t"] - tp) <= PRESS_S and (s["dp0"] > 0) == up:
                out[i] = "press"
    for i in cand:
        s = steps[i]
        if s["dp0"] < 0 or i in out or any(a <= s["t"] <= b for a, b in KETTLE_SKIP):
            continue
        f = [j for j in cand if steps[j]["dp0"] < 0 and j not in out
             and KETTLE_RUN_S[0] <= steps[j]["t"] - s["t"] <= KETTLE_RUN_S[1]
             and abs(abs(steps[j]["dp0"]) - s["dp0"]) <= KETTLE_MATCH * s["dp0"]]
        if f:
            out[i] = out[f[0]] = "kettle"
    return out


def carried(edges: list, steps: list, which: dict) -> dict:
    """{edge index: class} - each edge that holds a truth step as a place leg
    (one class only); and per step whether it was found."""
    at = {}
    for i, e in enumerate(edges):
        for k in e["legs"]:
            at[(k, round(e["entries"][k]["t"], 3))] = i
    lab, found = {}, {}
    for si, cls in which.items():
        s = steps[si]
        i = at.get((f"{s['meter']}|{s['ch']}", round(s["t"], 3)))
        found[si] = i is not None
        if i is not None:
            lab[i] = cls if lab.get(i, cls) == cls else None
    return {i: c for i, c in lab.items() if c}, found


def plug_truth(site: str, steps: list, hidden: dict, latency: dict) -> tuple:
    """Each plug's clear steps from the fed replay against the hidden replay's
    edges: ({step index: plug} - the truth, {edge index: plug}, {step index: found})."""
    parents = B.PROD_PARENTS.get(site, {})
    chans, floor = hidden["chans"], hidden["floor"]
    truth = {}
    for i, s in enumerate(steps):
        if s["meter"] not in plugs(site):
            continue
        up = parents.get(s["meter"], "")
        up = up if up in chans else ""
        fl = [floor.get(f"{up}|{c}", 0.0) for c, ph in (chans.get(up) or {}).items()
              if E._fits(s["phase"], None if ph is None else set(ph))]
        if abs(s["g"]) >= s["floor"] + max(fl, default=0.0):
            truth[i] = s["meter"]
    edges = hidden["edges"]
    order = sorted(range(len(edges)), key=lambda i: edges[i]["t"])
    starts = [edges[i]["t"] for i in order]
    pairs = []
    for si in truth:
        s = steps[si]
        lat = latency.get(s["meter"], 0.0)
        for k in range(bisect.bisect_left(starts, s["t"] - lat), bisect.bisect_right(starts, s["t"] + lat)):
            e = edges[order[k]]
            if e["dir"] != (1 if s["g"] > 0 else -1):
                continue
            if e["phases"] is not None and not E._fits(s["phase"], set(e["phases"])):
                continue
            if abs(e["g"] - abs(s["g"])) > E._tol([s["noise"], e["err"]], s["g"]):
                continue
            pairs.append((abs(e["t"] - s["t"]), si, order[k]))
    lab, found, used = {}, {si: False for si in truth}, set()
    for _, si, ei in sorted(pairs):
        if found[si] or ei in used:
            continue
        found[si], lab[ei] = True, truth[si]
        used.add(ei)
    return truth, lab, found


# ------------------------------------------------------------- scoring
def dominated(groups: list, lab: dict, unknown=frozenset()) -> dict:
    """Per class: the share of its labelled edges in a group whose most
    common label (unlabelled counted as one) is not the class - a tie is
    not the class's; ``unknown`` edges (outside the truth's reach) not
    counted at all."""
    of = collections.defaultdict(list)
    for i, g in enumerate(groups):
        if i not in unknown:
            of[g].append(i)
    n, bad = collections.Counter(), collections.Counter()
    for i, cls in lab.items():
        n[cls] += 1
        c = collections.Counter(lab.get(j, "") for j in of[groups[i]])
        top = max(c.values())
        if c[cls] < top or sum(1 for v in c.values() if v == top) > 1:
            bad[cls] += 1
    return {cls: bad[cls] / n[cls] for cls in n}


def nn(items: list, prop: str) -> dict:
    """Leave-one-out 1-NN on one property alone: {"acc", per class [right, n]}
    over the items that have it; ties broken by the earlier item."""
    xs = [(it[prop], it["cls"]) for it in items if it.get(prop) is not None]
    if len(xs) < 2:
        return {"acc": None, "bal": None, "by": {}}
    order = sorted(range(len(xs)), key=lambda i: xs[i][0])
    by = collections.defaultdict(lambda: [0, 0])
    for r, i in enumerate(order):
        cands = [order[j] for j in (r - 1, r + 1) if 0 <= j < len(order)]
        j = min(cands, key=lambda j: (abs(xs[j][0] - xs[i][0]), j))
        by[xs[i][1]][1] += 1
        by[xs[i][1]][0] += xs[j][1] == xs[i][1]
    return {"acc": sum(v[0] for v in by.values()) / len(xs), "bal": sum(r / n for r, n in by.values()) / len(by), "by": dict(by)}


def _nn_table(edges: list, lab: dict) -> dict:
    out = {}
    for d, name in ((1, "rise"), (-1, "fall"), (0, "both")):
        items = [{**{p: edges[i].get(p) for p in NN_PROPS}, "cls": c} for i, c in lab.items() if d in (0, edges[i]["dir"])]
        out[name] = {p: nn(items, p) for p in NN_PROPS}
    return out


def _cluster_ns():
    """cluster.py's functions (TRUTH_CLUSTER), run against this tree's edges and detector."""
    path = os.environ.get("TRUTH_CLUSTER")
    if not path:
        return None
    try:
        import numpy  # noqa: F401
        import sklearn  # noqa: F401
    except ImportError:
        return None
    src = Path(path).read_text(encoding="utf-8").split("# ---------------------------------------------------------------- main")[0]
    ns, argv = {"__file__": path, "__name__": "cluster"}, sys.argv
    sys.argv = [path, "truth"]
    try:
        exec(compile(src, path, "exec"), ns)
    finally:
        sys.argv = argv
    ns["E"], ns["D"] = E, D
    return ns


def m4(edges: list, m0: list, ns) -> dict:
    """cluster.py's m4e and m4b over every key, native and cut at m0's group
    count per key; {method: [group per edge]}."""
    import numpy as np
    from sklearn.cluster import AgglomerativeClustering
    from sklearn.neighbors import NearestNeighbors
    ns["RNG"] = np.random.default_rng(0)
    for e in edges:
        ns["enrich"](e, {})
    units = {}
    for e in edges:
        units.setdefault(ns["key_of"](e), None)
    for g in E.group(edges):
        units[g["key"]] = g["unit"]
    ns["UNITS"], ns["VERR"] = units, ns["volt_err"](edges)
    for e in edges:
        e["r_err"] = ns["r_err"](e)
    keys = collections.defaultdict(list)
    for i, e in enumerate(edges):
        keys[ns["key_of"](e)].append(i)

    def cut(X, k):
        rng = np.random.default_rng(0)
        if len(X) <= ns["AGG_MAX"]:
            return AgglomerativeClustering(n_clusters=k, linkage="average").fit_predict(X)
        pick = rng.choice(len(X), ns["AGG_MAX"], replace=False)
        lab = AgglomerativeClustering(n_clusters=k, linkage="average").fit_predict(X[pick])
        return lab[NearestNeighbors(n_neighbors=1).fit(X[pick]).kneighbors(X, return_distance=False)[:, 0]]
    out = {}
    for name, combo in (("m4e", ["size", "angle", "r_log"]), ("m4b", ns["FULL6"])):
        nat, eq = [-1] * len(edges), [-1] * len(edges)
        nxt = [0, 0]
        for k, idx in sorted(keys.items()):
            k0 = len({m0[i] for i in idx})
            if len(idx) < ns["MIN_KEY"]:
                subs = [[m0[i] for i in idx]] * 2
            else:
                seg = ns["size_segments"](edges, idx, units[k])
                X = ns["matrix"](edges, idx, seg, units[k], combo, {}, ns["VERR"])
                subs = [ns["agglo"](X), cut(X, min(k0, len(idx)))]
            for w, (lab, sub) in enumerate(zip((nat, eq), subs)):
                ids = {g: n for n, g in enumerate(sorted(set(sub)))}
                for i, g in zip(idx, sub):
                    lab[i] = nxt[w] + ids[g]
                nxt[w] += len(ids)
        out[name], out[name + "="] = nat, eq
    return out


def score_set(edges: list, lab: dict, truth: dict, found: dict, groupings: dict, with_nn: bool, since: float = -math.inf) -> dict:
    keys = {(edges[i]["place"], edges[i]["dir"], edges[i]["pattern"]) for i in lab}
    in_keys = [i for i, e in enumerate(edges) if (e["place"], e["dir"], e["pattern"]) in keys]
    out = {"classes": {}}
    for cls in sorted(set(truth.values())):
        mine = [si for si, c in truth.items() if c == cls]
        out["classes"][cls] = {"truth": len(mine), "found": sum(1 for si in mine if found.get(si)),
                               "edges": sum(1 for c in lab.values() if c == cls)}
    out["groups"] = {m: len({g[i] for i in in_keys}) for m, g in groupings.items()}
    unknown = frozenset(i for i, e in enumerate(edges) if e["t"] < since)
    out["dominated"] = {m: dominated(g, lab, unknown) for m, g in groupings.items()}
    if with_nn:
        out["nn"] = _nn_table(edges, lab)
    return out


def score(site: str, fed: dict, hidden: dict, ns=None) -> dict:
    res = {"site": site, "seconds": [fed["seconds"], hidden["seconds"]],
           "edges": [len(fed["edges"]), len(hidden["edges"])]}

    def groupings(run):
        g = {"m0": [e["m0"] for e in run["edges"]]}
        if ns is not None:
            g.update(m4(run["edges"], g["m0"], ns))
        return g
    steps = fed["steps"]
    if site == "home":
        gf = groupings(fed)
        if HOTTIE.exists():
            runs = _hottie_runs()
            which = oven_steps(steps, runs)
            lab, found = carried(fed["edges"], steps, which)
            # the oven's history begins on 4 Oct: the edges before it are no one's
            res["oven"] = score_set(fed["edges"], lab, which, found, gf, True, since=_hottie_since())
        which = kettle_steps(steps)
        lab, found = carried(fed["edges"], steps, which)
        res["kettle"] = score_set(fed["edges"], lab, which, found, gf, True)
    truth, lab, found = plug_truth(site, steps, hidden, fed["latency"])
    res["plugs"] = score_set(hidden["edges"], lab, truth, found, groupings(hidden), False)
    return res


# ------------------------------------------------------------- card / diff
def _parallel(jobs: list) -> list:
    """Each job's argv replayed by this file in a process of its own,
    PARALLEL at once, one hash seed for all; their results, in order."""
    with tempfile.TemporaryDirectory() as tmp:
        def one(k):
            out = f"{tmp}/{k}.json"
            p = subprocess.run([sys.executable, __file__, *jobs[k], f"OUT={out}"], capture_output=True, text=True,
                               env={**os.environ, "PYTHONHASHSEED": "0"})
            if p.returncode:
                raise RuntimeError(f"{' '.join(jobs[k])} failed:\n{p.stdout[-2000:]}{p.stderr[-2000:]}")
            return json.loads(Path(out).read_text(encoding="utf-8"))
        with ThreadPoolExecutor(PARALLEL) as pool:
            return list(pool.map(one, range(len(jobs))))


def _pct(v) -> str:
    return "   -" if v is None else f"{100 * v:5.1f}"


def lines(res: dict) -> list:
    out = []
    for site, r in res.items():
        for name in ("oven", "kettle", "plugs"):
            s = r.get(name)
            if not s:
                continue
            ms = list(s["groups"])
            out.append(f"{site} {name}: groups in the labelled keys " + ", ".join(f"{m} {s['groups'][m]}" for m in ms))
            out.append(f"  {'class':22s} {'truth':>6s} {'found%':>7s} {'edges':>6s}   dominated %: " + " ".join(f"{m:>6s}" for m in ms))
            for cls, c in s["classes"].items():
                out.append(f"  {cls:22s} {c['truth']:6d} {_pct(c['found'] / c['truth'] if c['truth'] else None):>7s} {c['edges']:6d}   "
                           + " " * 13 + " ".join(f"{_pct(s['dominated'][m].get(cls)):>6s}" for m in ms))
            if "nn" in s:
                out.append("  1-NN accuracy rise / fall / both (both balanced: the classes' mean): " + "; ".join(
                    f"{p} " + "/".join(_pct(s["nn"][d][p]["acc"]).strip() for d in ("rise", "fall", "both"))
                    + f" ({_pct(s['nn']['both'][p]['bal']).strip()})" for p in NN_PROPS))
    return out


def card(args: list) -> None:
    sites = [a for a in args if "=" not in a] or ["home", "kozolec"]
    label = next((a[6:] for a in args if a.startswith("LABEL=")), None)
    dials = [a for a in args if "=" in a and not a.startswith("LABEL=")]
    jobs = [(s, m) for s in sites for m in MODES]
    t0 = time.time()
    got = dict(zip(jobs, _parallel([[s, m, *dials] for s, m in jobs])))
    ns = _cluster_ns()
    res = {s: score(s, got[(s, "fed")], got[(s, "hidden")], ns) for s in sites}
    commit, utc = EB._stamp()
    head = f"truth card {label or commit} {utc}  {' '.join(dials) or 'defaults'}  - {len(jobs)} replays in {time.time() - t0:.0f} s"
    print("\n".join([head] + lines(res)))
    out = ROOT / "data" / "scorecard" / f"truth-{label or commit}-{utc}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"commit": commit, "label": label, "utc": utc, "dials": dials, "sites": res}, indent=1),
                   encoding="utf-8")
    print(f"\nwritten {out}")


def diff(a_path: str, b_path: str) -> None:
    A, Bj = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (a_path, b_path))
    print(f"{A['label'] or A['commit']} {A['utc']} -> {Bj['label'] or Bj['commit']} {Bj['utc']}")
    for site in sorted(set(A["sites"]) & set(Bj["sites"])):
        for name in ("oven", "kettle", "plugs"):
            sa, sb = A["sites"][site].get(name), Bj["sites"][site].get(name)
            if not sa or not sb:
                continue
            ms = [m for m in sa["groups"] if m in sb["groups"]]
            print(f"{site} {name}: groups " + ", ".join(f"{m} {sa['groups'][m]} -> {sb['groups'][m]}" for m in ms))
            for cls in sorted(set(sa["classes"]) | set(sb["classes"])):
                ca, cb = sa["classes"].get(cls, {}), sb["classes"].get(cls, {})
                fa = ca.get("found", 0) / ca["truth"] if ca.get("truth") else None
                fb = cb.get("found", 0) / cb["truth"] if cb.get("truth") else None
                print(f"  {cls:22s} truth {ca.get('truth', 0):4d} -> {cb.get('truth', 0):4d}  found {_pct(fa)} -> {_pct(fb)}  "
                      "dominated " + "  ".join(f"{m} {_pct(sa['dominated'][m].get(cls)).strip()} -> {_pct(sb['dominated'][m].get(cls)).strip()}"
                                               for m in ms))
            if "nn" in sa and "nn" in sb:
                for d in ("rise", "fall", "both"):
                    print(f"  1-NN {d:4s} " + "  ".join(f"{p} {_pct(sa['nn'][d][p]['acc']).strip()}->{_pct(sb['nn'][d][p]['acc']).strip()}"
                                                       for p in NN_PROPS))
                print("  1-NN both, balanced " + "  ".join(f"{p} {_pct(sa['nn']['both'][p].get('bal')).strip()}->"
                                                          f"{_pct(sb['nn']['both'][p].get('bal')).strip()}" for p in NN_PROPS))


def fetch(args: list) -> None:
    """The oven's history, the last DAYS days, merged into HOTTIE."""
    import fetch_history as F
    days = float(next((a[5:] for a in args if a.startswith("DAYS=")), 12))
    site = F.SITES["home"]
    token = F.read_token(ROOT / "data" / __import__("urllib.parse").parse.urlsplit(site["base"]).hostname)
    end = datetime.now(timezone.utc)
    rows = F.fetch(site["base"], token, HOTTIE_IDS, end - timedelta(days=days), end, 120)
    have = json.loads(HOTTIE.read_text(encoding="utf-8")) if HOTTIE.exists() else []
    by = {x[0]["entity_id"]: {(s["last_changed"], s["state"]) for s in x} for x in have}
    for eid, state, when in rows:
        by.setdefault(eid, set()).add((when, state))
    out = [[{"entity_id": eid, "state": s, "last_changed": w} for w, s in sorted(v, key=lambda x: datetime.fromisoformat(x[0]))]
           for eid, v in sorted(by.items())]
    HOTTIE.parent.mkdir(parents=True, exist_ok=True)
    HOTTIE.write_text(json.dumps(out), encoding="utf-8")
    print(f"{HOTTIE}: " + ", ".join(f"{x[0]['entity_id']} {len(x)}" for x in out))


if __name__ == "__main__":
    cmd = sys.argv[1:2]
    if cmd == ["card"]:
        card(sys.argv[2:])
    elif cmd == ["diff"] and len(sys.argv) == 4:
        diff(*sys.argv[2:])
    elif cmd == ["fetch"]:
        fetch(sys.argv[2:])
    elif len(sys.argv) >= 3 and sys.argv[2] in MODES:
        r = replay(sys.argv[1], sys.argv[2], sys.argv[3:])
        print(json.dumps({k: v for k, v in r.items() if k not in ("edges", "steps")}, default=list)[:2000])
    else:
        print(__doc__)
        raise SystemExit(1)
