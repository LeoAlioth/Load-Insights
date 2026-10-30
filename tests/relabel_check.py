"""Golden replay: every filed session, saved before a change that must not move any.

    python3.14 tests/relabel_check.py save SITE FOLDER [DIAL=V ...]      # writes data/scorecard/golden-<site>.json
    python3.14 tests/relabel_check.py compare SITE FOLDER [DIAL=V ...]   # lists the sessions that differ

Phases X, Y, Z are read back as a, b, c, so a pure rename of the grid's phases
compares equal (steps-and-claims plan, stage 1). Default dials SUBS=prod HOUSE=prod.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench as B  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "data" / "scorecard"
BACK = str.maketrans("XYZ", "abc")


def golden(folder: str, site: str) -> list:
    fleet, filed, _ = B._run(folder, site)
    rows = []
    for s in filed:
        who = s.parent_signature[0] if s.parent_signature else ""
        rows.append([round(s.start, 1), round(s.end, 1), s.phases.translate(BACK), round(s.energy_wh, 1), who,
                     s.signature_id if s.signature_id is not None else -1])
    return sorted(rows)


def main(argv):
    mode, site, folder, dials = argv[0], argv[1], argv[2], argv[3:]
    B._apply(["SUBS=prod", "HOUSE=prod", *dials])
    rows = golden(folder, site)
    path = OUT / f"golden-{site}.json"
    if mode == "save":
        OUT.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows))
        print(f"{len(rows)} sessions saved to {path}")
        return 0
    old = json.loads(path.read_text())
    a, b = {tuple(r[:5]) for r in old}, {tuple(r[:5]) for r in rows}
    ids_old = {tuple(r[:5]): r[5] for r in old}
    ids_new = {tuple(r[:5]): r[5] for r in rows}
    moved = [k for k in a & b if ids_old[k] != ids_new[k]]
    print(f"{len(old)} saved, {len(rows)} now; only saved {len(a - b)}, only now {len(b - a)}, "
          f"same session in another signature {len(moved)}")
    for k in sorted(a - b)[:10]:
        print("  only saved", k)
    for k in sorted(b - a)[:10]:
        print("  only now  ", k)
    return int(bool(a ^ b))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
