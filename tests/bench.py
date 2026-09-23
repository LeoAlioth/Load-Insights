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
        invisible to `score`: full-size A+C sessions, the spurious ladder of
        smaller A+C ones, and single-leg sessions.
surge   for each metered device, whether its dominant signature carries a
        motor's starting surge - checked against what the device physically is.
"""
from __future__ import annotations

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


def kiln(folder: str, dials) -> None:
    tag = _apply(dials)
    det, _, _ = _run(folder, None)
    tot = lambda s: sum(s.power.values())  # noqa: E731
    ac = [s for s in det.signatures if set(s.power) == {"a", "c"}]
    full = [s for s in ac if 5400 <= tot(s) <= 6400]
    ladder = [s for s in ac if 600 <= tot(s) < 5400]
    single = [s for s in det.signatures if set(s.power) in ({"a"}, {"c"}) and 2500 <= tot(s) <= 3400]
    top = max(full, key=lambda s: s.count) if full else None
    print(f"  {tag:44s} kiln full-size {sum(s.count for s in full):4d} sessions"
          f" (top x{top.count if top else 0}, {top.duration_s if top else 0:4.1f} s)"
          f"   ladder {sum(s.count for s in ladder):4d}   single-leg {sum(s.count for s in single):4d}")


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
