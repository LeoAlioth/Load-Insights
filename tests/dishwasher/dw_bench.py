"""Dishwasher vs Sock Eater on Home's Mansarda circuit, scored against truth.json.

    python3 dw_bench.py [KEY=VAL ...]     # dials go to bench._apply, e.g. SUBS=prod

Truth (truth.json, built from Mansarda phase C + the AEG states):
  dishwasher  flat ~2 kW blocks, Sock Eater idle, oven idle (9 clear runs)
  sock_eater  heater bursts while its AEG state is Running/Paused
A filed session on phase C counts for a class when at least half of its length
lies inside one of that class's burst windows (a minute of slack at each end).
Scored per class: capture (truth bursts hit by a session), and per signature
the share of its phase-C sessions in each class.
"""
import collections
import json
import sys
from pathlib import Path

TESTS = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, TESTS)
import bench as B  # noqa: E402

HERE = Path(__file__).resolve().parent
FOLDER = "/Users/anzek/Projects/Load_Insights/data/history/home-1022"
SLACK = 60.0
truth = json.load(open(HERE / "truth.json"))
CLASSES = {k: [(x["start"] - SLACK, x["end"] + SLACK) for x in truth[k]]
           for k in ("dishwasher", "sock_eater", "oven")}


def inside(s, ivs):
    d = max(s.end - s.start, 1.0)
    return sum(max(0.0, min(s.end, b) - max(s.start, a)) for a, b in ivs) / d


def corr(sig):
    """Report-only: how tied a signature's runs are to its fed devices' states.
    Per input, the bits by which the states its runs fell in beat their share of
    the time (sum of n_v/N * log2(n_v / e_v), n = its runs per state, e = the
    runs chance would put there); the best input and state are named. 0 = none."""
    best = (0.0, "", "")
    for name, row in (getattr(sig, "inputs", None) or {}).items():
        n, e = row.get("n") or {}, row.get("e") or {}
        N = sum(n.values())
        if N < 3:
            continue
        bits, top = 0.0, ("", 0.0)
        for v, c in n.items():
            if c > 0 and e.get(v, 0.0) > 0:
                bits += (c / N) * __import__("math").log2(c / e[v])
                if c / N > top[1]:
                    top = (v, c / N)
        if bits > best[0]:
            best = (bits, name.replace("sensor.aeg_", "").replace("select.aeg_", ""), top[0])
    return best


def classify(s):
    best = max(CLASSES, key=lambda k: inside(s, CLASSES[k]))
    return best if inside(s, CLASSES[best]) >= 0.5 else "other"


def main(dials):
    B._apply(dials)
    fleet, filed, _ = B._run(FOLDER, "home")
    det = fleet.main
    by_id = {x.id: x for x in det.signatures}

    def sid_of(s):
        if s.parent_signature:
            name, own = s.parent_signature
            while own.parent_signature:
                name, own = own.parent_signature
            d = fleet.subs.get(name)
            # the signature as it stands now: a merge retires ids (_moved), and the
            # session keeps the one it was filed under
            sid = d._current(own.signature_id) if d is not None and own.signature_id is not None else own.signature_id
            return f"{name}#{sid}"
        return str(det._current(s.signature_id)) if s.signature_id is not None else "none"

    # only the grid meter's and Mansarda's own sessions: a load on another meter
    # (Susilna's heater, on the same grid phase) is a different load whatever its time
    # ...and on grid phase a only: Mansarda's C is the grid's A (Susilna's switch-on moved
    # M1 a by its 1.3 kW, 15 Oct 10:23), the 1.3 kW load on the grid's c beside it is another
    rows = [(s, classify(s), sid_of(s)) for s in filed if s.energy_wh > 0 and max(s.power_by_phase().values(), default=0.0) >= 1200.0
            and s.phases == "a" and (sid_of(s).startswith("Mansarda#") or "#" not in sid_of(s))]
    rows = [r for r in rows if r[2].startswith("Mansarda#") or r[2].isdigit()]
    per_sig = collections.defaultdict(collections.Counter)
    for s, cls, sid in rows:
        per_sig[sid][cls] += 1
    out = {"dials": dials, "sessions_c": len(rows)}
    for cls in ("dishwasher", "sock_eater"):
        ivs = truth[cls]
        hit = [x for x in ivs if any(c == cls and s.end >= x["start"] - SLACK and s.start <= x["end"] + SLACK for s, c, _ in rows)]
        sigs = collections.Counter(sid for s, c, sid in rows if c == cls)
        n = sum(sigs.values())
        best = sigs.most_common(1)[0] if sigs else (None, 0)
        pure = (best[1] / sum(per_sig[best[0]].values())) if best[0] else 0.0
        out[cls] = {"bursts": len(ivs), "bursts_hit": len(hit), "sessions": n,
                    "in_best_signature": best[1], "best": best[0], "best_signature_purity": round(pure, 2),
                    "concentration": round(best[1] / n, 2) if n else 0.0,
                    "signatures_used": len(sigs)}
    print(json.dumps(out, indent=1))
    def sig_obj(sid):
        if "#" in str(sid):
            name, _, i = str(sid).partition("#")
            d = fleet.subs.get(name)
            return next((x for x in (d.signatures if d else []) if str(x.id) == i), None)
        return by_id.get(int(sid)) if str(sid).isdigit() else None
    print("signatures holding any dishwasher/sock-eater session (corr = bits tied to a device state, report only):")
    for sid, c in sorted(per_sig.items(), key=lambda kv: -sum(kv[1].values())):
        if c["dishwasher"] or c["sock_eater"]:
            o = sig_obj(sid); cb = corr(o) if o else (0.0, "", "")
            print(f"  {sid:>14}  dw {c['dishwasher']:3d}  se {c['sock_eater']:3d}  oven {c['oven']:3d}  other {c['other']:3d}   corr {cb[0]:.2f} bits  {cb[1]} {cb[2]}")


if __name__ == "__main__":
    main(sys.argv[1:])
