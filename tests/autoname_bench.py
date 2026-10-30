"""The auto-naming bar against real signatures (INS-21; plan docs/auto-naming-plan.md).

    python3 tests/autoname_bench.py                # data/autoname/{home,kozolec,andrejg}.json + lib_{home,kozolec}.json

Two sources per site, both read-only:
  * the LIVE export of the detected-loads sensor's `signatures` attribute (with
    the owner's names; carries no first/last-seen or level count, so those
    tests are skipped or read from the description),
  * `lib_<site>.json`: a production-style replay of the last ten days
    (`tools/dumplib.py` style: every meter's signatures with all fields, NO
    names - a replay has none).
Prints, per source, every signature that clears the bar and, for the export,
each hand-named load with what stopped it.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load  # noqa: E402

A = load("insights.autoname")
DATA = Path(__file__).resolve().parents[1] / "data" / "autoname"


def facts_from_export(s: dict, names: dict) -> "A.Facts":
    m = re.search(r"over ([\d.]+) (days|h)\b", s.get("description", ""))
    span = None if not m else float(m.group(1)) / (1.0 if m.group(2) == "days" else 24.0)
    g = s.get("guess") or {}
    return A.Facts(ref=("", s["id"]), phases=s["phases"].lower(), watts=float(sum(s["watts_by_phase"].values())),
                   duration_s=float(s["typical_duration_s"]), interval_s=s["typical_interval_s"], count=s["count"],
                   span_days=span, evidence=s["evidence"], hour_wh=s["hour_wh"], day_wh=s["day_wh"],
                   guess_kind=g.get("kind"), guess_confidence=g.get("confidence") or 0.0,
                   appliance=g.get("appliance"), named=names.get(s["id"]))


def facts_from_lib(s: dict) -> "A.Facts":
    g = s.get("guess") or {}
    return A.Facts(ref=(s["meter"], s["id"]), phases=s["phases"], watts=float(s["watts"]), duration_s=s["duration_s"],
                   interval_s=s["interval_s"], count=s["count"],
                   span_days=(s["last_seen"] - s["first_seen"]) / 86400.0, evidence=s["evidence"],
                   hour_wh=s["hour_wh"], day_wh=s["day_wh"], level_count=s["level_count"],
                   interval_mad=s["interval_mad"], guess_kind=g.get("kind"),
                   guess_confidence=g.get("confidence") or 0.0, appliance=g.get("appliance"), named=s.get("name"))


def line(f) -> str:
    return (f"{f.ref[0] or 'main':>10} #{f.ref[1]:<6} {f.phases:3s} {f.watts:6.0f} W run {f.duration_s:6.0f} s "
            f"every {f.interval_s or 0:7.0f} s x{f.count:<5} ev {f.evidence:.2f} busy {A.busy_hours(f.hour_wh):2d}")


def main() -> None:
    for site in ("home", "kozolec", "andrejg"):
        path = DATA / f"{site}.json"
        if path.exists():
            d = json.loads(path.read_text())
            names = {r: n for n, refs in d["named_loads"].items() for r in refs if isinstance(r, int)}
            print(f"=== {site}: live export, {len(d['signatures'])} signatures, named {sorted(d['named_loads'])}")
            clearing, hand = [], []
            for s in d["signatures"]:
                f = facts_from_export(s, names)
                v = A.recognise(f)
                if f.named:
                    hand.append((f, v))
                elif v.kind:
                    clearing.append((f, v))
            for f, v in hand:
                print(f"  NAMED {f.named!r:22} {line(f)} -> {v.kind or 'no: nearest ' + str(v.nearest) + ' fails ' + ', '.join(v.failed)}")
            for f, v in clearing:
                print(f"  CLEARS {v.kind:7} {line(f)}")
            print(f"  -> {len(clearing)} unnamed load(s) clear the bar")
        path = DATA / f"lib_{site}.json"
        if path.exists():
            d = json.loads(path.read_text())
            rows = [(facts_from_lib(s), s) for s in d["sigs"]]
            clearing = [(f, A.recognise(f)) for f, _ in rows]
            clearing = [(f, v) for f, v in clearing if v.kind]
            print(f"=== {site}: ten-day replay, {len(rows)} signatures (every meter)")
            for f, v in clearing:
                print(f"  CLEARS {v.kind:7} {line(f)}")
            print(f"  -> {len(clearing)} clear the bar")


if __name__ == "__main__":
    main()
