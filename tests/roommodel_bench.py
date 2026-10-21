"""INS-24 bench: the room model against the current methods on Kozolec's Bee Room (spec section 7).

    roommodel_bench.py fetch [DAYS]     read the recorder (token: data/ha.kozolec.hlevcek.com), cache hourly rows
    roommodel_bench.py [fit|A|B|C|all]  fit on everything / daily energy / room to absorb / stability

A and B are leave-one-day-out: each day is predicted by a model fitted on the others. The outdoor temperature
and the sun are the day's own (an upper bound: a forecast is worse), the setpoint is the day's mean room
temperature ("oracle") or the all-days median ("fixed"). Baselines: the mean of the other days; the other days'
daily energy regressed on the daily mean outdoor temperature (what a temperature response does); INS-2's k.
"""
import datetime as dt
import json
import statistics as st
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load  # noqa: E402

RM = load("insights.roommodel")
TH = load("insights.thermal")
ROOT = Path(__file__).resolve().parents[1]
H = "ha.kozolec.hlevcek.com"
CACHE = ROOT / "data" / "history" / "kozolec-room" / "bee_room.json"
ENT = {"tin": "sensor.bee_room_t_h_temperature", "tout": "sensor.weather_station_temperature",
       "kwh": "sensor.bee_room_heater_energy", "pv": "sensor.kozolec_total_pv_power",
       "p": "sensor.bee_room_heater_power"}
RATING_W = 1030.0
HOUR = 3600.0


def hist(entity, a, b):
    token = (ROOT / "data" / H).read_text().strip()
    url = (f"https://{H}/api/history/period/{a}?end_time={b}&filter_entity_id={entity}"
           "&minimal_response=true&significant_changes_only=0")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    out = []
    for s in (json.load(urllib.request.urlopen(req, timeout=240)) or [[]])[0]:
        try:
            out.append((dt.datetime.fromisoformat((s.get("last_updated") or s["last_changed"]).replace("Z", "+00:00")).timestamp(),
                        float(s["state"])))
        except (ValueError, KeyError, TypeError):
            pass
    return out


def at(rows, t):
    v = None
    for ts, x in rows:
        if ts > t:
            break
        v = x
    return v


def mean_held(rows, t0, t1):
    v = at(rows, t0)
    if v is None:
        return None
    acc, last = 0.0, t0
    for ts, x in rows:
        if ts <= t0:
            continue
        if ts >= t1:
            break
        acc += v * (ts - last)
        last, v = ts, x
    return (acc + v * (t1 - last)) / (t1 - t0)


def fetch(days):
    now = dt.datetime.now(dt.timezone.utc)
    a, b = (now - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = {k: hist(e, a, b) for k, e in ENT.items()}
    top = int(now.timestamp()) // 3600 * 3600
    t0 = top - days * 86400
    out = {"temps": {}, "outdoor": {}, "kwh_counter": {}, "sun": {}, "power": {}}
    for t in range(t0, top + 1, 3600):
        for key, src in (("temps", "tin"), ("outdoor", "tout"), ("kwh_counter", "kwh")):
            v = at(raw[src], t)
            if v is not None:
                out[key][str(t)] = v
        if t < top:
            for key, src in (("sun", "pv"), ("power", "p")):
                v = mean_held(raw[src], t, t + 3600)
                if v is not None:
                    out[key][str(t)] = v / 1000.0 if key == "sun" else v
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(out))
    print("cached", CACHE, {k: len(v) for k, v in out.items()})


def data():
    d = json.loads(CACHE.read_text())
    f = lambda k: {float(t): v for t, v in d[k].items()}
    temps, outdoor, ctr, sun = f("temps"), f("outdoor"), f("kwh_counter"), f("sun")
    energy = [(t, ctr[t + HOUR] - ctr[t]) for t in sorted(ctr) if (t + HOUR) in ctr and ctr[t + HOUR] >= ctr[t]]
    return temps, outdoor, energy, sun


def model_on(temps, outdoor, energy, sun, skip=None, why=None):
    keep = (lambda t: True) if skip is None else (lambda t: not skip(t))
    rows = [r for r in RM.hourly_rows(temps, [(t, k) for t, k in energy if keep(t)], outdoor, sun)]
    return RM.fit(rows, RM.quantum(temps), why)


def day_of(t):
    return int(t // 86400)


def show(m):
    print(f"C {m.c_kwh_per_k:.3f} kWh/K  UA {m.ua_w_per_k:.1f} W/K  G {m.g_w:.0f} W  "
          f"sun {'-' if m.sun_gain is None else f'{m.sun_gain:.0f} W/kW'}  coast {m.coast_hours:.1f} h  "
          f"hours {m.hours} heated {m.heated}  resid {m.resid_k:.3f} K  t {m.t_heater:.0f}/{m.t_loss:.0f}  halves differ {m.half_diff:.0%}")


def bench_fit():
    temps, outdoor, energy, sun = data()
    why = {}
    m = model_on(temps, outdoor, energy, sun, why=why)
    if m is None:
        print("unknown:", why)
    else:
        show(m)
    why = {}
    m = model_on(temps, outdoor, energy, None, why=why)
    print("without the sun series:", end=" ")
    show(m) if m else print("unknown:", why)


def days_complete(energy, temps, outdoor):
    by = {}
    for t, k in energy:
        by.setdefault(day_of(t), []).append((t, k))
    return {d: rows for d, rows in by.items() if len(rows) == 24 and all(t in outdoor for t, _ in rows)}


def mae_bias(errs):
    return st.mean(abs(e) for e in errs), st.mean(errs)


def bench_a():
    temps, outdoor, energy, sun = data()
    days = days_complete(energy, temps, outdoor)
    kwh = {d: sum(k for _, k in rows) for d, rows in days.items()}
    tmean = {d: st.mean(outdoor[t] for t, _ in rows) for d, rows in days.items()}
    room_t = {d: st.mean(temps[t] for t, _ in rows if t in temps) for d, rows in days.items()}
    setfix = st.median(room_t.values())
    out = {"model oracle": [], "model fixed": [], "mean of others": [], "regression on T": []}
    print(f"{len(days)} complete days; fixed setpoint {setfix:.1f} C (median of the days' room temperature)")
    print("day         actual  model(oracle)  model(fixed)  mean-others  regr-on-T   T_out  room")
    for d in sorted(days):
        others = [x for x in days if x != d]
        m = model_on(temps, outdoor, energy, sun, skip=lambda t, d=d: day_of(t) == d)
        if m is None:
            print(day_str(d), "model unknown for this fold"); continue
        outs = [outdoor[t] for t, _ in days[d]]
        suns = [sun.get(t) for t, _ in days[d]]
        pred = {"model oracle": RM.daily_energy(m, room_t[d], outs, RATING_W, suns),
                "model fixed": RM.daily_energy(m, setfix, outs, RATING_W, suns),
                "mean of others": st.mean(kwh[x] for x in others)}
        xs, ys = [tmean[x] for x in others], [kwh[x] for x in others]
        mx, my = st.mean(xs), st.mean(ys)
        sl = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
        pred["regression on T"] = my + sl * (tmean[d] - mx)
        for k, v in pred.items():
            out[k].append(v - kwh[d])
        print(f"{day_str(d)}  {kwh[d]:6.2f}  {pred['model oracle']:13.2f}  {pred['model fixed']:12.2f}  {pred['mean of others']:11.2f}  "
              f"{pred['regression on T']:9.2f}  {tmean[d]:5.1f}  {room_t[d]:.1f}")
    print("--- daily energy, kWh: MAE / bias")
    for k, e in out.items():
        if e:
            a, b = mae_bias(e)
            print(f"{k:16s} {a:.2f} / {b:+.2f}")


def day_str(d):
    return dt.datetime.fromtimestamp(d * 86400, dt.timezone.utc).strftime("%Y-%m-%d")


def bench_b():
    temps, outdoor, energy, sun = data()
    runs = TH.heating_runs(energy, temps, None, True)
    print(f"{len(runs)} heating runs (room kind: dips allowed)")
    kwh_of = dict(energy)
    errs = {"model": [], "INS-2 k": []}
    print("start (UTC)        kWh   rise   hours   model   INS-2")
    for i, r in enumerate(runs):
        hours, t = 0, r.start
        while kwh_of.get(t, 0.0) >= 0.2 * max(k for _, k in energy):
            hours += 1; t += HOUR
        hours = max(hours, 1)
        m = model_on(temps, outdoor, energy, sun, skip=lambda t, d=day_of(r.start): day_of(t) == d)
        others = runs[:i] + runs[i + 1:]
        k = TH.kwh_per_degree(others, TH.ROOM)
        start_t = temps[r.start]
        t_out = outdoor.get(r.start)
        pm = RM.room_to_absorb(m, start_t, start_t + r.rise, t_out, r.kwh * 1000.0 / hours, sun.get(r.start)) if m else None
        pk = k[0] * r.rise if k else None
        if pm is not None:
            errs["model"].append(pm - r.kwh)
        if pk is not None:
            errs["INS-2 k"].append(pk - r.kwh)
        ts = dt.datetime.fromtimestamp(r.start, dt.timezone.utc).strftime("%m-%d %H:%M")
        print(f"{ts}  {r.kwh:6.2f}  {r.rise:5.1f}  {hours:5d}  {'-' if pm is None else f'{pm:6.2f}'}  {'-' if pk is None else f'{pk:6.2f}'}")
    print("--- room to absorb over the run, kWh: MAE / bias (n)")
    for k, e in errs.items():
        if e:
            a, b = mae_bias(e)
            print(f"{k:8s} {a:.2f} / {b:+.2f}  ({len(e)})")


def bench_c():
    temps, outdoor, energy, sun = data()
    days = sorted(days_complete(energy, temps, outdoor))
    print("fit on blocks of days (the model must hold under a different weather):")
    for i in range(0, len(days) - 4, 2):
        block = set(days[i:i + 5])
        why = {}
        m = model_on(temps, outdoor, energy, sun, skip=lambda t, b=block: day_of(t) not in b, why=why)
        print(f"{day_str(days[i])}..{day_str(days[i + 4])}: ", end="")
        show(m) if m else print("unknown:", why.get("reason"))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd == "fetch":
        fetch(int(sys.argv[2]) if len(sys.argv) > 2 else 10)
    else:
        for name, fn in (("fit", bench_fit), ("A", bench_a), ("B", bench_b), ("C", bench_c)):
            if cmd in (name, "all"):
                print(f"=== {name}"); fn()
