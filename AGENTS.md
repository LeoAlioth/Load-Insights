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
| sample interval | ~6 s in the history; 2.4 s since Anze sped up SolarEdge polling (22-23 Sep) | ~6 s |
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
quantisation), and a third since: `SUSTAIN_SECONDS = 5.0` sat below Home's 6 s
sampling interval, so the guard meant to reject a transitional sample could
never fire. It is now `SUSTAIN_INTERVALS`, a count of measured intervals.

## What the detector is for

**Steady-state appliances first.** A kiln, a boiler, a fridge, a switched
pump, a compressor: these repeat, and the detector should find them well.
Highly variable loads - computers (Home's NASA station), variable-speed pumps
(Kozolec's Grundfos Scala2), a UPS - are harder to detect by nature and are
the better candidates for a meter of their own. So a change that clearly
improves the steady loads and makes the variable ones somewhat worse is a
change worth taking (Anze, 2026-09-23). Report the two groups separately so
the trade is visible, and do not reject a change for losses confined to the
variable ones.

## When Anze lists several things to build

Be thorough. Build every item on the list, and account for each one at the
end: built and measured, or deferred - with the reason said in so many words
and his agreement. Do not let one item's findings quietly stand in for
another, and do not decide alone that an item "belongs in its own release"
and then move on. On 2026-09-22 he said a detection on a sub-meter should
always override one on a higher level. It was scoped as "the largest
remaining piece", never built, and he believed a day later that it had been.
Keep the list in writing while working through it and tick items off
against it, not against memory.

## Never ship a detector change unmeasured

There is a bench. Use it — replaying costs minutes and a live site costs a
deploy cycle plus a ten-day rebuild.

```bash
python3 tests/run_all.py                                  # 15 files, all must pass
python3 tests/bench.py score kozolec FOLDER [DIAL=V ...]  # purity and concentration
python3 tests/bench.py score home FOLDER HOUSE=prod [DIAL=V ...]
python3 tests/bench.py kiln FOLDER HOUSE=prod [DIAL=V ...]  # the unmetered kiln
python3 tests/bench.py score home FOLDER HOUSE=residual   # the house less its sub-meters
python3 tests/bench.py score SITE FOLDER SUBS=prod        # feed production's meters to the Fleet
python3 tests/bench.py attrib SITE FOLDER                 # where each device's sessions are placed
python3 tests/bench.py pump FOLDER HOUSE=prod             # every hidrofor run, run by run
python3 tests/bench.py lengths KILN_FOLDER PUMP_FOLDER HOUSE=prod   # lengths vs real ones
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

**Kiln figures dated before 2026-09-23 are the OLD kiln metric**: signature
counts in a power band. That band also caught two other 3 kW single-phase
loads at Home, 2.5 and 5 minutes long, that run on days the kiln never fired.
And a signature averaging 5395 W against one at 5436 W moved 39 sessions in or
out of "full-size". Compare old figures with each other, not with new ones.
`bench.py kiln` now counts what was filed INSIDE the firings, against the 437
pulses the grid meter itself shows in them.

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
| `SUSTAIN_AGREE` | **1** | a new level is only the pending readings that AGREE with the newest; the ones before are the transition | switch | — | **shipped (gen 13).** The old rule took the median of everything away from the old level, so [1251, 1082, 436] - a sag, a half-caught switch, the stop - read as a 198 W step: it closed a 4-hour-old 179 W start and left the hidrofor's session open 7.8 h. Home purity 76.6 → 78.7 %, held out 75.0 → 78.6 %; hidrofor held out 208 → 213; Kozolec's boiler 499 / 337 → 500 / 339. The TIME away is still counted from the first reading that left - timing it from the agreeing readings alone cost the kiln a third of its pulses (399 → 264), whose off-gaps are two readings after a half-caught one |
| `SUSTAIN_AGREE_TOL` | 1.414 | readings agree within this many `noise_at`s... | count | 1–2 | swept 1.0 / 1.414 / 2.0: Home best at √2 (held out 76.8 / 78.6 / 77.1 %); Kozolec's boiler flat, its Scala2 prefers 1.0. √2 because two readings each carry the noise |
| `SUSTAIN_AGREE_REL` | 0.15 | ...or within this share of the step they are making | ratio | 0–0.25 | swept 0 / 0.1 / 0.15 / 0.25 on the kiln: 0 lost pulses (345 full-size) because phase A's off readings wander 19 W under load; 0.1-0.25 all ~395. 0.15 is the pairing tolerance, and the best for Kozolec |
| `SUSTAIN_AGREE_MAX_INTERVALS` | 12 | after this many intervals of readings that never agree, the old median decides - a wandering load | count | 3–∞ | swept 3 / 6 / 12 / 24 / ∞: flat from 12 up (Kozolec identical 12-∞), so it barely binds; kept as a safety net |
| `STEP_AT_HALFWAY` | **1** | a step is dated at the first reading more than half-way to the new level, not the first that left the old one | switch | — | **shipped (gen 13).** Kiln single-leg 141 → 118, main signature x335 → x384; hidrofor held out 213 → 220; workshop boiler held out 29 → 38; Kozolec's Scala2 held out 67 → 53 (it ramps). The kiln's LENGTHS were already right (42.0 s against 42.0 s off the grid meter) || `INTERVAL_PERCENTILE` | **0.5** | a reading's interval is this percentile of its last `INTERVAL_GAPS` gaps - its cadence, not the mean gap between recorded changes | ratio | 0.1–0.5 | **swept; shipped.** Home's three phases were 7.1/6.0/6.0 s from the running mean, all 6.0 with the median. Kiln full-size 305 → 337, ladder 130 → 92; held out, Home purity 76.7 → 77.6 %, Kozolec hidrofor 65 → 75 |
| `MATCHED_STOP_SAMPLES` / `_INTERVALS` | **0** (off) / 0.0 | a drop the size of an open edge may pass on fewer readings than a new level | count | 1–2 / 0–1 | **swept, not shipped.** Helps the kiln (single-leg 154 → 114) but costs ramping and wandering loads (Kozolec's Scala2 140 → 106). Awaiting cross-leg corroboration - see the single-leg plan |
| `CORROBORATED_STOP_SAMPLES` / `_INTERVALS` | **1** / 0.0 | a stop another leg of the same load vouches for passes on one reading | count | 1–2 | **swept; shipped.** 1 reading beats 2 (old metric: single-leg 80 vs 136 with the loose partner test). Off → on, inside the firings: full-size 353 → 399 of 437 pulses, single-leg 212 → 158, ladder 23 → 22. See the single-leg entry for the trade |
| `CORROBORATE_INTERVALS` | **1.0** | how close in time a partner leg must start and stop, in sample intervals | count | 1–2.5 | **swept**: the merge test's 15 s let unrelated loads vouch for each other; 1.0 kept the most of the hidrofor (208 vs 196 loose) |
| `CORROBORATE_BALANCE` | 0.7 | how near in power a partner leg must be | ratio | 0.7–0.85 | swept 0.7 and 0.85; 0.85 lost more single-leg than it saved |
| `CORROBORATED_CLOSES_ITS_EDGE` | 1 | close the edge the partners vouched for, not the newest of that size | switch | — | kiln ladder 114 → 98, pulse length back toward the true 42 s; did not recover the hidrofor |
| `CORROBORATED_SPLIT` | **1** | a leg whose start was bigger than the drop its closed partner vouches for is split: that part closes, the rest stays open as the coincident load | switch | — | **shipped (gen 13).** With the reading-level partner test it fired 164 times in ten days, 11 on the kiln, and cost Home 1.9 points held out; with only CLOSED partners: Home 77.6 → 78.6 %, held out 79.7 → 80.6 %, kiln main signature x385 → x397, single-leg 116 → 112, ladder 18 → 23. Kozolec identical || `BASELINE_EMA` | 0.02 | how fast the idle floor follows drift while nothing runs | ratio | 0.005–0.1 | not tested |
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
| `ORPHAN_MARGIN` | 0 (off) | give up on a start once it has run this many times the longest any same-sized load has | count | 2–8 | **swept 3 and 8, not shipped - side effects.** At 3 sessions over 3 h halve (157 → 84) but Home's dryer loses 13 of 34 sessions (it runs long; loads its size run short), pump runs missed 20 → 29, Kozolec's boiler 500 → 496. Needed the bench to slice: fed in one call the library is empty until the end || `MAX_OPEN_EDGES` | 12 | open starts kept per phase; the OLDEST is evicted past this | budget | 6–40 | **swept at both** (production path). Lower looks better at Home by share - cap 4 gives purity 84.9 % - but that is the population trap: NASA's dominant cluster falls 113 → 78. On absolute clusters Home's best is 8 (+3/+8/+1); Kozolec's hidrofor loses 10 at 8. Small, opposite, so it stays. Raising it is worse at Home (24: NASA 113 → 68). Wants eviction by staleness rather than by count |
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
| `PHASE_MAP_MIN_VOTES` | 30 | shared single-phase sessions before a three-phase meter's channels are mapped onto the grid connection's phases (`phase_mapping`) | count | 10–100 | not swept; Home's attic 3EM reaches 147 in five days and maps a→B, b→C, c→A; the Hiša 3EM 1327, identity |
| `SUB_OVERRIDE` | **1** | house sessions wait to be filed until every sub-meter fast enough to have seen them (two readings inside the run) has reported past their end; a partner then decides the signature | switch | — | **shipped (gen 14)** with the two below: Kozolec's Scala2 128 → 195 in its main signature, held out 53 → 61, purity unchanged; Home 78.6 / 80.6 → 79.1 / 80.7 %; kiln main signature x397 → x392 |
| `SUB_METER_IDENTITY` / `SUB_DEVICE_SHARE` | **1** / 0.5 | a session whose ENERGY a one-device meter accounts for joins the house signature most of that meter's sessions went to, when it fits; one device = its own library has ≥ this share in one signature | switch / ratio | 0.4–0.8 | almost all of the gain above; share not swept (hidrofor plug 99 %, Hiša 55 %) |
| `SUB_IDENTITY_CIRCUITS` | 0 | whether a circuit meter (many loads, e.g. Hiša) may decide too | switch | — | off was better at Home (79.1 / 80.7 vs 78.5 / 80.5 %), identical elsewhere |
| `SUB_POWER` | 0 (off) | take the quieter sub-meter's power for a matched session | switch | — | slightly worse (NASA held out 26 → 22) |

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
| `DEFAULT_MIN_EVIDENCE` | 0.7 | evidence needed to be offered (a setting until 2026-09-23) | 0.3–0.9 |
| `NAMING_START_ROWS` | 6 | rows the page opens with | 3–12 |
| `NAMING_ROWS_PER_NAME` | 4 | rows added per name given | 2–8 |
| `NAMING_MIN_ROWS` | 5 | rows shown even when too few clear the bar | 3–10 |
| `NAMING_MAX_ROWS` | 24 | the menu's hard length | 12–40 |
| `MIN_COUNT_TO_NAME` | 2 | a load seen once is not offered | 2–3 |
| `NAMING_MIN_WH` | 50 | energy a load must have used to be worth naming | 10–200 |
| `WHEN_*` | 0.65 / 7 d / 2 d / 3 | when "usually runs mornings" etc. may be said | — |

Two settings were REMOVED from the detection page on 2026-09-23 at Anze's
request; their values are fixed (`DEFAULT_MIN_EVIDENCE`, `MIN_NOISE_W`) and a
stored choice is ignored. What they did, measured before removing them:
"How sure before offering a load" (min evidence) only filters the naming
page, and changes it a lot - at Home 69 of 87 namable loads clear 0.5, 30
clear 0.7, 5 clear 0.9, and the first page differs at each. "Smallest change
to notice" (min_step_w, 10 W) is a floor under each phase's measured noise:
Kozolec measures 3 W on its own, so there the floor IS the threshold, but 1
to 10 W leave its scored loads alone (boiler 338-339); at Home it binds only
now and then (20 W costs 1.8 points of purity).

A menu row's label is cut at the dialog's width, so each row is two lines:
the label (`Signature.menu_row`'s headline) and the option's description,
which wraps - `menu_option_descriptions` in the translations, filled from the
same placeholders. A menu is also the only way to give a page a Back row: a
form's one control is Submit, which is why a load's own page is a menu and
its name box is one step further in.

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

**Sessions filed on one leg of a multi-phase load - diagnosed, partly fixed.**
Home's kiln (2-phase A+C, flat ~48 s pulses, ~9 s apart) produces ~150
single-leg sessions over ten days alongside ~305-337 proper A+C ones; the
grid meter shows 437 real pulses in its two firings. Classified by which of
`_merge_and_file`'s conditions refused each lone leg against its partner:
**ends apart** is
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

Where it stands:

1. **Cross-leg corroboration - built and shipped.** `Detector.process` now
   walks all phases in time order (proven neutral: every bench figure
   identical), so a leg can ask whether a balanced edge on another phase that
   started with it is stopping at the same moment. If so, the stop passes on
   one reading, and the edge the other legs vouched for is the one closed -
   not whichever same-sized edge is newest, which let the Kompresor's stop
   close the hidrofor's session. Counted inside the firings, against the 437
   pulses the grid meter shows there: full-size 353 -> 399, single-leg
   212 -> 158, ladder 23 -> 22. (The old signature-band count read 337 -> 374
   and 154 -> 96; its "96" was mostly two unrelated 3 kW loads, so fewer kiln
   legs are fixed than it suggested.) Kozolec untouched, as a single-phase site must be. Its
   cost - the hidrofor 216 -> 208 held out, Home purity 77.6 -> 75.0 % - is
   EXPLAINED and gone: checked run by run against the pump's own meter, the
   runs that went wrong were levels taken as the median of readings that did
   not agree (a sag, a half-caught switch, the stop), and corroboration only
   reshuffled which runs that hit. `SUSTAIN_AGREE` fixed the cause; the
   hidrofor is at 219 held out. Following the pump's sessions by LABEL had
   been misleading: the energy-matched label lands on anything that overlapped
   a pump run.
2. **Re-measure on 2.4 s data.** Home now polls at 2.4 s, where a 9 s off-gap
   is three or four readings. Expected to help further; will not repair the
   history already recorded, which is why step 1 had to be built.
3. **Split on a coincident drop - built and shipped** (`CORROBORATED_SPLIT`,
   gen 13). Only a partner that has CLOSED may vouch for a split; the
   reading-level test let noisy phases vouch for small loads.
4. Longer term: know which loads RAMP and never ask them for a plateau.

The global matched-stop rule stays off - not for its cost on variable loads,
which is acceptable, but because it does not help the steady ones either
(Kozolec's boiler 499 -> 494, Home's workshop boiler 64 -> 62) and, added to
corroboration, makes the kiln worse (single-leg 80 -> 97): uncorroborated
one-leg stops pull the legs apart again.

Measure each step with `bench.py kiln`: full-size sessions toward the pulse
count (437), single-leg and ladder toward 0 - and purity and concentration at
both sites. After generation 13: full-size 410 of 437, single-leg 112 (41 of
them over 90 s: pulses glued across their off-gaps on one leg, cause 1 above,
which is where the faster polling should tell), ladder 23.

**The pump's sessions run ~4 s short** (60 s runs, timed off its own meter).
The kiln's are right - its pulses are 42.0 s off the grid meter, not the 48 s
this file used to say, and the sessions measure 42.0 - so pairing does not
shorten sessions in general. What remains is the hidrofor's start reading
+7.6 s late, partly the two meters' own clocks. Measure with `bench.py
lengths` before changing pairing.

**Edge congestion has no root cause yet.** A phase accumulates open starts
whose stops are never matched, up to 12 at once. Not caused by the sustain
problem (peak open edges is unchanged when that is fixed).

**A sub-meter's detection now overrides the main meter's - built (gen 14).**
Anze asked for it on 2026-09-22 and it was not built then (see "When Anze
lists several things"): *a detection on a sub-meter should always override
one at a higher level, especially if it is the less noisy one.* As built
(`SUB_OVERRIDE`, `SUB_METER_IDENTITY`): the main meter keeps the TIMING, and a
one-device meter decides which signature a session joins. What it does NOT
yet fix, with production's meters fed in (`bench.py attrib`): Home's NASA
station has 0 of its 100 held-out sessions in a signature placed at its plug,
and 112 of the hidrofor's 361 sit in signatures placed at the main meter -
sessions the house split so differently that the meter's own signature does
not fit them, which the override will not force.

Measured 2026-09-23, before building it (`bench.py` `HOUSE=residual` and
`HOUSE=<circuit>`): the sub-meters are quieter but SLOWER - Home's Shellys
report every 8-11 s against the house's 6 s (2 s since the polling change),
Kozolec's boiler Shelly every 52 s. That decides both halves:
- *Subtracting* them from the house before detecting leaves a phantom pulse
  wherever the two meters see a step at different moments. Home held out:
  the whole house 80.6 % purity; less its sub-meters 64.3 %, with 106 sessions
  still labelled as the hidrofor that had been subtracted (phantoms at its
  switching moments). What is LEFT does gain a little - the workshop boiler,
  under no sub-meter, 34 -> 39 in its main cluster. Anze asked whether putting
  every series on one clock first would help: interpolating the sub-meters
  onto the house's readings gave 64.2 % and 92 phantoms; resampling everything
  to 1 s gave 57.9 % and 409, since the house's own steps become ramps. A
  slower meter never recorded WHEN inside its gap a step happened, and a line
  drawn across the gap only smears it. (Measured with the house PINNED - see
  `bench.py`: a first run let the replay swap two phases for the attic 3EM's
  own channels, and the 22 Sep test ran on a mis-configured house.)
- *Detecting on the circuit* loses what the slower meter cannot see: the kiln
  on the Hiša 3EM gave 236 full-size sessions against 409 on the house.
So the override has to keep the HOUSE meter's timing and take the sub-meter's
identity and power - session by session, matched - rather than hand detection
to the slower meter. Kozolec may be different: its Pro 4PM pushes a relay
switching within a second (reporting power only once a minute in between),
so subtracting THAT meter's steps is untested and could work. Test it
leave-one-out - subtract every meter but the one being scored. Its Shellys'
outbound websocket is already on and connected (checked on the devices,
2026-09-23); nothing on them sets how often power is reported.

**Orphaned starts run for hours.** A start whose stop is taken by another
edge stays open until `MAX_OPEN_S` (24 h) or a size-matching stop turns up.
`SUSTAIN_AGREE` removed one source (194 -> 157 sessions over three hours in
ten days at Home, 14 -> 11 at Kozolec). Expiring by how long a load of that
size is seen to run was built and TESTED (`ORPHAN_MARGIN`) and is off: size
alone cannot tell an orphan from a real long run of a size other loads run
briefly - Home's dryer lost a third of its sessions. A start should be given
up on when the phase shows it is no longer running, not by the clock.

**A sub-meter's phase labels need not be the grid connection's - fixed
(gen 14).** Home's attic 3EM ("Mansarda") calls the grid's C "b" and its A
"c"; the Hiša 3EM's line up. (Hiša is the house CIRCUIT's 3EM - say "under
the grid connection", not "under the house", for what sits at the top.)
`_same_load` demands the same letters, so the attic saw 627 main sessions live
and was credited with 92. `phase_mapping` now learns each channel's phase from
the single-phase sessions it shares with the main meter - a permutation, so
the kiln stepping on two phases cannot tie - and matching uses it: the attic's
credited sessions rose 214 -> 279 on held-out days. It is pure and
self-contained because Load Juggler needs the same answer to be easier to set
up (Anze, 2026-09-23).

**Live ticks cost a little that a backfill does not.** Production reads a
minute at a time. The recorder's start-of-window copy was fed in as a reading
once a minute on every phase, which cost Home 2 points of purity replayed at
one-minute slices; generation 13 drops it (`without_window_start`). What is
left at one-minute slices: Home's hidrofor 219 -> 203 in its main cluster,
from something done per call rather than per reading - not yet found. The
bench replays in 6-hour slices, as the backfill does (`SLICE=`).

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
`combine()` - `bench.py`'s `HOUSE=prod` - so it matches production. Checked
against the live generation-12 rebuild (2026-09-23): replayed from the same
start instant, the bench's kiln signatures came out x338 / x39 against live's
x335 / x38. From midnight instead they came out x319: the library's history
before a load shows up moves signature counts by tens. Compare runs over the
same span only.

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
