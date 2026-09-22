"""A bench for trying different ways of grouping sessions into appliances.

The detector files each closed session into a signature by walking the library
and taking the first one within a tolerance. That tolerance is hand-set, and
where a session is coarsely sampled it opens to four times its nominal width -
which is how one signature at Anze's house came to hold 1323 sightings spanning
382 to 892 W. The NILM literature does this differently: it CLUSTERS the events
and lets the modes of the distribution decide where the boundaries are, rather
than being told a tolerance up front.

This runs both over the same sessions and scores them, so the comparison is a
number rather than an opinion.

    python3 tests/cluster_lab.py kozolec
    python3 tests/cluster_lab.py home

GROUND TRUTH comes from the submeters. Kozolec's boiler, pressure pump, well
pump and pond charger each have a meter of their own, so a main-meter session
can be labelled by which device's energy rose while it ran - the same test the
window matcher uses. A clustering is then scored on two things a library is
for:

  PURITY       of the sessions in a cluster that carry a label, how many
               share the majority one. A cluster holding the boiler and the
               well pump is not a signature of anything.
  FRAGMENTS    how many clusters one device's sessions are spread over. The
               boiler appearing as six signatures is six rows to name.

Neither is meaningful alone: one cluster per session is perfectly pure and
useless, one cluster for everything is perfectly unfragmented and useless.
"""
from __future__ import annotations

import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load                                  # noqa: E402
import replay as R                                      # noqa: E402

D = load("insights.detect")
ROOT = Path(__file__).resolve().parents[1]

SITES = {
    "kozolec": {
        "glob": "data/history/kozolec/*.csv",
        "main": {"a": "sensor.multiplus_ii_48_15000_200_100_id_276_output_power_l1"},
        "subs": {
            "Boiler": "sensor.shellypro4pm_kozolec_switch_1_power",
            "Hidrofor": "sensor.kozolec_hidrofor_power",
            "Well pump": "sensor.kotlovnica_well_pump_power",
            "Pond EVSE": "sensor.pond_evse_power",
            "Pastir": "sensor.pastir_staja_power",
            "Bug lamp": "sensor.bug_lamp_power",
        },
    },
    "home": {
        "glob": "data/history/home/*.csv",
        "main": {p: f"sensor.se17k_home_power_phase_{p}" for p in "abc"},
        "subs": {
            "Workshop boiler": "sensor.workshop_boiler_power",
            "Attic AC": "sensor.attic_ac_power",
            "Hidrofor": "sensor.hidrofor_power",
            "EVBox": "sensor.evbox_elvi_power_active_import",
            "NASA station": "sensor.nasa_station_power",
            "Server UPS": "sensor.server_ups_power",
            "Susilna": "sensor.shellypmminig3_susilna_power",
        },
    },
}


# ----------------------------------------------------------------- the events
def sessions_for(site: dict):
    """Every session the detector closes, and the device meter that was on."""
    series = R.read_csv(sorted(str(ROOT / p) for p in [site["glob"]]), False) \
        if False else R.read_csv(sorted(__import__("glob").glob(str(ROOT / site["glob"]))), False)
    main = {p: series[e] for p, e in site["main"].items() if e in series}
    subs = {n: series[e] for n, e in site["subs"].items() if e in series}
    if not main:
        raise SystemExit("no main readings found - check the entity names")

    det = D.Detector()
    det.tz_offset_s = 0.0
    for p in main:
        det.phases[p].floor_zero = D.carries_generation(main[p]) is False

    closed, at = [], min(r[0] for rows in main.values() for r in rows)
    end_all = max(r[0] for rows in main.values() for r in rows)
    SLICE = 6 * 3600.0
    while at < end_all:
        end = at + SLICE
        batch = {p: [r for r in rows if at <= r[0] < end] for p, rows in main.items()}
        batch = {p: v for p, v in batch.items() if v}
        if batch:
            closed += det.process(batch, now_ts=end)
        at = end
    return closed, subs, det


def label(sessions, subs):
    """Which device meter's energy ROSE while each session ran."""
    out = {}
    for i, s in enumerate(sessions):
        span = s.duration_s
        if span <= 0:
            continue
        want = s.energy_wh
        if want <= 0:
            continue
        best, best_gap = None, None
        for name, rows in subs.items():
            got = D.energy_between(rows, s.start, s.end)
            if got is None:
                continue
            look = min(span, D.IDLE_WINDOW_S)
            before = D.energy_between(rows, s.start - look, s.start)
            if before is None:
                continue
            rose = got - before * (span / look)
            if rose <= 0:
                continue
            ratio = rose / want
            if D.ENERGY_MATCH_LO <= ratio <= D.ENERGY_MATCH_HI:
                gap = abs(ratio - 1.0)
                if best_gap is None or gap < best_gap:
                    best, best_gap = name, gap
        if best:
            out[i] = best
    return out


# ------------------------------------------------------------- the clusterers
def cluster_as_today(sessions, det_for_noise):
    """What the detector does now: file each session into the first signature
    within a tolerance, the tolerance widening when the session is coarse."""
    fresh = D.Detector()
    fresh.tz_offset_s = 0.0
    fresh.phases = det_for_noise.phases
    assign = {}
    for i, s in enumerate(sessions):
        fresh._file(s)
        assign[i] = s.signature_id
    return assign


def _mean_shift(xs, bandwidth, iters=60):
    """1-D mode seeking. No dependency, and none needed: a few thousand points
    on one axis is what this costs."""
    xs = sorted(xs)
    modes = []
    for x in xs:
        m = x
        for _ in range(iters):
            lo, hi = m - bandwidth, m + bandwidth
            near = [v for v in xs if lo <= v <= hi]
            if not near:
                break
            nm = sum(near) / len(near)
            if abs(nm - m) < 1e-9:
                break
            m = nm
        modes.append(m)
    merged = []
    for m in sorted(modes):
        if merged and abs(m - merged[-1]) <= bandwidth / 2:
            continue
        merged.append(m)
    return merged


def cluster_by_mode(sessions, rel=0.12, dur_factor=3.0):
    """Let the data say where the boundaries are.

    Per phase set, seek the modes of log(watts) - a bandwidth in log space is a
    PROPORTION of the power, so one number covers a 20 W load and a 20 kW one -
    then split each mode by duration class, because a load that runs for twenty
    seconds and one that runs for twenty minutes are not the same appliance
    however alike their draw."""
    by_phase = defaultdict(list)
    for i, s in enumerate(sessions):
        w = sum(s.power_by_phase().values())
        if w <= 0:
            continue
        by_phase[s.phases].append((i, math.log(w), s.duration_s))

    assign, next_id = {}, 1
    for phases, items in by_phase.items():
        modes = _mean_shift([x for _, x, _ in items], math.log1p(rel))
        buckets = defaultdict(list)
        for i, lw, dur in items:
            k = min(range(len(modes)), key=lambda j: abs(modes[j] - lw))
            buckets[k].append((i, dur))
        for k, members in buckets.items():
            # split the mode by duration class, longest first
            classes = []
            for i, dur in sorted(members, key=lambda m: -m[1]):
                for c in classes:
                    if c["ref"] / max(dur, 1.0) <= dur_factor:
                        c["ids"].append(i)
                        break
                else:
                    classes.append({"ref": max(dur, 1.0), "ids": [i]})
            for c in classes:
                for i in c["ids"]:
                    assign[i] = next_id
                next_id += 1
    return assign


# ------------------------------------------------------------------ the score
def score(assign, labels, sessions):
    clusters = defaultdict(list)
    for i, cid in assign.items():
        clusters[cid].append(i)
    pure_hits = pure_total = 0
    for cid, ids in clusters.items():
        got = [labels[i] for i in ids if i in labels]
        if not got:
            continue
        top = Counter(got).most_common(1)[0][1]
        pure_hits += top
        pure_total += len(got)
    # CONCENTRATION, not a count of fragments. Counting them says the boiler
    # is in 24 clusters and sounds like a disaster; 383 of its 489 sessions
    # are in ONE of them and the rest are a tail of real behaviour - 21 s
    # short cycles and 1449 s heat-ups from the same thermostat. What matters
    # is how much of a device lands in its main cluster, because that is the
    # row its owner would name (2026-09-22).
    spread = defaultdict(Counter)
    for i, name in labels.items():
        if i in assign:
            spread[name][assign[i]] += 1
    conc = {}
    for name, c in spread.items():
        total = sum(c.values())
        conc[name] = (c.most_common(1)[0][1] / total, total, len(c))
    return {
        "clusters": len(clusters),
        "purity": pure_hits / pure_total if pure_total else 0.0,
        "labelled": pure_total,
        "concentration": conc,
    }


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "kozolec"
    site = SITES[which]
    sessions, subs, det = sessions_for(site)
    labels = label(sessions, subs)
    print(f"{which}: {len(sessions)} sessions, {len(labels)} of them labelled by a submeter")
    print()
    for name, assign in (("as it works today", cluster_as_today(sessions, det)),
                         ("by mode seeking", cluster_by_mode(sessions))):
        r = score(assign, labels, sessions)
        print(f"  {name}: {r['clusters']} clusters, purity {r['purity']:.0%}")
        for n, (pct, total, nc) in sorted(r["concentration"].items(), key=lambda kv: -kv[1][1]):
            print(f"     {n:<12} {pct:>4.0%} of its {total:>4} sessions in one cluster "
                  f"(a tail of {nc - 1} more)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def describe_truth(sessions, labels):
    """What one device's sessions actually look like, which is the thing any
    clustering has to cope with."""
    by = defaultdict(list)
    for i, name in labels.items():
        s = sessions[i]
        by[name].append((sum(s.power_by_phase().values()), s.duration_s, s.energy_wh))
    print()
    print("  what a single device's sessions look like at the main meter:")
    for name, rows in sorted(by.items(), key=lambda kv: -len(kv[1])):
        ws = sorted(r[0] for r in rows)
        ds = sorted(r[1] for r in rows)
        es = sorted(r[2] for r in rows)
        def q(v, f):
            return v[int(len(v) * f)] if v else 0.0
        print(f"     {name:<12} n={len(rows):<5} "
              f"W p10..p90 {q(ws,.1):>6.0f}..{q(ws,.9):<6.0f} "
              f"s p10..p90 {q(ds,.1):>5.0f}..{q(ds,.9):<6.0f} "
              f"Wh p10..p90 {q(es,.1):>6.1f}..{q(es,.9):<6.1f}")


def sweep(sessions, labels, rels=(0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.65)):
    """Where purity and fragmentation trade off. One cluster per session is
    perfectly pure and useless; one cluster for everything is perfectly
    unfragmented and useless. The interesting question is where between them a
    device stops being six rows without starting to hold two appliances."""
    print()
    print("  bandwidth sweep - mode seeking on log power, split by duration class")
    print(f"     {'rel':>5}  {'clusters':>8}  {'purity':>6}  fragments per device")
    for rel in rels:
        a = cluster_by_mode(sessions, rel=rel)
        r = score(a, labels, sessions)
        frag = " ".join(f"{n.split()[0][:8]}:{c[0]:.0%}" for n, c in r["concentration"].items())
        print(f"     {rel:>5.2f}  {r['clusters']:>8}  {r['purity']:>5.0%}   {frag}")
