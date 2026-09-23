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
| `tests/bench.py` | the bench every dial was chosen with: `score`, `kiln`, `surge`, and `HOUSE=prod` to read Home as production does |
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
python3 tests/run_all.py                                  # 15 files, all must pass
python3 tests/bench.py score kozolec FOLDER [DIAL=V ...]  # purity and concentration
python3 tests/bench.py score home FOLDER HOUSE=prod [DIAL=V ...]
python3 tests/bench.py kiln FOLDER HOUSE=prod [DIAL=V ...]  # the unmetered kiln
python3 tests/replay.py data/history/kozolec              # the raw detector output
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
sessions are readable; ignore anything with single digits. Better still,
report the ABSOLUTE size of each device's dominant cluster beside the
percentage: if a change removes sessions, a share can rise with nothing
improved, but a dominant cluster that GROWS while the total shrinks means
spurious sessions went and real ones consolidated.

### How to tune without fooling yourself

- **Sweep the dial; a single trial is not a verdict.** A degradation says that
  one setting is wrong, not that the mechanism is. Best-fit pairing, the
  `alike` spread and the sustain guard were each dismissed once on a single
  bad point; two had interior optima and one was the biggest win found.
- **Solve each site on its own, then compare.** Where both agree, the value is
  real. Where they disagree, look for the measured quantity that explains the
  difference before averaging them. `NOISE_REL_FLOOR_FACTOR` was found this
  way: Kozolec's best floor and Home's best factor back-solved to the same 1.5.
- **Tune on one window, validate on days the tuning never saw.** Fetch the
  newest days with `tests/fetch_history.py` (it skips days already on disk)
  and keep them out of every sweep until the choice is made.
- **Dials interact.** Once a large change is decided, re-sweep the small ones
  on top of it rather than against the old baseline.
- **Ground truth only sees loads that have a sub-meter.** Home's kiln has none,
  so a change can raise every score while wiping it out. Check unmetered loads
  of interest separately at every point of a sweep.
- **Point sweeps at a fixed folder.** Build tuning and hold-out folders of
  symlinks and never replay `data/history/<site>` while a fetch is writing to
  it - runs started a few seconds apart will read different days.
- The laptop runs a Home replay in about two minutes on 0.1 GB, and ten at
  once. The sites are Raspberry Pis and a rebuild takes a ten-day backfill.
  Tune here; deploy only to confirm.

## The dials

Every tunable in the detector, what it does, whether it should exist as a
fixed number at all, and what has actually been measured. **Kind** is the
thing to check first:

- **count** — a multiple of something measured per reading. The good kind:
  one value serves every site.
- **ratio** — a relative tolerance. Usually fine, occasionally hides a count.
- **physical** — an absolute number about the world (watts, seconds). A
  candidate for replacing with a measurement; see the one rule above.
- **policy** — what to put in front of a person. Legitimately a judgement.
- **budget** — memory or time bounds. Change for resources, not accuracy.

"Tested" means swept against sub-meter ground truth unless it says otherwise.
*Not tested* is the honest default and most rows have it. Values are those in
the code; the sweeps are over the ten tuning days (8–17 Sep) unless marked as
held out (18–22 Sep).

### Reading the meter

| dial | value | what it does | kind | valid range | tested |
|---|---|---|---|---|---|
| `QUANTUM_MIN_SAMPLES` | 40 | changes to see before a resolution is believed | budget | 20–200 | estimator checked on real sensors (1 W, 0.01 A, 0.1 A found correctly); not swept |
| `QUANTUM_PERCENTILE` | 0.05 | which low percentile of changes is the quantum candidate | ratio | 0.01–0.2 | as above |
| `QUANTUM_LATTICE_TOL` | 0.25 | how close to a whole multiple a change must be | ratio | 0.1–0.4 | as above; without the lattice check Home got 37/38/58 W phantom floors |
| `QUANTUM_LATTICE_SHARE` | 0.9 | share of changes that must sit on the lattice | ratio | 0.7–0.98 | as above |
| `MIN_NOISE_W` | 10 | floor under measured noise; also the user's min-step setting | physical/policy | 5–80 (UI offers 5–80) | not tested |
| `NOISE_MAD_FACTOR` | 4.0 | measured noise = this × median idle deviation | count | 2–6 | not tested |
| `NOISE_REL_CAP` | 0.05 | ceiling on relative noise, so a bad signal cannot call itself all noise | ratio | 0.02–0.1 | not tested |
| `NOISE_REL_FLOOR_FACTOR` | **1.5** | level above which relative noise is measured, in units of `max(quantum, noise) / NOISE_REL_CAP` | count | 1–4 | **swept**: Kozolec's best absolute floor (300 W at 10 W noise) and Home's best factor both back-solve to 1.5. Replaced a flat 300 W |
| `GLITCH_FLOOR_W` | 200 | a house reading this far below zero is skipped as a glitch | physical | 50–500 | not tested |
| `IMPLAUSIBLE_BASELINE_W` | −400 | raises a repair when the idle floor goes this negative | physical | −1000 – −100 | not tested |
| `AMP_STEP_MEMORY` | 600 | current changes remembered for measuring the amps' resolution | budget | 200–2000 | not tested; measuring per pass instead silently disabled the PF gate |

### Finding steps and levels

| dial | value | what it does | kind | valid range | tested |
|---|---|---|---|---|---|
| `SUSTAIN_SAMPLES` | 2 | readings a new level must hold before it counts | count | 2–4 | Kozolec: 3 ≈ `SUSTAIN_INTERVALS` 1.5 (86.3 vs 87.3 % wconc); 4 is worse (80.6 %) |
| `SUSTAIN_SECONDS` | 5.0 | ...and at least this long | **physical** | — | **defective**: below the 6 s sampling interval at both sites, so it never fires |
| `SUSTAIN_INTERVALS` | **1.5** | ...and at least this many measured sample intervals. 1.5 ≈ three readings at 6 s | count | 0–3 | **swept at both sites and the kiln; shipped.** Kozolec wconc 72.2 → 87.3 %, confirmed held out 73.8 → 84.2 %. Home held-out purity 64.6 → 68.3 %. Kiln (no sub-meter) best at 1.5: full-size sessions 272 → 317, spurious ladder 236 → 145; from 2.0 it loses pulses. Costs loads that wander rather than switch (NASA station); `ALIKE_MAD_SHARE` gives most of it back. Made the old surge detector blind — see `_declare_surge` |
| `INTERVAL_PERCENTILE` | **0.5** | a reading's interval is this percentile of its last `INTERVAL_GAPS` gaps - its cadence, not the mean gap between recorded changes | ratio | 0.1–0.5 | **swept; shipped.** Home's three phases were 7.1/6.0/6.0 s from the running mean, all 6.0 with the median. Kiln full-size 305 → 337, ladder 130 → 92; held out, Home purity 76.7 → 77.6 %, Kozolec hidrofor 65 → 75 |
| `MATCHED_STOP_SAMPLES` / `_INTERVALS` | **0** (off) / 0.0 | a drop the size of an open edge may pass on fewer readings than a new level | count | 1–2 / 0–1 | **swept, not shipped.** Helps the kiln (single-leg 154 → 114) but costs ramping and wandering loads (Kozolec's Scala2 140 → 106). Awaiting cross-leg corroboration - see the single-leg plan |
| `BASELINE_EMA` | 0.02 | how fast the idle floor follows drift while nothing runs | ratio | 0.005–0.1 | not tested |
| `BASELINE_SEED_SAMPLES` | 24 | readings the first baseline is seeded from | budget | 12–120 | not tested |
| `BASELINE_SEED_PERCENTILE` | 0.25 | low percentile for the seed, so a load running at start is not the floor | ratio | 0.05–0.5 | not tested |
| `SLOW_FOLLOW` | 0.02 | how fast the tracked level follows drift, so a ramp is never a step | ratio | 0.005–0.1 | not tested |
| `Q_RECENT_SAMPLES` | 8 | idle readings the pre-step reactive median is taken over | count | 4–20 | not tested |
| `INRUSH_RATIO` | 2.5 | a start's first reading or level this many times the settled one is a motor's surge | ratio | 1.5–5 | on/off only; not swept. Checked physically: resistive loads (kiln, boilers) carry none, Kozolec's fridge carries +150 W. Home's Kompresor has never shown one, under old or new code |
| `INRUSH_SAMPLES` | 2.0 | ...if it lasts no more than this many sample intervals | count | 1–3 | not tested |

### Pairing steps into sessions

| dial | value | what it does | kind | valid range | tested |
|---|---|---|---|---|---|
| `MATCH_EDGE_REL` | 0.15 | a step down pairs with a step up this close in size (or the noise) | ratio | 0.05–0.3 | **swept at both**, on top of sustain 1.5. Kozolec 0.10 → 84.1, **0.15 → 87.3**, 0.20 → 86.3 % wconc; Home 0.10 drops purity to 63.9 %, 0.20 costs the workshop boiler 57 → 40. Stays |
| `PAIR_TIE_BAND` | **1.0** | how much better a size match must be to override recency. 0 = best-fit, 1 = newest that passes. Above 1 is identical to 1 | ratio | 0–1 | **swept** at both: Home prefers 1.0 clearly (purity 67.6 vs 64.8 % at 0.5); Kozolec flat 0.5–1.0. Stays |
| ~~`PAIR_AGE_WEIGHT`~~ | removed | weighted ABSOLUTE age rather than rank when choosing | — | — | **swept 0–3 at Kozolec, then removed**: every positive value cost ~6 points. Recency rank carries the information, magnitude does not |
| `MAX_OPEN_S` | 86400 | a start whose stop never came is dropped after this | physical | 3600–172800 | not tested |
| `MAX_OPEN_EDGES` | 12 | open starts kept per phase; the OLDEST is evicted past this | budget | 6–40 | **swept at both** (production path). Lower looks better at Home by share - cap 4 gives purity 84.9 % - but that is the population trap: NASA's dominant cluster falls 113 → 78. On absolute clusters Home's best is 8 (+3/+8/+1); Kozolec's hidrofor loses 10 at 8. Small, opposite, so it stays. Raising it is worse at Home (24: NASA 113 → 68). Wants eviction by staleness rather than by count |
| `NOISE_SESSION_WH` / `_S` | 3.0 Wh / 20 s | a session smaller AND shorter than both is dropped as a blip | physical | 1–10 Wh / 5–60 s | not tested |

### Combining phases

| dial | value | what it does | kind | valid range | tested |
|---|---|---|---|---|---|
| `MERGE_TOLERANCE_S` | 15 | per-phase sessions this close in start AND end are one load | physical | 5–30 | **swept 8–40 s and interval-based (2×, 4×)**: no effect; single-leg sessions stay at 144–161 throughout. So the single-leg problem is NOT a merge-window problem |
| `HELD_TAIL_S` | 60 | closed sessions wait this long for a partner on another phase | physical | 15–180 | not tested |
| `PHASE_BALANCE_MIN` | 0.4 | smallest leg / largest leg for a multi-phase session to stand | ratio | 0.2–0.7 | not tested. Note imbalance alone is weak evidence of two loads |

| `COMBINE_SETTLE_S` | **0.3** | readings of a summed house value closer than this are one update in pieces; only the last is kept | physical | 0.1–0.5 (bursts are < 50 ms, cadence ≥ 2.4 s) | **swept; shipping.** Home, production path: purity 68.4 → 75.7 %, wconc 31.7 → 50.9 %, hidrofor dominant cluster 356 → 471. Flat from 0.1 to 0.3. Kiln 347 → 305: the lost sessions are one-reading pulses that passed sustain only because a phantom reading supplied their second sample |

### Signatures: matching and merging

| dial | value | what it does | kind | valid range | tested |
|---|---|---|---|---|---|
| `MATCH_POWER_REL` | 0.10 | power tolerance for a session to match a signature | ratio | 0.05–0.25 | not tested |
| `ALIKE_MAD_SHARE` | **0.10** | share of two signatures' measured spread that may widen merge admission | ratio | 0–0.20 (above 0.20 the anti-walk guarantee breaks) | **swept.** On the template path it recovered Home's NASA station 22 → 35; on the production path with phantoms removed it does nothing at Home (0, 0.10 and 0.15 identical) - it was compensating for phantom fragmentation. Still positive at Kozolec on held-out days (hidrofor 58 → 65), where nothing is summed. Kept |
| `MATCH_DURATION_FACTOR` | 3.0 | duration tolerance for a load that keeps time | ratio | 1.5–6 | not tested |
| `LOOSE_DURATION_FACTOR` | 30.0 | duration tolerance for one that does not | ratio | 5–100 | the 3× → 30× switch looks like a cliff and was **swept as a continuous tolerance** (`exp(z × measured spread)`, z 2–6, at both sites): tight values lose clearly (Home's workshop boiler halves, Kozolec's hidrofor 142 → 99), loose ones converge back to the cliff. The cliff measures as right; the dial was removed |
| `DURATION_IDENTITY_COUNT` | 4 | sightings before a signature's duration can identify it | count | 2–10 | not tested |
| `DURATION_IDENTITY_SPREAD` | 0.35 | `duration_mad / duration` below which duration identifies the load | ratio | 0.1–0.6 | not tested |
| `MATCH_PF_TOL` | 0.15 | power-factor tolerance, now widened by both sides' `pf_mad` | ratio | 0.05–0.3 | not swept; the `pf_mad` widening replaced a hard gate and took Kozolec 86 → 30 signatures |
| `ABSORB_WINDOW` | 100 | caps the weight of history in a signature's running means | budget | 20–500 | not tested (Anze chose 100 over 50) |

### Power factor

| dial | value | what it does | kind | valid range | tested |
|---|---|---|---|---|---|
| `PF_TRUST_MAD` | 0.05 | widest error bar a factor may carry and still reach the classifier | ratio | 0.02–0.15 | judgement from the classifier's band width (heater starts at 0.93); not swept |
| `PF_MIN_QUANTA` | 10 | load size, in quanta of apparent power, below which factors stop meaning much. Now only published as `pf_floor_w` | count | 5–20 | the swing measurement it rests on (0.20 below 100 W at Kozolec) was measured; not swept |

### Attribution to device meters

| dial | value | what it does | kind | valid range | tested |
|---|---|---|---|---|---|
| `ENERGY_MATCH_LO` / `_HI` | 0.65 / 1.35 | a device's energy rise must be within this band of the session's | ratio | 0.5–0.9 / 1.1–1.5 | not tested; flat band that could be widened by the measured quantum instead |
| `ENERGY_MIN_QUANTA` | 2.0 | an energy rise must span this many power quanta × duration | count | 1–4 | not tested |
| `IDLE_WINDOW_S` | 900 | the preceding window the rise is measured against | physical | 300–1800 | not tested |
| `MATCH_PATIENCE_S` | 1200 | how long a main session waits for a slow device meter | physical | 300–3600 | set from observed Shelly reporting lag; not swept |
| `CROSS_METER_DURATION_FACTOR` | 12.0 | duration tolerance between a main and a device session | ratio | 3–30 | not tested |
| `SUB_SAMPLE_TAIL_S` | 7200 | device readings kept for the energy answer | budget | 3600–43200 | not tested |

### Suggesting one device across settings

| dial | value | what it does | kind | valid range | tested |
|---|---|---|---|---|---|
| `MATCH_LEVEL_RATIO` | 10.0 | widest span of power a device's settings may have | ratio | 6–30 | **swept** at Home: the kiln's group holds 17 members for every value 10–25 with the smallest at 667 W; unbounded admits a 165 W load. Kozolec has no group wider than 9.4× |
| `RUN_MEMORY` | 12 | run windows each signature remembers for "never two at once" | budget | 6–50 | not tested |
| `MAX_RECENT_SESSIONS` | 200 | the rolling session list | budget | — | spans only ~2 h at Home; do not build new logic on it |

### Library housekeeping

| dial | value | what it does | kind | valid range | tested |
|---|---|---|---|---|---|
| `MAX_SIGNATURES` | 200 | soft cap on the library; only unprotected signatures are evicted | budget | 100–500 | not tested; Home sits right at it |
| `ESTABLISHED_EVIDENCE` | 0.5 | evidence at which a signature is never evicted | policy | 0.3–0.7 | not tested |
| `ESTABLISHED_HORIZON_S` | 400 days | ...while seen within this | policy | — | not tested |
| `YOUNG_COUNT` | 3 | sightings under which a new signature is protected... | policy | 2–5 | not tested |
| `PRUNE_GRACE_S` | 6 h | ...for this long | policy | 1–48 h | not tested |
| `SUCCESSOR_*` | 7 d, 6 intervals, 0.5, 5 | when a quiet named load hints that an unnamed one is what it became | policy | — | not tested |

### Solar and supply

| dial | value | what it does | kind | valid range | tested |
|---|---|---|---|---|---|
| `PV_SHARE_MIN` / `_MAX` | 0.25 / 1.25 | share of an array's step that may explain a phase's step | ratio | — | not tested |
| `PV_MIN_SWING_W`, `PV_MIN_SAMPLES` | 200 W, 30 | evidence needed before deciding whether a reading sees the sun | physical/count | — | not tested |
| `EXPORT_FLOOR_W`, `EXPORT_SHARE` | 50 W, 0.005 | how much export marks a reading as carrying generation | physical/ratio | — | not tested |
| `SOURCE_IDLE_W`, `SOURCE_IDLE_SHARE` | 25 W, 0.9 | how an AC input is told apart as utility, generator or nothing | physical/ratio | — | not tested |
| `LIVE_SHARE` | 0.2 | how often a reference circuit must carry something to be one | ratio | — | not tested |

### Naming page (policy, `const.py` / `detection.py` / `config_flow.py`)

| dial | value | what it does | valid range |
|---|---|---|---|
| `DEFAULT_MIN_EVIDENCE` | 0.7 | evidence needed to be offered (user-configurable) | 0.3–0.9 |
| `NAMING_START_ROWS` | 6 | rows the page opens with | 3–12 |
| `NAMING_ROWS_PER_NAME` | 4 | rows added per name given | 2–8 |
| `NAMING_MIN_ROWS` | 5 | rows shown even when too few clear the bar | 3–10 |
| `NAMING_MAX_ROWS` | 24 | the menu's hard length | 12–40 |
| `MIN_COUNT_TO_NAME` | 2 | a load seen once is not offered | 2–3 |
| `NAMING_MIN_WH` | 50 | energy a load must have used to be worth naming | 10–200 |
| `WHEN_*` | 0.65 / 7 d / 2 d / 3 | when "usually runs mornings" etc. may be said | — |

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

**Sustain now costs loads that wander.** `SUSTAIN_SECONDS = 5.0` could never
fire at 6 s sampling; `SUSTAIN_INTERVALS = 1.5` fixed that, and was the
largest improvement found. But a level must now hold for about three
readings, and a continuously varying load rarely does: Home's NASA station
(computers) lost ground on both halves of the data. `ALIKE_MAD_SHARE`
recovers most of it. A cleaner fix would be to know which loads ramp or
wander - the transition-position test that told Kozolec's Scala2 pump apart
from an averaging meter - and not demand a plateau of them.

**The pairing cap binds, but moving it does not fix single-leg.** Home's
phase C sits at `MAX_OPEN_EDGES = 12` through a kiln firing. Swept 4-40 at
both sites: lower looks better by share at Home, which is mostly the
population trap; on absolute clusters Home prefers 8 and Kozolec 12, small and
opposite. Single-leg sessions stay at 134-184 throughout, and so they do across
every merge window from 8 to 40 s. The single-leg mechanism is still unknown -
it is neither the cap nor the merge window.

**A short surge is invisible at 6 s sampling, and a mean hides the rare
catch.** Home's Kompresor is an ABAC LN1 A39B 100 T3 DOL: 2.2 kW, three-phase,
started direct on line (no soft start, no star-delta) with a head unloader, so
it spins up against no compression and the inrush is over in well under a
second. Of 396 starts over fifteen days, the first reading caught a surge
>= 2.5x on 3 (0.8 %), up to 3.5x; the median first reading is exactly the
running level. That is an instrument limit, not a detector bug. But
`Signature.inrush_w` is a running MEAN, so three catches in 396 average to
nothing and the evidence is lost. Kozolec's fridge, a hermetic compressor
starting against pressure, catches its surge on most starts and shows +150 W.
For rare catches, the largest surge seen or the share of starts showing one
would keep what the mean discards.

**Sessions filed on one leg of a multi-phase load - diagnosed, not fixed.**
Home's kiln (2-phase A+C, flat ~48 s pulses, ~9 s apart) produces ~150
single-leg sessions over ten days alongside ~305-337 proper A+C ones; the grid
meter shows 441 real pulses. Classified by which of `_merge_and_file`'s
conditions refused each lone leg against its partner: **ends apart** is
involved in ~150 of 175 and the sole reason in 88. The legs start at the same
instant (+0.0 s) but one runs on - median 72 s past its partner, up to 10 min
- which is why no merge window up to 40 s helped. Causes, by weight:

1. **The sustain guard swallows the kiln's short OFF-gaps.** The kiln is off
   for one or two readings between pulses; a dip that short is rejected like
   any transient, so consecutive pulses glue into one long session on
   whichever leg rejected the gap. 69 of 71 single-level long legs contain
   real off-gaps in the raw data (23 of one reading, 64 of two). Single-leg
   sessions rose 87 -> 147 when sustain went 0 -> 1.5, the direct cost of the
   day's biggest win.
2. **Each leg had a different interval.** A running mean of recorded gaps
   measures how often a value CHANGES; Home's quiet phase A came out 7.1 s
   against C's 6.0, so one meter judged its two legs by different sustain
   thresholds. Fixed by `INTERVAL_PERCENTILE = 0.5` (all three now 6.0 s) -
   real, but single-leg barely moved, so it was not the driver.
3. **A coincident load on one leg** (11 of 88): the step up included another
   load, so the kiln's stop read as a step DOWN of a still-running load and
   the session ran on with the leftover.
4. Minor: starts apart, a partner already taken by a first-fit group, an
   unrelated load being the nearest partner.

Tried: merge window 8-40 s and interval-based - no effect. Open-edge cap 4-40
- no effect on single-leg. **Matched-stop rule** (a drop the size of an open
edge needs less sustain; `MATCHED_STOP_SAMPLES`, off) - kiln full-size
337 -> 364-381, single-leg 154 -> 114, but Kozolec's ramping Scala2 hidrofor
140 -> 106 and, at one reading, Home's wandering NASA station 114 -> 65. It
cannot tell a switched load stopping from a ramping one dipping.

The plan, in order:

1. **Let the new polling rate speak first.** At Home's 2.4 s a 9 s off-gap is
   three or four readings, which sustain 1.5 (3.6 s) accepts. Cause 1 may
   largely resolve itself. Re-measure single-leg on a week of 2.4 s data before
   building anything - `bench.py kiln`, with the raw pulse count as truth.
2. **Corroborate the stop across legs.** Switch the matched-stop rule on only
   for a leg of a multi-phase start: another phase opened a balanced edge at
   the same instant and has just stopped. The kiln's legs corroborate each
   other; single-phase ramping and wandering loads are untouched. Needs
   `Detector.process` to walk all phases in time order rather than phase by
   phase - behaviour-neutral by itself, since each phase's state is
   independent - so each phase can see its siblings' open edges.
3. **Split on a coincident drop** (cause 3): a level drop on one leg at the
   instant a balanced partner leg closes is that load stopping; split the long
   leg into the stopped part (the drop) and the residual load.
4. Longer term: know which loads RAMP (the transition-position test that told
   the Scala2 from an averaging meter) and never ask them for a plateau.

Measure each step against: kiln full-size sessions (toward 441), single-leg and
ladder (toward 0), and purity and concentration at both sites.

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

**Surge evidence: when-seen is not better than the mean.** Tried keeping a
surge's size WHEN caught (`SURGE_MIN_SEEN`) so rare catches count. It rescues
Home's hidrofor (7 catches at +1828 W on 871 W) but inflates the small
electronic signatures 5-20x - NASA station 0.34 -> 5.22, the 32 W signature
Server UPS and Susilna share 0.36 -> 8.63 - which would reach their
descriptions as "starts at 334 W before settling". A noise guard
(`SURGE_MIN_NOISE`) did nothing: those catches are genuine 270-340 W first
readings, most likely another load switching at the same moment, not noise.
The classifier's final guess changed for no device either way. Not adopted.
What would separate a motor from a coincidence is CONSISTENCY - a motor's
surge ratio repeats, a coincidence's is random. The counters are kept.
Physically checked: kiln and both boilers carry no surge under any rule.
Kozolec's hidrofor is a Grundfos Scala2 with a built-in frequency converter,
so it soft-starts and should NOT surge.

**About a third of Home's house readings are phantoms.** Production sums grid
and inverter with `combine()`, which emits at every input's timestamp against
the other input's last value. When both update together - 26,310 of 26,466
sub-second gaps are under 50 ms - the first sum is computed against a stale
partner and corrected within 50 ms. `max_skew_s` was meant to catch this and
must not be used on recorder data: Home Assistant only records a CHANGE, so an
inverter at 0 W all night looks hours stale and every night sample is dropped
(the kiln fell from 347 sessions to 119). `COMBINE_SETTLE_S` keeps only the
last reading of each burst instead, which needs no judgement about freshness.

**The bench read a slightly different input than production at Home.** The
replay read Anze's template sensor; production sums grid and inverter itself.
Values agree to 0.1 W, but timestamps and change-only recording differ, and
the scores did too (purity 67.6 vs 68.4 %). Replay Home through
`combine()` - the bench's `benchhouse.py` - so it matches production.

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
