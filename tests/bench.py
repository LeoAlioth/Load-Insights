"""Score a detector setting against the two sites' own history, the way every
dial in AGENTS.md was chosen. No Home Assistant; runs in about two minutes a
site and ten at once on a laptop.

    python3 tests/bench.py score  SITE FOLDER [DIAL=VALUE ...]
    python3 tests/bench.py kiln   FOLDER [DIAL=VALUE ...]
    python3 tests/bench.py surge  SITE FOLDER [DIAL=VALUE ...]
    python3 tests/bench.py pump   FOLDER [DIAL=VALUE ...]
    python3 tests/bench.py mat    FOLDER [DIAL=VALUE ...]   Home's floor mat against its thermostat
    python3 tests/bench.py home   FOLDER [DIAL=VALUE ...]   score, mat, kiln and pump off two replays
    python3 tests/bench.py lengths KILN_FOLDER PUMP_FOLDER [DIAL=VALUE ...]

SITE is a key of SITES below (home, kozolec, andrejg); FOLDER a directory of the
per-day CSVs fetch_history.py writes. Build tuning and hold-out folders of
symlinks rather than pointing at data/history/<site> while a fetch is
writing to it - runs started seconds apart would read different days.

DIAL is any module-level constant of insights/detect.py, plus one of the
bench's own:

    HOUSE=prod   build the house reading the way PRODUCTION does - the grid
                 meter negated plus a third of the inverter, through combine()
                 with COMBINE_SETTLE_S (PROD_HOUSE) - rather than reading Anze's
                 template sensor. They agree to 0.1 W but not in timing, and the
                 scores differed (purity 67.6 vs 68.4 %). Always use it at Home;
                 Andrej's site has no other house reading.
    SLICE=6      feed the replay in slices this many hours long, as
                 production's backfill does (the default); 0 for one call
    LIVE=5       ...but the last this many days in one-minute passes, as a
                 site runs once it has caught up (0, the default: none)
    FEED=name    with SUBS=prod, feed only these meters (one dial each) of
                 PROD_SUBS and EXTRA_SUBS - energy_bench's worth
    EXTRA=folder another folder of CSVs read beside FOLDER (Home's fans and
                 blinds in data/history/home-extra)

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
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay as R  # noqa: E402

D = R.D
SOURCE = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()   # a change here rebuilds what R._cached keeps
# Each site's house reading and the device meters ground truth is labelled by.
SITES = {
    "kozolec": {
        "main": {"a": "sensor.multiplus_ii_48_15000_200_100_id_276_output_power_l1"},
        "subs": {
            "Boiler": "sensor.boiler_power",
            "Hidrofor": "sensor.kozolec_hidrofor_power",
            "Well pump": "sensor.well_pump_power",
            "Pond EVSE": "sensor.pond_evse_power",
            "Pastir": "sensor.pastir_staja_power",
            "Bug lamp": "sensor.bug_lamp_power",
        },
    },
    "home": {
        "main": {p: f"sensor.se17k_home_power_phase_{p}" for p in "abc"},
        "subs": {
            "Workshop boiler": "sensor.workshop_boiler_power",
            "Attic AC": "sensor.attic_ac_power",
            "Hidrofor": "sensor.hidrofor_power",
            "EVBox": "sensor.evbox_elvi_power_active_import",
            "NASA station": "sensor.attic_office_power",
            "Server UPS": "sensor.server_ups_power",
            "Susilna": "sensor.shellypmminig3_susilna_power",
        },
    },
    # Home's two circuits as main meters of their own, replayed from Home's
    # history: how well a load is found from its circuit's meter. Their
    # reactive power their own, from the 3EMs' apparent power and power
    # factor, as every meter's (the unify audit, 2026-10-03). Mansarda's phase
    # labels are its own, not the grid's - no matter here.
    "home-hisa": {"main": {p: f"sensor.hisa_phase_{p}_active_power" for p in "abc"},
                  "subs": {"Blaževa Soba": "sensor.blaz_pc_power"}},
    "home-mansarda": {"main": {p: f"sensor.mansarda_phase_{p}_active_power" for p in "abc"},
                      "subs": {"NASA station": "sensor.attic_office_power"}},
    # Andrej's site (2026-10-03): no house reading of its own, HOUSE=prod builds
    # it under these ids. The charger's truth is the go-e's own total, every ~5 s
    "andrejg": {"main": {p: f"sensor.andrejg_load_power_phase_{p}" for p in "abc"},
                "subs": {"Stara Polnilnica": "sensor.goe_216841_nrg_11"}},
}
# What production's _derive_load sums into the house at each site that has a
# grid meter and an inverter: (the grid meter per phase, the grid's sign, the
# inverter's one total, a third on each phase). Both SolarEdge M1s read export
# POSITIVE - Andrej's Energy dashboard declares its grid inverted - so -1.
PROD_HOUSE = {
    "home": ({p: f"sensor.solaredge_se17k_m1_ac_power_{p}" for p in "abc"}, -1.0, "sensor.solaredge_se17k_i1_ac_power"),
    "andrejg": ({p: f"sensor.solaredge_i2_m1_ac_power_{p}" for p in "abc"}, -1.0, "sensor.solaredge_i2_ac_power"),
}
MIN_SESSIONS = 60                      # devices below this are too few to read
# Every meter production reads, the way _resolve_submeters hands them over:
# a three-phase meter per phase under its OWN labels, anything else as one
# total whose phase is unknown. SUBS=prod feeds these to the Fleet;
# SUBS=circuits only the meters others hang under (PROD_PARENTS: Home's two
# 3EMs); SUBS=none no meter at all - the main-meter estimate, the meters then
# only the truth. The default (lab) feeds only SITES' device meters, as the
# bench always has.
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
    "home-hisa": {"Blaževa Soba": "sensor.blaz_pc_power"},
    "home-mansarda": {"Vtičnice - pisarna": "sensor.attic_office_power"},
    # production reads the charger through OCPP (sensor.charger_power_active_import,
    # kW, clock-aligned every 900 s while charging - 154 readings in ten days); the
    # go-e's own total stands in for it - the same charger, in W every ~5 s
    "andrejg": {"Stara Polnilnica": "sensor.goe_216841_nrg_11"},
}
# Meters production's set of 2026-09-30 lacks, their first days fetched into
# data/history/home-extra (fetch_history's fans and blinds groups, the recorder
# holding them from 22 Sep): truth wherever their history is read, fed only by
# name (FEED=). The blinds went on the Energy dashboard on 2026-10-02.
EXTRA_SUBS = {
    "home": {"Bathroom fan": "sensor.bathroom_fan_switch_0_power",       # a Shelly 1PM Mini, ~15 W
             "West Blinds": "sensor.west_blinds_power",                 # Shelly 2PMs, ~100 W for seconds
             "North Blinds": "sensor.north_blinds_power",
             "East Blinds": "sensor.living_room_east_blinds_power"},
}
# the meters production's options declare to hold one device (2026-09-30), and the bathroom fan
PROD_SINGLE = {
    "home": ["Polnilnica", "Workshop charger", "Hidrofor", "Attic AC", "Workshop boiler", "Bathroom fan"],
    "kozolec": ["Boiler", "Washing machine", "Well pump", "Water pump", "Pond", "Pond EVSE", "Pastir",
                "Bug lamp", "Bathroom IR Panel"],
}
# the Energy dashboard's nesting among PROD_SUBS (2026-09-30); Kozolec's all hang under its inverter
# (West Blinds on the grid connection, beside Attic AC)
PROD_PARENTS = {"home": {"Blaževa Soba": "Hiša", "Vtičnice - pisarna": "Mansarda", "Bathroom fan": "Mansarda",
                         "North Blinds": "Mansarda", "East Blinds": "Mansarda"}}
# the bench's own dials, each a global of the same name - see _apply
SUBS = "lab"                           # lab, prod, circuits or none - see PROD_SUBS
HOUSE = ""                             # HOUSE=prod - see the docstring
START_STATE = True                     # the recorder's start-of-window row, as production gets it
SLICE = 6.0                            # production's backfill slice, hours; SLICE=0 for one call
LIVE = 0.0                             # the last days in one-minute passes
NOQ = False                            # NOQ=1: the replay ignores reactive power (--no-q)
_flag = lambda v: bool(float(v))  # noqa: E731
OWN = {"SUBS": str, "HOUSE": str, "START_STATE": _flag, "SLICE": float, "LIVE": float, "NOQ": _flag}
SWITCHES: list = []                    # SWITCH=entity dials, fed as --switch
DRIVERS: list = []                     # DRIVER=entity dials, fed as --driver
STAGES: list = []                      # INPUT=entity dials, fed as --input
FEED: list = []                        # FEED=meter dials - see the docstring
EXTRAS: list = []                      # EXTRA=folder dials - see the docstring
LISTS = {"SWITCH": SWITCHES, "DRIVER": DRIVERS, "INPUT": STAGES, "FEED": FEED, "EXTRA": EXTRAS}
FIRING_MIN_PULSES = 20                 # fewer is two 3 kW loads coinciding, not a firing

# What each metered device physically is, for `surge`. Kozolec's hidrofor is a
# Grundfos Scala2 with a built-in frequency converter, so it soft-starts.
PHYSICS = {
    "Hidrofor": "motor", "Well pump": "motor", "Attic AC": "motor",
    "Boiler": "resistive", "Workshop boiler": "resistive",
    "NASA station": "electronic", "Server UPS": "electronic", "EVBox": "electronic",
    "Pond EVSE": "electronic", "Pastir": "electronic", "Bug lamp": "electronic",
}
HOUSE_IDS = {p: f"sensor.se17k_home_power_phase_{p}" for p in "abc"}

def _peak(s) -> float:
    """A session's peak, summed over its phases."""
    return sum(max(v for _, v in lv) for lv in s.levels.values())


def _at(rows, t, default=None):
    """The value ``rows`` held at ``t``: its last reading at or before it."""
    i = bisect.bisect_right(rows, (t, float("inf"))) - 1
    return rows[i][1] if i >= 0 else default


def _near(sessions):
    """A lookup over ``sessions``: (t, tol) -> those starting within tol of t,
    in start order."""
    got = sorted(sessions, key=lambda x: x.start)
    starts = [x.start for x in got]
    return lambda t, tol: got[bisect.bisect_left(starts, t - tol):bisect.bisect_right(starts, t + tol)]


def _prod_house(s: dict, key=None, site: str = "home") -> dict:
    """A site's house reading the way production builds it (PROD_HOUSE): the
    grid meter, signed, plus a third of the inverter, through combine(). Under
    ``key`` (the files it is built from, and the settle) the combine, ~8 s of
    every Home replay, is kept in replay's CACHE."""
    grid, sign, inverter = PROD_HOUSE.get(site, ({}, 1.0, None))
    inv = s.get(inverter)
    if inv:
        m1 = {p: s[e] for p, e in grid.items() if s.get(e)}
        build = lambda: {p: D.combine([(rows, sign), (inv, 1.0 / 3.0)], settle_s=D.COMBINE_SETTLE_S) for p, rows in m1.items()}  # noqa: E731
        for p, rows in (R._cached(key, build) if key else build()).items():
            s[SITES[site]["main"][p]] = rows
    return s


def _apply(dials) -> str:
    """Set each DIAL=VALUE: an entity list of the bench's (LISTS), one of its
    own settings (OWN), or a constant of detect.py."""
    for d in dials:
        k, v = d.split("=", 1)
        if k in LISTS:
            if v not in LISTS[k]:
                LISTS[k].append(v)
        elif k in OWN:
            globals()[k] = OWN[k](v)
        elif hasattr(D, k):
            cur = getattr(D, k)
            setattr(D, k, v if isinstance(cur, str) else type(cur)(float(v)))
        else:
            raise SystemExit(f"no such dial: {k}")
    if SUBS not in ("lab", "prod", "circuits", "none"):
        raise SystemExit(f"SUBS={SUBS}: lab, prod, circuits or none")
    if HOUSE not in ("", "prod"):
        raise SystemExit(f"HOUSE={HOUSE}: prod is the only house left")
    return " ".join(dials) or "defaults"


_RUNS: dict = {}


def stamps(folder: str) -> list:
    """What a replay of ``folder`` reads - its files and the EXTRA= folders',
    as R._stamp sees them, and their units: a cache key for what is built
    from them."""
    files = R.expand([folder] + EXTRAS)
    return [R._stamp(f) for f in files] + sorted(R.units(files).items())


def _run(folder: str, site: str | None, before=None):
    """Replay a folder; return the Fleet, every session its house detector
    filed, and each sub-meter's full series. Once per folder and site in a
    process, so `home` scores the kiln, the pump and the mat off one replay.
    ``before`` may rebuild the series before the house is built from them
    (energy_bench's planted loads)."""
    if (folder, site) not in _RUNS:
        _RUNS[(folder, site)] = _replay(folder, site, before)
    return _RUNS[(folder, site)]


def _replay(folder: str, site: str | None, before=None):
    argv = [folder, *EXTRAS, "--slice-hours", str(SLICE)] + ([] if START_STATE else ["--no-start-state"])
    if LIVE:
        argv += ["--live-days", str(LIVE)]
    # the house roles pinned to what production reads, never guessed: with the
    # 3EMs' power factors in the history the guess took Hiša's power for the
    # house's (2026-09-30)
    which = site or next((n for n in SITES if Path(folder).name.startswith(n)), None)   # kiln/pump replay with no site
    if NOQ:
        argv.append("--no-q")
    for pin in ([f"power_{p}={e}" for p, e in SITES[which]["main"].items()] if which else []):
        argv += ["--role", pin]
    for eid in SWITCHES:
        argv += ["--switch", f"{eid.split(':')[0]}={eid}"]
    for eid in DRIVERS:
        argv += ["--driver", eid]
    for eid in STAGES:
        argv += ["--input", eid]
    if site and SUBS in ("prod", "circuits"):
        meters, parents = {**PROD_SUBS[site], **EXTRA_SUBS.get(site, {})}, PROD_PARENTS.get(site, {})
        feed = (FEED if SUBS == "prod" and FEED else
                [n for n in PROD_SUBS[site] if SUBS == "prod" or n in set(parents.values())])
        for n in feed:
            e = meters[n]
            argv += (["--sub-phases", f"{n}={','.join(e)}"] if isinstance(e, list) else ["--sub", f"{n}={e}"])
            argv += ["--single", n] if n in PROD_SINGLE.get(site, []) else []
            # under its circuit where that is fed too; otherwise under the main, as it then would be
            argv += ["--parent", f"{n}={parents[n]}"] if parents.get(n) in feed else []
    elif site and SUBS == "lab":
        for n, e in SITES[site]["subs"].items():
            argv += ["--sub", f"{n}={e}"]

    def transform(s):
        s = before(s) if before else s                   # planted loads: the house is built afresh
        key = None if before else ("prod_house", stamps(folder), D.COMBINE_SETTLE_S)
        return _prod_house(s, key, which or "home") if HOUSE == "prod" else s
    return R.run(R.parse(argv), transform, say=lambda *a, **k: None)


def _labels_from(folder: str, site: str, fed: dict) -> dict:
    """The device meters the score is labelled by - SITES', always.
    Fed production's meters, the Fleet also sees circuit meters, and a circuit
    would 'label' half the house; fed none, there is nothing to label by."""
    if SUBS == "lab":
        return fed
    s = R.read_csv([folder], False)
    return {n: sorted(s[e]) for n, e in SITES[site]["subs"].items() if s.get(e)}


def label(sessions, subs):
    """Which device meter's energy ROSE while each session ran."""
    out = {}
    for i, s in enumerate(sessions):
        span = s.duration_s
        if span <= 0:
            continue
        want = s.energy_wh
        if want <= 0:
            continue
        best, best_gap = None, None
        for name, rows in subs.items():
            got = D.energy_between(rows, s.start, s.end)
            if got is None:
                continue
            # what the meter drew just BEFORE the run, held over it - not its
            # average over the minutes before: a charge that follows another
            # had the last one in that average and read as nothing (Kozolec's
            # charger, 2026-10-01)
            i0 = bisect.bisect_right(rows, (s.start - 10.0, float("inf"))) - 1
            if i0 < 0:
                continue
            rose = got - rows[i0][1] * span / 3600.0
            if rose <= 0:
                continue
            ratio = rose / want
            if D.ENERGY_MATCH_LO <= ratio <= D.ENERGY_MATCH_HI:
                gap = abs(ratio - 1.0)
                if best_gap is None or gap < best_gap:
                    best, best_gap = name, gap
        if best:
            out[i] = best
    return out


def _score(assign, labels):
    """Purity (of a signature's labelled sessions, the share of its majority
    device) and, per device, its concentration: (share in its main
    signature, sessions, signatures). Concentration, not a count of
    fragments: the boiler in 24 clusters sounds like a disaster, but 383 of
    its 489 sessions in ONE of them is the row its owner would name; the
    rest is a tail of real behaviour (2026-09-22)."""
    clusters = collections.defaultdict(list)
    for i, cid in assign.items():
        clusters[cid].append(i)
    pure_hits = pure_total = 0
    for ids in clusters.values():
        got = [labels[i] for i in ids if i in labels]
        if got:
            pure_hits += collections.Counter(got).most_common(1)[0][1]
            pure_total += len(got)
    spread = collections.defaultdict(collections.Counter)
    for i, name in labels.items():
        if i in assign:
            spread[name][assign[i]] += 1
    conc = {name: (c.most_common(1)[0][1] / sum(c.values()), sum(c.values()), len(c)) for name, c in spread.items()}
    return {"clusters": len(clusters), "purity": pure_hits / pure_total if pure_total else 0.0,
            "concentration": conc}


# Which production meter each labelled device should end up placed at, and the
# circuit that contains it where there is one.
HOME_OF = {
    "home": {"Hidrofor": ("Hidrofor",), "NASA station": ("Vtičnice - pisarna", "Mansarda"),
             "Workshop boiler": ("Workshop boiler",), "Server UPS": ("Server UPS",),
             "Susilna": ("Susilna",), "EVBox": ("Polnilnica",), "Attic AC": ("Attic AC", "Mansarda")},
    "kozolec": {"Boiler": ("Boiler",), "Hidrofor": ("Water pump",), "Well pump": ("Well pump",),
                "Pond EVSE": ("Pond EVSE",), "Pastir": ("Pastir",), "Bug lamp": ("Bug lamp",)},
    "andrejg": {"Stara Polnilnica": ("Stara Polnilnica",)},
}


def attrib(site: str, folder: str, dials) -> None:
    """For each metered device: of its labelled sessions, how many went into a
    signature PLACED at its own meter (or the circuit around it) - and how
    many main-meter sessions each production meter was credited with."""
    global SUBS
    SUBS = "prod"
    tag = _apply(dials)
    fleet, filed, fed = _run(folder, site)
    det = fleet.main
    labels = label(filed, _labels_from(folder, site, fed))
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
    maps = {n: fleet.phase_map(n) for n in fleet.phase_votes}
    print(f"  {'':44s} phase maps: " + "; ".join(
        f"{n} " + ",".join(f"{k}->{v}" for k, v in sorted(m.items())) + f" ({sum(sum(r.values()) for r in fleet.phase_votes[n].values())} votes)"
        for n, m in maps.items()))


def score(site: str, folder: str, dials) -> None:
    tag = _apply(dials)
    fleet, filed, subs = _run(folder, site)
    det = fleet.main
    subs = _labels_from(folder, site, subs)
    assign = {i: det.signature_of(s).id for i, s in enumerate(filed) if det.signature_of(s)}
    labels = label(filed, subs)
    r = _score(assign, labels)
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
    s = R.read_csv([folder], False)
    a = s.get("sensor.solaredge_se17k_m1_ac_power_a") or []
    c = s.get("sensor.solaredge_se17k_m1_ac_power_c") or []

    pulses = []
    for i in range(1, len(a)):
        t, w = a[i]
        if not 2600 < a[i - 1][1] - w < 3400:
            continue
        cb, ca = _at(c, a[i - 1][0]), _at(c, t + 7.0)
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
    fleet, filed, _ = _run(folder, "home" if SUBS != "lab" else None)
    det = fleet.main
    pulses = _kiln_pulses(folder)
    fires = _firings(pulses)
    inside = lambda t: any(a <= t <= b for a, b in fires)  # noqa: E731
    n = collections.Counter()
    for s in filed:
        if not inside(s.start):
            continue
        w, long = _peak(s), s.end - s.start > 90
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
    rows = R.read_csv([folder], False).get(FRIDGE_POWER) or []
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
    fleet, filed, _ = _run(folder, "kozolec" if SUBS != "lab" else None)
    det = fleet.main
    truth = _fridge_runs(folder)
    at = _near(filed)
    hit, ratio, per_sig = collections.Counter(), [], collections.defaultdict(collections.Counter)
    for t0, t1, step, kind in truth:
        near = [f for f in at(t0, 30) if 30 <= _peak(f) <= 130]
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


def inputs_bench(folder: str, dials) -> None:
    """Home's loads against the settings fed as INPUT= dials: which the
    detector ties to one value of a setting, how strongly, and what that did
    to their evidence - the naming page's bar is DEFAULT_MIN_EVIDENCE."""
    tag = _apply(dials)
    fleet, filed, _ = _run(folder, "home" if SUBS != "lab" else None)
    det = fleet.main
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
    fleet, filed, subs = _run(folder, site)
    det = fleet.main
    labels = label(filed, subs)
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
              f"  surge ratio {ratio:5.2f}")


def _pump_runs(folder: str) -> list:
    """The hidrofor's runs from its own meter. It polls every 10 s but Home
    Assistant records only changes, so the reading before a start can be
    minutes old: a switch is taken as the middle of the 10 s before the first
    reading that shows it."""
    h = R.read_csv([folder], False).get("sensor.hidrofor_power") or []
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
    _, filed, _ = _run(folder, "home" if SUBS != "lab" else None)
    on_a = _near(x for x in filed if "a" in x.phases)
    n, runs = collections.Counter(), _pump_runs(folder)
    for a, b in runs:
        near = on_a(a, 25)
        # the A leg's peak, not the whole session's: a run married to
        # another phase is "multi", not "wrong size"
        best = min((x for x in near if 600 <= max(v for _, v in x.levels["a"]) <= 1300),
                   key=lambda x: abs(x.start - a), default=None)
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
    a = R.read_csv([kfolder], False).get("sensor.solaredge_se17k_m1_ac_power_a") or []
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
        at, out = _near(x for x in filed if keep(x)), []
        for t0, t1 in real:
            got = at(t0, tol_start)[:1]                  # the first to start in the window
            if got and abs(got[0].end - t1) <= 30:
                out.append(((got[0].end - got[0].start) - (t1 - t0), got[0].start - t0, got[0].end - t1))
        return out
    med = lambda xs: statistics.median(xs) if xs else float("nan")  # noqa: E731
    for name, real, e in (
            ("kiln", truth, errors(kfolder, truth, lambda x: x.phases == "ac" and 5400 <= _peak(x) <= 6400, 10)),
            ("hidrofor", _pump_runs(pfolder), errors(pfolder, _pump_runs(pfolder),
                                                     lambda x: x.phases == "a" and 600 <= _peak(x) <= 1300, 15))):
        print(f"  {tag:30s} {name:8s} {len(e):3d}/{len(real)} matched: length off by {med([x[0] for x in e]):+5.1f} s"
              f" (start {med([x[1] for x in e]):+5.1f}, end {med([x[2] for x in e]):+5.1f});"
              f" real median {med([t1 - t0 for t0, t1 in real]):5.1f} s")


MAT_THERMOSTAT = "climate.termostat_kopalnica:hvac_action"


def mat(folder: str, dials) -> None:
    """Home's floor mat against its thermostat's heating (no sub-meter): the
    signature holding the most runs that start 5.5 s after the thermostat goes
    on, its hours counted once, and how many of them the thermostat was NOT
    heating for - the over-report Anze would rather not have (2026-09-30:
    "capturing 80 % of actual energy ... is much preferred over assigning 20 %
    over"). Local days from the first to the last of heating on record."""
    tag = _apply(dials + [f"SWITCH={MAT_THERMOSTAT}"] if f"SWITCH={MAT_THERMOSTAT}" not in dials else dials)
    fleet, filed, _ = _run(folder, "home")                 # with Home's device meters, as `score` runs
    det = fleet.main
    heat = [(a, b) for a, b in R.read_switch([folder], MAT_THERMOSTAT) if b is not None]
    starts = sorted(a + 5.5 for a, _ in heat)

    def aligned(t):
        i = bisect.bisect_left(starts, t - 15)
        return i < len(starts) and abs(starts[i] - t) <= 15
    by = collections.Counter(det.signature_of(s).id for s in filed if det.signature_of(s) and aligned(s.start))
    if not by or not heat:
        print(f"  {tag:44s} mat: nothing starts with the thermostat")
        return
    sid = by.most_common(1)[0][0]
    day = lambda t: (t + det.tz_offset_s) // 86400 * 86400 - det.tz_offset_s  # noqa: E731
    lo, hi = day(heat[0][0]), day(heat[-1][1]) + 86400

    def union(iv):
        out = []
        for a, b in sorted((max(a, lo), min(b, hi)) for a, b in iv if b > lo and a < hi):
            if out and a <= out[-1][1]:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        return out
    runs = union((s.start, s.end) for s in filed if det.signature_of(s) and det.signature_of(s).id == sid)
    warm = union(heat)
    counted = sum(b - a for a, b in runs) / 3600
    heating = sum(b - a for a, b in warm) / 3600
    inside = sum(max(0.0, min(b, d) - max(a, c)) for a, b in runs for c, d in warm) / 3600
    print(f"  {tag:44s} mat #{sid}: heating {heating:5.1f} h, counted once {counted:5.1f} h, OVER {counted - inside:5.1f} h"
          f" (precision {inside / max(counted, 1e-9):4.0%}), caught {inside / max(heating, 1e-9):4.0%};"
          f" next by aligned starts " + ", ".join(f"#{i} x{n}" for i, n in by.most_common(3)[1:]))


def home(folder: str, dials) -> None:
    """Everything Home is benched on, its thermostat fed, off two replays: the
    score and the mat with the device meters, the kiln and the pump without."""
    dials = dials + [f"SWITCH={MAT_THERMOSTAT}"]
    score("home", folder, dials)
    mat(folder, dials)
    kiln(folder, dials)
    pump(folder, dials)


CMDS = {"score": (score, 2), "kiln": (kiln, 1), "inputs": (inputs_bench, 1), "fridge": (fridge, 1),
        "surge": (surge, 2), "attrib": (attrib, 2), "pump": (pump, 1), "mat": (mat, 1),
        "home": (home, 1), "lengths": (lengths, 2)}       # command -> (function, arguments before the dials)


def main() -> int:
    fn, n = CMDS.get(sys.argv[1] if len(sys.argv) > 1 else "", (None, 0))
    if fn is None or len(sys.argv) < 2 + n:
        print(__doc__)
        return 1
    fn(*sys.argv[2:2 + n], sys.argv[2 + n:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
