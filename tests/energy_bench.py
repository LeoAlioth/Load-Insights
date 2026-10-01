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

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench as B  # noqa: E402

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


def planted(plants: list):
    """The series with the loads planted, for the bench to build its house
    from - or None, nothing to plant."""
    specs = []
    for p in plants:
        specs += SETS.get(p, [p])
    if not specs:
        return None

    def plant(s):
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
                        g = B._at(raw, ts, 0.0)
                        return (abs(g - lv * watts) - abs(g)) / max(B._at(volts, ts, 230.0), 100.0)
                    s[f"{GRID}current_{ph}"] = _square(amps, ivs, more)
        return s
    return plant


def _above(rows: list, times: list, a: float, b: float, floor: float) -> float:
    """The device's energy above its idle floor over [a, b], its readings held
    until the next - the same measure as its truth. Not what its meter ROSE
    by against the minutes before, as bench.label once read it: a charge
    that follows another has the last one in its "before", and read as
    nothing - 13.8 kWh of Kozolec's charger scored as no one's (2026-10-01)."""
    import bisect
    i = max(bisect.bisect_right(times, a) - 1, 0)
    wh, t = 0.0, a
    while i < len(rows) and t < b:
        nxt = rows[i + 1][0] if i + 1 < len(rows) else b
        end = min(b, nxt)
        if end > t and rows[i][0] <= t:
            wh += max(0.0, rows[i][1] - floor) * (end - t)
        t = max(t, end)
        i += 1
    return wh / 3600.0


def _started_with(rows: list, times: list, s) -> bool:
    """Did the device's meter switch on with the run - rise, at any moment
    from just before its start to a minute after, by at least half of what the
    run started at? Only then is the run the device's. Energy drawn DURING it
    is not enough: that credits every long run a device merely ran beside, and
    each of the overlapping ones again - Susilna scored 86 kWh right of 63
    (2026-10-01). And the peak, not the reading a minute on: a pump runs 60-70
    s, was off again by then, and a third of its own runs scored as nobody's -
    its group under half its own, and all of it "not found" (2026-10-01)."""
    import bisect
    first = sum(rows_[0][1] for rows_ in s.levels.values() if rows_)
    before = B._at(rows, s.start - 10.0, 0.0)
    i, j = bisect.bisect_right(times, s.start - 10.0), bisect.bisect_right(times, s.start + 60.0)
    peak = max([before] + [r[1] for r in rows[i:j]])
    return first > 0 and peak - before >= 0.5 * first


def _overlap(ivs: list, a: float, b: float) -> float:
    return sum(max(0.0, min(b, y) - max(a, x)) for x, y in ivs)


def energy(site: str, folder: str, dials: list) -> None:
    plants = [d[6:] for d in dials if d.startswith("PLANT=")]
    # QGATE=x: only runs whose quality is at least x have their energy
    # attributed - the rest are detected but left as no one's (see
    # QUALITY_SNR_FULL in detect.py)
    gate = next((float(d[6:]) for d in dials if d.startswith("QGATE=")), 0.0)
    tag = B._apply([d for d in dials if not d.startswith(("PLANT=", "QGATE="))]) + (f" QGATE={gate:g}" if gate else "")
    fleet, filed, subs = B._run(folder, site, planted(plants))
    det = fleet.main
    devices = B._labels_from(folder, site, subs)
    truth: dict = {}
    floors: dict = {}
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
        floors[name] = floor
    times = {name: [r[0] for r in rows] for name, rows in devices.items()}
    for name, watts, _, ivs in PLANTS:
        truth[name] = watts * sum(b - a for a, b in ivs) / 3600.0
    per_sig: dict = {}
    gated = 0.0
    for s in filed:
        if s.energy_wh <= 0:
            continue
        if s.quality < gate:
            gated += s.energy_wh
            continue
        # the signature it stands in now, merges followed; one since evicted
        # still counts - its runs were detected and their energy written
        sig = det.signature_of(s)
        row = per_sig.setdefault(sig.id if sig is not None else f"gone {s.signature_id}", {"total": 0.0, "n": 0})
        row["total"] += s.energy_wh
        row["n"] += 1
        for name, rows in devices.items():
            if not _started_with(rows, times[name], s):
                continue
            got_wh = _above(rows, times[name], s.start, s.end, floors[name])
            if got_wh > 0:
                row[name] = row.get(name, 0.0) + min(got_wh, s.energy_wh)
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
    total = sum(r["total"] for r in per_sig.values())
    print(f"  {tag}   {len(per_sig)} signatures with energy, {total/1000:.0f} kWh detected in all"
          + (f", {gated/1000:.0f} kWh more left unattributed below quality {gate:g}" if gate else ""))
    # the signatures holding most of the detected energy: a blob here is
    # energy no name will ever fit
    big = sorted(per_sig.items(), key=lambda kv: -kv[1]["total"])[:5]
    for sid, row in big:
        sig = det._sig(sid) if isinstance(sid, int) and hasattr(det, "_sig") else None
        what = (f"{sig.phases.upper()} {sum(sig.power.values()):.0f} W ~{sig.duration_s/60:.0f} min" if sig is not None else "evicted")
        devs = " ".join(f"{k}:{v/row['total']:.0%}" for k, v in sorted(row.items(), key=lambda kv: -kv[1] if isinstance(kv[1], float) else 0) if k not in ("total", "n") and v >= 0.02 * row["total"])
        print(f"     #{sid}: {row['n']} runs, {row['total']/1000:.1f} kWh ({row['total']/total:.0%}), {what}  {devs}")
    print(f"  {'device':22s} {'truth kWh':>9s} {'in sigs kWh':>11s} {'sigs kWh':>9s} {'precision':>9s} {'recall':>7s}  signatures (id, runs, kWh, share)")
    wp = wr = wt = wrong = 0.0
    for name in sorted(truth, key=lambda n: -truth[n]):
        g, t = got[name], truth[name]
        if t < 50.0:                      # under 50 Wh over the replay: nothing to score
            continue
        prec = g["in"] / g["of"] if g["of"] else None
        rec = g["in"] / t
        wt += t
        wrong += g["of"] - g["in"]
        wr += rec * t
        wp += (prec if prec is not None else 0.0) * t
        sigs = " ".join(f"#{sid}:{n}x{kwh}kWh@{sh:.0%}" for sid, n, kwh, sh in sorted(g["sigs"], key=lambda x: -x[2])[:4])
        # ...and where the rest of its energy went: the signatures holding most
        # of it, whether or not the device is their majority
        went = sorted(((row.get(name, 0.0), sid, row) for sid, row in per_sig.items() if row.get(name, 0.0) > 0), key=lambda x: x[0], reverse=True)[:3]   # ids mix numbers and "gone N"
        went_s = " ".join(f"#{sid}:{e/1000:.2f}kWh={e/row['total']:.0%}of{row['n']}" for e, sid, row in went)
        print(f"  {name:22s} {t/1000:9.2f} {g['in']/1000:11.2f} {g['of']/1000:9.2f} "
              f"{'-' if prec is None else f'{prec:8.0%}':>9s} {rec:7.0%}  {sigs}   holds most: {went_s}")
    if wt:
        P, Rc = wp / wt, wr / wt
        f = 1.25 * P * Rc / (0.25 * P + Rc) if P + Rc else 0.0
        print(f"  energy-weighted  precision {P:.1%}  recall {Rc:.1%}  F0.5 {f:.1%}   over {wt/1000:.1f} kWh of truth"
              f"   - {wrong/1000:.1f} kWh in devices' signatures was not theirs")


def check() -> None:
    """python3 tests/energy_bench.py check"""
    rows = [(0.0, 5.0), (100.0, 3005.0), (200.0, 5.0)]
    t = [r[0] for r in rows]
    assert round(_above(rows, t, 0, 300, 5.0), 2) == 83.33          # 3 kW above a 5 W floor for 100 s
    assert round(_above(rows, t, 120, 140, 5.0), 2) == 16.67        # ...of which 20 s
    assert _above(rows, t, 250, 300, 5.0) == 0.0                     # idle: nothing
    class S:                                                          # a run of 3 kW from 100 s
        start, levels = 100.0, {"a": [(100.0, 3000.0)]}
    assert _started_with(rows, t, S)
    S.start = 150.0                                                   # a run beside it, started later
    assert not _started_with(rows, t, S)
    pump = [(0.0, 0.5), (100.0, 1100.0), (110.0, 880.0), (150.0, 0.5)]  # on 50 s: off again a minute on
    S.start, S.levels = 101.0, {"a": [(101.0, 900.0)]}
    assert _started_with(pump, [r[0] for r in pump], S)
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["check"]:
        check()
        raise SystemExit(0)
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)
    energy(sys.argv[1], sys.argv[2], sys.argv[3:])
