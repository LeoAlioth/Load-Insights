import sys
import dw_bench as W
B = W.B
B._apply(["SUBS=prod", "HOUSE=prod"])
fleet, filed, _ = B._run(sys.argv[1] if len(sys.argv) > 1 else W.FOLDER, "home")
print("grid phases:", {p: (round(st.noise, 1), round(st.interval or 0, 1)) for p, st in sorted(fleet.main.phases.items())}, "(noise W, interval s)")
for name in ("Hiša", "Mansarda"):
    det = fleet.subs[name]
    print(f"{name}: map {fleet.phase_map(name)}  noise per own phase:", {c: round(st.noise or 0, 1) for c, st in sorted(det.phases.items())},
          " votes:", {c: {p: round(n) for p, n in r.items()} for c, r in (fleet.phase_votes.get(name) or {}).items()})
