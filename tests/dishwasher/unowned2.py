import sys, datetime
import dw_bench as W
B = W.B
D = B.D
LOG = []
_orig = D.Detector._file
def _hook(self, s, *a, **k):
    LOG.append((self, s))
    return _orig(self, s, *a, **k)
D.Detector._file = _hook
B._apply(sys.argv[1:])
fleet, filed, _ = B._run(W.FOLDER, "home")
tz = datetime.timezone(datetime.timedelta(hours=2))
f = lambda t: datetime.datetime.fromtimestamp(t, tz).strftime("%d %H:%M:%S")
m = fleet.subs["Mansarda"]
msess = sorted((s for d, s in LOG if d is m), key=lambda s: s.start)
print("Mansarda filed", len(msess), "sessions;", sum(1 for s in msess if s.max_w >= 1200), "of 1.2 kW+")
for s in sorted(filed, key=lambda s: s.start):
    if s.energy_wh <= 0 or s.phases != "a" or s.max_w < 1200 or W.classify(s) != "dishwasher":
        continue
    near = [(f(x.start), round(x.duration_s), x.phases, round(x.max_w), x.signature_id) for x in msess if abs(x.start - s.start) <= 120 and x.max_w >= 300]
    print(f(s.start), f"{s.duration_s:5.0f}s", round(s.max_w), "W  owner", s.parent_signature[0] if s.parent_signature else "-", "sig", s.signature_id, "| Mansarda 300W+ sessions within 2 min:", near)
