"""The truth bursts no filed run of 1.2 kW+ on grid phase a covers: what WAS filed near each."""
import sys, datetime, json
sys.path.insert(0, "/Users/anzek/Projects/Load_Insights/tests")
import dw_bench as W
B = W.B
D = B.D
LOG = []
_orig = D.Detector._file
def _hook(self, s, *a, **k):
    LOG.append((self, s)); return _orig(self, s, *a, **k)
D.Detector._file = _hook
B._apply(sys.argv[1:])
fleet, filed, _ = B._run(W.FOLDER, "home")
tz = datetime.timezone(datetime.timedelta(hours=2))
f = lambda t: datetime.datetime.fromtimestamp(t, tz).strftime("%d %H:%M:%S")
m = fleet.subs["Mansarda"]
msess = sorted((s for d, s in LOG if d is m), key=lambda s: s.start)
truth = W.truth
for cls in ("dishwasher", "sock_eater"):
    rows = [s for s in filed if s.energy_wh > 0 and s.phases == "a" and s.max_w >= 1200.0]
    missed = [x for x in truth[cls] if not any(s.end >= x["start"] - W.SLACK and s.start <= x["end"] + W.SLACK for s in rows)]
    print(f"== {cls}: {len(missed)} of {len(truth[cls])} bursts with no 1.2 kW+ run on a")
    for x in missed:
        a, b = x["start"], x["end"]
        near_g = [(f(s.start), round(s.duration_s), s.phases, round(s.max_w), "own" if s.parent_signature else "-") for s in filed
                  if s.end >= a - 120 and s.start <= b + 120 and s.max_w >= 300]
        near_m = [(f(s.start), round(s.duration_s), s.phases, round(s.max_w)) for s in msess if s.end >= a - 120 and s.start <= b + 120 and s.max_w >= 300]
        print(f"  {f(a)} - {f(b)} {b - a:5.0f}s {x.get('watts', '')}  grid:", near_g[:4], " mansarda:", near_m[:4])
