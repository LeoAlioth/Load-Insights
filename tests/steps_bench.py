"""Steps from a replay: how much of the grid's edges the meters inside it claim.

    python3.14 tests/steps_bench.py SITE FOLDER [DIAL=V ...]     # default SUBS=prod HOUSE=prod

Builds every meter's declared edges from a replayed Fleet, maps each channel onto the grid's
phases (``Fleet.phase_map``), and claims them inside-out (``insights/steps.py``). Prints, per
owner, the steps and how many grid edges of at least MIN_W they took, and what is left unclaimed
(an unmetered load). Read-only: the detector itself is not changed by it.
"""
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench as B  # noqa: E402
from _load import load  # noqa: E402

S = load("insights.steps")
MIN_W = 300.0


def depth_of(fleet, name):
    n, parent = 0, fleet.parents.get(name)
    while parent:
        n, parent = n + 1, fleet.parents.get(parent)
    return n + (1 if name != "grid" else 0)


def edges_of(fleet):
    """name -> [Edge], the grid's under "grid"."""
    out = {"grid": []}
    for ph, st in fleet.main.phases.items():
        noise = st.noise_at()
        out["grid"] += [S.Edge("grid", ph, g[0], g[1], noise) for g in st.declared]
    for name, det in fleet.subs.items():
        mp = fleet.phase_map(name)
        rows = []
        for c, st in det.phases.items():
            ph = mp.get(c)
            if not ph or len(ph) != 1:
                continue                      # unmapped, or a total over several phases
            noise = st.noise_at()
            rows += [S.Edge(name, ph, g[0], g[1], noise) for g in st.declared]
        out[name] = rows
    return out


def main(argv):
    site, folder, dials = argv[0], argv[1], argv[2:]
    B._apply(["SUBS=prod", "HOUSE=prod", *dials])
    fleet, _, _ = B._run(folder, site)
    edges = edges_of(fleet)
    depth = {n: depth_of(fleet, n) for n in edges}
    steps = S.build_steps(edges, depth, gain=lambda m: 1.0 if m == "grid" else fleet.gain(m, "p"))
    big = [s for s in steps if any(e.meter == "grid" and abs(e.size) >= MIN_W for e in s.edges)]
    by_owner = collections.Counter(s.owner for s in big)
    total = sum(by_owner.values())
    print(f"{site}: {len(steps)} steps, {total} with a grid edge of {MIN_W:.0f} W or more")
    for owner, n in by_owner.most_common():
        print(f"  {owner:22s} {n:6d}  {100 * n / total:5.1f} %")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
