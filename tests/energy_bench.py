"""How much of each metered device's ENERGY the detector puts in that device's
signatures, and how much of those signatures' energy is really the device's -
recall and precision in watt-hours, the way a named load's energy statistics
will be right or wrong. Loads can also be PLANTED into the house reading, a
square wave of known size and timing, to score a load no meter watches.

    python3 tests/energy_bench.py home data/history/home [DIAL=value ...] [PLANT=watts:on_s:every_s:phase ...]
    python3 tests/energy_bench.py kozolec data/history/kozolec

PLANT=set1 plants three loads at Home: 1.2 kW for 8 min every 5 h on A,
150 W for 20 min every 3 h on B, 40 W for 45 min every 2 h on B. A plant is
added to the house reading, and to the grid meter's power and current so the
power factor stays consistent (a resistive load). Anze (2026-09-30): "a better
measurement mechanism to compare the current system to the new one" - this
scores the old and the new detector on the same footing, energy first, and
weighs precision above recall (F0.5), since under-reporting is the lesser evil.
"""
from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench as B  # noqa: E402

D, R = B.D, B.R
PLANTS: list = []          # (name, watts, phase, [(on, off), ...])
GRID = "sensor.solaredge_se17k_m1_ac_"
SETS = {"set1": ["1200:480:18000:a", "150:1200:10800:b", "40:2700:7200:b"]}


def _intervals(t0: float, t1: float, on_s: float, every_s: float, seed: int) -> list:
    rnd = random.Random(seed)
    out, t = [], t0 + rnd.uniform(0.2, 0.8) * every_s
    while t + on_s < t1:
        out.append((t, t + on_s))
        t += every_s * rnd.uniform(0.8, 1.2)
    return out


def _square(rows: list, ivs: list, amp) -> list:
    """rows plus a square wave: ``amp(ts, level_change)`` says what a change of
    level adds to the reading at that instant; a row is inserted at each edge
    so the step shows when it happened, not at the next reading."""
    edges = sorted([(a, 1) for a, b in ivs] + [(b, -1) for a, b in ivs])
    out, level, k, held = [], 0, 0, None
    for ts, w in rows:
        while k < len(edges) and edges[k][0] <= ts:
            level += edges[k][1]
            if held is not None and (not out or out[-1][0] < edges[k][0]):
                out.append((edges[k][0], held + amp(edges[k][0], level)))
            k += 1
        held = w
        out.append((ts, w + amp(ts, level)))
    return out


def _held(rows: list, ts: float, default: float) -> float:
    import bisect
    i = bisect.bisect_right(rows, (ts, float("inf"))) - 1
    return rows[i][1] if i >= 0 else default


def install(plants: list) -> None:
    """Plant the loads under whatever reader the bench installed."""
    specs = []
    for p in plants:
        specs += SETS.get(p, [p])
    if not specs:
        return
    orig = B._read_csv

    def read(paths, keep):
        s = orig(paths, keep)
        for k, spec in enumerate(specs):
            watts, on_s, every_s, ph = spec.split(":")
            watts, on_s, every_s = float(watts), float(on_s), float(every_s)
            house = s.get(B.HOUSE_IDS[ph])
            if not house:
                continue
            name = f"plant {watts:g} W {ph.upper()}"
            ivs = _intervals(house[0][0], house[-1][0], on_s, every_s, seed=11 + k)
            if not any(x[0] == name for x in PLANTS):
                PLANTS.append((name, watts, ph, ivs))
            s[B.HOUSE_IDS[ph]] = _square(house, ivs, lambda ts, lv: lv * watts)
            grid, amps = s.get(f"{GRID}power_{ph}"), s.get(f"{GRID}current_{ph}")
            volts = s.get(f"{GRID}voltage_{ph}n") or []
            if grid:
                raw = list(grid)
                s[f"{GRID}power_{ph}"] = _square(grid, ivs, lambda ts, lv: -lv * watts)
                if amps:
                    # the meter's current is unsigned: what the extra import does to it
                    def more(ts, lv, raw=raw, volts=volts):
                        g = _held(raw, ts, 0.0)
                        return (abs(g - lv * watts) - abs(g)) / max(_held(volts, ts, 230.0), 100.0)
                    s[f"{GRID}current_{ph}"] = _square(amps, ivs, more)
        return s
    if R.read_csv is orig:
        R.read_csv = read
    B._read_csv = read


def _rose(rows: list, s) -> float:
    """The device's energy above its idle draw while the session ran - as
    cluster_lab.label reads a device meter."""
    span = s.duration_s
    if span <= 0:
        return 0.0
    got = D.energy_between(rows, s.start, s.end)
    look = min(span, D.IDLE_WINDOW_S)
    before = D.energy_between(rows, s.start - look, s.start)
    if got is None or before is None:
        return 0.0
    return got - before * (span / look)


def _overlap(ivs: list, a: float, b: float) -> float:
    return sum(max(0.0, min(b, y) - max(a, x)) for x, y in ivs)


def energy(site: str, folder: str, dials: list) -> dict:
    plants = [d[6:] for d in dials if d.startswith("PLANT=")]
    tag = B._apply([d for d in dials if not d.startswith("PLANT=")])
    install(plants)
    det, filed, subs = B._run(folder, site)
    devices = B._labels_from(folder, site, subs)
    truth: dict = {}
    for name, rows in devices.items():
        # the device's idle draw: the level it holds a tenth of the TIME - by
        # time, since a meter that reports on change reads mostly while running
        held = sorted((w, t2 - t1) for (t1, w), (t2, _) in zip(rows, rows[1:]))
        span, acc, floor = sum(d for _, d in held), 0.0, 0.0
        for w, d in held:
            acc += d
            if acc >= 0.1 * span:
                floor = w
                break
        truth[name] = sum(max(0.0, w - floor) * d for w, d in held) / 3600.0
    for name, watts, _, ivs in PLANTS:
        truth[name] = watts * sum(b - a for a, b in ivs) / 3600.0
    per_sig: dict = {}
    for s in filed:
        if s.energy_wh <= 0:
            continue
        # the signature it stands in now, merges followed; one since evicted
        # still counts - its runs were detected and their energy written
        sig = det.signature_of(s)
        row = per_sig.setdefault(sig.id if sig is not None else f"gone {s.signature_id}", {"total": 0.0, "n": 0})
        row["total"] += s.energy_wh
        row["n"] += 1
        for name, rows in devices.items():
            rose = _rose(rows, s)
            if rose > 0:
                row[name] = row.get(name, 0.0) + min(rose, s.energy_wh)
        for name, watts, _, ivs in PLANTS:
            ov = _overlap(ivs, s.start, s.end)
            if ov > 0:
                row[name] = row.get(name, 0.0) + min(watts * ov / 3600.0, s.energy_wh)
    got = {name: {"in": 0.0, "of": 0.0, "sigs": []} for name in truth}
    for sid, row in per_sig.items():
        best = max((k for k in row if k not in ("total", "n")), key=row.get, default=None)
        if best is not None and row[best] >= 0.5 * row["total"]:
            got[best]["in"] += row[best]
            got[best]["of"] += row["total"]
            got[best]["sigs"].append((sid, row["n"], round(row["total"] / 1000.0, 2), round(row[best] / row["total"], 2)))
    out = {"site": site, "dials": tag, "devices": {}}
    total = sum(r["total"] for r in per_sig.values())
    print(f"  {tag}   {len(per_sig)} signatures with energy, {total/1000:.0f} kWh detected in all")
    # the signatures holding most of the detected energy: a blob here is
    # energy no name will ever fit
    big = sorted(per_sig.items(), key=lambda kv: -kv[1]["total"])[:5]
    for sid, row in big:
        sig = det._sig(sid) if isinstance(sid, int) and hasattr(det, "_sig") else None
        what = (f"{sig.phases.upper()} {sum(sig.power.values()):.0f} W ~{sig.duration_s/60:.0f} min" if sig is not None else "evicted")
        devs = " ".join(f"{k}:{v/row['total']:.0%}" for k, v in sorted(row.items(), key=lambda kv: -kv[1] if isinstance(kv[1], float) else 0) if k not in ("total", "n") and v >= 0.02 * row["total"])
        print(f"     #{sid}: {row['n']} runs, {row['total']/1000:.1f} kWh ({row['total']/total:.0%}), {what}  {devs}")
    out["detected_kwh"] = round(total / 1000.0, 1)
    print(f"  {'device':22s} {'truth kWh':>9s} {'in sigs kWh':>11s} {'sigs kWh':>9s} {'precision':>9s} {'recall':>7s}  signatures (id, runs, kWh, share)")
    wp = wr = wt = 0.0
    for name in sorted(truth, key=lambda n: -truth[n]):
        g, t = got[name], truth[name]
        if t < 50.0:                      # under 50 Wh over the replay: nothing to score
            continue
        prec = g["in"] / g["of"] if g["of"] else None
        rec = g["in"] / t
        out["devices"][name] = {"truth_kwh": round(t / 1000.0, 3), "in_kwh": round(g["in"] / 1000.0, 3),
                                "of_kwh": round(g["of"] / 1000.0, 3), "precision": prec, "recall": rec, "sigs": g["sigs"]}
        wt += t
        wr += rec * t
        wp += (prec if prec is not None else 0.0) * t
        sigs = " ".join(f"#{sid}:{n}x{kwh}kWh@{sh:.0%}" for sid, n, kwh, sh in sorted(g["sigs"], key=lambda x: -x[2])[:4])
        # ...and where the rest of its energy went: the signatures holding most
        # of it, whether or not the device is their majority
        went = sorted(((row.get(name, 0.0), sid, row) for sid, row in per_sig.items() if row.get(name, 0.0) > 0), reverse=True)[:3]
        went_s = " ".join(f"#{sid}:{e/1000:.2f}kWh={e/row['total']:.0%}of{row['n']}" for e, sid, row in went)
        out["devices"][name]["went"] = [(sid, round(e / 1000.0, 3), round(e / row["total"], 2), row["n"]) for e, sid, row in went]
        print(f"  {name:22s} {t/1000:9.2f} {g['in']/1000:11.2f} {g['of']/1000:9.2f} "
              f"{'-' if prec is None else f'{prec:8.0%}':>9s} {rec:7.0%}  {sigs}   holds most: {went_s}")
    if wt:
        P, Rc = wp / wt, wr / wt
        f = 1.25 * P * Rc / (0.25 * P + Rc) if P + Rc else 0.0
        out["precision"], out["recall"], out["f05"] = P, Rc, f
        print(f"  energy-weighted  precision {P:.1%}  recall {Rc:.1%}  F0.5 {f:.1%}   over {wt/1000:.1f} kWh of truth")
    if os.environ.get("OUT_JSON"):
        json.dump(out, open(os.environ["OUT_JSON"], "w"), indent=1)
    return out


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)
    energy(sys.argv[1], sys.argv[2], sys.argv[3:])
