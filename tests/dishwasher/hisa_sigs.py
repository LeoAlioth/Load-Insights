import dw_bench as W
B = W.B
B._apply(["SUBS=prod", "HOUSE=prod"])
fleet, filed, _ = B._run("/Users/anzek/Projects/Load_Insights/data/history/home", "home")
det = fleet.subs["Hiša"]
print("Hiša's signatures on its phase c under 200 W (id, W, runs, typical run s, interval s, name):")
for s in sorted(det.signatures, key=lambda s: -s.count):
    if s.phases == "c" and sum(s.power.values()) < 200 and s.count >= 5:
        print(f"  #{s.id:4d} {sum(s.power.values()):5.0f} W  runs {s.count:4.0f}  ~{s.duration_s:5.0f} s  every ~{s.interval_s or 0:5.0f} s  {s.name or ''}")
print("grid signatures on phase c under 200 W with 20+ runs:")
for s in sorted(fleet.main.signatures, key=lambda s: -s.count):
    if s.phases == "c" and sum(s.power.values()) < 200 and s.count >= 20:
        print(f"  #{s.id:4d} {sum(s.power.values()):5.0f} W  runs {s.count:4.0f}  ~{s.duration_s:5.0f} s  every ~{s.interval_s or 0:5.0f} s  {s.name or ''}  where {dict(list(s.locations.items())[:3])}")
