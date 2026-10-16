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
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load  # noqa: E402

P = load("insights.profile")
S = load("insights.series")
M = load("insights.model")
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
    (what Kozolec's HA templates do)."""
    for k, v in (dials or {}).items():
        mod, name = k.rsplit(".", 1)
        setattr(P if mod == "profile" else load(f"insights.{mod}"), name, v)
    model, series = read(site)
    cons = S.combine(series, model.consumption_terms())
    target = S.subtract_all(cons, series, list(out)) if out else cons
    actual = {t.timestamp(): v for t, v in target}
    fits = {"x": cons, **{o: series[o] for o in out}} if mode == "helper" else {"x": target}
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


if __name__ == "__main__":
    a = sys.argv[1:]
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
