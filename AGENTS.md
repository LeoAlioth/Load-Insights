# Working on Load Insights

A Home Assistant integration that finds individual appliances in whole-house
meter readings, names them, and forecasts consumption. Two live sites drive
every decision here: **Home** (3-phase, SolarEdge SE17K + M1 meter, several
Shellys) and **Kozolec** (off-grid, single-phase, Victron MultiPlus II).

## Where things are

| path | what |
|---|---|
| `custom_components/load_insights/insights/detect.py` | the pure detector - edges, sessions, signatures, merging. No Home Assistant imports; this is what the bench runs. |
| `custom_components/load_insights/insights/classify.py` | what a load might be, from power, duration, factor, name hints (EN + SL) |
| `custom_components/load_insights/detection.py` | the HA runner - reads the recorder, resolves sub-meters from the Energy dashboard, owns `DETECTOR_GENERATION` |
| `custom_components/load_insights/config_flow.py` | setup and the naming pages |
| `custom_components/load_insights/sensor.py` | entities and the diagnostic attributes |
| `tests/replay.py` | run the detector over exported CSVs, no HA |
| `tests/cluster_lab.py` | ground-truth scoring, and each site's device-meter list |
| `tests/run_all.py` | every pure test file |

Keep new logic in `insights/` where it can be replayed and tested. Anything
that needs `hass` belongs in the layer above it.

## The two sites

| | Home | Kozolec |
|---|---|---|
| supply | 3-phase, grid-tied | single-phase, off-grid |
| main meter | SolarEdge SE17K inverter + M1 meter | Victron MultiPlus II 48/15000 |
| sample interval | ~6 s | ~6 s |
| power resolution | 1 W | 1 W |
| current resolution | **0.01 A** (2.4 VA) | **0.1 A** (23 VA) |
| measured noise | 26 / 25 / 37 W per phase | 10 W |
| library size | ~200 signatures | ~33 |
| notable loads | kiln (2-phase A+C, 5.9 kW, flat 48 s pulses), 3-phase compressor | boiler 1.8 kW, two fridges, Scala2 pump, EVSE |

The current-resolution row is why Kozolec's power factors are worthless below
~230 W and Home's are fine above ~24 W. It is the clearest example of why
nothing here should be a tuned constant.

## The one rule that matters

**A constant that describes the world must be measured from the data. A
constant that describes what to show a person may be a judgement.**

Every instrument differs, and a number tuned on one site is wrong on the
other. So resolution, noise, sampling interval and the error bars that follow
from them are measured per reading, from its own history, and the constants
around them are dimensionless *counts* of those measurements:

| measured per reading | where it comes from |
|---|---|
| `PhaseState.quantum` | `measure_quantum` — a low percentile of its changes, confirmed against a lattice |
| `PhaseState.noise` | median deviation while idle |
| `PhaseState.interval` | observed gap between samples |
| `PhaseState.q_quantum` | `V x dI` from the amps behind the power factor |
| `Signature.pf_mad` / `power_mad` / `duration_mad` | spread across sightings |

`PF_MIN_QUANTA`, `ENERGY_MIN_QUANTA`, `NOISE_REL_FLOOR_FACTOR` and friends are
counts, not watts. The same value means 23 VA at Kozolec's 0.1 A Victron and
2.4 VA at Home's 0.01 A SolarEdge, and neither site is configured.

Legitimately policy, and fine as plain constants: `MAX_SIGNATURES`,
`NAMING_START_ROWS`, `DEFAULT_MIN_EVIDENCE`, `YOUNG_COUNT`. These say what to
put in front of a person, not what an instrument can do.

Watch for absolute constants that should be relative. Two have been found and
fixed this way (`NOISE_REL_MIN_LEVEL`, a flat 300 W standing in for
quantisation); at least one remains: **`SUSTAIN_SECONDS = 5.0` is below Home's
6 s sampling interval, so the guard that should reject a transitional sample
as a level can never fire.**

## Never ship a detector change unmeasured

There is a bench. Use it — replaying costs minutes and a live site costs a
deploy cycle plus a ten-day rebuild.

```bash
python3 tests/run_all.py                       # 15 files, all must pass
python3 tests/replay.py data/history/kozolec   # the detector over real history
python3 tests/replay.py data/history/home
```

`data/` is gitignored twice over; site history never reaches the remote. Each
site is one folder of per-day CSVs exported from the History panel.

For anything touching clustering or attribution, score it against **sub-meter
ground truth** rather than counting signatures — fewer signatures is also what
over-merging looks like. `tests/cluster_lab.py` holds each site's device-meter
entity list and the two metrics:

- **purity** — does one signature hold one device
- **concentration** — does one device land mostly in one signature

Report both. A change that raises concentration while dropping purity is a
trade, not a win, and the numbers belong in the commit message.

Beware the metric's own trap: changing detection changes the *session set*, so
percentages are over different populations. Only devices with 100+ labelled
sessions are readable; ignore anything with single digits.

## Commit messages carry the evidence

Unusually for a repo this size, commit messages here record what was measured,
what was tried and rejected, and what a number came from. Keep that up. The
bodies are long on purpose — a future reader needs to know that a constant was
swept rather than picked, and which explanations were already ruled out.

State clearly when something was a judgement rather than a measurement. A
plausible story survives precisely because nobody checks it.

## Storage and generations

`DETECTOR_GENERATION` (in `detection.py`) gates the stored signature library.
**Bump it whenever the meaning of anything persisted changes** — a rebuild is
the only way to undo damage already baked into a library, because merging is
destructive. A fix to the merge rules stops new damage and cannot un-merge
what walked together before.

Adding a field with a default needs no bump; changing what an existing field
means does.

## Deploying

`dev` builds automatically and both sites pull it via a HACS webhook. Tokens
live in `data/<hostname>` (gitignored).

1. push to `dev` — `.gitea/workflows/GiteaPreRelease.yml` tags a build
2. both sites install it themselves within a minute or two
3. restart each (`POST /api/services/homeassistant/restart`), wait for `RUNNING`
4. a generation bump rebuilds by itself; otherwise call
   `load_insights.reset_detection` to re-run the 10-day backfill
5. wait for `caught_up`, then read the numbers back off the sensor

The detected-loads sensor publishes what the gates measured — `resolution_w`,
`pf_floor_w`, `noise_floor_w`, `baseline_w` — so a gate can be checked against
a site instead of assumed. Add to that list rather than debugging blind.

Every attribute assembled per update must be in `_unrecorded_attributes`;
`tests/test_sensor_attributes.py` fails otherwise. The library runs to well
over 16 KB and the recorder will choke on it.

## Known shortfalls

Open defects, with what is measured and what is guessed. Numbers are from the
two sites' ten-day exports unless stated.

### Confirmed, unfixed

**`SUSTAIN_SECONDS = 5.0` cannot fire.** It exists to stop a transitional
sample being taken for a level, but Home samples every 6 s, so any two
consecutive samples clear it. Raising it to ~8 s collapses spurious
intermediate multi-phase sessions from 53 to 15 in one kiln firing and moves
the merged duration from 42.1 s to a correct 48.0 s. It is not a free change:
single-leg sessions get worse (75 -> 123) and past ~13 s the kiln itself
disappears (100 -> 40 detections), because a 48 s pulse is only ~7 samples.
Should be expressed in measured sample intervals, alongside a fix for the
next item.

**Sessions filed on one leg of a multi-phase load.** 75 of a kiln firing's
pulses file as single-phase at half power. The legs are NOT missed - 91 % of
A-side edge opens have a C-side open within 8 s, and the phases sample
together (0.0 s offset). The congested phase (up to 12 simultaneous open
edges against the other's 8) pairs the stop against the wrong start, giving a
72 s session where the pulse is 48, and a duration that wrong can no longer
merge with the other leg inside `MERGE_TOLERANCE_S`.

**`_pair` reports durations ~12 % short** (42.1 s median against a true 48 s).
Best-size-fit corrects that but is measurably worse overall - see the long
comment in `_pair`. Likely wants a cost combining size gap and age.

**Edge congestion has no root cause yet.** A phase accumulates open starts
whose stops are never matched, up to 12 at once. Not caused by the sustain
problem (peak open edges is unchanged when that is fixed).

**Sub-meter detections are built and discarded.** Each device meter runs its
own detector - 54 signatures across six meters at Kozolec, far more at Home -
and none of it reaches the main library. Anze's stated principle, not yet
implemented: *a detection on a sub-meter should always override one at a
higher level, especially if it is the less noisy one.* The gap is visible in
the numbers: Kozolec's car charger has 11 clean signatures on its own meter
and the main meter attributes nothing to it.

**The rolling session list is too short to reason with.** `MAX_RECENT_SESSIONS
= 200` spans about **2.1 hours** at Home and holds 11 of 199 signatures.
`suggest_levels` was fixed by giving each signature its own `runs` memory, but
anything else reading `recent` inherits the same blindness.

### Instrument limits, not bugs

- `sensor.workshop_boiler_power` has a **46 W quantum** and reports about every
  7 minutes. It cannot support the global step floor and contributes no
  signatures.
- Shelly *energy* sensors report in **0.10 kWh steps, ~1/hour**. Harmless
  today - the window matcher integrates the *power* series - but never build
  attribution on those entities.
- Kozolec's hidrofor (Grundfos Scala2) **ramps rather than switches**, running
  104-247 W and reading as one flat level. Its transition samples cluster at a
  fixed ~0.7 of the gap, which is how a ramping load can be told from an
  averaging meter.
- Three Home device meters contribute 0 signatures: `workshop_boiler_energy`,
  `attic_ac_energy`, `shellypmminig3_ecda3bc6b054_energy`.

### Upgrade caveat

`Signature.runs` only fills as sessions are absorbed. After deploying a build
that adds it, an existing library has none, so `suggest_levels` behaves as
before until a `reset_detection` re-runs the backfill.

## Things already ruled out

Don't re-chase these; each cost real time.

- **Home's kiln ladder** (one flat 2-phase load appearing as ~16 signatures at
  descending powers) is *not*: PV/template skew (it fires at night, 0-5 %
  daylight), meter averaging (no meter at either site averages - the Kozolec
  boiler has 36 clean switches and zero intermediate samples), partial
  transition samples (6 % of rises), or element heating decay (107 aligned
  pulses are flat to 1 %: 100.0 / 100.2 / 100.2 / 99.8 / 98.7 %). It *is* at
  least partly the inoperative `SUSTAIN_SECONDS` guard plus edge mis-pairing.
- **`_pair` must stay most-recent-first.** Best-size-fit was tried and is worse
  at both sites.
- **Imbalance alone does not mean two loads glued together.** A machine with a
  3-phase motor and single-phase parts is legitimately unbalanced. Rank the
  evidence: co-occurrence reliability first, size second, imbalance last. Real
  multi-phase loads here are large *and* balanced (2980/2935, 839/853/837);
  the small unbalanced ones (51/65 W) are coincidences.
- **2-phase loads are normal.** The kiln is A+C. Never assume multi-phase means
  three.
- **A varying load looks like an averaging meter** to a naive test. Require
  stable levels either side of a switch before calling a sample "blended", or
  a computer's power trace masquerades as instrument behaviour.
- **Repeated `consolidate` passes do nothing** - it reaches a fixed point after
  one pass. A library that shrank over time did so from merge damage that a
  rebuild undoes.
