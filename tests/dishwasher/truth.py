"""Ground truth for Mansarda phase C: dishwasher vs Sock Eater vs oven bursts, from the 10 days in home-1022."""
import csv, glob, datetime, bisect, json, statistics
D="/Users/anzek/Projects/Load_Insights/data/history/home-1022/"
WANT={"sensor.mansarda_phase_c_active_power","sensor.aeg_sock_eater_appliancestate","sensor.aeg_sock_eater_cyclephase","sensor.aeg_le_hottie_appliancestate","sensor.mansarda_phase_a_active_power","sensor.mansarda_phase_b_active_power"}
ser={k:[] for k in WANT}
for f in sorted(glob.glob(D+"*_configured.csv")):
    for r in csv.DictReader(open(f)):
        e=r["entity_id"]
        if e in WANT: ser[e].append((datetime.datetime.fromisoformat(r["last_changed"]).timestamp(), r["state"]))
for e in ser: ser[e].sort()
def state_at(e,t):
    s=ser[e]; i=bisect.bisect_right([x[0] for x in s],t)-1
    return s[i][1] if i>=0 else None
keys={e:[x[0] for x in ser[e]] for e in ser}
def st(e,t):
    i=bisect.bisect_right(keys[e],t)-1
    return ser[e][i][1] if i>=0 else None
pw=[(t,float(v)) for t,v in ser["sensor.mansarda_phase_c_active_power"] if v not in ("unavailable","unknown")]
# baseline = rolling low of the last 10 min; a burst = power over baseline + 1200 W
runs=[]; on=None; base=0.0
import collections
win=collections.deque()
for t,w in pw:
    win.append((t,w))
    while win and win[0][0]<t-600: win.popleft()
    low=min(x[1] for x in win)
    hot=w-low>=1200
    if hot and on is None: on=[t,t,w,low]
    elif hot: on[1]=t; on[2]=max(on[2],w)
    elif on is not None and t-on[1]>60: runs.append(tuple(on)); on=None
if on: runs.append(tuple(on))
# merge runs closer than 90 s (a thermostat cycling is one period)
merged=[]
for r in runs:
    if merged and r[0]-merged[-1][1]<90: merged[-1]=(merged[-1][0],r[1],max(merged[-1][2],r[2]),merged[-1][3])
    else: merged.append(r)
out=[]
for a,b,mx,low in merged:
    se=st("sensor.aeg_sock_eater_appliancestate",a); se_b=st("sensor.aeg_sock_eater_appliancestate",b)
    oh=st("sensor.aeg_le_hottie_appliancestate",a); ph=st("sensor.aeg_sock_eater_cyclephase",a)
    se_on=lambda s: s in ("Running","Paused","Delayed Start")
    if oh not in (None,"Ready To Start","Off","unavailable") : lab="oven"
    elif se_on(se) or se_on(se_b): lab="sock_eater"
    else: lab="dishwasher_or_other"
    out.append({"start":a,"end":b,"dur_s":round(b-a),"peak_w":round(mx),"base_w":round(low),"sock":se,"phase":ph,"oven":oh,"label":lab})
json.dump(out,open("/private/tmp/claude-1773206459/-Users-anzek-Projects/4b9e35e1-1c3c-4305-92d3-4b8a8a8f0980/scratchpad/dw/truth_runs.json","w"),indent=1)
c=collections.Counter(o["label"] for o in out); print(len(out),"bursts",dict(c))
tz=datetime.timezone(datetime.timedelta(hours=2))
for o in out:
    if o["label"]!="sock_eater": print(datetime.datetime.fromtimestamp(o["start"],tz).strftime("%d %H:%M:%S"),o["dur_s"],"s",o["peak_w"],"W base",o["base_w"],o["label"],o["sock"],o["oven"])
