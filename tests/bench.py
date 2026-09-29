"""Score a detector setting against the two sites' own history, the way every
dial in AGENTS.md was chosen. No Home Assistant; runs in about two minutes a
site and ten at once on a laptop.

    python3 tests/bench.py score  SITE FOLDER [DIAL=VALUE ...]
    python3 tests/bench.py kiln   FOLDER [DIAL=VALUE ...]
    python3 tests/bench.py surge  SITE FOLDER [DIAL=VALUE ...]
    python3 tests/bench.py pump   FOLDER [DIAL=VALUE ...]
    python3 tests/bench.py lengths KILN_FOLDER PUMP_FOLDER [DIAL=VALUE ...]

SITE is a key of cluster_lab.SITES (home, kozolec); FOLDER a directory of the
per-day CSVs fetch_history.py writes. Build tuning and hold-out folders of
symlinks rather than pointing at data/history/<site> while a fetch is
writing to it - runs started seconds apart would read different days.

DIAL is any module-level constant of insights/detect.py, plus one of the
bench's own:

    HOUSE=prod   build Home's house reading the way PRODUCTION does - the grid
                 meter negated plus a third of the inverter, through combine()
                 with COMBINE_SETTLE_S - rather than reading Anze's template
                 sensor. They agree to 0.1 W but not in timing, and the scores
                 differed (purity 67.6 vs 68.4 %). Always use it at Home.
    HOUSE=residual  that, less every meter in CIRCUITS
    HOUSE=Hiša   one CIRCUITS meter read as though it were the house
    SLICE=6      feed the replay in slices this many hours long, as
                 production's backfill does (the default); 0 for one call

score   purity (does one signature hold one device) and concentration (does
        one device land in one signature), per device with the ABSOLUTE size
        of its dominant cluster beside the share: a share can rise because
        sessions were removed, a dominant cluster that grows cannot.
kiln    what a setting does to Home's kiln, which has no sub-meter and so is
        invisible to `score`. Against the pulses the grid meter itself shows,
        counts what was filed inside the firings: full-size A+C sessions, the
        spurious ladder of smaller A+C ones, and sessions on one leg only.
surge   for each metered device, whether its dominant signature carries a
        motor's starting surge - checked against what the device physically is.
attrib  with production's meters fed in: how many of each device's sessions
        are placed at its own meter, and what each meter was credited with
pump    every run Home's hidrofor meter recorded, and what the house detector
        filed for it: clean, long (its stop went elsewhere), short, multi
        (married to another phase), wrong size, missing. Run by run, which is
        what found the median-of-disagreeing-readings fault; following the
        pump's LABELLED sessions had pointed nowhere.
lengths how far session lengths sit from the real ones: kiln pulses timed off
        the grid meter, pump runs off the pump's own meter.
fridge  Kozolec's two fridges, which have no meter: their runs taken off the
        house reading, each tagged by how it starts, against what was filed -
        caught, how long, and over how many signatures.
"""
from __future__ import annotations

import bisect
import collections
import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cluster_lab as lab  # noqa: E402
import replay as R  # noqa: E402

D = R.D
MIN_SESSIONS = 60                      # devices below this are too few to read
# Every meter production reads, the way _resolve_submeters hands them over:
# a three-phase meter per phase under its OWN labels, anything else as one
# total whose phase is unknown. SUBS=prod feeds these to the Fleet; the
# default feeds only cluster_lab's device meters, as the bench always has.
PROD_SUBS = {
    "home": {
        "Hiša": [f"sensor.hisa_phase_{p}_active_power" for p in "abc"],
        "Mansarda": [f"sensor.mansarda_phase_{p}_active_power" for p in "abc"],
        "Blaževa Soba": "sensor.blaz_pc_power",
        "Vtičnice - pisarna": "sensor.attic_office_power",
        "Polnilnica": "sensor.evbox_elvi_power_active_import",
        "Server UPS": "sensor.server_ups_power",
        "Susilna": "sensor.shellypmminig3_susilna_power",
        "Workshop charger": "sensor.workshop_charger_power",
        "Hidrofor": "sensor.hidrofor_power",
        "Attic AC": "sensor.attic_ac_power",
        "Workshop boiler": "sensor.workshop_boiler_power",
    },
    "kozolec": {
        "Boiler": "sensor.boiler_power",
        "Car charger": "sensor.power_strip_power",
        "Washing machine": "sensor.washing_machine_power",
        "Well pump": "sensor.well_pump_power",
        "Water pump": "sensor.kozolec_hidrofor_power",
        "Pond": "sensor.pond_filter_power",
        "Pond EVSE": "sensor.pond_evse_power",
        "Pastir": "sensor.pastir_staja_power",
        "Bug lamp": "sensor.bug_lamp_power",
        "Bathroom IR Panel": "sensor.bathroom_ir_panel_switch_0_power",   # a Shelly heartbeating once a minute
    },
}
SUBS = "lab"
LAST_FLEET = None                      # the Fleet of the last _run, for attrib
SWITCHES: list = []                    # SWITCH=entity dials, fed as --switch
DRIVERS: list = []                     # DRIVER=entity dials, fed as --driver
STAGES: list = []                      # STAGE=entity dials, fed as --stage
PINNED: list = []                      # --role pins for a house built by _house_as
START_STATE = True                     # the recorder's start-of-window row, as production gets it
SLICE_HOURS = 6.0                      # production's backfill slice; SLICE=0 for one call
FIRING_MIN_PULSES = 20                 # fewer is two 3 kW loads coinciding, not a firing

# What each metered device physically is, for `surge`. Kozolec's hidrofor is a
# Grundfos Scala2 with a built-in frequency converter, so it soft-starts.
PHYSICS = {
    "Hidrofor": "motor", "Well pump": "motor", "Attic AC": "motor",
    "Boiler": "resistive", "Workshop boiler": "resistive",
    "NASA station": "electronic", "Server UPS": "electronic", "EVBox": "electronic",
    "Pond EVSE": "electronic", "Pastir": "electronic", "Bug lamp": "electronic",
}
_read_csv = R.read_csv
HOUSE_IDS = {p: f"sensor.se17k_home_power_phase_{p}" for p in "abc"}

# Home's circuit and device meters that sit directly under the grid
# connection (nothing here is inside another), by the channels each publishes.
# "Hiša" is one of them - the house circuit's 3EM, not the whole house.
# Which HOUSE phase a channel carries is MEASURED - see _phase_map - because
# the labels lie: the attic 3EM's phase b carries what the house shows on C.
CIRCUITS = {
    "Hiša": [f"sensor.hisa_phase_{p}_active_power" for p in "abc"],
    "Mansarda": [f"sensor.mansarda_phase_{p}_active_power" for p in "abc"],
    "Hidrofor": ["sensor.hidrofor_power"],
    "Susilna": ["sensor.shellypmminig3_susilna_power"],
}


def _at(rows, t):
    i = bisect.bisect_right(rows, (t, float("inf"))) - 1
    return rows[i][1] if i >= 0 else None


def _phase_map(s, channels, house) -> dict:
    """channel -> the house phase it carries: the one whose steps match the
    channel's own (same moment, within a quarter in size) most often. A
    multi-channel meter's channels take DIFFERENT phases - the permutation
    with the most matches - since a 2-phase load such as the kiln steps on
    two house phases at once and would otherwise tie."""
    import itertools
    hits = {}
    for ch in channels:
        rows = s.get(ch) or []
        c = collections.Counter()
        for (t0, w0), (t1, w1) in zip(rows, rows[1:]):
            d = w1 - w0
            if abs(d) <= 300 or t1 - t0 >= 30:
                continue
            for p, hr in house.items():
                a, b = _at(hr, t0 - 3), _at(hr, t1 + 8)
                if a is not None and b is not None and abs((b - a) - d) < 0.25 * abs(d):
                    c[p] += 1
        hits[ch] = c
    phases = sorted(house)
    best, score = {}, -1
    for perm in itertools.permutations(phases, len(channels)):
        sc = sum(hits[ch][p] for ch, p in zip(channels, perm))
        if sc > score:
            best, score = dict(zip(channels, perm)), sc
    return best


def _house_as(mode: str) -> None:
    """HOUSE=prod: the house the way production builds it. HOUSE=residual:
    that, less every meter in CIRCUITS, each read as of the house's own
    readings. HOUSE=<a CIRCUITS name>: that meter alone, as if it were the
    house - to see what detecting a load on its own circuit is worth."""
    global PINNED
    # Pin the house to the reading built here. Left to guess, the replay
    # refuses any phase that dips below zero as "carrying generation" - which
    # a house less its sub-meters does - and silently took the attic 3EM's
    # own channels instead for two of the three phases (2026-09-23).
    PINNED = [] if mode == "prod" else [f"power_{p}={HOUSE_IDS[p]}" for p in "abc"]

    def read(paths, keep):
        s = _read_csv(paths, keep)
        inv = s.get("sensor.solaredge_se17k_i1_ac_power")
        if inv:
            for p in "abc":
                m1 = s.get(f"sensor.solaredge_se17k_m1_ac_power_{p}")
                if m1:
                    s[HOUSE_IDS[p]] = D.combine(
                        [(m1, -1.0), (inv, 1.0 / 3.0)], settle_s=D.COMBINE_SETTLE_S)
        if mode == "prod":
            return s
        house = {p: s[HOUSE_IDS[p]] for p in "abc" if s.get(HOUSE_IDS[p])}
        maps = {name: _phase_map(s, chans, house) for name, chans in CIRCUITS.items()}
        print("phase map: " + "; ".join(f"{n} " + ",".join(f"{c.split('_')[-3] if 'phase' in c else 'one'}->{p}"
                                                              for c, p in m.items()) for n, m in maps.items()))
        if mode == "residual":
            for p, hr in house.items():
                chans = [s[c] for m in maps.values() for c, q in m.items() if q == p and s.get(c)]
                s[HOUSE_IDS[p]] = [(ts, w - sum((_at(r, ts) or 0.0) for r in chans)) for ts, w in hr]
            return s
        if mode in ("residual_interp", "residual_1s"):
            # Anze (2026-09-23): what if every series were put on one clock by
            # interpolating first? Between two readings no further apart than
            # the meter's own cadence the line is drawn; across a longer gap
            # the value is HELD, because Home Assistant records only changes
            # and a line across a quiet hour would invent a ramp.
            def interp(rows):
                gaps = sorted(b[0] - a[0] for a, b in zip(rows, rows[1:]))
                reach = 2.0 * gaps[len(gaps) // 2] if gaps else 0.0
                times = [r[0] for r in rows]
                def at(ts):
                    i = bisect.bisect_right(times, ts) - 1
                    if i < 0:
                        return 0.0
                    if i + 1 < len(rows) and rows[i + 1][0] - rows[i][0] <= reach:
                        (t0, v0), (t1, v1) = rows[i], rows[i + 1]
                        return v0 + (v1 - v0) * (ts - t0) / (t1 - t0)
                    return rows[i][1]
                return at, reach
            for p, hr in house.items():
                chans = [interp(s[c])[0] for m in maps.values() for c, q in m.items() if q == p and s.get(c)]
                if mode == "residual_interp":
                    grid = hr
                else:
                    hat, _ = interp(hr)
                    grid = [(float(ts), hat(ts)) for ts in range(int(hr[0][0]) + 1, int(hr[-1][0]))]
                s[HOUSE_IDS[p]] = [(ts, w - sum(f(ts) for f in chans)) for ts, w in grid]
            return s
        m = maps[mode]
        for p in "abc":
            s.pop(HOUSE_IDS[p], None)
        for c, p in m.items():
            if s.get(c):
                s[HOUSE_IDS[p]] = s[c]
        return s
    R.read_csv = read


def _apply(dials) -> str:
    for d in dials:
        k, v = d.split("=", 1)
        if k == "SUBS":
            global SUBS
            SUBS = v
            continue
        if k == "START_STATE":
            global START_STATE
            START_STATE = bool(float(v))
            continue
        if k == "SLICE":
            global SLICE_HOURS
            SLICE_HOURS = float(v)
            continue
        if k == "HOUSE":
            _house_as(v)
            continue
        if k == "SWITCH":
            SWITCHES.append(v)            # an entity whose on-periods say when a load runs
            continue
        if k == "DRIVER":
            DRIVERS.append(v)             # a number a load's runs may follow
            continue
        if k == "INPUT":
            STAGES.append(v)              # a setting a device reports
            continue
        if not hasattr(D, k):
            raise SystemExit(f"no such dial: {k}")
        setattr(D, k, type(getattr(D, k))(float(v)))
    return " ".join(dials) or "defaults"


def _run(folder: str, site: str | None):
    """Replay a folder; return the main detector, every session it filed, and
    each sub-meter's full series."""
    filed, seen, full = [], {}, {}
    of, op = D.Detector._file, D.Fleet.process

    def spy_file(self, s, *a, **kw):
        of(self, s, *a, **kw)
        if self is seen.get("main"):
            filed.append(s)

    def spy_proc(self, m, sub, *a, **kw):
        seen["main"] = self.main
        global LAST_FLEET
        LAST_FLEET = self
        for name, byp in (sub or {}).items():
            merged = []                   # this slice's phases summed...
            for rows in byp.values():
                merged = D._sum_series(merged, list(rows))
            full.setdefault(name, []).extend(merged)     # ...after the last slice's
        return op(self, m, sub, *a, **kw)

    D.Detector._file, D.Fleet.process = spy_file, spy_proc
    argv = ["replay.py", folder, "--slice-hours", str(SLICE_HOURS)] + ([] if START_STATE else ["--no-start-state"])
    for pin in PINNED:
        argv += ["--role", pin]
    for eid in SWITCHES:
        argv += ["--switch", f"{eid.split(':')[0]}={eid}"]
    for eid in DRIVERS:
        argv += ["--driver", eid]
    for eid in STAGES:
        argv += ["--input", eid]
    if site and SUBS == "prod":
        for n, e in PROD_SUBS[site].items():
            argv += (["--sub-phases", f"{n}={','.join(e)}"] if isinstance(e, list) else ["--sub", f"{n}={e}"])
    elif site:
        for n, e in lab.SITES[site]["subs"].items():
            argv += ["--sub", f"{n}={e}"]
    saved, sys.argv = sys.argv, argv
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            R.main()
    finally:
        sys.argv = saved
        D.Detector._file, D.Fleet.process = of, op
    return seen["main"], filed, {k: sorted(v) for k, v in full.items() if v}


def _labels_from(folder: str, site: str, fed: dict) -> dict:
    """The device meters the score is labelled by - cluster_lab's, always.
    Fed production's meters, the Fleet also sees circuit meters, and a circuit
    would 'label' half the house."""
    if SUBS != "prod":
        return fed
    s = _read_csv([folder], False)
    return {n: sorted(s[e]) for n, e in lab.SITES[site]["subs"].items() if s.get(e)}


# Which production meter each labelled device should end up placed at, and the
# circuit that contains it where there is one.
HOME_OF = {
    "home": {"Hidrofor": ("Hidrofor",), "NASA station": ("Vtičnice - pisarna", "Mansarda"),
             "Workshop boiler": ("Workshop boiler",), "Server UPS": ("Server UPS",),
             "Susilna": ("Susilna",), "EVBox": ("Polnilnica",), "Attic AC": ("Attic AC", "Mansarda")},
    "kozolec": {"Boiler": ("Boiler",), "Hidrofor": ("Water pump",), "Well pump": ("Well pump",),
                "Pond EVSE": ("Pond EVSE",), "Pastir": ("Pastir",), "Bug lamp": ("Bug lamp",)},
}


def attrib(site: str, folder: str, dials) -> None:
    """For each metered device: of its labelled sessions, how many went into a
    signature PLACED at its own meter (or the circuit around it) - and how
    many main-meter sessions each production meter was credited with."""
    global SUBS
    SUBS = "prod"
    tag = _apply(dials)
    det, filed, fed = _run(folder, site)
    labels = lab.label(filed, _labels_from(folder, site, fed))
    per = collections.defaultdict(lambda: [0, 0])
    for i, name in labels.items():
        sig = det.signature_of(filed[i])
        per[name][0] += 1
        if sig and sig.location in HOME_OF[site].get(name, ()):
            per[name][1] += 1
    credit = collections.Counter()
    for sig in det.signatures:
        for m, n in sig.locations.items():
            credit[m] += n
    where = collections.defaultdict(collections.Counter)
    for i, name in labels.items():
        sig = det.signature_of(filed[i])
        where[name][sig.location if sig else "-"] += 1
    print(f"  {tag:44s} placed right: " + "  ".join(
        f"{n.split()[0]}:{b}/{a}" for n, (a, b) in sorted(per.items(), key=lambda x: -x[1][0]) if a >= 20))
    print(f"  {'':44s} credited: " + "  ".join(f"{m}:{n}" for m, n in credit.most_common()))
    print(f"  {'':44s} placed at: " + "  ".join(
        f"{n.split()[0]}->" + ",".join(f"{w}:{c}" for w, c in where[n].most_common(3))
        for n in sorted(where, key=lambda n: -sum(where[n].values())) if sum(where[n].values()) >= 20))
    maps = {n: LAST_FLEET.phase_map(n) for n in LAST_FLEET.phase_votes}
    print(f"  {'':44s} phase maps: " + "; ".join(
        f"{n} " + ",".join(f"{k}->{v}" for k, v in sorted(m.items())) + f" ({sum(sum(r.values()) for r in LAST_FLEET.phase_votes[n].values())} votes)"
        for n, m in maps.items()))


def score(site: str, folder: str, dials) -> None:
    tag = _apply(dials)
    det, filed, subs = _run(folder, site)
    subs = _labels_from(folder, site, subs)
    assign = {i: det.signature_of(s).id for i, s in enumerate(filed) if det.signature_of(s)}
    labels = lab.label(filed, subs)
    r = lab.score(assign, labels, filed)
    big = {n: v for n, v in r["concentration"].items() if v[1] >= MIN_SESSIONS}
    tot = sum(v[1] for v in big.values()) or 1
    wc = sum(v[0] * v[1] for v in big.values()) / tot
    per = "  ".join(f"{n.split()[0]}:{v[0]*100:.0f}%={round(v[0]*v[1])}/{v[1]}x{v[2]}"
                    for n, v in sorted(big.items(), key=lambda x: -x[1][1]))
    iv = "/".join(f"{st.interval:.1f}" for st in det.phases.values() if st.interval)
    print(f"  {tag:44s} iv {iv:13s} sigs {r['clusters']:3d}  purity {r['purity']*100:5.1f}%"
          f"  wconc {wc*100:5.1f}%   {per}")


def _kiln_pulses(folder: str) -> list:
    """The kiln's pulses as the grid meter itself shows them: A and C dropping
    ~3 kW together (m1 reads minus the house, so a load switching on is a
    drop). The truth `kiln` scores against - no detector involved."""
    s = _read_csv([folder], False)
    a = s.get("sensor.solaredge_se17k_m1_ac_power_a") or []
    c = s.get("sensor.solaredge_se17k_m1_ac_power_c") or []

    def at(rows, t):
        i = bisect.bisect_right(rows, (t, float("inf"))) - 1
        return rows[i][1] if i >= 0 else None

    pulses = []
    for i in range(1, len(a)):
        t, w = a[i]
        if not 2600 < a[i - 1][1] - w < 3400:
            continue
        cb, ca = at(c, a[i - 1][0]), at(c, t + 7.0)
        if cb is None or ca is None or not 2400 < cb - ca < 3500:
            continue
        if pulses and t - pulses[-1] < 20:
            continue                      # the same pulse's second reading
        pulses.append(t)
    return pulses


def _firings(pulses: list) -> list:
    """Group pulses into firings: runs of at least FIRING_MIN_PULSES with no
    gap over half an hour. A lone coincidence of two 3 kW loads is not one."""
    runs = []
    for t in pulses:
        if runs and t - runs[-1][-1] < 1800:
            runs[-1].append(t)
        else:
            runs.append([t])
    return [(r[0] - 60, r[-1] + 60) for r in runs if len(r) >= FIRING_MIN_PULSES]


def kiln(folder: str, dials) -> None:
    """Scored by what the detector FILED inside the kiln's firings, against the
    pulses the raw meter shows. Counting signatures in a power band instead -
    what this printed until 2026-09-23 - also caught Home's other 3 kW loads
    on one phase (two of them, 2.5 and 5 min long, run on days the kiln never
    fired) and depended on which signatures the library happened to keep."""
    tag = _apply(dials)
    det, filed, _ = _run(folder, "home" if SUBS == "prod" else None)
    pulses = _kiln_pulses(folder)
    fires = _firings(pulses)
    inside = lambda t: any(a <= t <= b for a, b in fires)  # noqa: E731
    watts = lambda s: sum(max(v for _, v in lv) for lv in s.levels.values())  # noqa: E731
    n = collections.Counter()
    for s in filed:
        if not inside(s.start):
            continue
        w, long = watts(s), s.end - s.start > 90
        if s.phases == "ac" and 5400 <= w <= 6400:
            n["full"] += 1
            n["glued"] += long            # two pulses read as one
        elif s.phases == "ac" and 600 <= w < 5400:
            n["ladder"] += 1
        elif s.phases in ("a", "c") and 2500 <= w <= 3400:
            n["single long" if long else "single"] += 1
    raw = sum(1 for t in pulses if inside(t))
    tot = lambda s: sum(s.power.values())  # noqa: E731
    full = [s for s in det.signatures if set(s.power) == {"a", "c"} and 5400 <= tot(s) <= 6400]
    top = max(full, key=lambda s: s.count) if full else None
    print(f"  {tag:44s} kiln {raw} pulses in {len(fires)} firings: full-size {n['full']:4d}"
          f" ({n['glued']} over 90 s)   ladder {n['ladder']:3d}"
          f"   single-leg {n['single'] + n['single long']:3d} ({n['single long']} over 90 s)"
          f"   top signature x{top.count if top else 0} {top.duration_s if top else 0:4.1f} s")


FRIDGE_POWER = "sensor.multiplus_ii_48_15000_200_100_id_276_output_power_l1"


def _fridge_runs(folder: str) -> list:
    """Kozolec's compressor runs as the house reading itself shows them - no
    detector involved: a rise of 40-75 W held for minutes, closed by the first
    fall of 60-130 % of what it had sagged to, 10 to 60 minutes later. Each
    tagged by how it STARTED, the one thing that tells the two fridges apart
    (2026-09-28): "A" a one-reading surge of 250 W or more, "B" a start some
    12 W above where it settles a minute later, "?" neither seen.
    Returns (start, end, step, kind)."""
    import statistics as st
    rows = _read_csv([folder], False).get(FRIDGE_POWER) or []
    runs, i = [], 6
    while i < len(rows) - 25:
        t0 = rows[i][0]
        base = st.median(v for _, v in rows[i - 6:i])
        win = [(t, v) for t, v in rows[i:i + 60] if t - t0 <= 240]
        m1 = [v for t, v in win if 50 <= t - t0 <= 110]
        m3 = [v for t, v in win if 150 <= t - t0 <= 240]
        first = [v for t, v in win if t - t0 <= 12]
        if not (m1 and m3 and first) or rows[i][1] - base < 35:
            i += 1
            continue
        s1, s3 = st.median(m1) - base, st.median(m3) - base
        if not (40 <= s1 <= 75 and 30 <= s3 <= 75):
            i += 1
            continue
        end, j = None, i + 5
        while j < len(rows) - 5 and rows[j][0] - t0 <= 3600:
            before = st.median(v for _, v in rows[j - 5:j])
            after = st.median(v for _, v in rows[j:j + 5])
            if -1.3 * (before - base) <= after - before <= -0.6 * (before - base) and after - base < 0.5 * s3:
                end = rows[j][0]
                break
            j += 1
        if end is None or not 600 <= end - t0 <= 3600:
            i += 1
            continue
        early = [v for t, v in win if 5 <= t - t0 <= 25 and v - base < 250]
        e = (st.median(early) - base) if early else s1
        kind = "A" if max(first) - base >= 250 else ("B" if e - s1 >= 12 else "?")
        runs.append((t0, end, s1, kind))
        i = j
    return runs


def fridge(folder: str, dials) -> None:
    """What the detector filed for Kozolec's two fridges, against their runs
    in the raw reading: how many it caught at the right moment, how long it
    thought they ran against how long they did, and how many signatures they
    were spread over - ideally two, one per fridge."""
    import statistics as st
    tag = _apply(dials)
    det, filed, _ = _run(folder, "kozolec" if SUBS == "prod" else None)
    truth = _fridge_runs(folder)
    starts = sorted(((f.start, f) for f in filed), key=lambda p: p[0])
    keys = [k for k, _ in starts]
    hit, ratio, per_sig = collections.Counter(), [], collections.defaultdict(collections.Counter)
    for t0, t1, step, kind in truth:
        lo = bisect.bisect_left(keys, t0 - 30)
        near = [f for k, f in starts[lo:lo + 20] if abs(k - t0) <= 30]
        near = [f for f in near if 30 <= sum(max(v for _, v in lv) for lv in f.levels.values()) <= 130]
        if not near:
            continue
        f = min(near, key=lambda f: abs(f.start - t0))
        hit[kind] += 1
        ratio.append((f.end - f.start) / (t1 - t0))
        sig = det.signature_of(f)
        per_sig[kind][sig.id if sig else None] += 1
    n = collections.Counter(k for *_, k in truth)
    days = (truth[-1][0] - truth[0][0]) / 86400 if len(truth) > 1 else 1
    top = lambda c: ", ".join(f"#{i}:{k}" for i, k in c.most_common(3))  # noqa: E731
    print(f"  {tag:44s} fridge runs {len(truth)} ({len(truth) / days:.1f} a day: A {n['A']} B {n['B']} ? {n['?']})"
          f"  caught A {hit['A']} B {hit['B']} ? {hit['?']}"
          f"  length filed/true median {st.median(ratio) if ratio else 0:.2f}"
          f" (within 25 %: {sum(0.75 <= r <= 1.25 for r in ratio)}/{len(ratio)})")
    print(f"  {'':44s} signatures  A: {top(per_sig['A'])}   B: {top(per_sig['B'])}   ?: {top(per_sig['?'])}")
    # how cleanly the two fridges are apart: each signature's majority kind
    # among its A and B runs, over all of them
    sids = set(per_sig["A"]) | set(per_sig["B"])
    both = sum(per_sig["A"][i] + per_sig["B"][i] for i in sids)
    kept = sum(max(per_sig["A"][i], per_sig["B"][i]) for i in sids)
    print(f"  {'':44s} fridge purity {kept / max(both, 1):.0%} ({kept}/{both} A/B runs in a signature of their own kind)")
    byid = {x.id: x for x in det.signatures}
    for kind in ("A", "B"):
        for sid, _ in per_sig[kind].most_common(2):
            sig = byid.get(sid)
            for n in (sig.drivers if sig else {}):
                d, g = sig.driver_effect(n, "d"), sig.driver_effect(n, "g")
                fmt = lambda e: f"{e[0] * 100:+5.1f} %/unit r2 {e[1]:.2f}" if e else "-"  # noqa: E731
                print(f"  {'':44s} {kind} #{sid} x{sig.count} ev {sig.evidence:.2f}  {n}: length {fmt(d)}   gap {fmt(g)}")


MAT = "climate.termostat_kopalnica:hvac_action"


def switch(folder: str, dials) -> None:
    """Home's bathroom floor mat against its thermostat's heating: of the
    heating runs, how many the house filed a session for at the right moment
    and phase, how many of those were credited to the thermostat, and which
    signatures they went to - one, ideally. Also what else was credited to it.
    Pass SWITCH=climate.termostat_kopalnica:hvac_action to feed the switch."""
    tag = _apply(dials)
    det, filed, _ = _run(folder, "home" if SUBS == "prod" else None)
    truth = [(a, b) for a, b in R.read_switch([folder], MAT) if b is not None and 20 <= b - a <= 3600]
    starts = sorted(((f.start, f) for f in filed), key=lambda p: p[0])
    keys = [k for k, _ in starts]
    name = D.SWITCH_PREFIX + MAT.split(":")[0]
    found, sigs, credited_sig = 0, collections.Counter(), collections.Counter()
    for a, b in truth:
        lo = bisect.bisect_left(keys, a - 15)
        near = [f for k, f in starts[lo:lo + 20] if abs(k - a) <= 15 and f.phases == "c"
                and 450 <= sum(max(v for _, v in lv) for lv in f.levels.values()) <= 850]
        if not near:
            continue
        found += 1
        sig = det.signature_of(min(near, key=lambda f: abs(f.start - a)))
        sigs[sig.id if sig else None] += 1
    others = sum(s.locations.get(name, 0) for s in det.signatures if not (s.phases == "c" and 450 <= s.power.get("c", 0) <= 850))
    placed = [s for s in det.signatures if s.location == name]
    top = ", ".join(f"#{i}:{k}" for i, k in sigs.most_common(4))
    print(f"  {tag:44s} heating runs {len(truth)}  filed at the moment {found}  in signatures {top}")
    print(f"  {'':44s} signatures placed at the thermostat: " + (", ".join(f"#{s.id} {sum(s.power.values()):.0f} W x{s.count} ({s.locations.get(name, 0)} credited)" for s in placed) or "none")
          + f"   credited to it off the mat's size: {others}")


def inputs_bench(folder: str, dials) -> None:
    """Home's loads against the settings fed as INPUT= dials: which the
    detector ties to one value of a setting, how strongly, and what that did
    to their evidence - the naming page's bar is DEFAULT_MIN_EVIDENCE."""
    tag = _apply(dials)
    det, filed, _ = _run(folder, "home" if SUBS == "prod" else None)
    bar = 0.7                                  # const.DEFAULT_MIN_EVIDENCE
    print(f"  {tag}   signatures {len(det.signatures)}, over the naming bar {sum(s.evidence >= bar for s in det.signatures)}")
    for name in STAGES:
        share = det.input_time.get(name) or {}
        total = sum(share.values()) or 1.0
        print(f"    {name}: time " + ", ".join(f"{v} {t / total:.1%}" for v, t in sorted(share.items(), key=lambda kv: -kv[1])[:8]))
        tied = [(s, s.input_of(name)) for s in det.signatures]
        tied = [(s, g) for s, g in tied if g]
        for s, (value, sh, lift) in sorted(tied, key=lambda x: -x[1][2])[:12]:
            was = D.INPUT_EVIDENCE
            D.INPUT_EVIDENCE = 0
            plain = s.evidence
            D.INPUT_EVIDENCE = was
            print(f"      #{s.id:<5} {sum(s.power.values()):6.0f} W {s.phases:3s} {s.duration_s:6.0f} s x{s.count:<4} in {value!r}: "
                  f"{sh:.0%} of its runs, {lift:5.1f}x chance   evidence {plain:.2f} -> {s.evidence:.2f}"
                  f"{'  (now over the bar)' if plain < bar <= s.evidence else ''}")
        if not tied:
            print("      no load tied to it")


def surge(site: str, folder: str, dials) -> None:
    tag = _apply(dials)
    det, filed, subs = _run(folder, site)
    labels = lab.label(filed, subs)
    bydev = collections.defaultdict(collections.Counter)
    for i, name in labels.items():
        sig = det.signature_of(filed[i])
        if sig:
            bydev[name][sig.id] += 1
    byid = {s.id: s for s in det.signatures}
    print(f"  [{tag}]")
    for name, c in sorted(bydev.items()):
        if sum(c.values()) < 20:
            continue
        s = byid.get(c.most_common(1)[0][0])
        if s is None:
            continue
        w = sum(s.power.values())
        ratio = s.inrush_w / max(w, 1.0)
        print(f"     {name:18s} {PHYSICS.get(name, '?'):10s} {w:6.0f} W x{s.count:4d}"
              f"  surge ratio {ratio:5.2f}  seen {s.inrush_seen:3d}x at +{s.inrush_when_seen:5.0f} W")


def _pump_runs(folder: str) -> list:
    """The hidrofor's runs from its own meter. It polls every 10 s but Home
    Assistant records only changes, so the reading before a start can be
    minutes old: a switch is taken as the middle of the 10 s before the first
    reading that shows it."""
    h = _read_csv([folder], False).get("sensor.hidrofor_power") or []
    runs, on = [], None
    for t, w in h:
        if on is None and w > 300:
            on = t - 5.0
        elif on is not None and w < 100:
            runs.append((on, t - 5.0))
            on = None
    return runs


def pump(folder: str, dials) -> None:
    tag = _apply(dials)
    det, filed, _ = _run(folder, "home" if SUBS == "prod" else None)
    watts = lambda x: sum(max(v for _, v in lv) for lv in x.levels.values())  # noqa: E731
    on_a = sorted((x for x in filed if "a" in x.phases), key=lambda x: x.start)
    starts = [x.start for x in on_a]
    n, runs = collections.Counter(), _pump_runs(folder)
    for a, b in runs:
        i = bisect.bisect_left(starts, a - 25)
        near, best = [], None
        while i < len(on_a) and on_a[i].start <= a + 25:
            near.append(on_a[i])
            i += 1
        for x in near:
            if 600 <= max(v for _, v in x.levels["a"]) <= 1300 and (
                    best is None or abs(x.start - a) < abs(best.start - a)):
                best = x
        if best is None:
            n["missing" if not near else "wrong size"] += 1
        elif best.phases != "a":
            n["multi"] += 1
        else:
            n["long" if best.end > b + 25 else "short" if best.end < b - 25 else "clean"] += 1
    print(f"  {tag:44s} {len(runs)} runs: " + "  ".join(
        f"{k} {n[k]}" for k in ("clean", "long", "short", "multi", "wrong size", "missing")))


def lengths(kfolder: str, pfolder: str, dials) -> None:
    import statistics
    tag = _apply(dials)
    watts = lambda x: sum(max(v for _, v in lv) for lv in x.levels.values())  # noqa: E731
    a = _read_csv([kfolder], False).get("sensor.solaredge_se17k_m1_ac_power_a") or []
    truth = []
    for t in _kiln_pulses(kfolder):
        i = bisect.bisect_left(a, (t, -1e18))
        before, j = a[i - 1][1], i
        while j < len(a) and abs(a[j][1] - before) > 600 and a[j][0] - t < 600:
            j += 1
        if j < len(a) and a[j][0] - t < 600:     # each switch: the middle of its interval
            truth.append((t - 0.5 * (a[i][0] - a[i - 1][0]), 0.5 * (a[j - 1][0] + a[j][0])))

    def errors(folder, real, keep, tol_start):
        _, filed, _ = _run(folder, None)
        got = sorted((x for x in filed if keep(x)), key=lambda x: x.start)
        st, out = [x.start for x in got], []
        for t0, t1 in real:
            i = bisect.bisect_left(st, t0 - tol_start)
            if i < len(got) and abs(got[i].start - t0) <= tol_start and abs(got[i].end - t1) <= 30:
                out.append(((got[i].end - got[i].start) - (t1 - t0), got[i].start - t0, got[i].end - t1))
        return out
    med = lambda xs: statistics.median(xs) if xs else float("nan")  # noqa: E731
    for name, real, e in (
            ("kiln", truth, errors(kfolder, truth, lambda x: x.phases == "ac" and 5400 <= watts(x) <= 6400, 10)),
            ("hidrofor", _pump_runs(pfolder), errors(pfolder, _pump_runs(pfolder),
                                                     lambda x: x.phases == "a" and 600 <= watts(x) <= 1300, 15))):
        print(f"  {tag:30s} {name:8s} {len(e):3d}/{len(real)} matched: length off by {med([x[0] for x in e]):+5.1f} s"
              f" (start {med([x[1] for x in e]):+5.1f}, end {med([x[2] for x in e]):+5.1f});"
              f" real median {med([t1 - t0 for t0, t1 in real]):5.1f} s")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == "score":
        score(sys.argv[2], sys.argv[3], sys.argv[4:])
    elif cmd == "kiln":
        kiln(sys.argv[2], sys.argv[3:])
    elif cmd == "switch":
        switch(sys.argv[2], sys.argv[3:])
    elif cmd == "inputs":
        inputs_bench(sys.argv[2], sys.argv[3:])
    elif cmd == "fridge":
        fridge(sys.argv[2], sys.argv[3:])
    elif cmd == "surge":
        surge(sys.argv[2], sys.argv[3], sys.argv[4:])
    elif cmd == "attrib":
        attrib(sys.argv[2], sys.argv[3], sys.argv[4:])
    elif cmd == "pump":
        pump(sys.argv[2], sys.argv[3:])
    elif cmd == "lengths":
        lengths(sys.argv[2], sys.argv[3], sys.argv[4:])
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
