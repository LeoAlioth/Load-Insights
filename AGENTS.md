# Working on Load Insights

A Home Assistant integration that finds individual appliances in whole-house
meter readings, names them, and forecasts consumption. Two live sites drive
every decision here: **Home** (3-phase, SolarEdge SE17K + M1 meter, several
Shellys) and **Kozolec** (off-grid, single-phase, Victron MultiPlus II).

## Where things are

| path | what |
|---|---|
| `custom_components/load_insights/insights/detect.py` | the pure detector - edges, sessions, signatures, merging. No Home Assistant imports; this is what the bench runs. |
| `custom_components/load_insights/insights/phases.py` | which of a device's sensors are its per-phase readings, and which phase each of a meter's channels carries (`phase_mapping`). Load Juggler carries a verbatim copy at `custom_components/dynamic_ocpp_evse/phases.py`, synced by hand: change it here, copy it there whole. Stdlib only, no Load Insights imports; `tests/test_phases.py` fails while the two differ. |
| `custom_components/load_insights/insights/classify.py` | what a load might be, from power, duration, factor, name hints (EN + SL) |
| `custom_components/load_insights/detection.py` | the HA runner - reads the recorder, resolves sub-meters from the Energy dashboard, owns `DETECTOR_GENERATION` |
| `custom_components/load_insights/config_flow.py` | setup and the naming pages |
| `custom_components/load_insights/sensor.py` | entities and the diagnostic attributes |
| `tests/replay.py` | run the detector over exported CSVs, no HA |
| `tests/bench.py` | the bench every dial was chosen with: `score`, `kiln`, `surge`, and `HOUSE=prod` to read Home as production does; ground-truth scoring and each site's device-meter list (`SITES`) |
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
| `PhaseState.interval` | its cadence: the 10th percentile of the gaps after a reading that moved (`CADENCE_GAPS`), stored with the phase |
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

**The local history is the last ten days** (Anze, 2026-09-30): the sites'
recorders keep ten days, production's reset re-reads ten, so a longer replay
measures a library production can never hold. `data/history/<site>/` holds
Home 19-28 Sep and Kozolec 20-29 Sep; older CSVs moved to
`data/history/<site>-older/`. Every bench number in this file from before
2026-09-30 15:00 was measured on 20-22 days and is not comparable with later
ones; the ten-day baseline is stated where it was first measured below.

**Ten-day results on `phase-events` (2026-09-30 evening): starts as all-phase
events (EVENT_WINDOW_INTERVALS 3, EVENT_BALANCE 0.2), a device its start
cluster, runs filed on their own phases, an event closed after the window
plus the sustain time:** Home purity 80.9 %, wconc 56.9 % (hidrofor 72 % in
one signature of 62), the mat 42.6 h / 1.8 h outside heating / caught 53 %,
kiln 454 / 53, pump 452 clean / 17 missing of 665; Kozolec 99.9 / 93.7 %,
fridges 68 of 89; energy Home 674 kWh detected, the pump's signature 1,024
runs at 30 % pump (its cluster still holds another ~900 W single-phase load
on A), Susilna 17 % caught (7 before). The compressor is one three-leg
cluster (315 events, 2.5 kW) and the kiln one A+C cluster (490). Against the
ten-day baseline below: purity +5.4, wconc -1.7, kiln single-legs 65 -> 53,
pump 443 -> 452 clean. With the event closed at the window alone (a leg at
the edge not yet declared): 82.5 / 59.7, kiln 455 / 47, pump 444 - within a
point or two, and the wait is right by construction.

**Ten-day baseline (2026-09-30 afternoon, Home 19-28 Sep with the thermostat,
Kozolec 20-29 Sep), steps per phase, filing on the run's own phases:** Home purity 75.5 %, wconc 58.6 %
(hidrofor 71 % in one signature of 42), the mat 43.6 h counted once against
76.4 h of heating, 1.8 h outside it, caught 55 %; kiln 441 full-size / 65
single-leg (272 pulses in 2 firings); pump 443 clean, 69 long, 43 short, 38
multi, 58 wrong size, 14 missing of 665; Kozolec 99.9 / 93.3 %, fridges 75 of 91
lengths within 25 %, purity 87 %; energy Kozolec 73.4 / 74.1 %, Home 41 / 3 %.
The 22-day numbers (86.2 / 86.0) came from a long warm-up production never
gets: on ten days the pump's runs land in a 4,586-run blob on A (17 % pump by
energy) instead of a signature of their own. Before the own-phases rule the
same ten days gave 76.2 / 46.3 and a C-phase signature carrying A runs.

There is a bench. Use it — replaying costs minutes and a live site costs a
deploy cycle plus a ten-day rebuild.

```bash
python3 tests/run_all.py                                  # 16 files, all must pass
python3 tests/bench.py score kozolec FOLDER [DIAL=V ...]  # purity and concentration
python3 tests/bench.py score home FOLDER HOUSE=prod [DIAL=V ...]
python3 tests/bench.py kiln FOLDER HOUSE=prod [DIAL=V ...]  # the unmetered kiln
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
over-merging looks like. `tests/bench.py` holds each site's device-meter
entity list (`SITES`) and the two metrics:

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

### Sub-meters' reactive power in the replay

Production derives a reactive power for every sub-meter from what it
publishes beside its power; the replay handed sub-meters none, so on the
graphs every step of Home's two 3EMs sat at "no power factor" (Anze,
2026-09-30). The export now pulls the 3EMs' per-phase power factor and
apparent power (`shellys-pf` group; the recorder held them from 2026-09-20 only,
earlier days came back empty), and `replay.py` derives
each `--sub-phases` meter's reactive power from the power factor found beside
its power (same pseudo-device) and hands it to the Fleet as `sub_q`. Device
meters fed with `--sub` still get none: their Shellys publish no factor.
The bench now PINS the house power roles to the site's configured entities
(`bench.SITES[site]["main"]`) instead of letting the replay guess: with
the 3EMs' factors in the history the guess took Hiša's power for the house's
and every count halved (caught in the recapture, 2026-09-30).

### The energy score (`tests/energy_bench.py`)

`python3 tests/energy_bench.py home data/history/home SWITCH=... [PLANT=set1]`
scores what a named load's statistics will get right: per device meter, the
watt-hours of its runs that land in signatures the device dominates (>= 50 %
of the signature's energy) against the device's energy above its idle draw
(the level it holds a tenth of the time, by time). Precision = of those
signatures' energy, the device's share; recall = of the device's energy, the
share caught; F0.5 weighs precision (under-reporting is the lesser evil). It
also lists the signatures holding most detected energy and, per device, where
its energy went. `PLANT=watts:on_s:every_s:phase` adds a square-wave load to
the house reading (and the grid meter's power and current) to score a load no
meter watches. 2026-09-30, 20 days: Kozolec new 72.7 / 67.5 % (old 78.0 /
66.6; the boiler 68 -> 82 % precise, the water pump 13 -> 52 % caught, the
pond EVSE 90 -> 75 % precise); Home new 55.8 / 10.1 % (old: no signature
reaches 50 % of any device). The Home number is the blob: one signature with
16,876 runs and ~470 kWh holding 4-9 % of every device's energy.

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
- **The pass slicing is the noise floor.** Re-run a verdict at SLICE=4, 5,
  7 and 8 beside the default 6: on the same code the pump's clean runs move
  482-490 and Home's energy precision 76-88 % (*A meter's cadence*,
  2026-10-02). A difference inside that is not a result.
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

**Periodic or change-driven?** Read off each sensor's own history (the gaps
between recorded rows, the recorder skipping unchanged values; script in the
2026-09-30 session, `sensor_kinds.py`): Home's Shellys report on a fixed beat
(Susilna and the server UPS 60 s, the hidrofor 10 s, the EVBox 10 s), the 3EMs
every 5-16 s on change, the SolarEdge house reading is a template that moves
whenever either input does (median 1.9 s), the workshop boiler every 146 s;
Kozolec's Multiplus 5.2 s (62 % at the beat), the bug lamp and pastir 10 s,
the pond EVSE 10 s, the Shellys 60 s. What it buys: on a periodic sensor a
step's true moment is anywhere in the beat before the reading; on a
change-driven one it is at the reading. The detector's per-phase `interval`
(median gap) already carries the useful part of this for pairing tolerances;
a periodic flag would add the timing-uncertainty shape only, so nothing was
built (Anze asked whether knowing helps, 2026-09-30).
The Shellys' 60 s beat was not the devices' limit: their outbound WebSocket
to Home Assistant was off, so the integration only polled them once a minute.
Enabled on all of Home's Wi-Fi Shellys 2026-09-30 (as at Kozolec before); from
then on they push on change like the 3EMs, and history before that date keeps
the 60 s cadence. Site notes: HA_Configs/home/NOTES.md.

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
| `SUSTAIN_SECONDS` | 5.0 | ...and at least this long | **physical** | — | only until a reading's first moving gap (`CADENCE_GAPS`); was **defective** as the guard itself - below the 6 s sampling interval at both sites, so it never fired |
| ~~`SUSTAIN_INTERVALS`~~ | replaced by `SUSTAIN_CADENCES` (3, 2026-10-01; see *A meter's cadence*) | ...and at least this many measured sample intervals. 1.5 ≈ three readings at 6 s | count | 0–3 | **swept at both sites and the kiln; shipped.** Kozolec wconc 72.2 → 87.3 %, confirmed held out 73.8 → 84.2 %. Home held-out purity 64.6 → 68.3 %. Kiln (no sub-meter) best at 1.5: full-size sessions 272 → 317, spurious ladder 236 → 145; from 2.0 it loses pulses. Costs loads that wander rather than switch (NASA station); `ALIKE_MAD_SHARE` gives most of it back. Made the old surge detector blind — see `_declare_surge` |
| `SUSTAIN_AGREE` | always on (the switch removed 2026-09-30) | a new level is only the pending readings that AGREE with the newest; the ones before are the transition | switch | — | **shipped (gen 13).** The old rule took the median of everything away from the old level, so [1251, 1082, 436] - a sag, a half-caught switch, the stop - read as a 198 W step: it closed a 4-hour-old 179 W start and left the hidrofor's session open 7.8 h. Home purity 76.6 → 78.7 %, held out 75.0 → 78.6 %; hidrofor held out 208 → 213; Kozolec's boiler 499 / 337 → 500 / 339. The TIME away is still counted from the first reading that left - timing it from the agreeing readings alone cost the kiln a third of its pulses (399 → 264), whose off-gaps are two readings after a half-caught one |
| `SUSTAIN_AGREE_TOL` | 1.414 | readings agree within this many `noise_at`s... | count | 1–2 | swept 1.0 / 1.414 / 2.0: Home best at √2 (held out 76.8 / 78.6 / 77.1 %); Kozolec's boiler flat, its Scala2 prefers 1.0. √2 because two readings each carry the noise |
| `SUSTAIN_AGREE_REL` | 0.15 | ...or within this share of the step they are making | ratio | 0–0.25 | swept 0 / 0.1 / 0.15 / 0.25 on the kiln: 0 lost pulses (345 full-size) because phase A's off readings wander 19 W under load; 0.1-0.25 all ~395. 0.15 is the pairing tolerance, and the best for Kozolec |
| `SUSTAIN_AGREE_MAX_INTERVALS` | 12 | after this many intervals of readings that never agree, the old median decides - a wandering load | count | 3–∞ | swept 3 / 6 / 12 / 24 / ∞: flat from 12 up (Kozolec identical 12-∞), so it barely binds; kept as a safety net |
| `STEP_AT_HALFWAY` | always on (the switch removed 2026-09-30) | a step is dated at the first reading more than half-way to the new level, not the first that left the old one | switch | — | **shipped (gen 13).** Kiln single-leg 141 → 118, main signature x335 → x384; hidrofor held out 213 → 220; workshop boiler held out 29 → 38; Kozolec's Scala2 held out 67 → 53 (it ramps). The kiln's LENGTHS were already right (42.0 s against 42.0 s off the grid meter) |
| ~~`INTERVAL_PERCENTILE`~~ / ~~`INTERVAL_GAPS`~~ | 0.5 / 60, replaced by the moving gaps' cadence (2026-10-02; see *A meter's cadence*) | a reading's interval was this percentile of its last 60 gaps - its cadence, not the mean gap between recorded changes | ratio | 0.1–0.5 | **swept; shipped (2026-09-23)**: Home's three phases were 7.1/6.0/6.0 s from the running mean, all 6.0 with the median. Kiln full-size 305 → 337, ladder 130 → 92; held out, Home purity 76.7 → 77.6 %, Kozolec hidrofor 65 → 75. The median still read how often a value CHANGES once a leg held between polls (12-18 s for a 6 s meter changing on a quarter of its polls) |
| `CADENCE_GAPS` / `MOVING_PERCENTILE` | 600 / 0.10 | a reading's interval - its cadence - is this percentile of its last 600 gaps after a reading that moved past the noise; from the first such gap, stored with the phase | budget / ratio | — / 0.05–0.25 | p5 / p10 / p25 read off ten days of every meter (the table in `detect.py`): the 10th is the lowest that skips the Victron's once-a-minute refresh. The ONE gap measure since 2026-10-02 - sustain, silence, span, the event, corroboration and crowding windows, sample counts and the Fleet's tolerances (see *A meter's cadence*) |
| `CORROBORATED_STOP` | 1 reading / 0 s (inline since 2026-10-01) | a stop another leg of the same load vouches for passes on one reading | count | 1–2 | **swept; shipped.** 1 reading beats 2 (old metric: single-leg 80 vs 136 with the loose partner test). Off → on, inside the firings: full-size 353 → 399 of 437 pulses, single-leg 212 → 158, ladder 23 → 22. See the single-leg entry for the trade |
| `CORROBORATE_INTERVALS` | **1.0** | how close in time a partner leg must start and stop, in sample intervals (cadences since 2026-10-02: ~1.1 s at Home's grid by day, the median gap's 2.0 before) | count | 1–2.5 | **swept**: the merge test's 15 s let unrelated loads vouch for each other; 1.0 kept the most of the hidrofor (208 vs 196 loose). In cadences, one trial each on 66f190a (before the shortfall fix): 1.0 / 1.5 / 1.8 / 2.0 / 2.5 / 3.0 - kiln full-size 470 / 468 / 476 / 472 / 478 / 472, pump clean 482 / 486 / 482 / 489 / 492 / 484, inside the slicing noise; left at 1 |
| `CORROBORATE_BALANCE` | 0.7 | how near in power a partner leg must be | ratio | 0.7–0.85 | swept 0.7 and 0.85; 0.85 lost more single-leg than it saved |
| `CORROBORATED_CLOSES_ITS_EDGE` | always on (the switch removed 2026-09-30) | close the edge the partners vouched for, not the newest of that size | switch | — | kiln ladder 114 → 98, pulse length back toward the true 42 s; did not recover the hidrofor |
| `CORROBORATED_SPLIT` | always on (the switch removed 2026-09-30) | a leg whose start was bigger than the drop its closed partner vouches for is split: that part closes, the rest stays open as the coincident load | switch | — | **shipped (gen 13).** With the reading-level partner test it fired 164 times in ten days, 11 on the kiln, and cost Home 1.9 points held out; with only CLOSED partners: Home 77.6 → 78.6 %, held out 79.7 → 80.6 %, kiln main signature x385 → x397, single-leg 116 → 112, ladder 18 → 23. Kozolec identical || `BASELINE_EMA` | 0.02 | how fast the idle floor follows drift while nothing runs | ratio | 0.005–0.1 | not tested |
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
| `MATCH_POWER_REL` | 0.10 | power tolerance for a session to match a name's description after a reset (`_reclaim`); filing no longer matches by power | ratio | 0.05–0.25 | not tested |
| `ALIKE_MAD_SHARE` | **0.10** | share of two signatures' spread that may widen a name's return after a reset (`alike` now serves only that) | ratio | 0–0.20 | swept for merging, which is gone; kept for name recovery |
| `MATCH_DURATION_FACTOR` | 3.0 | duration tolerance for a load that keeps time | ratio | 1.5–6 | not tested |
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
| `SUB_OVERRIDE` | always on (the switch removed 2026-09-30) | house sessions wait to be filed until every sub-meter fast enough to have seen them (two readings inside the run) has reported past their end; a partner then decides the signature | switch | — | **shipped (gen 14)** with the two below: Kozolec's Scala2 128 → 195 in its main signature, held out 53 → 61, purity unchanged; Home 78.6 / 80.6 → 79.1 / 80.7 %; kiln main signature x397 → x392 |
| `SUB_METER_IDENTITY` / `SUB_DEVICE_SHARE` | always on (the switch removed 2026-09-30) / 0.5 | a session whose ENERGY a one-device meter accounts for joins the house signature most of that meter's sessions went to, when it fits; one device = its own library has ≥ this share in one signature | switch / ratio | 0.4–0.8 | almost all of the gain above; share not swept (hidrofor plug 99 %, Hiša 55 %) |
| `SAG_CLOSE` | always on (the switch removed 2026-10-01) | a load running alone is followed as it sags, and a stop may close it at that size as well as at its start's | switch | — | **benched, shipped (2026-09-28).** Kozolec's fridges (the new `fridge` bench, 252 runs off the house reading): caught 125 → 216, right length (±25 %) 71 → 153; Kozolec purity 98.2 → 98.3 %, wconc 92.4 → 92.6 %; Home purity 77.4 → 77.6 %, wconc 55.5 → 54.9 % (Hidrofor 717 → 712 of ~1200); kiln full-size 410 → 412, single-leg 112 → 104, top signature x397 → x387; pump clean 831 → 830 |
| `METER_PHASES_MIN` | 20 | a one-device meter takes only house sessions on the phases it has been credited this many sightings of; fewer, and it takes anything | count | 10–50 | **benched, no change**: on the history to 22 Sep, on or off gave identical placements and scores at both sites (Home purity 77.4 %, wconc 55.5 %; one Blaževa Soba credit 56 → 55). The misplacements it stops - Hidrofor credited an A+B and a B load - came on 25 Sep, after that history; not swept |
| `SWITCH_END_SHARE` | 0.25 | a switch's stop may miss the session's by this share of the run | share | — | set from the mat's edges, not swept: start 3-9 s after the heating (5-95 %), stop within 25 % for 1243 of 1335 |
| `SWITCH_MEMORY_S` | 4 h | how long a switch's on-periods and a number's readings are kept, counted from the oldest reading a pass brings | s | — | **a fix, benched**: counted from the pass's END, a six-hour backfill slice had lost its first two hours' switch-ons before filing them - 462 of 1232 fitting mat runs uncredited, and the mat's signature short of the half that places it (762 of 1738). Counted from its start: every one credited |
| `DRIVER_MIN_RUNS` / `DRIVER_MIN_R2` | 30 / 0.25 | a number is believed about a load's run length (or gap) once the load has this many runs and the number explains this share of their spread; then only the unexplained spread counts against its tightness | count / r² | — | **benched, no change**: Kozolec 8-27 Sep with `sensor.apartma_t_h_temperature` and `sensor.weather_station_temperature` - the fridges' signatures (#1 x307, #3 x227, each holding runs of both fridges) learn r² 0.00-0.05, nowhere near believed; fridge bench and score identical (purity 98.3 %, wconc 92.6 %). Only the synthetic test shows it working (+4 %/°C, r² 1.00). Not swept |
| `INPUT_MIN_RUNS` / `INPUT_MIN_SHARE` / `INPUT_MIN_LIFT` | 8 / 0.6 / 3 | a load runs IN one value of a setting (a washer's cycle phase, a fan's speed) once it has this many runs (time-weighted), this share of them in the value, and that share this many times the value's share of the time | runs / share / × | — | **benched, no change to the scores** (2026-09-28): Home 18-27 Sep with the Sock Eater's cycle phase, appliance state and sub-phase and the RF ceiling fan's speed - purity 71.7 %, wconc 31.1 % fed or not. Two loads tied: #413 5.8 kW A+B+C 6 s x16 (62 % of runs in Wash, 8.1× chance) and #578 127 W A+C 38 s x194 (73 % in Running, 4.1×). The washer's own parts - probably #274 1.8 kW on A (the heater), #7 48 W and #201 126 W on A (the inverter drum) - run in Wash 3-4× chance but share their signatures with look-alikes, so none reaches the share. Weighted by sighting instead of time, a 49 W load starting 285 times a day remembered eight hours - one wash - and read as tied (89 %, 4.7×): hence `INPUT_TIME_TAU_S` |
| `INPUT_TIME_TAU_S` | 14 d | how long a value's share of the time, and a load's runs in it, are remembered | s | — | see above; not swept |
| `INPUT_EVIDENCE` | **1** | a load tied to a value of a setting counts as tight about time (evidence's duration term raised to its share) | switch | — | **benched, scores unchanged; the naming page is not**: scores identical on or off; evidence of the two tied loads 0.72 → 0.77 and 0.50 → 0.65, none newly over the naming bar (2026-09-28). On ten days with the same four inputs (2026-10-01): scores identical again (Home SUBS=prod 77.4 / 49.1 %, the mat, kiln and pump too), but eight loads tied, four of the washer's clear the bar only with it - #1542 148 W in Wash 0.54 → 0.70, #1392 25 W and #1960 22 W in Dry 0.50 → 0.70, #150 294 W in Dry 0.62 → 0.77 - and Mansarda's list clears 7 instead of 5 |
| `INPUT_SPLIT` (`INPUT_RARE_SHARE` 0.25, `INPUT_SPLIT_MIN_TIME_S` 1 d, `INPUT_SPLIT_LIFT` 3, `INPUT_MIN_EPISODES` 3, `INPUT_MIN_COVERAGE` 0.5) | always on (the switch removed 2026-09-30) | a run made while a setting holds a rare value joins only signatures born in it; one born by chance goes back into an alike twin (pair lift < 3) or turns ordinary once it has run in under half of the value's episodes since its birth (at least 3) | switch | — | **benched, shipped (2026-09-29).** Home 18-27 Sep with the Sock Eater's phase, state and sub-phase: purity 63.8 → 65.4 %, wconc 29.8 → 30.5 % (office plug 19 → 16 %); kiln and pump benches identical (no washer there). Judged by runs alone, the kiln firing through one afternoon's washing split into Wash/Rinse/Spin/Dry signatures (a burst read as 7× chance) and the floor mat grew a Delayed Start one; by episodes they go back. Tied now: 119 W A+C in Rinse, 61 W C in Dry, 98 W B in End Of Cycle. The heater (~2 kW on A, 2-60 min, in wash AND dry) scatters into one- and two-run signatures and does not tie on 13 washes |
| ~~`HOLD_MIN_INTERVAL_S` / `HOLD_SUSTAIN_S`~~ | 20 s / 30 s, replaced by `SUSTAIN_CADENCES` (2026-10-01: one rule for every meter; see *A meter's cadence*) | on a meter this slow in a steady state (a Shelly heartbeating once a minute, a Zigbee plug reporting on change), a pending change counts as having held until the next reading, and holding 30 s is enough | s | — | **benched, shipped (2026-09-29).** Kozolec's IR panel meter alone: 13 runs of 164 min → 46 of 48 min (its readings: 52 runs, mean 40 min, off 2.4 min between); the house's ~500 W signature credited to the panel 8 → 27 of 63; the Boiler meter's library 132 runs of 36 min at 937 W (pulses merged) → 1467 of 3 min at 1906 W (its element: 1546 pulses of ~1 min at 1.9 kW). Kozolec purity 98.3 %, wconc 93.9 → 94.0 %; Home 63.8/29.8 → 63.9/29.6 % (production meters 64.1/29.1 → 64.1/29.2 %); kiln, pump, fridges identical. The Shelly needs no change: it reports each switch within 1-4 s |
| `HOURLY_KEEP_S` | 11 d | how far back each signature keeps its energy by clock hour, for backfilling a newly named load's statistics | s | — | not a detector dial; sized to the 10-day backfill |
| `EDGE_LAG_REACH_S` / `EDGE_LAG_BIN_S` / `EDGE_LAG_MIN` / `EDGE_WINDOW_DEFAULT_S` | 60 s / 2 s / 50 / 10 s | how far either side of an edge an input's change is looked for, the lag histogram's bins, the changes seen before a lag is believed (`lag_window`: a peak 4x the even spread), and the window before that | s | — | not swept; Home's thermostat learned (-11, 0) s from 17k edges, the mat's edges at -5.8 +- 1.7 s |
| `EDGE_LIBRARY` | 150 | edge clusters kept per phase and direction | count | — | not swept; Home used ~400 in all |
| `EDGE_NOISE_SHARE` / `EDGE_SCALE_REL` | **0.25** / 0.02 | one measurement error on the edge-size scale: this share of the phase's measured noise at small steps, this share of the step at large | ratio | 0.25–1 | **swept 0.25 / 0.5 / 1 (2026-09-30)**: Home 82.6/66.4 vs 80.9/61.3 vs 76.3/50.2, pump 1153 / 915 / 410, Kozolec fridges 188 / 154 / 81. A fixed 15 W ran every small fall at Kozolec into one cluster |
| `EDGE_KERNEL` / `EDGE_BIN` / `EDGE_TAU_S` / `EDGE_RECUT` | 1.0 / 0.25 / 10 d / 32 | smoothing of the size histogram (in measurement errors), its bin, how fast it fades, how many steps before its valleys are cut again | count | — | offline: 1 error wins among 1, 1.5, 2, 3 on physics grounds; the score always prefers wider (only 15 % labelled) |
| `EDGE_ANGLE` (`EDGE_ANGLE_BIN` 3°, `EDGE_ANGLE_KERNEL` 6°) | **off** | the step's reactive angle, atan2(dQ, dP), as a second clustering dimension: within a size segment, steps are cut again at the valleys of their angle density, so a pump (~35°) and a heater (0°) of one size are two kinds of edge | switch | — | **not yet measurable**: only a SIGNED var gives the angle, and Home's grid meter signs it only from 2026-09-30 07:57 - ten days of it exist from about 2026-10-10 (Kozolec's Victron and the 3EMs are unsigned). Synthetic test only (900 W at 0 and at 630 var: one cluster off, two on). Bench it on Home's ten days then, against the purity, the pump (its cluster shares A with the compressor's leg) and the kiln, whose legs read 0.866 with opposite-signed var |
| `PAIR_MIN_RUNS` / `ABOVE_CHANCE_ODDS` | 8 / 100 | a pair is accepted once it has this many runs and a Chernoff bound puts the odds of its count by chance under 1 in `ABOVE_CHANCE_ODDS` | count | — | **replaced shares of 30 % and 20 % (2026-09-30)**: mat hours outside heating 21.7 -> 11.5; odds 10, 100 and 1000 identical. `LINK_MIN` and the device union-find went with the all-phase events (a device is its start cluster) |
| `EVENT_WINDOW_INTERVALS` / `EVENT_BALANCE` | 3 / 0.2 | rises on different phases within this many of the slowest phase's reading intervals, the smallest at least this share of the largest, are ONE start event, clustered on their phase pattern and total size | count / ratio | 2–4 / 0.2–0.5 | measured on Home's ten days: at 2 intervals the compressor's three-leg event never formed, at 3 it did (70 steps, a cluster of 28), at 4 little more; the pump gets a false companion within 5 s 1 % of the time; 0.2 / 0.5 / 0.9 balance alike offline. In cadences since 2026-10-02 (3.3 s at Home by day, 6 s on the median): 5.5 - about the old seconds - on 66f190a cost purity 78.2 → 76.0 %, the kiln and pump unchanged |

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
| `PV_MIN_SAMPLES` | 30 | evidence needed before deciding whether a reading sees the sun | count | — | not tested |
| `EXPORT_FLOOR_W`, `EXPORT_SHARE` | 50 W, 0.005 | how much export marks a reading as carrying generation | physical/ratio | — | not tested |
| `GENERATION_MIN_SAMPLES` | 200 (= 1 / `EXPORT_SHARE`) | fewest readings `carries_generation` judges; fewer answer None, and a None keeps the last verdict (`floor_zero` is saved with the phase) | derived | — | **shipped 2026-09-25.** A one-minute live pass (~25 readings) judged alone read one dip as an export and switched `floor_zero` off every pass; one cloud edge then left Home's phase B floor at -1483 W, never corrected because the phase was never idle again. The bench did not show it: `replay.py` judges the whole series once |
| `SOURCE_IDLE_W` | 25 W | below this a reading carries nothing - a generator's idle AC input (`carries_load`, with `LIVE_SHARE`). `SOURCE_IDLE_SHARE` (0.9) went with `classify_source`, 2026-10-01 | physical | — | not tested |
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

1. push to `dev` — `.gitea/workflows/GiteaRelease.yml` tags a pre-release
   build (its `PRE` flag: a push to `dev` or `dev-*`)
2. both sites install it themselves within a minute or two
3. restart each (`POST /api/services/homeassistant/restart`), wait for `RUNNING`
4. a generation bump rebuilds by itself; otherwise call
   `load_insights.reset_detection` to re-run the 10-day backfill
   (`forget_names: true` drops the carried names too - a full reset; the
   named loads' registry entities are then removed by hand, and naming a
   load again recreates the same entity ids, so its statistics carry on)
5. wait for `caught_up`, then read the numbers back off the sensor
   (a named load's sensors appear as its name re-attaches during the re-read;
   before the follow-up to build 1613 they stayed unavailable until a reload
   of the integration - `POST /api/config/config_entries/entry/<id>/reload`)

The detected-loads sensor publishes what the gates measured — `resolution_w`,
`pf_floor_w`, `noise_floor_w`, `baseline_w` — so a gate can be checked against
a site instead of assumed. Add to that list rather than debugging blind.

Every attribute assembled per update must be in `_unrecorded_attributes`;
`tests/test_sensor_attributes.py` fails otherwise. The library runs to well
over 16 KB and the recorder will choke on it.

**Sub-meter reactive power (2026-09-30 night, Home's ten days):** a 3EM
publishes no reactive power and an UNSIGNED power factor (RPC and Modbus
alike, fw 2.0.1). Its reactive part is now sqrt(S^2 - P^2) from its apparent
power, each reading paired with the power reading of the SAME update
(SAME_UPDATE_S): read "as of", a kiln leg's switch-off paired 34 W with the
3505 VA before it. Checked on the kiln's legs at Hiša, which must read 0.866
(phase to phase): within 0.03 of it, A / C / A+C, 62 / 43 / 36 % before, 99 /
63 / 90 % with the pairing on the power factor, 98 / 86 / 98 % on the apparent
power; the step-weighted IQR of power factor inside a group Hiša 0.126 ->
0.022, Mansarda 0.244 -> 0.110. The house scores do not move (purity 79.4 %,
wconc 55.8 % either way, SUBS=prod): the house's grouping never reads a
sub-meter's reactive power. Ruled out: signing the size by the grid meter's
signed change (choose +size or -size by the nearer change) - its noise flips
small levels, 98 / 86 / 98 -> 94 / 39 / 82 %, spread 0.022 -> 0.198. The same
pairing on the grid meter's own signed var changed nothing.

**Ruled out 2026-09-30 night: a sub-meter's steps as evidence in the house's
clustering** (SUB_KEYS - each meter's steps a "meter:<name>" input, keying
house steps it saw at the same moment the way a thermostat's change does).
Home purity 79.4 -> 81.0 %, wconc 55.8 -> 57.6, kiln 454 -> 462 full-size, pump
451 -> 453 clean, the mat caught 53 -> 49 %; energy F0.5 30.4 -> 28.1 (precision
74.2 -> 80.3, recall 9.0 -> 7.8). Kozolec energy F0.5 72.7 -> 66.7, recall 73.8
-> 46.3: the Pond EVSE's rises and falls landed in differently keyed clusters,
its runs stopped closing, and one run of 10.5 kW for 289 min took 50 kWh.
Voltage-normalised sizes were measured earlier in the cluster lab: purity
0.860 / conc 0.760 against 0.861 / 0.766 for plain size (h 0.015).

**Experiments branch, 2026-10-01 (not on dev): measured on the corrected
benches** - energy: a run is a device's only when its meter switched on with
it, credited with the meter's draw above its idle floor; labels: against the
meter's draw just before the run (the old "minutes before" average read a
charge that followed another as no one's). Ten days, SUBS=prod:

| version | Home pur/wconc | mat over | Home F0.5 / wrong kWh | Koz wconc | Koz F0.5 / wrong |
|---|---|---|---|---|---|
| baseline (dev) | 75.4 / 48.1 | 1.8 h | 38.4 % / 8.0 | 89.9 | 80.9 % / 22.4 |
| split + close, noise as before | 78.3 / 52.1 | 1.8 h | 34.2 % / 8.1 | 89.6 | 81.3 % / 23.6 |
| + noise from moves, factor 4 | 72.0 / 44.4 | 5.9 h | 38.2 % / 7.2 | 95.0 | 82.9 % / 18.0 |
| ... factor 5 | 74.4 / 48.9 | 2.8 h | 36.9 % / 7.4 | 94.6 | 81.3 % / 21.9 |
| ... factor 3 | 71.0 / 38.5 | 5.4 h | 41.7 % / 10.2 | 97.1 | 82.4 % / 16.9 |
| ... factor 3 + EDGE_BY_METER | 70.8 / 35.9 | 3.9 h | 47.1 % / 5.7 | 97.4 | 83.8 % / 17.6 |
| ... + an unplaced step joins the busier located cluster (deployed, generation 17) | 71.4 / 36.0 | 3.9 h | 41.0 % / 3.6 | 97.4 | 82.6 % / 18.9 |
| hard, starts only (a; experiments default) | 70.5 / 35.3 | 6.8 h | 45.3 % / 3.0 | 97.3 | 82.7 % / 18.9 |
| soft, starts only | 70.9 / 37.6 (228 sigs) | 3.3 h | 43.9 % / 5.9 | 97.1 | 83.1 % / 16.7 |
| soft, all steps | 70.8 / 37.2 | 3.7 h | 40.9 % / 8.5 | 97.1 | 82.1 % / 17.8 |
| a device spans a plain and a placed cluster | 69.9 / 38.6 | 3.9 h | 40.4 % / 6.5 | 97.4 | 80.7 % / 25.2 |
| placement size tolerance 30 % | 69.9 / 37.7 | 20.6 h | 41.4 % / 3.8 | 97.5 | 82.6 % / 18.9 |
| meter window 3 x its interval | 69.2 / 37.6 | 5.9 h | 45.2 % / 11.4 | 97.1 | 78.6 % / 16.5 |
| meter window 2 x its cadence, placement only | 70.2 / 40.1 | 13.1 h | 43.2 % / 9.7 | 97.2 | 82.7 % / 18.9 |
| (a) with energy gated at quality 0.3 / 0.5 | - | - | 36.8 % / 2.0, 33.7 % / 1.7 | - | 82.3 % / 18.6, 81.7 % / 17.9 |
| unified: meters' own declared steps, same direction (hard) | 72.0 / 39.9 | 5.4 h | 46.1 % / 11.5 | 97.5 | 83.2 % / 17.3 |
| + meter vs grid over the meter's span (hard; kiln ladder 63 -> 27) | 70.0 / 38.8 | 4.7 h | 41.2 % / 5.7 | 97.4 | 83.3 % / 17.3 |
| + symmetric union with chaining, overlap as timing (hard) | 70.3 / 37.6 | 5.5 h | 45.1 % / 9.3 | 97.7 | 81.2 % / 19.3 |
| + span from one cadence before the first new reading, hard, gate 0.3 | 70.1 / 37.4 | 4.9 h | 38.6 % / 7.4 | 97.7 | 82.8 % / 16.7 |
| ... soft, gate 0.3 | 70.5 / 36.0 (233 sigs) | 3.3 h | 34.5 % / 5.2 | 98.1 | 83.2 % / 16.7 |
| + a reading followed by silence held, on any meter (hold fix; hard, gate 0.3 from here) | 69.9 / 39.5 | 5.8 h | 34.9 % / 6.6 | 97.6 | 81.6 % / 19.9 |
| level confirmed over time, p2 of 60 gaps, 2 / 3 cadences | 72.9 / 43.9, 76.4 / 48.5 | 14.1 h, 6.9 h | 36.3 % / 2.8, 36.9 % / 8.2 | 94.9, 95.7 | 66.2 % / 25.3, 57.0 % / 45.2 |
| confirmed over 1.5 / 2 periods, silence by the cadence | 70.0 / 38.2, 71.3 / 47.3 | 5.3 h, 25.0 h | 35.3 % / 4.0, 24.0 % / 1.7 | 96.0, 97.6 | 79.4 % / 21.5, 65.6 % / 1.5 |
| one cadence rule, p5 of 60 gaps, 2 / 3 cadences | 73.5 / 41.5, 72.6 / 43.9 | 2.2 h, 2.2 h | 34.7 % / 10.0, 37.6 % / 9.2 | 92.6, 96.7 | 53.8 % / 40.2, 80.3 % / 23.7 |
| ... p5 of 600 gaps, 2 / 3 cadences | 73.4 / 44.1, 76.1 / 48.4 | 2.4 h, 7.8 h | 34.2 % / 9.6, 37.1 % / 12.0 | 97.3, 97.2 | 69.1 % / 25.7, 82.9 % / 18.9 |
| moving-gap cadence (p10), 2 cadences | 73.1 / 42.3 | 2.5 h | 21.5 % / 1.5 | 97.6 | 83.1 % / 16.2 |
| ... 3 cadences | 75.3 / 48.2 | 4.2 h | 42.5 % / 7.8 | 98.1 | 84.2 % / 17.1 |
| + union capped by the slower meter's reach | 75.3 / 49.6 | 2.0 h | 42.6 % / 8.0 | 98.1 | 84.2 % / 17.1 |
| + a meter that held its value is no unplaced step's place (deployed, generation 18) | 73.6 / 47.0 | 2.3 h | 44.0 % / 2.8 | 98.1 | 84.3 % / 18.1 |
| ... the same ungated, as production runs; generation 17 ungated was 41.0 % / 3.6 and 82.6 % / 18.9 | - | - | 48.0 % / 3.7 | - | 84.6 % / 17.5 |

The symmetric versions file the pump's runs at the wrong size 91-95 times
(53-59 before) - open, not the span start. The hold fix took it to 47, the
moving-gap cadence at three to 43-44 (505-506 clean of 665); the kiln's ladder
from 41 to 30, and 24 with the union capped by the slower meter.

**A meter's cadence (2026-10-01).** One number sets how long a new level
must hold, how long a silence means the value held, and how far back a
change's span can start: SUSTAIN_CADENCES (3) of the meter's cadence.
- **Measured from all gaps, a low percentile misreads a polled meter.**
  Kozolec's Victron is polled every 5.3 s, and once a minute its integration
  refreshes every entity anywhere in the poll. While the EVSE charged, the
  5th percentile read 2.3 s, even over 600 gaps. A normal poll then counted as
  silence, a held stand-in confirmed the boiler's half-caught start, and its
  stop closed the EVSE's charge (27.09 11:37).
- **A high percentile misreads a heartbeat meter.** The IR panel and Susilna
  write every 60 s but report a change within 5-12 s.
- **The gaps after a reading that moved past the noise read both** - how
  often the meter writes while its value changes. At their 10th percentile:
  Victron 5.1 s, grid meter (Home) 1.1 s in the detector (1.6 for the meter
  alone), hidrofor 9.9, EVSE 10.1, IR panel 5.0, Hisa 4.8-6.0, Mansarda
  4.1-4.8. The 10th is the lowest that skips the Victron's refresh: at most
  9.3 % of its post-move gaps in any 600.
- **One update is readings under 0.5 s apart** (SAME_UPDATE_S). Pairs are
  all under 0.2 s - Home's grid meter and inverter, a 3EM's power and
  apparent power. Real readings come from 0.8 s: the grid meter 3,064 times
  0.8-1.0 s apart in ten days, which the old 1 s limit dropped.
- **The 3EMs write a phase no faster than about every 4 s**, as recorded:
  Hisa A 49 gaps of 1-2 s against 6,864 of 4-5 s. Hisa's 8-16 s writes
  merged the floor mat into the kiln's start (one reading at the mat's level).
- **Confirming over the period (the median gap) instead** held Hisa's levels
  30 s, its writes being 15 s apart when little moves.
- **One gap measure (2026-10-02).** The interval IS the cadence: every
  caller of the median of the last 60 gaps (`INTERVAL_PERCENTILE`) and of
  `reading_cadence` (the 5th percentile of all gaps, until ten moving gaps
  were known) reads the moving gaps' 10th percentile, taken from the first
  moving gap (the smallest until there are ten) and stored with the phase.
  The once-a-minute heartbeat meter (`test_a_slow_meters_silence_is_a_held_value`)
  keeps 18 of 20 cycles from cold and 20 of 20 after a restart, as with
  `reading_cadence` (14 / 16 with the moving gaps neither used from the
  first nor stored). The interval test pinned an artefact: its quiet leg
  skipped exactly a third of an alternating value's readings, so the median
  read 6 s while every gap after a move was 12; rewritten as the recorder
  behaves - the value changes on a quarter of the polls and holds between -
  the median reads 12-18 s and the cadence 6.0.
- **Why the pump lost runs under it** (the previous phase's lead: 490 -> 482
  clean, missing 25 -> 31). Run by run against its meter: 10 of the 18 runs
  lost were closed by `_unseen_stop` 5-11 s after their start, at their full
  size - "missing" (an 11 s run is a blip) or "short". Each time the phase
  already carried a shortfall nothing fitted - an older run followed while
  alone up to everything above the floor (a 407 W start followed to 1,348 W)
  - and the pump, the next run of about its size, was taken for the one that
  stopped unseen at its first settling drop. The gap estimate there 1.06 s
  against the median's 1.94, the sustain identical (it already ran on the
  cadence). Not caused by the estimator: `_unseen_stop` closes the same 680
  runs at Home either way, as young; the estimator only reshuffled which pump
  runs met the trap. Fixed where it is: a run is fitted only to what fell
  short SINCE it started (`_Open.short0`, the shortfall standing when it
  began, not stored). Kozolec's pond EVSE was its other victim (energy recall
  76 -> 95 %). Counting each run at min(followed, start) instead fixed the
  pump too but lost a ramping load's real growth (Kozolec's Scala2 signature
  took 9 kWh not its own) - checkpoint d067ce6, not kept.
- **Benched as a 2 x 2, each the mean of five pass slicings (SLICE 4-8)**:

  | | old estimators | one estimator | old + shortfall fix | shipped (both) |
  |---|---|---|---|---|
  | Home + thermostat pur / wconc | 78.1 / 48.1 | 78.5 / 49.4 | 77.1 / 47.7 | 79.1 / 50.1 |
  | Home SUBS=prod pur / wconc | 75.2 / 46.0 | 76.6 / 47.1 | 76.3 / 46.8 | 77.3 / 47.8 |
  | kiln full / ladder / single | 472.6 / 13.6 / 7.2 | 469.0 / 12.6 / 8.2 | 474.0 / 14.0 / 4.0 | 472.6 / 12.2 / 3.4 |
  | pump clean / missing (of 665) | 486.0 / 26.8 | 483.8 / 30.0 | 491.2 / 27.2 | 492.2 / 27.8 |
  | mat h / over | 48.7 / 2.1 | 49.5 / 2.1 | 48.9 / 2.1 | 49.4 / 2.1 |
  | Kozolec pur / wconc | 99.2 / 93.4 | 99.1 / 95.4 | 99.1 / 93.3 | 99.2 / 95.6 |
  | fridges within 25 % / purity | 64.8 / 87 | 64.8 / 87 | 65.0 / 86 | 65.0 / 86 |
  | energy Home P / R / F0.5 | 81.3 / 27.8 / 58.6 | 87.3 / 27.4 / 60.6 | 82.9 / 27.6 / 59.1 | 81.9 / 28.4 / 59.4 |
  | ... pooled P / wrong kWh | 81.5 / 10.1 | 85.8 / 7.2 | 79.7 / 11.2 | 82.8 / 9.3 |
  | energy Kozolec P / R / F0.5 | 94.6 / 81.3 / 91.6 | 94.3 / 81.3 / 91.4 | 95.4 / 93.8 / 95.1 | 95.3 / 93.6 / 95.0 |

  The estimator alone is the precision win (Home's pump signature 6.2 ->
  3.2 kWh not its own) and cost the kiln 3.6 pulses and the pump 3 more
  missing; the shortfall fix alone costs Home a point of purity and 1.8 of
  pooled energy precision for 5 pump runs, 3 kiln single-legs and Kozolec's
  charger; together every precision figure is at or above the old one but
  the fridges' A/B purity (87 -> 86 %, one run: the two fridges Anze would
  rather see as one). What the fix gives back of the estimator's Home
  precision (85.8 -> 82.8 pooled) is the trade still open: the pump
  signature's extra wrong energy is a handful of 25-67 min runs starting
  within a minute of the pump. The one traced (09-26 05:31, 613 Wh) is a
  phantom: a multi-close took a phase's last open runs while it still read
  700 W, the level fell back to the floor, and the 700 W came back as a new
  start - a fault older than either change.
- **The pass slicing is the bench's noise**: SLICE 4 / 5 / 6 / 7 / 8 on the
  old code moved the pump's clean runs 482-490, the kiln's full-size 469-475,
  Home's purity 77.1-78.6, Kozolec's wconc 89.8-95.8 (6 and 7 are the low
  ones: the Scala2's "69 -> 121" was mostly that) and Home's energy precision
  76-88 % (its headline weighs Susilna's 1 %-recall signatures by her 63 kWh;
  read the pooled precision beside it). PYTHONHASHSEED changes nothing.

Unplaced steps are a SIZE disagreement between the meter's step and the
grid's, not timing: Hisa 36 of 168, Mansarda 44 of 186 inside the window
(2026-10-01). Wider windows place and split wrongly. The quality score
(step: size over noise x settling x not crowded; run: lesser end x size
agreement) gates Home's wrong energy down; Kozolec's wrong energy sits in
runs the detector is sure of.

SPLIT_BY_METERS books a house step its meters stepped with as their shares
and the rest; TOO_BIG (`_unseen_stop`) closes a run bigger than the whole
reading, on its start size (its followed size carried held drops and booked a 3,346 W charge
at 4,448 W for 410 min). NOISE_FROM_MOVES re-learns noise from moves: a 65 W
load cycling on Hisa's phase C (25-50 s) had held the house's floor at 228 W.
EDGE_BY_METER groups a single-phase step under the innermost meter that saw
all of it; it splits devices whose meter misses steps (Home 279 signatures,
270 with the busier-cluster rule - the fragmentation is NOT solved: Home's
wconc 36 against the baseline's 48).

## Known shortfalls

Open defects, with what is measured and what is guessed. Numbers are from the
two sites' ten-day exports unless stated.

### Confirmed, unfixed

- **Signature churn with a device per start cluster** (2026-09-30, the
  all-phase events): Home makes 1,835 signatures in ten days and keeps 237 -
  the 200 cap evicts unnamed ones and their cluster's next run makes a new one.
  Named loads are never evicted, so the statistics are safe; what suffers is
  the library's memory of unnamed loads and the store's size. A per-cluster
  cap tied to the edge library (EDGE_LIBRARY) or signatures born only from
  established clusters are the candidates; the recurrence gate at 8 steps
  cost Home too much (see "ruled out").

- **Home's pump and the compressor's A leg share one size cluster, and the
  legs rule chains them into one device** (traced 2026-09-30, ten days,
  `dbg_blob.py`). Cluster #2 on A (911 W, 1,193 rises) holds the hidrofor's
  starts (470 labelled, alone on their phase 94 % of the time) AND the
  three-phase Kompresor's A leg (the unlabelled 710: 53 % come with a B and a
  C rise within 3 s, median legs 831 / 848 / 834 W - the notes say ~840 W per
  leg). The compressor's legs link #2 to #20 (B 859 W) and #3 (C 697 W): 291
  and 286 co-starts against 117 and 192 by chance, so the chance test joins
  them, and device filing then pools the pump's runs with the compressor's.
  Not PV: the chained clusters' steps come with SMALLER inverter swings than
  a random daytime step. A share rule on the links cannot fix it either: the
  pump dilutes the A leg's share to 24 %, so the compressor would lose its
  legs instead. Per phase the two starts are the same size and both are
  motors; as EVENTS they are (900, 0, 0) and (840, 850, 830) and trivially
  apart - the case for clustering joined all-phase events rather than
  per-phase steps (see the vector study under "ruled out": even on the
  scores, the kiln A+C on 99 % of its events).

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
edge needs less sustain; removed 2026-09-30) - kiln full-size
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

Measured 2026-09-23, before building it (the bench's `HOUSE=residual` and
`HOUSE=<circuit>`, removed 2026-10-01 once this was settled): the sub-meters
are quieter but SLOWER - Home's Shellys report every 8-11 s against the
house's 6 s (2 s since the polling change), Kozolec's boiler Shelly every 52 s.
That decides both halves:
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
  drawn across the gap only smears it. (Measured with the house pinned to
  the built series: a first run let the replay swap two phases for the attic
  3EM's own channels, and the 22 Sep test ran on a mis-configured house.)
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
size is seen to run was built and TESTED (`ORPHAN_MARGIN`, since removed): size
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
surge ratio repeats, a coincidence's is random. The counters
(`inrush_seen`, `inrush_when_seen`) were never read and went in the
2026-10-01 cleanup; build consistency, not a count, if this comes back.
Physically checked: kiln and both boilers carry no surge under any rule.
Kozolec's hidrofor is a Grundfos Scala2 with a built-in frequency converter,
so it soft-starts and should NOT surge.

**About a third of Home's house readings are phantoms.** Production sums grid
and inverter with `combine()`, which emits at every input's timestamp against
the other input's last value. When both update together - 26,310 of 26,466
sub-second gaps are under 50 ms - the first sum is computed against a stale
partner and corrected within 50 ms. `max_skew_s` (removed 2026-10-01) was
meant to catch this and cannot work on recorder data: Home Assistant only records a CHANGE, so an
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

- **All-phase events read off the raw house readings** (2026-09-30, Anze's window
  rule: each phase's level from the K readings nearest the step on each side,
  the window reaching at most 30 s and cut at the previous / next trigger on any
  phase, components under the phase's noise zero). Against 5,100 meter-labelled
  events at Home the vectors score V 0.257 / 0.320 / 0.375 for K 1 / 2 / 3, the
  detector's own per-phase groups 0.47 on the same events; 45 % of events come
  out multi-phase, the single-phase hidrofor among them ('ab' 384, 'abc' 265 of
  2,689). The house reading is the grid meter and the inverter combined, so PV
  swings and combination transients move all three phases together, and a
  level difference between two windows cannot tell that from a load. Joining
  per-phase DETECTED steps by time (V 0.466, device-in-top 0.387 vs 0.489 /
  0.322 per phase) is the viable form of a multi-phase event; scripts
  `vector_events_c.py` (raw windows) and `vector_events.py` (joined steps).
  Anze's objection (2026-09-30): the first comparison changed two things at
  once - the detector's steps carry a sustain check, the raw vectors did not.
  Re-run with each component required to hold across the next K readings
  (SUSTAIN=1): V 0.325 (K 2) and 0.374 (K 3) against 0.320 / 0.375 without,
  the detector's groups 0.477 / 0.479 on the same events. The sustain check
  is not the difference. Anze's second objection: "multi-phase" counted any
  component over its phase's own noise floor, so a 40 W wobble on B beside a
  900 W start on A made an 'ab' event. With a component counted only above a
  share of the event's biggest one (REL): K 3, sustain, 0.2 -> V 0.403,
  device-in-top 0.326; 0.35 -> V 0.409 / 0.337 (per-phase groups 0.479 /
  0.394); multi-phase events 31 % -> 28 %, the kiln 'ac' on 88 % of its
  events. What remains of the gap is the trigger's own component: ~15 % of the
  hidrofor's events come out as 'c' or 'b' because its A start, a motor with
  an inrush, fails the hold test while a coincident change elsewhere passes.
  Measuring components off raw windows is the weak part, not the vector;
  vectors built from the detector's own steps joined by time scored about
  even (V 0.466 / device-in-top 0.387 vs 0.489 / 0.322). The labelled set has
  one multi-phase device (the kiln), so it cannot show the vectors' upside.
  The baseline still differs in being the ONLINE grouping (fading histogram,
  input keys); the single-phase patterns are the same cut.
  The fair form, on the ten days (`vector_events.py edges_ten.json`): the
  detector's own per-phase steps joined into one event when they come within
  3 s and the smaller is at least BALANCE of the larger, clustered per
  pattern on the total size. BALANCE 0.2 / 0.5 / 0.9: V 0.451 / 0.456 /
  0.448, purity 0.878 / 0.875 / 0.865, device-in-top 0.369 / 0.373 / 0.370;
  the per-phase groups on the SAME events 0.460 / 0.867 / 0.373. Even. The
  kiln comes out 'ac' on 660 of 664 events without any device link, which is
  the structural gain; the labelled set cannot score it. BALANCE hardly
  matters, as Anze said (a real multi-phase load shows on its other phases at
  well over 20 %).
- **Capping an edge cluster's width** at what the meter resolves (8 units of the
  size scale, a noise either side at small steps, ~8 % at large; segments cut
  at their thinnest interior points), meant to break the catch-all groups
  (Home's phase C group #21: 10-700 W, 2,881 steps). Widths 6 / 8 / 12: Home
  purity 72.1 / 81.4 / 78.9 (86.2 without), wconc 67-74 (86.0), the mat's hours
  outside heating 140 h (2.4 h), pump clean 1048-1064 (1157), Kozolec wconc
  98.9 -> 89.6 (the variable-speed hidrofor over 18 signatures). Narrower
  segments leave too few thermostat-keyed steps per segment to clear the chance
  test, so the mat loses its keyed cluster and lands in the blob; and a
  variable load IS a continuum. The blob is not the cluster's width: it is one
  device chained from many rise clusters (see the energy bench).
- **Devices without chaining** (2026-09-30): two rise clusters one device only
  when their links are at least half of BOTH clusters' steps, instead of "more
  often than chance". It does what it says - the chance test had chained 32
  clusters (194-598 W, all three phases, 11,228 runs) and 46 more (730-2,192 W)
  into two devices at Home; the share rule left single-cluster devices plus
  the kiln's two legs (0.2, 0.3 and 0.5 alike). And the bench got worse:
  Home 86.2 / 86.0 -> 79.8 / 59.7 %, the mat 53.8 h caught 57 % -> 32.1 h
  caught 34 %, pump clean 1157 -> 1084, the hidrofor's signature 66 % -> 47 %
  pump by energy; Kozolec unchanged. The blob those chains make is where the
  coincidental and mis-paired runs collect, and while it holds them the named
  loads stay clean; dissolve it and they land in the load whose start size
  they share. Fix the runs (pairing), not the device rule. `dbg_devices.py`.
  Re-run on the ten days with runs filed on their own phases (0.3 and 0.5
  identical): Home purity 75.5 -> 81.5, wconc 58.6 -> 57.0, pump 443 -> 446
  clean, kiln 441 / 65 -> 450 / 61, mat unchanged, Kozolec unchanged; the
  pump's energy still 1 % caught, its runs in a 957-run signature that is
  44 % pump. The gain in purity is real, but the rule breaks exactly the
  case it was meant to protect: the compressor's A leg shares the pump's
  cluster, its co-start share is diluted to 24 %, and the compressor loses
  its legs. Not adopted; the event unit (Known shortfalls) is the fix.
- **The kiln-leg ideas** (branch `kiln-legs`, 2026-09-30, redone on the
  density / device base): LEARNED_SETTLE (a run settles by what its pairs
  learned it drops, pairs learning against the start step) and
  LEGS_STOP_TOGETHER (a smaller drop while the partner legs stopped whole
  closes the leg; 1 opens a run for the rest, 2 does not). Against 86.2 / 86.0,
  mat 53.8 h / 2.4 h outside heating / caught 57 %, kiln 861 / 176, pump 1157:
  settle alone 80.8 / 74.3, mat 201.7 / 127.3 h, kiln 863 / 156, pump 1103;
  legs 1 alone 70.6 / 71.3, mat 230.7 / 143.5 h, kiln 866 / 167; both (1, 1)
  75.0 / 63.7, mat 32.0 / 1.2 h caught 34 %, kiln 864 / 138, pump 1093;
  (0, 2) 73.8 / 66.0, mat 32.4 / 1.2 h, kiln 874 / 151; (1, 2) 82.0 / 70.7,
  mat 218.5 / 134.2 h, kiln 863 / 142. Thirty-odd single-leg pulses of 709
  against 4-15 points of purity and 60-70 clean pump runs: dropped, branch
  deleted.
- **A size scale that follows the phase's noise** (2026-09-30): the edge
  histogram's unit (a quarter of the phase's noise) is frozen at the phase's
  first step - at Home a quiet moment, 10 / 20 / 24 W on A / B / C, while the
  phases run at 80 / 43 / 121 W of noise by the end and C at 120-176 W all day
  (`dbg_unit.py`). Re-binning the histogram onto the current noise whenever it
  moved 1.5x from the unit: Kozolec's energy precision 72.5 -> 80.9 % (the pond
  EVSE's signature 66 -> 85 % its own; its noise settled from 28 to 10 W), score
  and fridges unchanged; Home 86.2 / 86.0 -> 70.3 / 69.4 %, the mat 53.8 h caught
  57 % -> 33.0 h caught 35 %, pump 1157 -> 1077. Home's noise swings tenfold
  between night and day, so the scale re-set several times a day and no segment
  held. The idea has something at Kozolec; if it comes back it is as a unit that
  settles slowly (a long-run noise, or one that only ever moves once the phase
  has been measured for days), never one that rides the daily swing.
- **Filing a run only once its start cluster has recurred** (FILE_MIN_STEPS 8,
  on the all-phase events, ten days, 2026-09-30): Kozolec wconc 93.3 -> 99.8 %
  (the hidrofor 98 % in 3 signatures for 60 % in 10) but Home purity 82.5 ->
  80.4, wconc 59.7 -> 54.8, the hidrofor 74 -> 68 %, and a quarter of Home's
  detected energy left unfiled; signatures made 1,835 -> 973. Home is the
  harder site, so not adopted. The churn it aimed at stands as a shortfall.
- **Apparent power (V x I) as a fingerprint.** The step in |S| depends on the
  baseline's reactive and active power - a 1 kW load at PF 0.9 moves |S| by
  0.80-1.09 kVA depending on what else runs - and an inductive load can cancel
  a capacitive baseline and read resistive. Only the SIGNED reactive step dQ is
  the load's own (vars add; magnitudes do not), and given dP and dQ, S and PF
  follow, so a separate apparent-power feature adds nothing. Where the var is
  signed (Home's m1 from 2026-09-30) the coordinate to cluster on is dQ itself
  with the var resolution as its error bar, not the ratio PF (a flat 0.15 PF
  tolerance lumps 2 kW at 0.98 and 0.99, 120 var apart). Where it is V x I
  there is no usable reactive feature. Benched: ignoring reactive power
  entirely (`NOQ=1`) changes nothing at either site - no filing decision reads
  PF any more (`Signature.matches` was dead and is gone; `alike` serves only
  the orphan names), so the V x I factor is description only.

- **Home's kiln ladder** (one flat 2-phase load appearing as ~16 signatures at
  descending powers) is *not*: PV/template skew (it fires at night, 0-5 %
  daylight), meter averaging (no meter at either site averages - the Kozolec
  boiler has 36 clean switches and zero intermediate samples), partial
  transition samples (6 % of rises), or element heating decay (107 aligned
  pulses are flat to 1 %: 100.0 / 100.2 / 100.2 / 99.8 / 98.7 %). It *is* at
  least partly the inoperative `SUSTAIN_SECONDS` guard plus edge mis-pairing.
- **Home's kiln is wired phase to phase, A-C** (measured 2026-09-30 off the
  Hiša 3EM's per-phase real and apparent power at 256 pulses: the real step is
  0.868 of the apparent step on A, quartiles 0.867-0.870, and 0.871 on C; 99 %
  and 95 % of pulses within 0.04 of sqrt(3)/2, none near 1.0). One resistive
  element between two phases draws one current 30 degrees off each phase
  voltage, so each phase reads PF 0.866 with reactive parts of opposite sign;
  two elements to neutral would read 1.0 on both. Anze expected phase to
  neutral; the house meter's V x I gave 0.92, blurred by the baseline. So the
  kiln is always a two-phase load with equal currents on A and C, its per-phase
  power factor 0.87 is geometry, not the element, and with the signed var its
  dQ on A and C will have opposite signs. Script: `kiln_wiring.py` (session
  scratch), the `shellys-pf` export group.
- **`_pair` must stay most-recent-first.** Best-size-fit was tried and is worse
  at both sites.
- **Pairing on the edge library, tried 2026-09-29 and parked** (patch kept locally
  in `data/patches/2026-09-29-edge-pairing-experiments.patch`). All on Home 18-27
  Sep with the thermostat, `HOUSE=prod` - measure the mat that way; the replay's
  default house reading gives other hours (the 30 % gate's 91.1 -> 87.9 h was on
  it). Shipped baseline: purity 73.7, wconc 48.6, mat 106.4-110.5 h counted once
  against 89.9 h of heating.
  - *A device MOVES WITH an input* (it changed at >= 0.2 of its starts or stops):
    its runs need that change at an edge. Neutral alone (mat 106.6 h): most of
    the excess is real mat runs missing their stop, not look-alikes.
  - *Expected edges* from the input's changes, steering pairing (a drop as big or
    bigger closes the device's run at its size; a rise opens it and a second
    run for the rest; an unseen stop closes it once the window passes): purity
    74.0-74.7, wconc 50.1-51.8 - but the mat swung 98.7-140 h between near-
    identical variants. Tried with it and worse: a smaller drop read as the
    device stopping while something started (made up ~535 W runs); protecting
    the device's run while its input is on (mat 119 h, runs overlapping 45 h);
    keeping movers and non-movers from merging (mat 133 h); a run check (>= 30/
    50/70 % of the run inside the on-periods: 98.7 / 110.5 / 109.1 h - noise).
  - *Held drops* in place of the stepped-down guess (a drop fitting no run is
    held; held drops completing a run close it together): purity 72.3-74.2 but
    the mat 140.5 h (joint) or 80 h of overlapping runs (held only), the pump
    1118 -> 1088 clean, the fridges 230 -> 211 caught. The stepped-down guess is
    what erodes runs (the mat's +614 W lost -191 W to a stranger and stayed open
    15.8 h) AND what follows real drift (fridge, pump); removing it needs each
    pair's learned stop-to-start size first.
  Why they swing: a few runs the meter never closed - hours long - decide the
  mat, and they land wherever size-matching and merge chains put them (the 15.8 h
  run went into a 437 W signature, merged into 465 W, then into 443 W). The fix
  is pairing on learned edge pairs and devices with levels (stages B and C),
  not more rules on this one.
- **`_unseen_stop` waiting for the shortfall to hold across N settled levels**
  (Anze: "a couple impossible readings before we decide", 2026-09-29), checked
  at rises as well as drops. The closes that fall within 30 s of a >1 kW solar
  swing did not fall (23-30 % of sunny closes at N 1-3, against 13 % of sunny
  moments): the shortfall there persists, so it is not a one-reading blip. The
  mat was erratic - 100.6 / 146.2 / 85.4 h against the shipped 91.1 (thermostat
  89.9) - and Home purity 71.0 / 73.2 / 73.4 against 73.7. The shipped check
  closes at the first settled level after a drop.
- **Filing a run only into a signature it does not overlap, as first built**
  ("one device never runs twice at once", 2026-09-29) made Home's floor mat
  worse against its thermostat (89.9 h heating, 18-27.09): 114.0 -> 127.9 h
  counted once, though it held MORE runs that start with the thermostat (1579
  vs 1540). The idea is sound; the sessions it judged were not. In a busy
  stretch a mat start is paired with the wrong stop, so two real mat runs
  overlap and the guard refused the second, and a stop lost in another load's
  step left runs open for 9-16 h that then landed in the mat. Re-test it once
  sessions close properly (`_unseen_stop`). Home purity +0.2, wconc -0.4,
  Kozolec even. Its energy side did ship: `_spread` counts overlapping time
  once.
  Re-tested on top of `_unseen_stop`, at filing AND at merging (`alike`
  refusing two signatures whose runs overlap), 2026-09-29: Home purity 73.7 ->
  74.5 but wconc 48.6 -> 45.1 (the hidrofor over 59 signatures, not 35),
  Kozolec's fridge purity 86 -> 77 %, the mat 91.1 -> 94.0 h. Mis-paired runs
  of one device overlap each other too, so the guard splits real devices. It
  needs sessions that are right in the first place, not as a fix for wrong ones.
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
- **Dials that were off, removed with their code (2026-09-30)** - each was
  benched and lost or tied; kept only as these lines:
  `MATCHED_STOP_SAMPLES` (a drop the size of an open edge on fewer readings:
  kiln single-leg 154 -> 114 but Kozolec's ramping Scala2 140 -> 106);
  `SWITCH_IDENTITY` (a switch's runs join its usual signature: 71.9 vs 71.7 %
  purity - the credit alone places the mat); `MATCH_DURATION_SCORE` (duration
  scores between two fits: fridge purity +1, Kozolec 39 -> 45 signatures);
  `SUB_IDENTITY_CIRCUITS` (circuit meters deciding identity: worse at Home);
  `SUB_POWER` (the quieter sub-meter's power: NASA 26 -> 22). And run length in
  grouping at all - `keeps_time`, `duration_factor`, `LOOSE_DURATION_FACTOR`,
  `DURATION_IDENTITY_*`, the pairs' overdue close (`PAIR_OVERDUE`) and the
  newest-bigger-load step-down guess (`STEP_DOWN_GUESS`): Anze, 2026-09-29, run
  length and frequency raise confidence, they do not group. Without them Home
  76.0 / 56.2 % against 75.9 / 53.7 with, 147 signatures against 197.
- **Fixed dials and their dead branches, removed 2026-10-01** (pure deletion:
  every bench identical before and after): `EDGE_BY_METER` "soft" and off,
  and placing every step rather than starts only (`EDGE_WHERE_RISES_ONLY`) -
  see the experiments table; `EDGE_DEVICE_SPANS` (a device spanning a plain
  and a placed cluster: Home 69.9 / 38.6, Kozolec F0.5 80.7 %); `TOO_BIG`
  "shrink" and off; the running-mean interval under `INTERVAL_PERCENTILE`;
  the switches on `SAG_CLOSE`, `NOISE_FROM_MOVES`, `SPLIT_BY_METERS`.
- **Device-level pairing rules on the edge library, removed 2026-09-30**:
  learned part-way drops as immediate step-downs (never fired at Home once the
  rise and drop had to be one learned device; 3 joint stops instead of a device
  cost the kiln 8 full-size pulses), joint stops and multi-run closes limited
  to one device's runs (kiln full-size 863 -> 851, single-leg 180 -> 199 for
  +0.2 purity), and signatures kept apart by their starts' input keys (no
  effect: 76.7 vs 76.8 %, mat 108.1 vs 107.4 h). A first "apart" rule - never
  merge a signature with one the switch gate kept its runs out of - split the
  mat three ways (#20 58.6 h of 89.9 h heating, the rest in #312 and #38).
- **Ablation of every rule that was on, 2026-09-30** (Home 18-27 Sep with its
  thermostat, `bench.py home`; Kozolec `score` and `fridge`). Base: Home 76.1 /
  55.4 % (170 signatures), mat 93.9 h counted once of 89.9 h heating, 25.8 h of
  it not heating (73 % precision), kiln 864 full / 175 single-leg, pump 1117,
  Kozolec 98.3 / 94.1, fridges 167 at the right length. Removed as neutral:
  `START_SHAPE_SPLIT` and its shape machinery (Kozolec's two fridges kept apart
  by their starts: everything within noise, and one right combined fridge is
  what Anze wants), `PAIR_HOME` (identical: a device's home decides first),
  `ORPHAN_MARGIN` (was off). Kept, each clearly needed off it: the three
  cross-leg corroboration rules (kiln 864 -> 787 without the stop one),
  `SAG_CLOSE` (fridges 167 -> 92), `SAME_CLUSTER_ENDS` and `INPUT_ENDS` (mat
  OVER 25.8 -> 39.1 / 47.8 h), `_unseen_stop` (38.8 h), `DEVICE_HOME` (38.9 h),
  learned pairing (38.1 h, pump -11), the switch gate (33.8 h), input keys on
  edges (60.2 h), `SUB_OVERRIDE` / `SUB_METER_IDENTITY` (Kozolec wconc 94.1 ->
  87), `SUSTAIN_AGREE`, `STEP_AT_HALFWAY`, `PHASE_BALANCE_MIN`, the blip drop.
  Held drops and joint stops (`HELD_DROPS`) bench BETTER off (Home 77.3 / 57.5)
  but the bench has no stepped load: a washer's 2 kW -> 400 W is then one
  1.2 kW level, 29 % over its energy. Re-measured on ten days (2026-10-01,
  against 7b21e9e): off trades precision for recall, so kept. Home with the
  thermostat 77.1 / 47.8 -> 75.4 / 48.2 % (the server UPS's runs in NASA's
  signatures 81 -> 158, the pump's 61 -> 81), Home's energy P/R 78.4 / 27.1 ->
  68.8 / 37.7 % (NASA's and Susilna's loose signatures crossing half; the
  workshop boiler 92 -> 87 % precise), fridges at the right length 65 -> 62;
  Kozolec's energy recall 81.7 -> 95.1 % - the pond EVSE 76 -> 96 %, and that
  is the joint stops: dropping only the held drops' lift of the followed size
  leaves it at 76 and costs the workshop boiler 92 -> 80 %. Joint stops fire
  1,224 times in Home's ten days (205 of them closing a run a device meter
  accounts for), 33 at Kozolec. Mixed, kept: `SETTLE_SHARE` (off: kiln
  single-leg 175 -> 147, but pump -20 and the mat +7 h), `HOLD_MIN_INTERVAL_S`
  (Kozolec wconc -1.1, Home +1.5). `ENERGY_MATCH_LO` cannot be ablated - the
  score's labels use it too.
- **Repeated `consolidate` passes do nothing** - it reaches a fixed point after
  one pass. A library that shrank over time did so from merge damage that a
  rebuild undoes. (Consolidation itself is gone since 2026-09-30: signatures
  merge only when their runs are one device's.)
- **Clustering the edges, tried 2026-09-30** (offline, 7,858 house steps a
  device meter or the kiln's pulses labelled, 7-28 Sep; `V` = V-measure). As
  deployed, nearest within +-10 %: V 0.436, device in its top cluster 0.52,
  498 clusters. Density valleys on size in measurement errors: 0.593 / 0.82 /
  78 - shipped. Worse: scikit-learn's Gaussian mixture with BIC (0.397 with PF,
  0.516 size only), HDBSCAN (0.30-0.35, shatters devices), DBSCAN (0.167, merges
  everything); splitting by the V x I power factor, in every method, even
  weighted by each step's own PF error (the kiln's V x I factor moves with the
  other loads' reactive power, 0.88-0.95); normalising steps to 230 V (no
  change). scikit-learn is a candidate once the meter's signed reactive power
  gives a second real dimension - re-test then. Do not tune the width on V:
  unlabelled loads merge unseen, so wider always "wins".
- **Step P / (V x step I) as a factor** is steady only where the step dominates
  the phase's current (the kiln at night: 0.926, middle half 0.910-0.960); the
  pump reads 1.09 and a resistive boiler 1.25, because current magnitudes do
  not add. The meter's signed reactive power (Home, from 2026-09-30) is exact.
- **All-phase events measured from raw readings, as first tried** (every
  phase's shift over 20 s before and 6-24 s after any trigger): V 0.379 against
  0.466 joining per-phase steps within 3 s at a balance of 1:2, and 0.489
  per-phase - the wide windows took in other loads' changes. Measure the size
  over +-2 readings and widen only while no other phase triggers (Anze).
