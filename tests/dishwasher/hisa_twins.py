"""Why do Hiša's born-in 60 W fragments not merge back into the ordinary twin (#2)?
Prints every gate of Signature.alike for each fragment against the biggest signature."""
import sys
import dw_bench as W
B = W.B
B._apply(["SUBS=prod", "HOUSE=prod", "SLICE=0", *sys.argv[1:]])
fleet, filed, _ = B._run(W.FOLDER, "home")
det = fleet.subs["Hiša"]
D = B.D
sigs = [s for s in det.signatures if s.phases == "c" and 50 <= sum(s.power.values()) <= 70 and s.count >= 5]
big = max(sigs, key=lambda s: s.count)
noise = det.phases["c"].noise
print(f"twin #{big.id} runs {big.count:.0f} {sum(big.power.values()):.0f} W ~{big.duration_s:.0f} s mad {big.power_mad:.1f} split {big.split_s} pf {big.pf}")
for s in sorted(sigs, key=lambda s: -s.count):
    if s is big:
        continue
    born = s.born_in or big.born_in
    lift = s.input_lift(big, born) if born else None
    gates = {
        "len": s.takes_len(big.duration_s) and big.takes_len(s.duration_s),
        "lift": lift,
        "power": abs(sum(s.power.values()) - sum(big.power.values())),
        "pf": (s.pf, big.pf, D.pf_tolerance(s.pf_mad, big.pf_mad) if s.pf is not None and big.pf is not None else None),
        "alike": s.alike(big, noise),
    }
    print(f"  #{s.id:4d} runs {s.count:5.0f} ~{s.duration_s:4.0f} s split {s.split_s} born {s.born_in!r} judged {s.born_judged} takes {s.takes_in}  {gates}")
