"""Score a detector setting against the two sites' own history, the way every
dial in AGENTS.md was chosen. No Home Assistant; runs in about two minutes a
site and ten at once on a laptop.

    python3 tests/bench.py score  SITE FOLDER [DIAL=VALUE ...]
    python3 tests/bench.py kiln   FOLDER [DIAL=VALUE ...]
    python3 tests/bench.py surge  SITE FOLDER [DIAL=VALUE ...]

SITE is a key of cluster_lab.SITES (home, kozolec); FOLDER a directory of the
per-day CSVs fetch_history.py writes. Build tuning and hold-out folders of
symlinks rather than pointing at data/history/<site> while a fetch is
writing to it - runs started seconds apart would read different days.

DIAL is any module-level constant of insights/detect.py, plus one of the
bench's own:

    HOUSE=prod   build Home's house reading the way PRODUCTION does - the grid
                 meter negated plus a third of the inverter, through combine()
                 with COMBINE_SETTLE_S - rather than reading Anze's template
                 sensor. They agree to 0.1 W but not in timing, and the scores
                 differed (purity 67.6 vs 68.4 %). Always use it at Home.

score   purity (does one signature hold one device) and concentration (does
        one device land in one signature), per device with the ABSOLUTE size
        of its dominant cluster beside the share: a share can rise because
        sessions were removed, a dominant cluster that grows cannot.
kiln    what a setting does to Home's kiln, which has no sub-meter and so is
        invisible to `score`. Against the pulses the grid meter itself shows,
        counts what was filed inside the firings: full-size A+C sessions, the
        spurious ladder of smaller A+C ones, and sessions on one leg only.
surge   for each metered device, whether its dominant signature carries a
        motor's starting surge - checked against what the device physically is.
"""
from __future__ import annotations

import bisect
import collections
import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cluster_lab as lab  # noqa: E402
import replay as R  # noqa: E402

D = R.D
MIN_SESSIONS = 60                      # devices below this are too few to read
FIRING_MIN_PULSES = 20                 # fewer is two 3 kW loads coinciding, not a firing

# What each metered device physically is, for `surge`. Kozolec's hidrofor is a
# Grundfos Scala2 with a built-in frequency converter, so it soft-starts.
PHYSICS = {
    "Hidrofor": "motor", "Well pump": "motor", "Attic AC": "motor",
    "Boiler": "resistive", "Workshop boiler": "resistive",
    "NASA station": "electronic", "Server UPS": "electronic", "EVBox": "electronic",
    "Pond EVSE": "electronic", "Pastir": "electronic", "Bug lamp": "electronic",
}
_read_csv = R.read_csv


def _production_house() -> None:
    def read(paths, keep):
        s = _read_csv(paths, keep)
        inv = s.get("sensor.solaredge_se17k_i1_ac_power")
        if not inv:
            return s
        for p in "abc":
            m1 = s.get(f"sensor.solaredge_se17k_m1_ac_power_{p}")
            if m1:
                s[f"sensor.se17k_home_power_phase_{p}"] = D.combine(
                    [(m1, -1.0), (inv, 1.0 / 3.0)], settle_s=D.COMBINE_SETTLE_S)
        return s
    R.read_csv = read


def _apply(dials) -> str:
    for d in dials:
        k, v = d.split("=", 1)
        if k == "HOUSE":
            if v == "prod":
                _production_house()
            continue
        if not hasattr(D, k):
            raise SystemExit(f"no such dial: {k}")
        setattr(D, k, type(getattr(D, k))(float(v)))
    return " ".join(dials) or "defaults"


def _run(folder: str, site: str | None):
    """Replay a folder; return the main detector, every session it filed, and
    each sub-meter's full series."""
    filed, seen, full = [], {}, {}
    of, op = D.Detector._file, D.Fleet.process

    def spy_file(self, s):
        of(self, s)
        if self is seen.get("main"):
            filed.append(s)

    def spy_proc(self, m, sub, *a, **kw):
        seen["main"] = self.main
        for name, byp in (sub or {}).items():
            merged = full.get(name, [])
            for rows in byp.values():
                merged = D._sum_series(merged, list(rows))
            full[name] = merged
        return op(self, m, sub, *a, **kw)

    D.Detector._file, D.Fleet.process = spy_file, spy_proc
    argv = ["replay.py", folder]
    if site:
        for n, e in lab.SITES[site]["subs"].items():
            argv += ["--sub", f"{n}={e}"]
    saved, sys.argv = sys.argv, argv
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            R.main()
    finally:
        sys.argv = saved
        D.Detector._file, D.Fleet.process = of, op
    return seen["main"], filed, {k: sorted(v) for k, v in full.items() if v}


def score(site: str, folder: str, dials) -> None:
    tag = _apply(dials)
    det, filed, subs = _run(folder, site)
    assign = {i: det.signature_of(s).id for i, s in enumerate(filed) if det.signature_of(s)}
    labels = lab.label(filed, subs)
    r = lab.score(assign, labels, filed)
    big = {n: v for n, v in r["concentration"].items() if v[1] >= MIN_SESSIONS}
    tot = sum(v[1] for v in big.values()) or 1
    wc = sum(v[0] * v[1] for v in big.values()) / tot
    per = "  ".join(f"{n.split()[0]}:{v[0]*100:.0f}%={round(v[0]*v[1])}/{v[1]}x{v[2]}"
                    for n, v in sorted(big.items(), key=lambda x: -x[1][1]))
    iv = "/".join(f"{st.interval:.1f}" for st in det.phases.values() if st.interval)
    print(f"  {tag:44s} iv {iv:13s} sigs {r['clusters']:3d}  purity {r['purity']*100:5.1f}%"
          f"  wconc {wc*100:5.1f}%   {per}")


def _kiln_pulses(folder: str) -> list:
    """The kiln's pulses as the grid meter itself shows them: A and C dropping
    ~3 kW together (m1 reads minus the house, so a load switching on is a
    drop). The truth `kiln` scores against - no detector involved."""
    s = _read_csv([folder], False)
    a = s.get("sensor.solaredge_se17k_m1_ac_power_a") or []
    c = s.get("sensor.solaredge_se17k_m1_ac_power_c") or []

    def at(rows, t):
        i = bisect.bisect_right(rows, (t, float("inf"))) - 1
        return rows[i][1] if i >= 0 else None

    pulses = []
    for i in range(1, len(a)):
        t, w = a[i]
        if not 2600 < a[i - 1][1] - w < 3400:
            continue
        cb, ca = at(c, a[i - 1][0]), at(c, t + 7.0)
        if cb is None or ca is None or not 2400 < cb - ca < 3500:
            continue
        if pulses and t - pulses[-1] < 20:
            continue                      # the same pulse's second reading
        pulses.append(t)
    return pulses


def _firings(pulses: list) -> list:
    """Group pulses into firings: runs of at least FIRING_MIN_PULSES with no
    gap over half an hour. A lone coincidence of two 3 kW loads is not one."""
    runs = []
    for t in pulses:
        if runs and t - runs[-1][-1] < 1800:
            runs[-1].append(t)
        else:
            runs.append([t])
    return [(r[0] - 60, r[-1] + 60) for r in runs if len(r) >= FIRING_MIN_PULSES]


def kiln(folder: str, dials) -> None:
    """Scored by what the detector FILED inside the kiln's firings, against the
    pulses the raw meter shows. Counting signatures in a power band instead -
    what this printed until 2026-09-23 - also caught Home's other 3 kW loads
    on one phase (two of them, 2.5 and 5 min long, run on days the kiln never
    fired) and depended on which signatures the library happened to keep."""
    tag = _apply(dials)
    det, filed, _ = _run(folder, None)
    pulses = _kiln_pulses(folder)
    fires = _firings(pulses)
    inside = lambda t: any(a <= t <= b for a, b in fires)  # noqa: E731
    watts = lambda s: sum(max(v for _, v in lv) for lv in s.levels.values())  # noqa: E731
    n = collections.Counter()
    for s in filed:
        if not inside(s.start):
            continue
        w, long = watts(s), s.end - s.start > 90
        if s.phases == "ac" and 5400 <= w <= 6400:
            n["full"] += 1
            n["glued"] += long            # two pulses read as one
        elif s.phases == "ac" and 600 <= w < 5400:
            n["ladder"] += 1
        elif s.phases in ("a", "c") and 2500 <= w <= 3400:
            n["single long" if long else "single"] += 1
    raw = sum(1 for t in pulses if inside(t))
    tot = lambda s: sum(s.power.values())  # noqa: E731
    full = [s for s in det.signatures if set(s.power) == {"a", "c"} and 5400 <= tot(s) <= 6400]
    top = max(full, key=lambda s: s.count) if full else None
    print(f"  {tag:44s} kiln {raw} pulses in {len(fires)} firings: full-size {n['full']:4d}"
          f" ({n['glued']} over 90 s)   ladder {n['ladder']:3d}"
          f"   single-leg {n['single'] + n['single long']:3d} ({n['single long']} over 90 s)"
          f"   top signature x{top.count if top else 0} {top.duration_s if top else 0:4.1f} s")


def surge(site: str, folder: str, dials) -> None:
    tag = _apply(dials)
    det, filed, subs = _run(folder, site)
    labels = lab.label(filed, subs)
    bydev = collections.defaultdict(collections.Counter)
    for i, name in labels.items():
        sig = det.signature_of(filed[i])
        if sig:
            bydev[name][sig.id] += 1
    byid = {s.id: s for s in det.signatures}
    print(f"  [{tag}]")
    for name, c in sorted(bydev.items()):
        if sum(c.values()) < 20:
            continue
        s = byid.get(c.most_common(1)[0][0])
        if s is None:
            continue
        w = sum(s.power.values())
        ratio = s.inrush_w / max(w, 1.0)
        print(f"     {name:18s} {PHYSICS.get(name, '?'):10s} {w:6.0f} W x{s.count:4d}"
              f"  surge ratio {ratio:5.2f}  seen {s.inrush_seen:3d}x at +{s.inrush_when_seen:5.0f} W")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == "score":
        score(sys.argv[2], sys.argv[3], sys.argv[4:])
    elif cmd == "kiln":
        kiln(sys.argv[2], sys.argv[3:])
    elif cmd == "surge":
        surge(sys.argv[2], sys.argv[3], sys.argv[4:])
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
