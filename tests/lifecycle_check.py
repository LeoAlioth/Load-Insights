"""How runs live and end, beside the scorecard (run-lifecycle plan, Task 0).

    python3.14 tests/lifecycle_check.py SITE [FOLDER] [MODE=none|prod] [DIAL=V ...]

One replay at SLICE=6 with the last LIVE days (3 by default) in one-minute
passes. After every pass, over every detector: the most open starts on one
phase, open starts sharing a step group (owned and not), open starts older
than 3x their group's usual length, and - at Home with the meters fed - the
Inkubator's live power (Hiša's phase-C 10-100 W, 10-60 s signatures named so,
as the 2026-10-28 trace did) against its ceiling. The closes by kind are the
scorecard's pairing line.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import bench as B

D = B.D
ROOT = Path(__file__).resolve().parent.parent


def main(argv: list) -> None:
    site = argv[0]
    folder = next((a for a in argv[1:2] if "=" not in a), f"{ROOT}/data/history/{site.split('-')[0]}")
    mode = next((a[5:] for a in argv if a.startswith("MODE=")), "prod")
    dials = [a for a in argv[1:] if "=" in a and not a.startswith("MODE=")]
    if not any(a.startswith("LIVE=") for a in dials):
        dials.append("LIVE=3")
    if site in B.PROD_HOUSE and not any(a.startswith("HOUSE=") for a in dials):
        dials.append("HOUSE=prod")
    B._apply([f"SUBS={mode}"] + dials)
    st = {"passes": 0, "max_open": Counter(), "same_group": Counter(), "stale": 0, "open_seen": 0,
          "ink_max": 0.0, "ink_ceiling": 0.0, "ink_over": 0, "ink_passes": 0}
    orig = D.Fleet.process

    def process(self, *a, **k):
        orig(self, *a, **k)
        now = k.get("now_ts") if k.get("now_ts") is not None else a[4]
        st["passes"] += 1
        for key, det in self.detectors():
            for ph, s in det.phases.items():
                st["max_open"][key or "main"] = max(st["max_open"][key or "main"], len(s.open_edges))
                groups = Counter((o.cluster, bool(o.meter)) for o in s.open_edges if o.cluster is not None)
                for (cid, owned), n in groups.items():
                    if n > 1:
                        st["same_group"]["owned" if owned else "plain"] += 1
                for o in s.open_edges:
                    st["open_seen"] += 1
                    usual = det.usual_length(o.cluster) if o.cluster is not None else None
                    if usual and now - o.since > 3.0 * usual:
                        st["stale"] += 1
        hisa = self.subs.get("Hiša") if hasattr(self, "subs") else None
        if hisa is None:
            return
        for sig in hisa.signatures:
            if not sig.name and sig.count >= 3 and sig.phases in ("c", "Z") \
                    and 10 <= sig.watts <= 100 and 10 <= sig.duration_s <= 60:
                hisa.rename(sig.id, "Inkubator")
        live = sum(w for n, _, w in hisa.live_runs(now) if n == "Inkubator")
        ceiling = self.live_ceiling().get("Inkubator", 0.0)
        st["ink_passes"] += 1
        st["ink_max"] = max(st["ink_max"], live)
        st["ink_ceiling"] = max(st["ink_ceiling"], ceiling)
        st["ink_over"] += live > ceiling > 0

    D.Fleet.process = process
    B._replay(folder, site)
    print(f"{site} {mode} {' '.join(dials)}: {st['passes']} passes")
    print("  most open starts on one phase: " + ", ".join(f"{k} {v}" for k, v in sorted(st["max_open"].items())))
    print(f"  passes x phases with open starts sharing a group: plain {st['same_group']['plain']}, "
          f"owned {st['same_group']['owned']}")
    print(f"  open starts older than 3x their group's usual length: {st['stale']} of {st['open_seen']} seen")
    if st["ink_passes"]:
        print(f"  Inkubator live: max {st['ink_max']:.0f} W, ceiling {st['ink_ceiling']:.0f} W, "
              f"over it (before the cap) in {st['ink_over']} of {st['ink_passes']} passes")


if __name__ == "__main__":
    main(sys.argv[1:])
