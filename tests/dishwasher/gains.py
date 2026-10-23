import sys, math
import dw_bench as W
B = W.B
B._apply(["SUBS=prod", "HOUSE=prod"])
fleet, filed, _ = B._run(W.FOLDER, "home")
for name in sorted(fleet.subs):
    acc = (fleet.meter_gain.get(name) or {}).get("p")
    if not acc:
        print(f"{name:22s} no gain learned"); continue
    ring = acc[2:]
    med = sorted(ring)[len(ring)//2] if ring else None
    print(f"{name:22s} n={acc[1]:5.0f} mean x{math.exp(acc[0]):.3f}  ring {len(ring):3d} median x{math.exp(med):.3f}" if med is not None else f"{name:22s} n={acc[1]:5.0f} mean x{math.exp(acc[0]):.3f}  no ring", f" -> gain() {fleet.gain(name, 'p'):.3f}")
