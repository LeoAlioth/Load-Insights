"""Trace every placement question the grid asks on phase a around one moment."""
import sys, datetime
import dw_bench as W
B = W.B; D = B.D
tz = datetime.timezone(datetime.timedelta(hours=2))
T0 = datetime.datetime(2026, 10, 21, 13, 36, 50, tzinfo=tz).timestamp(); T1 = T0 + 30
f = lambda t: datetime.datetime.fromtimestamp(t, tz).strftime("%H:%M:%S")
_sm, _ws, _mt = D.Fleet._step_meter, D.Fleet._window_size, D.Fleet._meter_totals
def step_meter(self, ph, since, size, up):
    got = _sm(self, ph, since, size, up)
    if ph == "a" and T0 <= since <= T1:
        print(f"[step_meter] {f(since)} {'+' if up else '-'}{size:.0f} W -> {got!r}  conf {self.main.placement_conf} window {self.main.placement_window}")
    return got
def window_size(self, name, ph, since, up, size=None):
    got = _ws(self, name, ph, since, up, size)
    if ph == "a" and T0 <= since <= T1:
        print(f"   [window_size] {name} size {size} -> {got}")
    return got
def meter_totals(self, ph, since, window, up=None):
    got = _mt(self, ph, since, window, up)
    if ph == "a" and T0 <= since <= T1:
        print(f"   [meter_totals] {f(since)} up={up}: " + ", ".join(f"{n}: share {None if v[0] is None else round(v[0])} raw {None if v[1] is None else round(v[1])} conf {v[3]:.2f}" for n, v in got.items() if v[0] or v[1]))
    return got
D.Fleet._step_meter, D.Fleet._window_size, D.Fleet._meter_totals = step_meter, window_size, meter_totals
B._apply(sys.argv[1:])
fleet, filed, _ = B._run(W.FOLDER, "home")
m = fleet.subs["Mansarda"]; h = fleet.subs["Hidrofor"]
print("Mansarda c declared:", [(f(e[0]), round(e[1]), round(e[3]-T0,1), round(e[4]-T0,1)) for e in m.phases["c"].declared if T0 - 20 <= e[0] <= T1])
print("Hidrofor declared:", [(f(e[0]), round(e[1])) for c in h.phases for e in h.phases[c].declared if T0 - 20 <= e[0] <= T1 + 20])
print("grid a declared:", [(f(e[0]), round(e[1]), round(e[3]-T0,1), round(e[4]-T0,1)) for e in fleet.main.phases["a"].declared if T0 - 20 <= e[0] <= T1 + 20])
