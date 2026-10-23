import sys
import dw_bench as W
B = W.B
B._apply(["SUBS=prod", "HOUSE=prod", "SLICE=0", *sys.argv[1:]])
fleet, filed, _ = B._run(W.FOLDER, "home")
det = fleet.subs["Hiša"]
sigs = [s for s in det.signatures if s.phases == "c" and 50 <= sum(s.power.values()) <= 70 and s.count >= 5]
print(f"Hiša 50-70 W signatures on c with 5+ runs: {len(sigs)}, runs {sum(s.count for s in sigs):.0f}")
for s in sorted(sigs, key=lambda s: -s.count)[:12]:
    keys = {}
    for cid in (s.edges.get("start") or {}):
        c = det.cluster(int(cid)) if hasattr(det, "cluster") else None
        if c is not None and c.keys: keys = {k.replace("sensor.aeg_", ""): v for k, v in c.keys.items() if v}
    born = (s.born_in or "").replace("sensor.aeg_", "").replace("select.aeg_", "")
    print(f"  #{s.id:4d} {sum(s.power.values()):4.0f} W runs {s.count:5.0f} ~{s.duration_s:4.0f} s every ~{s.interval_s or 0:6.0f} s  keys {keys} born {born!r} judged {s.born_judged} takes {len(s.takes_in)}")
