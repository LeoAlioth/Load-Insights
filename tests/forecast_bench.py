"""Forecast backtest on exported long-term statistics (read-only, no HA).

Data: ``data/forecast/<site>-prefs.json`` (energy/get_prefs) and
``<site>-stats.json`` (recorder/statistics_during_period, hourly "change" in
kWh) - fetched with HA_Configs/tools/ws.mjs. Series are built as the
coordinator builds them; each origin calls ``profile.forecast`` and settles
through ``scoring.Ledger`` so the numbers are production's own.
No temperature or calendar inputs (the history carries none): this scores the
profile and its level correction, which is what leaving loads and abnormal
days out of the learning changes.

    python3.14 tests/forecast_bench.py SITE START END [STEP_H] [DIAL=V ...]
    SITE: home | kozolec; START/END: ISO dates (origins), STEP_H default 3
Dials (temporary): OUT=comma,separated,device,energy,ids  (left out of the
site series - the series scored is consumption less these)

The covariate backtest (INS-15): temperature, calendars and attached sensors
included, every series of the site, production's own windows and signals:

    python3.14 tests/forecast_bench.py cov SITE START END [STEP_H=1] [module.CONST=V ...] [TEMPS=0|1|2|3] \
        [FROM=iso] [SERIES=site,id,...] [TAG=name] [WORKERS=2]

Windows used for INS-15: W1 2026-09-28 .. 2026-10-11T23:00 (both sites), W2 Home 2026-01-12 ..
2026-01-25T23:00, W2 Kozolec 2026-08-17 .. 2026-08-30T23:00 (its whole-house series starts 2026-07-04, so no
heating-season window with 12 weeks behind it exists). A change is compared with the previous commit:
check it out, run the same windows, diff the JSONs. Sweep a constant with ``covariates.ALPHA=0.05``.
TEMPS=0 forecasts without the temperature pair (the "young series" baseline); TEMPS=3 is production before INS-20 (raw sensor, weather entity forecast); TEMPS=2 corrects it as production does (INS-20,
heatnudge.corrected_series against data/forecast/<site>/weather.json: the weather entity's state history, history/history_during_period).

Data (read-only, data/forecast/<site>/, from HA_Configs/tools/ws.mjs and curl):
  fetch.json    energy/get_prefs; recorder/statistics_during_period "change" (kWh, hour)
                for SiteModel.from_prefs(prefs).all_statistic_ids(); the same call with
                types ["mean"] for the outdoor temperature entity and every attached input
                (start_time well before the window less 12 weeks)
  options.json  GET /api/diagnostics/config_entry/<entry> -> data.config.options
  cal-*.json    GET /api/calendars/<id>?start=&end= for every linked calendar
Results: data/forecast/results/<site>-<TAG>-<START>.json plus a table on stdout.
"""
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load  # noqa: E402

P = load("insights.profile")
S = load("insights.series")
M = load("insights.model")
INP = load("insights.inputs")
SC = load("insights.scoring")
TZ = ZoneInfo("Europe/Ljubljana")
DATA = Path(__file__).resolve().parents[1] / "data" / "forecast"
LEADS = SC.LEADS


def read(site):
    prefs = json.loads((DATA / f"{site}-prefs.json").read_text())[0]["result"]
    stats = json.loads((DATA / f"{site}-stats.json").read_text())[0]["result"]
    series = {sid: [(datetime.fromtimestamp(r["start"] / 1000, TZ), float(r["change"]))
                    for r in rows if r.get("change") is not None]
              for sid, rows in stats.items()}
    return M.SiteModel.from_prefs(prefs), series


def run(site, start, end, step_h=3, out=(), mode="new", dials=None, since=None):
    """-> dict of metrics for the consumption-less-``out`` series.
    mode "new": fit the series with ``out`` already subtracted.
    mode "helper": fit consumption and each out load apart, subtract forecasts
    (what Kozolec's HA templates do).
    mode "parts": every device fitted on its own, the house built from them
    by profile.assemble (own part + mature parts, ``out`` never added back)."""
    for k, v in (dials or {}).items():
        mod, name = k.rsplit(".", 1)
        setattr(P if mod == "profile" else load(f"insights.{mod}"), name, v)
    model, series = read(site)
    cons = S.combine(series, model.consumption_terms())
    target = S.subtract_all(cons, series, list(out)) if out else cons
    actual = {t.timestamp(): v for t, v in target}
    fits = {"x": cons, **{o: series[o] for o in out}} if mode == "helper" else {"x": target}
    kids = {}
    if mode == "parts":
        listed = {d.energy for d in model.devices}
        for d in model.devices:
            kids.setdefault(d.included_in if d.included_in in listed else None, []).append(d.energy)
    ledger = SC.Ledger()
    t0 = datetime.fromisoformat(start).replace(tzinfo=TZ)
    t1 = datetime.fromisoformat(end).replace(tzinfo=TZ)
    now = t0
    while now <= t1:
        fc = {}
        for key, rows in fits.items():
            fc[key] = P.forecast([r for r in rows if r[0].timestamp() < now.timestamp()], now, 168)
        base = fc["x"]
        hourly = list(base.hourly)
        if mode == "parts":
            nk = now.timestamp()
            cut = {k: [r for r in v if r[0].timestamp() < nk] for k, v in series.items()}
            cut[None] = [r for r in cons if r[0].timestamp() < nk]
            fit = lambda stat, rows, signed=False: P.forecast(rows, now, 168, signed=signed)  # noqa: E731
            fitted = {None: fit(None, cut[None]), **{k: fit(k, v) for k, v in cut.items() if k and v}}
            house = P.assemble(cut, kids, out, fitted, fit, now, {})[2]
            hourly = list(house.hourly)
        if mode == "helper":
            sub = [sum(fc[o].hourly[i][1] for o in out) for i in range(168)]
            hourly = [(t, max(0.0, v - s)) for (t, v), s in zip(hourly, sub)]
        tomorrow = None
        if now.hour == SC.NOON:
            tom = (P.floor_hour(now) + timedelta(days=1)).replace(hour=0)
            tomorrow = sum(v for t, v in hourly if t.date() == tom.date())
        ledger.record(now, hourly, [(v, v) for _, v in hourly], tomorrow)
        ledger.settle(now + timedelta(hours=0), [(datetime.fromtimestamp(k, TZ), v) for k, v in actual.items()
                                                if k < now.timestamp()])
        now += timedelta(hours=step_h)
    # settle the tail against everything known
    ledger.settle(t1 + timedelta(days=9), [(datetime.fromtimestamp(k, TZ), v) for k, v in actual.items()])
    res = {}
    for name, lead in LEADS.items():
        rows = [e for e in ledger.errors[lead] if since is None or e[0] >= since]
        res[name] = (1000 * sum(abs(a - p) for _, a, p in rows) / len(rows), len(rows)) if rows else (None, 0)
    d = ledger.day_errors
    res["tomorrow_kwh_mae"] = (sum(abs(a - p) for _, a, p in d) / len(d), len(d)) if d else (None, 0)
    res["days"] = [(x, round(a, 2), round(p, 2)) for x, a, p in d]
    return res


# ---------------------------------------------------------------- covariates
# The covariate backtest. Builds each site's series as coordinator.py does
# (combine, subtract_all of the left-out loads, every device), gives each the
# temperature history and a measured "forecast", the calendars and the
# attached sensors scoped as production scopes them, and the numeric inputs
# linked to a device as its nowcast states. Origins run over a window; each
# origin forecasts every series from the 12 weeks before it and settles through
# scoring.Ledger. Origin-major so the signals are built once per origin.

HISTORY_WEEKS = 12
# the Slovenian public holidays 2025-2026 (the `holidays` library is not installed here)
SI_HOLIDAYS = {date(2025, 10, 31), date(2025, 11, 1), date(2025, 12, 25), date(2025, 12, 26),
               date(2026, 1, 1), date(2026, 1, 2), date(2026, 2, 8), date(2026, 4, 5), date(2026, 4, 6),
               date(2026, 4, 27), date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 24), date(2026, 6, 25),
               date(2026, 8, 15), date(2026, 10, 31), date(2026, 11, 1), date(2026, 12, 25), date(2026, 12, 26)}
NUMERIC_DOMAINS = ("sensor", "input_number", "number")
CTX: dict = {}


def read_cov(site):
    d = DATA / site
    fetched = json.loads((d / "fetch.json").read_text())
    prefs, stats, means = fetched[0]["result"], fetched[1]["result"], fetched[2]["result"]
    opts = json.loads((d / "options.json").read_text())
    series = {sid: [(datetime.fromtimestamp(r["start"] / 1000, TZ), float(r["change"]))
                    for r in rows if r.get("change") is not None]
              for sid, rows in stats.items()}
    mean = {sid: sorted((r["start"] / 1000.0, float(r["mean"])) for r in rows if r.get("mean") is not None)
            for sid, rows in means.items()}
    events = {}
    for f in sorted(d.glob("cal-*.json")):
        ent = {"cal-workday.json": "calendar.workday_sensor_calendar", "cal-guests.json": "calendar.kozolec_guests"}[f.name]
        if ent not in (opts.get("calendar_entities") or ()):
            continue
        out = []
        for ev in json.loads(f.read_text()):
            def at(x):
                if "dateTime" in x:
                    return datetime.fromisoformat(x["dateTime"]).timestamp()
                dd = date.fromisoformat(x["date"])
                return datetime(dd.year, dd.month, dd.day, tzinfo=TZ).timestamp()
            out.append((at(ev["start"]), at(ev["end"]), ev.get("summary") or ""))
        events[ent] = out
    return M.SiteModel.from_prefs(prefs), opts, series, mean, events


def _init(site, dials, temps):
    """Worker set-up: data, dials, the series of the site."""
    for k, v in dials.items():
        mod, name = k.rsplit(".", 1)
        setattr(P if mod == "profile" else load(f"insights.{mod}"), name, v)
    INP = load("insights.inputs")
    CAL = load("insights.calendars")
    model, opts, series, mean, events = read_cov(site)
    if temps in (2, 3):
        w = json.loads((DATA / site / "weather.json").read_text())[0]["result"]
        CTX["wref"] = sorted((x["lu"], float(x["a"]["temperature"])) for v in w.values() for x in v if x.get("a", {}).get("temperature") is not None)
    cons = S.combine(series, model.consumption_terms())
    left_out = [e for e in (opts.get("forecast_exclude") or []) if e in series]
    scored = S.subtract_all(cons, series, left_out) if left_out else cons
    rows_of = {"site": scored, **{d.energy: series[d.energy] for d in model.devices if series.get(d.energy)}}
    parent = {d.energy: d.included_in for d in model.devices}
    links = opts.get("input_links") or {}

    def within(target):
        if target.startswith("load:"):
            return []
        out, cur = [], target
        while cur and cur not in out:
            out.append(cur)
            cur = parent.get(cur)
        return out

    inputs = [e for e in (opts.get("input_entities") or ())]
    linked = {}
    for eid in inputs:
        if eid.split(".", 1)[0] in NUMERIC_DOMAINS:
            for target in links.get(eid) or []:
                for stat in within(target):
                    if eid not in linked.setdefault(stat, []):
                        linked[stat].append(eid)
    t_all = [t.timestamp() for r in rows_of.values() for t, _ in r]
    lo, hi = min(t_all), max(t_all) + 14 * 86400.0
    keys = [lo + i * 3600.0 for i in range(int((hi - lo) // 3600) + 1)]
    keys = [k - (k % 3600.0) for k in keys]
    cal_signals = [CAL.CalendarSignals.from_events(ent, ev, keys) for ent, ev in events.items()]
    CTX.update(site=site, model=model, opts=opts, rows_of=rows_of, ts_of={k: [t.timestamp() for t, _ in v] for k, v in rows_of.items()},
               mean=mean, within=within, linked=linked, links=links, inputs=inputs,
               cal_signals=cal_signals, temp_entity=opts["outdoor_temperature_entity"], use_temps=temps,
               fc_hours=28 if site == "home" else 48,      # the weather entities' forecast hours (diagnostics: temperature_forecast_hours)
               INP=INP, CAL=CAL, scored_left=left_out, hol=SI_HOLIDAYS,
               mts={k: [t for t, _ in v] for k, v in mean.items()}, labels={d.energy: d.label for d in model.devices})


def _window(eid, lo, now_k):
    """The hourly means of ``eid`` in [lo, now_k): the history a refresh reads."""
    import bisect
    ts, vals = CTX["mts"][eid], CTX["mean"][eid]
    i, j = bisect.bisect_left(ts, lo), bisect.bisect_left(ts, now_k)
    return {k: v for k, v in vals[i:j]}


def _origin(args):
    """Forecast every series at one origin -> {series: (hourly[:30], bands[:30], tomorrow, info)}."""
    import bisect
    now_ts, from_ts, only = args
    now = datetime.fromtimestamp(now_ts, TZ)
    INP, CAL = CTX["INP"], CTX["CAL"]
    cut_k = P.floor_hour(now).timestamp()
    lo = max(now_ts - HISTORY_WEEKS * 7 * 86400.0, from_ts or 0.0)
    horizon_keys = [b.timestamp() for b in P.hour_buckets(P.floor_hour(now), 168)]
    temps_hist = temps_fc = None
    if CTX["use_temps"] and CTX["temp_entity"] in CTX["mean"]:
        temps_hist = _window(CTX["temp_entity"], lo, cut_k) or None
        allm = {k: v for k, v in CTX["mean"][CTX["temp_entity"]]}
        temps_fc = {k: allm[k] for k in horizon_keys[:CTX["fc_hours"]] if k in allm} or None
    if CTX["use_temps"] == 3 and temps_hist:      # production before INS-20: the raw sensor as history, the weather entity as the forecast
        temps_fc = load("insights.heatnudge").hourly_from_points(CTX["wref"], horizon_keys[:CTX["fc_hours"]]) or None
    if CTX["use_temps"] == 2 and temps_hist:      # INS-20: the production correction against the weather entity's own history
        hn = load("insights.heatnudge")
        ref = hn.hourly_from_points(CTX["wref"], [lo - lo % 3600 + 3600 * i for i in range(int((now_ts - lo) // 3600) + 1)])
        wfc = hn.hourly_from_points(CTX["wref"], horizon_keys[:CTX["fc_hours"]])      # the weather entity as the forecast: its own frame
        temps_hist, temps_fc, _, _ = hn.corrected_series(temps_hist, ref, wfc, now_ts, hn.night_hours(46.1, 14.3, now_ts))      # both sites: Ljubljana basin, 0.1 degree
        temps_hist, temps_fc = temps_hist or None, temps_fc or None
    signals = list(CTX["cal_signals"])
    for eid in CTX["inputs"]:
        if eid not in CTX["mean"]:
            continue                     # no long-term statistics: the recorder's ten days of states are gone for these windows
        raw = _window(eid, lo, cut_k)
        labels, kind = load("insights.inputs").label_history(raw)
        if not INP.usable(labels):
            continue
        cur = labels.get(max(labels)) if labels else None
        merged = dict(labels)
        merged.update(INP.project(labels, horizon_keys, TZ, cur))
        signals.append(CAL.CalendarSignals.from_labels(eid, merged))
    out = {}
    for key, rows in CTX["rows_of"].items():
        if only and key not in only:
            continue
        ts = CTX["ts_of"][key]
        i, j = bisect.bisect_left(ts, lo), bisect.bisect_left(ts, now_ts)
        if j - i < 24:
            continue
        sub = rows[i:j]
        stat = None if key == "site" else key
        sigs = INP.scoped(signals, CTX["links"], CTX["within"], stat) or None
        nums = []
        for st in CTX["linked"].get(key, []):
            if st in CTX["mean"]:
                w = _window(st, lo, cut_k)
                if w:
                    nums.append((w, w[max(w)]))
        fc = P.forecast(sub, now, 168, CTX["hol"], temps_hist, temps_fc, sigs,
                        [h for h, _ in nums] or None, [v for _, v in nums] or None)
        T = fc.temperature
        info = {"temp": [T.engaged, round(T.heating_kwh_per_degh * 1000, 2), round(T.cooling_kwh_per_degh * 1000, 2),
                         round(T.t_heating, 2), round(T.t_cooling, 2), round(T.t_needed, 2), T.days,
                         round(T.explained, 4), T.reason],
                "cal": [[m.entity, m.existence.engaged, round(m.existence.contrast, 3), round(m.existence.explained, 4),
                         {t: [round(f.on, 3), round(f.explained, 4)] for t, f in m.titles.items()}]
                        for m in fc.calendars if m.engaged],
                "now": [fc.nowcast.engaged, list(fc.nowcast.used), [round(x, 2) for x in fc.nowcast.t], fc.nowcast.days]
                if fc.nowcast.engaged else None}
        out[key] = ([(t.timestamp(), v) for t, v in fc.hourly[:30]], [tuple(b) for b in fc.bands[:30]],
                    fc.tomorrow_kwh if now.hour == SC.NOON else None, info)
    return now_ts, out


def run_cov(site, start, end, step_h=1, dials=None, temps=1, from_iso=None, only=None, workers=2, tag="run"):
    import multiprocessing as mp
    t0 = datetime.fromisoformat(start).replace(tzinfo=TZ)
    t1 = datetime.fromisoformat(end).replace(tzinfo=TZ)
    origins, now = [], t0
    while now <= t1:
        origins.append(now.timestamp())
        now += timedelta(hours=step_h)
    from_ts = datetime.fromisoformat(from_iso).replace(tzinfo=TZ).timestamp() if from_iso else None
    ctx = mp.get_context("spawn")
    with ctx.Pool(workers, _init, (site, dials or {}, temps)) as pool:
        res = dict(pool.map_async(_origin, [(o, from_ts, only) for o in origins], chunksize=2).get(timeout=900))
    _init(site, dials or {}, temps)     # the parent needs the actuals and labels
    ledgers, infos = {}, {}
    for o in origins:
        for key, (hourly, bands, tomorrow, info) in res.get(o, {}).items():
            led = ledgers.setdefault(key, SC.Ledger())
            nowdt = datetime.fromtimestamp(o, TZ)
            led.record(nowdt, [(datetime.fromtimestamp(k, TZ), v) for k, v in hourly], bands, tomorrow)
            infos.setdefault(key, []).append((o, info))
    settle_at = t1 + timedelta(days=9)
    out = {}
    for key, led in ledgers.items():
        led.settle(settle_at, [(t, v) for t, v in CTX["rows_of"][key] if t.timestamp() >= t0.timestamp() - 86400])
        r = {}
        for name in ("hour_ahead", "day_ahead"):
            rows = led.errors[LEADS[name]]
            r[name] = [1000 * sum(abs(a - p) for _, a, p in rows) / len(rows), len(rows)] if rows else [None, 0]
        d = led.day_errors
        r["tomorrow_kwh"] = [sum(abs(a - p) for _, a, p in d) / len(d), len(d)] if d else [None, 0]
        r["coverage"] = (sum(1 for _, a, lo, hi in led.band_errors if lo <= a <= hi) / len(led.band_errors)
                         if led.band_errors else None)
        seq = infos[key]
        n = len(seq)
        r["temp_engaged_frac"] = sum(1 for _, i in seq if i["temp"][0]) / n
        first = next((o for o, i in seq if i["temp"][0]), None)
        r["temp_first_engaged"] = datetime.fromtimestamp(first, TZ).isoformat() if first else None
        r["signals_engaged_frac"] = sum(1 for _, i in seq if i["cal"]) / n
        r["nowcast_engaged_frac"] = sum(1 for _, i in seq if i["now"]) / n
        r["first_origin"] = datetime.fromtimestamp(seq[0][0], TZ).isoformat()
        ts0 = max(CTX["ts_of"][key][0], from_ts or 0.0)
        r["temp_first_days_of_history"] = round((first - ts0) / 86400.0, 2) if first else None
        if first:       # the engagement trace: origin, then the temperature fit as it stood
            r["temp_trace"] = [[o, *i["temp"][:7]] for o, i in seq]
        r["last"] = seq[-1][1]
        r["last_origin"] = datetime.fromtimestamp(seq[-1][0], TZ).isoformat()
        r["label"] = CTX["labels"].get(key, key)
        r["rows"] = len(CTX["rows_of"][key])
        out[key] = r
    return out


def table(site, out):
    cols = f"{'series':44} {'h-a W':>8} {'d-a W':>8} {'tmrw kWh':>9} {'cov':>5} {'temp%':>6} {'sig%':>5} {'now%':>5}"
    print(f"== {site}\n{cols}")
    for key, r in sorted(out.items(), key=lambda kv: (kv[0] != "site", kv[0])):
        f = lambda x: "-" if x is None else f"{x:.1f}"  # noqa: E731
        print(f"{key[:44]:44} {f(r['hour_ahead'][0]):>8} {f(r['day_ahead'][0]):>8} "
              f"{(f'{r['tomorrow_kwh'][0]:.2f}' if r['tomorrow_kwh'][0] is not None else '-'):>9} "
              f"{(f'{r['coverage']:.2f}' if r['coverage'] is not None else '-'):>5} "
              f"{100 * r['temp_engaged_frac']:6.0f} {100 * r['signals_engaged_frac']:5.0f} {100 * r['nowcast_engaged_frac']:5.0f}")
    tot = sum(r["day_ahead"][0] for r in out.values() if r["day_ahead"][0] is not None)
    print(f"sum of day-ahead MAE over series: {tot:.1f} W")


def main_cov(a):
    dials, pos, kw = {}, [], {}
    for x in a:
        if "=" in x and "." in x.split("=")[0]:
            k, v = x.split("=")
            dials[k] = float(v)
        elif x.startswith(("TEMPS=", "WORKERS=")):
            k, v = x.split("=")
            kw[k.lower()] = int(v)
        elif x.startswith("FROM="):
            kw["from_iso"] = x[5:]
        elif x.startswith("SERIES="):
            kw["only"] = set(x[7:].split(","))
        elif x.startswith("TAG="):
            kw["tag"] = x[4:]
        else:
            pos.append(x)
    site, start, end = pos[:3]
    step = int(pos[3]) if len(pos) > 3 else 1
    out = run_cov(site, start, end, step, dials, **kw)
    table(site, out)
    res = DATA / "results"
    res.mkdir(exist_ok=True)
    name = res / f"{site}-{kw.get('tag', 'run')}-{start}.json"
    name.write_text(json.dumps({"site": site, "start": start, "end": end, "step_h": step, "dials": dials,
                                "temps": kw.get("temps", 1), "from": kw.get("from_iso"), "series": out},
                               indent=1, default=str))
    print("->", name)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "cov":
        main_cov(a[1:])
        sys.exit(0)
    dials, pos = {}, []
    out = ()
    mode = "new"
    since = None
    for x in a:
        if x.startswith("OUT="):
            out = tuple(x[4:].split(","))
        elif x.startswith("SINCE="):
            since = datetime.fromisoformat(x[6:]).replace(tzinfo=TZ).timestamp()
        elif x.startswith("MODE="):
            mode = x[5:]
        elif "=" in x:
            k, v = x.split("=")
            dials[k] = float(v)
        else:
            pos.append(x)
    site, start, end = pos[:3]
    r = run(site, start, end, int(pos[3]) if len(pos) > 3 else 3, out, mode, dials, since)
    for k, v in r.items():
        if k != "days":
            print(f"{k:18} {v[0]:.2f}  (n={v[1]})" if v[0] is not None else k)
    if "--days" in sys.argv:
        print("days:", r["days"][-20:])
