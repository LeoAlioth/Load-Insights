# Detector and integration (custom_components/load_insights)

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
built (the owner asked whether knowing helps, 2026-09-30).
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
| `NOISE_MAD_FACTOR` | 3.0 | measured noise = this × median idle deviation | count | 2–6 | 3 / 4 / 5 on ten days (2026-10-01, see the constant's note) |
| `NOISE_WINDOW` / `REL_REFRESH_S` | 240 / 300 s | the noise is the median of the last 240 idle moves at every reading (`_slide`); the relative noise the median of its last 240, worked out every 300 s of the readings' clock | count / budget | — | **a fix (2026-10-04, exp4)**: in blocks of 120 counted from the first reading, one reading dropped moved every later block - see *The detector reacts to readings that should not matter*. 300 s against 3600 s and slid at every move, Home circuits only |
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
| `SETTLED_FOLLOW` (INS-8 a, **shipped 2026-10-20 with the fold, no dial**) | 0.25 | while a run is the only one on its phase and younger than `SHAPE_SETTLED_S`, the phase's level follows its readings at `SETTLED_FOLLOW` per reading (not `SLOW_FOLLOW`), so the run's followed size and the size it is booked at on closing (`_close`) are what it settled at, not its first ~3 s | ratio | 0.1–0.5 | benched with the fold: Hidrofor hidden size error +10.9 % -> -1.1 %, Home fed 65.0/10.6 -> 66.1/10.7 - see *INS-9 bench* below; not swept |
| `SETTLED_SHARE` (INS-9 2, **shipped 2026-10-20 with the follow, no dial**) | 0.35 | `_close` folds a multi-level run's leading falls (its start decay, declared by the grid as small falls that join the run as levels), each inside `SETTLED_SHARE` of the start and within `SHAPE_SETTLED_S`, into its start: booked at the level it settled at. Not shippable alone: Home fed impurity +2.4 and Hidrofor impurity 13.7 -> 29.8 | ratio | 0.2-0.5 | **shipped with the follow** (owner, 2026-10-20; every slicing 0 sessions differ, 66 replays); share not swept |
| `GAIN_FALLS` / `GAIN_MEDIAN` / `GAIN_UNGATED` / `GAIN_RING` (INS-9 1) | **off** / off / off / 200 | the Fleet's cross-meter gain learns from falls too (`_step_meter` run for learning only) / is the median of the last `GAIN_RING` log ratios, not a running mean / teaches every meter whose one clean step faced the grid's within `METER_GAIN_BOUND`, not only the meter the step was placed on | switch | — | **benched 2026-10-20, no gain on the card** - see below |
| `FOLD_SLOW` / `FOLD_SHARE` / `FOLD_S` (INS-9 2, edges.py) | **off** / 0.35 / 60 s | an edge's window after a rise skips the opposite small steps that follow it, the window before a fall the same-way small steps that lead up to it (start decay, pre-stop drift) | switch | — | **benched 2026-10-20, small gain on the edge and truth cards** - see below |
| `SAME_SPAN` (INS-9 3, edges.py) | **off** | a step's windows end at the steps of every meter of its comparison, latency-shifted onto its own clock | switch | — | **benched 2026-10-20: loses, not slicing-invariant as built** - see below |

**INS-8 bench (2026-10-19, HEAD 9ed4b9f + the three dials, Home and Kozolec ten days + third site, `card SLICINGS=all`, capture / impurity % at SLICE=6, `data/scorecard/*ins8-*.json`).**
Base -> a / b1 / b2 / a+b1 / a+b2. Home devices hidden 32.7/14.2 -> 32.6/14.2, 32.7/14.2, 32.7/14.2, 32.6/14.2, 32.6/14.2; circuits 44.9/13.1 -> 43.7/13.0, 44.1/11.7, 43.8/13.7, 45.5/12.4, 43.8/13.3; fed 65.0/10.6 -> 66.1/10.7, 65.0/11.2, 65.5/10.8, 65.9/11.2, 66.5/10.4. Home partition fed 62.5/9.2 -> 62.6/9.4, 62.1/8.6, 62.6/9.2, 62.2/8.6, 62.8/9.2. Kozolec devices fed 86.9/2.1 and hidden 84.3/5.3: every variant within 0.1 point. Third site charger hidden 90.5/5.9 -> 90.7/6.2 with (a), fed 95.4/5.0 -> 95.2/5.0; b1, b2 nothing. Steady loads (Home: Polnilnica, Susilna, Hidrofor, Workshop boiler; Kozolec: boiler, car charger, pond, pond EVSE, IR panel, well pump) fed: Home 72.4/9.9 -> 73.3/10.1, 73.2/9.8, 72.5/9.8, 72.9/10.0, 74.0/10.1; Kozolec 87.1/2.1 -> 87.1/2.0 for (a) and (a+b), the b's identical. Variable loads (office plug, Server UPS, Blaževa Soba; Kozolec water pump) fed: Home 44.8/13.6 -> 46.1/13.0, 42.5/17.4, 46.2/14.7, 46.9/16.1, 45.7/11.9. Pump bench (Home fed): clean 667 of 798 -> 672 (a), 661 (b1), 667 (b2). Invariance: 6~1, 6~LIVE=1 and 0~6 differ in 0 sessions for every variant at every site and mode; 6~6R and L1~L1R in Home fed differ in 2 sessions in the base and in a, b1, b2, a+b2, 3 in a+b1 (the base's 2 are the standing restart difference). Pairing, Home fed: observed-stop share 73.7 % -> 73.1 (a), 74.2 (b1); within 20 % of start 74.2 -> 73.5 (a), 74.0 (b1); stops that closed nothing 7,778 -> 7,458 (a), 7,475 (b1). The 27 Sep 13:00-13:01 Hidrofor case does not reproduce at HEAD in any variant or the base (the pump's 865 W is a meter-taken share, its runs close at 6.6 and 0.8 min), so b1 and b2 were measured by the card alone; b2 changes nothing in Home fed's pump bench and moves Home circuits by a point, b1 changes Home circuits / fed by up to 2 points of impurity beside moves of the same size in the other direction at single loads (Server UPS, Polnilnica, office plug): inside the noise band of the 2026-10-04 card (Home circuits devices 2.6 / 1.9), which was measured on older code and is not a current band. Not decided; flipping any on is a detector change (generation bump) after the owner's call.

**INS-9 bench (2026-10-19/20, HEAD 9dc0cb9 + dials, Home / Kozolec ten days + third site + the two circuit sites, `data/scorecard/*i9-*.json`; capture / impurity % at SLICE=6, hidden / circuits / fed).**
Base -> `GAIN_FALLS+MEDIAN+UNGATED` (the energy card is the same with the edge dials on: they never reach it) / `SETTLED_FOLD` / `SETTLED_FOLD+SETTLED_START`. Home devices hidden 32.7/14.2 same, same, 32.6/14.1; circuits 44.9/13.1 -> 45.2/13.9, 45.2/13.2, 43.9/13.1; fed 65.0/10.6 -> 64.2/11.0, 66.4/13.0, 66.1/10.7. Home partition fed 62.5/9.2 -> 62.7/9.8, 63.9/10.9, 62.5/9.3. Kozolec devices hidden 84.3/5.3 and fed 86.9/2.1: every variant within 0.1. Third site charger fed 95.4/5.0 same in all. home-hisa hidden 30.1/20.7 -> same, 33.2/17.0, 26.7/12.3; home-mansarda fed 60.1/5.7 -> 60.6/5.8, 60.5/5.9, 62.1/6.3. The gain dials move no site by a point of capture; per load (Home circuits, fed) the moves of 2-7 points go both ways. Steady loads fed Home 72.4/9.9 -> 71.3/10.2, 75.0/13.1, 73.5/10.1; Kozolec unchanged. Invariance 0 sessions at 6~1, 6~L1, 0~6 in every variant; 6~6R / L1~L1R Home fed 2 base, 3 for the gain dials (SETTLED_FOLD 0). Pairing Home fed, observed stops within 20 % of start: 74.2 -> 74.3 (gain), 74.2 (fold): unchanged (the fold books the size, the start edge `o.watts` the pairing reads is not changed).
**Booked size of the Hidrofor's runs (pump_size.py, booked first level over the plug's level while the run holds, 698 clean runs of 798):** hidden: median +10.9 % (89 within 5 %, 289 within 10 %) -> SETTLED_FOLD -1.1 % (417 / 559) -> with SETTLED_START -1.1 % (423 / 559); SETTLED_START alone +10.9 % (88 / 288). Fed (the plug owns the run): +1.3 % (348 / 520) -> +1.1 % (362 / 550) -> +1.0 % (368 / 550). Why SETTLED_START alone never engaged (traced on 2,346 closes of 700-1300 W runs on phase A): the booked level of the pump's multi-level runs is their FIRST level, the surge-high start (960-1056 W against ~845 W), the decay being declared by the grid as small falls that join the run as later levels; (a) only reads `o.now`, set on a steady reading with exactly one open edge on the phase, which was None on ~1,800 closes (other loads open, or multi-level) and engaged on 116. Cost: Home fed Hidrofor impurity 13.7 -> 29.8 with the fold alone (the pump's signature takes more of other loads at its new size), with the pair (fold + SETTLED_START) it stays within 2 points of the base; Home fed Workshop boiler capture 73.5 -> 86.8-89.4.
**Edges (edge card, truth card; Home / Kozolec / third).** `FOLD_SLOW`: Home grid>Hidrofor conservation 43.7 -> 47.4 % (50.2 with SAME_SPAN), truth plugs found Hidrofor 42.7 -> 49.3 %, pooled Home 47.4 -> 49.7; slicing-invariant (0 edges differ); mixed % up a little (Kozolec 20.1 -> 23.7, third 8.4 -> 9.7). `SAME_SPAN` alone: conservation down (Hidrofor 43.7 -> 38.8, Hiša>Blaževa Soba 17.8 -> 14.1) and 6~0 differ in 7,908 Home edges (75 Kozolec, 1 third): the decision for a moment can fall before the peers' next step is fed, so it is not slicing-invariant as built - a fault, and with it the truth card (whose hidden edges cannot take a plug's span) reads it as -6 points for the Hidrofor. `GAIN_*` on the truth card: Hidrofor 42.7 -> 47.2-48.3, Kozolec Boiler 69.0 -> 73.0, Server UPS +3-8, Workshop boiler -3 to +0.4. Edge card with the gain dials: slicing-invariant, Home grid>Hidrofor conservation 43.7 -> 50.3 %, Kozolec grid>Boiler 82.5 -> 88.7 %, Server UPS 51.0 -> 48.3, Blaževa Soba 24.0 -> 21.3, mixed % Kozolec 20.1 -> 23.9. Not decided; nothing is on.

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
| `ABSORB_WINDOW` | 100 | caps the weight of history in a signature's running means | budget | 20–500 | not tested (the owner chose 100 over 50) |

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
| `MATCH_PATIENCE_S` | 1200 | how long a main session waits for a slow device meter, and a meter's session for the grid's that started with it to vote on | physical | 300–3600 | set from observed Shelly reporting lag; not swept |
| `METER_WAIT_CAP_S` | 300 | the most the grid is read behind its meters; the horizon itself is the slowest meter's declaring lag (`Fleet._horizon`), at least `EDGE_LAG_REACH_S` with inputs fed; *Wait for slower meters, at most* on the settings page | physical / policy | 0–300 | the owner's bound (2026-10-02: "up to 5 minutes"); the horizons the ten days come to are under *A pass is only a pause* |
| `LAG_REACH_S` / `LAG_SAMPLES` / `LAG_MIN_SAMPLES` / `LAG_PERCENTILE` | 300 s / 200 / 20 / 0.95 | each meter's lag against the grid, from the clear steps the two share: looked for this far either side, the last 200, believed from 20, at the 95th percentile - its report lag sets its latency (`PhaseState.latency`), its declaring lag the horizon | s / budget / count / ratio | — | not swept (2026-10-02, the owner: latency replaces the cadence) |
| `MODEL_REFRESH_S` | 3600 | how often, on the readings' clock, the horizon is worked out again from the lags learned | budget | — | not swept; a fixed grid of the clock so the slicing cannot move it |
| `CROSS_METER_DURATION_FACTOR` | 12.0 | duration tolerance between a main and a device session | ratio | 3–30 | not tested |
| `SUB_SAMPLE_TAIL_S` | 7200 | device readings kept for the energy answer: at least this before a pass's first, and back to the oldest run or session still to be placed (2026-10-02) | budget | 3600–43200 | not tested |
| `PHASE_MAP_MIN_VOTES` | 30 | shared single-phase sessions before a three-phase meter's channels are mapped onto the grid connection's phases (`phase_mapping`) | count | 10–100 | not swept; Home's attic 3EM reaches 147 in five days and maps a→B, b→C, c→A; the Hiša 3EM 1327, identity |
| `SUB_OVERRIDE` | always on (the switch removed 2026-09-30) | house sessions wait to be filed until every sub-meter fast enough to have seen them (two readings inside the run) has reported past their end; a partner then decides the signature | switch | — | **shipped (gen 14)** with the two below: Kozolec's Scala2 128 → 195 in its main signature, held out 53 → 61, purity unchanged; Home 78.6 / 80.6 → 79.1 / 80.7 %; kiln main signature x397 → x392 |
| `SUB_METER_IDENTITY` / `SUB_DEVICE_SHARE` | always on (the switch removed 2026-09-30) / 0.5 | a session whose ENERGY a one-device meter accounts for joins the house signature most of that meter's sessions went to, when it fits; one device = its own library has ≥ this share in one signature | switch / ratio | 0.4–0.8 | almost all of the gain above; share not swept (hidrofor plug 99 %, Hiša 55 %) |
| `SAG_CLOSE` | always on (the switch removed 2026-10-01) | a load running alone is followed as it sags, and a stop may close it at that size as well as at its start's | switch | — | **benched, shipped (2026-09-28).** Kozolec's fridges (the new `fridge` bench, 252 runs off the house reading): caught 125 → 216, right length (±25 %) 71 → 153; Kozolec purity 98.2 → 98.3 %, wconc 92.4 → 92.6 %; Home purity 77.4 → 77.6 %, wconc 55.5 → 54.9 % (Hidrofor 717 → 712 of ~1200); kiln full-size 410 → 412, single-leg 112 → 104, top signature x397 → x387; pump clean 831 → 830 |
| `METER_PHASES_MIN` | 20 | a one-device meter takes only house sessions on the phases it has been credited this many sightings of; fewer, and it takes anything | count | 10–50 | **benched, no change**: on the history to 22 Sep, on or off gave identical placements and scores at both sites (Home purity 77.4 %, wconc 55.5 %; one Blaževa Soba credit 56 → 55). The misplacements it stops - Hidrofor credited an A+B and a B load - came on 25 Sep, after that history; not swept |
| `SWITCH_END_SHARE` | 0.25 | a switch's stop may miss the session's by this share of the run | share | — | set from the mat's edges, not swept: start 3-9 s after the heating (5-95 %), stop within 25 % for 1243 of 1335 |
| `SWITCH_MEMORY_S` | 4 h | how long a switch's on-periods and a number's readings are kept, counted from the oldest run or session still to be asked about (from the oldest reading a pass brought until 2026-10-02) | s | — | **a fix, benched**: counted from the pass's END, a six-hour backfill slice had lost its first two hours' switch-ons before filing them - 462 of 1232 fitting mat runs uncredited, and the mat's signature short of the half that places it (762 of 1738). Counted from its start: every one credited |
| `INPUT_MIN_RUNS` / `INPUT_MIN_SHARE` / `INPUT_MIN_LIFT` | 8 / 0.6 / 3 | a load runs IN one value of a setting (a washer's cycle phase, a fan's speed) once it has this many runs (time-weighted), this share of them in the value, and that share this many times the value's share of the time | runs / share / × | — | **benched, no change to the scores** (2026-09-28): Home 18-27 Sep with the Sock Eater's cycle phase, appliance state and sub-phase and the RF ceiling fan's speed - purity 71.7 %, wconc 31.1 % fed or not. Two loads tied: #413 5.8 kW A+B+C 6 s x16 (62 % of runs in Wash, 8.1× chance) and #578 127 W A+C 38 s x194 (73 % in Running, 4.1×). The washer's own parts - probably #274 1.8 kW on A (the heater), #7 48 W and #201 126 W on A (the inverter drum) - run in Wash 3-4× chance but share their signatures with look-alikes, so none reaches the share. Weighted by sighting instead of time, a 49 W load starting 285 times a day remembered eight hours - one wash - and read as tied (89 %, 4.7×): hence `INPUT_TIME_TAU_S` |
| `INPUT_TIME_TAU_S` | 14 d | how long a value's share of the time, and a load's runs in it, are remembered | s | — | see above; not swept |
| `INPUT_EVIDENCE` | **1** | a load tied to a value of a setting counts as tight about time (evidence's duration term raised to its share) | switch | — | **benched, scores unchanged; the naming page is not**: scores identical on or off; evidence of the two tied loads 0.72 → 0.77 and 0.50 → 0.65, none newly over the naming bar (2026-09-28). On ten days with the same four inputs (2026-10-01): scores identical again (Home SUBS=prod 77.4 / 49.1 %, the mat, kiln and pump too), but eight loads tied, four of the washer's clear the bar only with it - #1542 148 W in Wash 0.54 → 0.70, #1392 25 W and #1960 22 W in Dry 0.50 → 0.70, #150 294 W in Dry 0.62 → 0.77 - and Mansarda's list clears 7 instead of 5 |
| `INPUT_SPLIT` (`INPUT_RARE_SHARE` 0.25, `INPUT_SPLIT_MIN_TIME_S` 1 d, `INPUT_SPLIT_LIFT` 3, `INPUT_MIN_EPISODES` 3, `INPUT_MIN_COVERAGE` 0.5) | always on (the switch removed 2026-09-30) | a run made while a setting holds a rare value joins only signatures born in it; one born by chance goes back into an alike twin (pair lift < 3) or turns ordinary once it has run in under half of the value's episodes since its birth (at least 3) | switch | — | **benched, shipped (2026-09-29).** Home 18-27 Sep with the Sock Eater's phase, state and sub-phase: purity 63.8 → 65.4 %, wconc 29.8 → 30.5 % (office plug 19 → 16 %); kiln and pump benches identical (no washer there). Judged by runs alone, the kiln firing through one afternoon's washing split into Wash/Rinse/Spin/Dry signatures (a burst read as 7× chance) and the floor mat grew a Delayed Start one; by episodes they go back. Tied now: 119 W A+C in Rinse, 61 W C in Dry, 98 W B in End Of Cycle. The heater (~2 kW on A, 2-60 min, in wash AND dry) scatters into one- and two-run signatures and does not tie on 13 washes |
| ~~`HOLD_MIN_INTERVAL_S` / `HOLD_SUSTAIN_S`~~ | 20 s / 30 s, replaced by `SUSTAIN_CADENCES` (2026-10-01: one rule for every meter; see *A meter's cadence*) | on a meter this slow in a steady state (a Shelly heartbeating once a minute, a Zigbee plug reporting on change), a pending change counts as having held until the next reading, and holding 30 s is enough | s | — | **benched, shipped (2026-09-29).** Kozolec's IR panel meter alone: 13 runs of 164 min → 46 of 48 min (its readings: 52 runs, mean 40 min, off 2.4 min between); the house's ~500 W signature credited to the panel 8 → 27 of 63; the Boiler meter's library 132 runs of 36 min at 937 W (pulses merged) → 1467 of 3 min at 1906 W (its element: 1546 pulses of ~1 min at 1.9 kW). Kozolec purity 98.3 %, wconc 93.9 → 94.0 %; Home 63.8/29.8 → 63.9/29.6 % (production meters 64.1/29.1 → 64.1/29.2 %); kiln, pump, fridges identical. The Shelly needs no change: it reports each switch within 1-4 s |
| `HOURLY_KEEP_S` | 11 d | how far back each signature keeps its energy by clock hour (`hourly`): the window a named load's statistics are written from (`plan_rewrite`) - at its sensor's first hour, by `load_insights.backfill_statistics`, and checked every hour after (`DetectionRunner._check_named`: rewritten from the first hour more than `REWRITE_TOLERANCE_KWH`, 0.01 kWh, off). Hours age out as a run is filed and once an hour (`Signature.age`); the window never starts before now minus this | s | — | not a detector dial; sized to the 10-day backfill |
| `OLDER_MIN_COUNT` / `OLDER_MIN_WH` / `OLDER_MIN_EVIDENCE` / `OLDER_KEEP_S` | 10 / 500 Wh / 0.7 / 365 d | an unnamed load seen this often, this much in all, this sure, keeps the hours that age out of `hourly` in `older` for a year; naming it writes them onto its meter before the window (`plan_carry`), once. Stored apart: `load_insights.<entry>.detection.history`, written with the library, dropped with it | count / Wh / score / s | — | policy, not swept (2026-10-04) |
| `EDGE_LAG_REACH_S` / `EDGE_LAG_BIN_S` / `EDGE_LAG_MIN` / `EDGE_WINDOW_DEFAULT_S` | 60 s / 2 s / 50 / 10 s | how far either side of an edge an input's change is looked for, the lag histogram's bins, the changes seen before a lag is believed (`lag_window`: a peak 4x the even spread), and the window before that | s | — | not swept; Home's thermostat learned (-11, 0) s from 17k edges, the mat's edges at -5.8 +- 1.7 s |
| `EDGE_LIBRARY` | 150 | edge clusters kept per phase and direction, the weakest evicted as a new one is born (at a pass's end until 2026-10-02) | count | — | not swept; Home used ~400 in all |
| `EDGE_NOISE_SHARE` / `EDGE_SCALE_REL` | **0.25** / 0.02 | one measurement error on the edge-size scale: this share of the phase's measured noise at small steps, this share of the step at large | ratio | 0.25–1 | **swept 0.25 / 0.5 / 1 (2026-09-30)**: Home 82.6/66.4 vs 80.9/61.3 vs 76.3/50.2, pump 1153 / 915 / 410, Kozolec fridges 188 / 154 / 81. A fixed 15 W ran every small fall at Kozolec into one cluster. **The noise is the quietest measured** (2026-10-04): frozen at a group's first step, Home's A and C units were 93 and 55 W on 23 Sep - 2 Oct (a dawn seed's 372 / 218 W), one A cluster held starts of 12-370 W and "started again" was 15.9 % of hidden closes; re-binned down as the noise falls (`_rescale`), 9.7 %, and Home's card hidden / circuits / fed devices 19.7/15.9 -> 33.9/16.7, 32.9/16.1 -> 44.0/12.9, 53.9/11.0 -> 67.3/7.6, Kozolec and the third site identical |
| `EDGE_KERNEL` / `EDGE_BIN` / `EDGE_TAU_S` / ~~`EDGE_RECUT`~~ | 1.0 / 0.25 / 10 d / every step (32 steps until 2026-10-04) | smoothing of the size histogram (in measurement errors), its bin, how fast it fades; its valleys are cut again at every step, the step in it | count | — | offline: 1 error wins among 1, 1.5, 2, 3 on physics grounds; the score always prefers wider (only 15 % labelled). Every 32 steps counted from the group's first, a step dropped moved every later cut (exp4); on the clock (600 / 3600 s) as robust but 3-5 points less pure at Home hidden |
| `EDGE_ANGLE` (`EDGE_ANGLE_BIN` 3°, `EDGE_ANGLE_KERNEL` 6°) | **off** | the step's reactive angle, atan2(dQ, dP), as a second clustering dimension: within a size segment, steps are cut again at the valleys of their angle density, so a pump (~35°) and a heater (0°) of one size are two kinds of edge | switch | — | **not yet measurable**: only a SIGNED var gives the angle, and Home's grid meter signs it only from 2026-09-30 07:57 - ten days of it exist from about 2026-10-10 (Kozolec's Victron and the 3EMs are unsigned). Synthetic test only (900 W at 0 and at 630 var: one cluster off, two on). Bench it on Home's ten days then, against the purity, the pump (its cluster shares A with the compressor's leg) and the kiln, whose legs read 0.866 with opposite-signed var |
| `PAIR_MIN_RUNS` / `ABOVE_CHANCE_ODDS` | 8 / 100 | a pair is accepted once it has this many runs and a Chernoff bound puts the odds of its count by chance under 1 in `ABOVE_CHANCE_ODDS` - from the run that makes it (worked out once a pass until 2026-10-02) | count | — | **replaced shares of 30 % and 20 % (2026-09-30)**: mat hours outside heating 21.7 -> 11.5; odds 10, 100 and 1000 identical. `LINK_MIN` and the device union-find went with the all-phase events (a device is its start cluster) |
| `EVENT_WINDOW_INTERVALS` / `EVENT_BALANCE` | 3 / 0.2 | rises on different phases within this many of the slowest phase's reading intervals, the smallest at least this share of the largest, are ONE start event, clustered on their phase pattern and total size | count / ratio | 2–4 / 0.2–0.5 | measured on Home's ten days: at 2 intervals the compressor's three-leg event never formed, at 3 it did (70 steps, a cluster of 28), at 4 little more; the pump gets a false companion within 5 s 1 % of the time; 0.2 / 0.5 / 0.9 balance alike offline. In cadences since 2026-10-02 (3.3 s at Home by day, 6 s on the median): 5.5 - about the old seconds - on 66f190a cost purity 78.2 → 76.0 %, the kiln and pump unchanged |

**A group's voltage slope** (INS-7, 2026-10-19; `edges.volt_slope`, `summary(g)["volt_k"]`, the truth card's *voltage slope* line; a descriptor of a group, never a grouping input, so no dial and no effect on any other figure). The least-squares slope k of ln size on ln V (the voltage the load drew at, the on side's, averaged over a multi-phase edge's legs) across a group's edges, divided by the reliability of the voltages (1 less the edges' own voltage errors `ve` over the voltages' spread: regression dilution) and withheld where that is under a half or the group has under `GROUP_MIN` edges. Truth card on the ten days (k, se, edges): Home's oven E100 1.26 (0.30, 21), E165 0.97 (0.28, 24), E265 1.63 (0.20, 18), MW (constant power) 0.19 (0.56, 7); kettle 1.03 (0.27, 37), panini press 0.67 (0.21, 27); Hidrofor 1.00 (0.19, 249), Susilna 1.55 (0.20, 60); Kozolec's boiler -0.07 (0.09, 138; the inverter holds the voltage, reliability 0.84). So the order holds - constant power under the resistive elements - but the resistive loads read 1.0-1.6, not the 2 of P ~ V^2, with reliability ~1 at Home (so it is not the voltage's error that flattens them; not explained), and the groups of thousands of mixed edges (Blaževa Soba 3.1, Server UPS 3.7, Workshop boiler 7.8) read time-of-day, not a load: read k as a rank within a pure group, not an exponent, until a consumer needs more.

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

### The day's repair (`Fleet._repair`, shipped c689283 2026-10-04; re-benched 2026-10-19)

Once a day, 25 min past each local midnight on the readings' clock (slicing
invariant by construction, and stored: `_repaired_to`, the day's runs and
readings survive a restart), every detector's runs of the finished day are
booked no higher than its phase drew above the day's floor (the level held a
tenth of the day, by time) plus its measured noise; the excess comes off the
runs no meter measured, the one open longest first, and their signatures'
hours with them; the hourly statistics check then rewrites the named loads'
hours. `REPAIR_SLACK_NOISE=1e9` switches it off (the bench's ablation).

| dial | value | what it does | kind | valid range | tested |
|---|---|---|---|---|---|
| `REPAIR_DAY_S` | 86400 | the repaired span | policy | - | `test_the_days_repair_*` |
| `REPAIR_FLOOR_SHARE` | 0.1 | the day's floor: the level a phase held this share of the day, by time | ratio | 0.05-0.2 | exp15 only |
| `REPAIR_SLACK_NOISE` | 2.0 | slack above the drawn level, in measured noise | count | 1-4 | exp15 and the 2026-10-19 off/on cards |
| `REPAIR_WAIT_S` | `MATCH_PATIENCE_S + METER_WAIT_CAP_S` | wait past the day's end so its last runs are filed | derived | - | - |

Bench (exp15, 2026-10-04): impurity down at every site (the third site hidden 9.7 -> 7.3,
Kozolec hidden 4.0 -> 2.2, Home fed devices 8.6 -> 6.7) for about a point of capture.
**Re-benched 2026-10-19** (off -> on, capture / impurity, same code, ten days):
Home devices hidden 33.2 / 15.4 -> 32.7 / 14.2, circuits 47.6 / 15.9 -> 44.9 / 13.1,
fed 67.5 / 12.1 -> 65.0 / 10.6; Home partition fed 62.3 / 10.8 -> 62.5 / 9.2; Kozolec
fed devices 84.8 / 5.5 -> 84.3 / 3.9, hidden 84.7 / 6.7 -> 84.3 / 5.3; the third site fed
devices 96.5 / 8.3 -> 95.4 / 5.0. Impurity is down everywhere; Home with meters loses
2.5-2.7 points of capture on the devices table (the plan's limit is two: left to the
owner, the repair being live), the other tables and sites a point or less. No load
loses more than 6 points (Vticnice - pisarna -5.9, Susilna -4.5); the pairing block
and the session counts are identical on and off; the slicings agree (card
`restart4`, repair on). The two candidates exp15 named (runs filed before their meter's
location was known, a share given to a plug whose phase was not mapped) moved 5 runs in
ten days; the kitchen fridge under the incubator (#64) is beyond a re-pairing: its
steps are never declared (they hit the declared ones at chance). Tests:
`test_the_days_repair_is_the_same_at_every_slicing`, `test_the_days_repair_after_a_restart`.
Job note: `data/scorecard/jobs/repair-2026-10-12.md`.

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

Two settings were REMOVED from the detection page on 2026-09-23 at the owner's
request; their values are fixed (`DEFAULT_MIN_EVIDENCE`, `MIN_NOISE_W`) and a
stored choice is ignored. What they did, measured before removing them:
"How sure before offering a load" (min evidence) only filters the naming
page, and changes it a lot - at Home 69 of 87 namable loads clear 0.5, 30
clear 0.7, 5 clear 0.9, and the first page differs at each. "Smallest change
to notice" (min_step_w, 10 W) is a floor under each phase's measured noise:
Kozolec measures 3 W on its own, so there the floor IS the threshold, but 1
to 10 W leave its scored loads alone (boiler 338-339); at Home it binds only
now and then (20 W costs 1.8 points of purity).

Each meter holding several devices has its own list of its own detector's
signatures (`Fleet.namable`, see *A load inside a meter holding several
devices is that meter's*); a row is (meter, id), the grid's under "".

A menu row's label is cut at the dialog's width, so each row is two lines:
the label (`Signature.menu_row`'s headline) and the option's description,
which wraps - `menu_option_descriptions` in the translations, filled from the
same placeholders. A menu is also the only way to give a page a Back row: a
form's one control is Submit, which is why a load's own page is a menu and
its name box is one step further in.

### Forecast covariates (covariates.py, nowcast.py, calendars.py, inputs.py)

A term engages when its coefficient's t-value, with the variance clustered by DAY
(`covariates.day_block_t`: CR1 plus a Newey-West lag of one day, the larger of the two), clears
Student's t at `ALPHA` - for the nowcast's best lead `ALPHA / LEADS`. Features are centred on the
same hour of day's weather and the residual is against what the profile expected WITHOUT that hour
(leave-one-out, `Profile.loo_kwh`: in-sample, a slot seen once has residual exactly 0). The
`explained` share is still computed and published; it gates nothing (it drives
`Nowcast.band_shrink`). **Removed gates (2026-10-19):** `covariates.MIN_HOURS` 336 and
`MIN_EXPLAINED` 0.03, `nowcast.MIN_HOURS` 336 and `MIN_EXPLAINED` 0.03. Calendars and attached sensors: shipped the same day, below. Benches: `tests/forecast_bench.py cov`,
W1 = 28 Sep - 11 Oct (both sites), W2 = Home 12 - 25 Jan, Kozolec 17 - 30 Aug (its whole house
starts 4 Jul, so no heating-season window with 12 weeks behind it exists); whole-house series,
signals and nowcast states as production builds them.

| constant | value | kind | tested |
|---|---|---|---|
| `ALPHA` | 0.0005 | policy (false-engagement rate per term per fit) | swept 0.05 / 0.005 / 0.0005, temperature + hour-of-day centring, whole house hour-ahead / day-ahead W, noon-tomorrow kWh, OLD -> NEW. Home W1 (1241.0 / 1166.9 / 10.694): 0.05 -> 1249.1 / 1176.3 / 10.798, 0.005 -> 1242.6 / 1169.5 / 10.692, **0.0005 -> equal**. Home W2 (1226.0 / 1207.4 / 12.889): 0.05 -> 1245.9 / 1224.5 / 12.456, 0.005 -> 1239.4 / 1206.9 / 12.361, **0.0005 -> 1217.5 / 1202.8 / 12.319**. Kozolec: equal at all three. Only 0.0005 passes at Home; at 0.005 the whole house's own weak response (66 W/degC, t 3.1 on 84 days) engages and costs 1-2 % hour-ahead. The bar it sets at G day blocks: t 11.4 at 4, 5.4 at 8, 4.1 at 15, 3.4 at 31 |
| `MIN_DAYS`, `MIN_DAY_ROWS` | 4, 12 | count | 4 day blocks of 12+ rows (the owner's ~72 h, plus one day for the test's degrees of freedom); not swept. At 4 days even t = 11.4 is the bar, so nothing real engages before ~6 days: the bee room heater (kozolec, from 4 Oct) engaged at 7.6 days of history (12 Oct 06:00, 40 W/degC, t 6.9 against 5.9 needed at 7 days) and stood down again at 13 days (t 3.1 against 4.3); hour-ahead 178.4 -> 176.4 W, day-ahead 195.1 -> 195.8 W against no temperature at all |
| `MIN_SPREAD_C` | 1.0 degC | physical | a feature is fitted only if its centred weighted SD reaches it; far below an ordinary week, above a fog week. Whole-house cuts to 4 / 7 / 14 days of both sites never engage in October (t 0-2.1, 4-14 days; reason shown) |
| `HEATING_BASE_C` / `COOLING_BASE_C` | 15 / 24 | physical | not tested |
| `JOINT_RIDGE` | 1e-3 | ratio | not tested (numerical guard; the nowcast's variance uses the ridged fit) |
| `MIN_PER_HOUR_OF_DAY` | 5 | count | not tested (calendar table fallback) |
| `FACTOR_CLAMP` | (0.1, 5.0) | physical | not tested |
| `HALF_LIFE_WEEKS` | 3 | policy | not tested here |
| `LEVEL_CLAMP`, `LEVEL_DAMPING`, `LEVEL_MIN_HOURS` | (0.5, 2.0), 0.5, 12 | policy | not tested here; the level still judges the last 24 h with the in-sample residual (it is not a fit) |

**Result, signals off (the plan's columns), OLD -> NEW, `site` series, hour-ahead W / day-ahead W / noon-tomorrow kWh, then the sum of
day-ahead MAE over all series.** Home W1: 1241.0 / 1166.9 / 10.694 -> equal; sum 3981.8 -> 3972.6.
Home W2: 1226.0 / 1207.4 / 12.889 -> 1217.5 / 1202.8 / 12.319; sum 2821.5 -> 2815.4 (without `workshop_total`, whose
history holds one 11.9-million-kWh hour). Kozolec W1: 331.4 / 331.4 / 2.892 -> equal; sum 2185.6 -> 2185.7 (+0.06 W, `pond_filter`,
which now engages). Kozolec W2: 269.8 / 261.7 / 2.220 -> equal; sum equal. No series rises by more than 1 % and 5 W. Newly engaged
(temperature, Home W1): `central_heating` 6.8 -> 10.5 W/degC (t 5.6, 84 days), `attic_ac` cooling 79 -> 88 W/degC (day-ahead 16.1 -> 10.7 W),
`blaz_pc` 6.3, `mansarda` 24 W/degC (t 3.8), `west_blinds` and the bathroom fan at 0.02-0.05 W/degC (t 3.5-6.5): all within 1 W. Nowcast (the
evidence rule replaces 336 h and 3 %): Kozolec `boiler` hour-ahead 169.8 -> 155.9 W, `well_pump` 13.9 -> 13.2 W (now engaged, t 7.1 at 66 days),
Home `talno_gretje` 115.5 -> 91.5 W, `attic_ac` loses its nowcast (t under 3.4) and still improves (15.4 -> 11.5 W): none worse by 1 % and 5 W.
Whole-house p10-p90 coverage 0.759 -> 0.759, 0.676 -> 0.673 (Home W1, W2), 0.486 and 0.664 unchanged (Kozolec).

**Calendars and attached sensors take the same rule (shipped 2026-10-19, the owner's decision, though it fails three cells).** A signal's
on-vs-off contrast (`calendars._contrast_t`: the weighted least squares of `a = e (c1 + c2 on)`, row weights `w / e`, day-clustered `t`) must
clear `t_crit(G - 1, ALPHA)` with on AND off each on `MIN_DAYS` days; `calendars.MIN_ON_HOURS` / `MIN_OFF_HOURS` / `MIN_EXPLAINED` and
`inputs.MIN_HOURS` are gone (the residual share is still computed, and a negative one still refuses). Calendars and the attached sensors
fit the residual leave-one-out, like the temperature term, and when a signal passes detection against the mixture profile but no effect
fit engages against the baseline, the MIXTURE profile stays (the baseline "was for nothing": the attic sensor from 3 Aug, day-ahead
10 -> 136 W; with the gate alone it took Home W1 hour-ahead 1312 -> 1243 W). Against the old gates, with production's signals on, `site`
hour-ahead W / day-ahead W / noon-tomorrow kWh, at `ALPHA` 0.0005, reproduced on the shipped code (2026-10-19): Home W1 1366.3 / 1171.5 / 10.575 ->
1241.0 / 1166.9 / 10.694; Home W2 1465.4 / 1268.0 / 14.660 -> 1221.5 / 1206.1 / 12.319; Kozolec W1 349.8 / 336.8 / 3.010 -> 331.4 / 331.4 / 2.892;
Kozolec W2 288.4 / 255.9 / 2.011 -> 268.5 / 262.1 / 2.215. Sums of day-ahead MAE 4129.2 -> 4029.3, 3006.0 -> 2818.9, 2222.6 -> 2189.3, 2091.1 -> 2035.1.
It fails three cells: Home W1 noon-tomorrow +0.12 kWh (1.1 %), Kozolec W2 day-ahead +6.2 W (2.4 %) and noon-tomorrow +0.20 kWh (the old gates engage
the boiler temperature at Kozolec W2, which happens to help the day-ahead). The owner shipped it regardless: Home's hour-ahead gain
(125 and 243 W) outweighs the small Kozolec loss. Without it, the temperature term on top of the old calendar gates left Home W2 with signals on
1.4 % worse hour-ahead than before INS-15 (1465.4 -> 1486.3 W). `DETECTOR_GENERATION` is not affected (forecast only, nothing stored changes meaning).

**What is running now (INS-17, 2026-10-18).** The `running_markdown` attribute of the Detected loads sensor (`Fleet.running` -> `running_markdown`) lists the GRID's runs, legs started within `MERGE_TOLERANCE_S` being one, each told through `_Open.meter` (the meter whose own step placed it) and that meter's run of it, the `_Open` opened at its `_steps_over` rise. A meter's run never makes a row of its own, so nothing is listed twice; a meter holding one device is a row named after it (or its signature's name); a run the grid placed on no meter, or whose meter run is gone, is the grid's own guess. A meter's load carries the meter's own signature id, the one `Fleet.namable` offers under that meter. Watts are the runs' `open_scale`d watts, as `Detector.active` reports them. The `#id` on a naming row's detail line is what the card links by; HA has no URL into an options-flow step or a single repair (frontend checked 2026-10-12), so the link opens the entry, highlighted. `Fleet.running` is the ONE answer to "is this load on": the named running sensors (`Fleet.active_by_name`), the naming page's "on now" and `signatures[].running` (`Fleet.running_refs`), and the card. The state, `unknown_load_power` and the `active` / `meters.*.active` lists stay phase totals, their `signature`/`name` the total's best match, not "is X running". Do not add a second way to say a load is on (`Detector.active_by_name` / `running_now` were deleted for it). A named load running past its typical length (`elapsed > duration_s x MATCH_DURATION_FACTOR + 60`) is not matched by `_guess` and reads off; a meter's run with no grid run behind it does not turn its sensor on (a placement miss to fix in detection, not a reason for a second source).

### The house forecast and the SOC planning sensors (2026-10-17)

- **House forecast = its parts.** `profile.assemble` (pure; the coordinator and `tests/forecast_bench.py MODE=parts` call the same walk): a meter's own part is its series less its mature parts (`PART_MIN_WEEKS` = 6, swept 0-12), built = own + mature parts' forecasts, the whole's independent fit supplies the band only. The own part is SIGNED (`forecast(signed=True)`, `subtract_all(floor=None)`): Home's grid-derived consumption sits 99 W below its top-level meters, and a remainder clamped at zero put +216 W into the sum. Only the sum is clamped; the remainder sensor stays clamped. `forecast_exclude` ("Not in the house forecast") is never added back and always comes out of its holder's own part.
- **Planning sensors** (`insights/planning.py`, `PlanSensor`; options page "SOC planning", keys in `const.py`): `soc_target`, `soc_minimum`, `daily_excess`, one balance `net_i = w_p(i) x production_scale x production_i - w_c(i) x consumption_scale x use_i`, raw values, last reading kept (`held_since`) when an hour of the horizon is missing. Ported from Kozolec's three template helpers; `plan_all` is what the coordinator and a replay both call. The sun is `_solar_forecast` (the dashboard's forecast integrations, arrays summed) - identical to the Open-Meteo day sensors' `wh_period` over the hours they share (checked 2026-10-17, max difference 0.0 Wh). Planning-only per-device flag `plan_spread_evenly` (the tanks): their energy spread evenly over the horizon, as the helpers did.
- **Room and rate sensors** (what Load Juggler's early start reads): *Room to absorb today* (`room_to_absorb`, kWh) and *Absorption rate* (`absorption_rate`, kW, the 95th percentile of the active hours) each exist for an Energy-dashboard device (`DeviceRoomSensor` / `DeviceRateSensor`, from the coordinator's hourly series, `device_room` / `device_rate`) and for a detected named load (`NamedLoadRoom` / `NamedLoadRate`, both from `Fleet.hourly_by_name`, hour start -> Wh, the last `HOURLY_KEEP_S` = 11 days of its filed runs; `rate_from_wh` divides by 1000). The same rule makes and forgets both of a name's: not for a name that is a dashboard device (its own) or a metered device. An hour's energy is its mean kW; a named load's history is shorter than a device's, so its rate needs five active hours inside 11 days.
- **Base consumption** (`base_consumption`, `PlanSensor` with `which="base"`, W): the LOWER of today's and tomorrow's 10th percentile (`BASE_PERCENTILE`, interpolated, so about the second-lowest hour of a twelve-hour day) of the house forecast's hours (`use`, kWh an hour x 1000) between the sun forecast's first and last lit hour of that day (`daylight`: above 1 % of the day's peak, as `sun_window` reads it; no `sun.sun` entity). Only the daylight hours the horizon still holds count; a day with none (today after dusk) or with no sun forecast is unknown and the other stands alone; both unknown = unavailable. Never held (no `held_since`), enabled with the planning sensors (sun forecast linked and a battery capacity). Load Juggler reads it through an optional entity field. Not the detector's idle `base_load` sensor. **P10, not the minimum (the owner, 2026-10-19):** Kozolec read 8 W for tomorrow 11:00 (today's 173 W), one odd forecast hour. Replayed (profile-only house forecast at 06:00 each day, the sun forecast stood in for by the day's measured solar; `scratchpad` throw-away script): Kozolec 15 Aug - 14 Oct, 61 days, P10 median 437 W (range 227-1,311, p10-p90 274-775) against the old minimum's 192 W (136-280); Home 1 Sep - 14 Oct, 44 days, P10 median 2,643 W (1,831-3,262) against 2,131 W (1,479-2,925). The minimum is mostly the sunrise or sunset shoulder hour; P10 skips it and reads the second-lowest hour of a twelve-hour day, not an outlier (the percentile was not adjusted). The replay's forecast has no temperature or nowcast, so it never produced the 8 W hour; that came from production's signals.

## Storage and generations

`DETECTOR_GENERATION` (in `detection.py`) gates the stored signature library.
**Bump it whenever the meaning of anything persisted changes** — a rebuild is
the only way to undo damage already baked into a library, because merging is
destructive. A fix to the merge rules stops new damage and cannot un-merge
what walked together before.

Adding a field with a default needs no bump; changing what an existing field
means does.

The hours a confident unnamed load keeps past the ten days (`Signature.older`,
a year of them) are not in the library's store but in
`load_insights.<entry>.detection.history`, saved with it (`_persist`) and
read only beside the library it was saved with: a reset or a generation
starts it empty.

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
  the fridges' A/B purity (87 -> 86 %, one run: the two fridges the owner would
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

### To explore or discuss (2026-10-03)

- [ ] **Discuss: multi-step pairing** - a start that ramps in steps (Mansarda's
  ~400 W load: 142 -> 404 -> 465 W over 15 s) is booked at its first step and
  cannot pair with its full-size stop. Agreed meanwhile: a run placed at a
  meter is closed once that meter's total reads below the run's opening size.
- [ ] **Back burner (forecasts, parked 2026-10-03 - detection first): an input's
  "existence".** A sensor input's existence means "hours its history is known",
  so a sensor whose history starts mid-window (the attic living-room
  temperature, from 3 Aug) acts as a date marker. Since linked inputs act only
  on their device (d016699), Attic AC's baseline stands on its July and its
  day-ahead error rose 54 -> 145 W. Proposed: a sensor input fits only its
  states, never its existence; calendars and binary inputs keep both. Also
  parked: the site still forecasts worse with signals than without (1237 vs
  1070 W hour-ahead over 1-3 Oct; on the INS-15 windows, Home W1 1366 vs 1241 W,
  Home W2 1465 vs 1226 W hour-ahead). The first cause is found (2026-10-19): a signal that
  passes detection against the mixture profile replaces the profile by a baseline
  without its on-hours, and the fit of its effects against that baseline can then say
  no - the baseline stays; the attic sensor, from 3 Aug, is the case (day-ahead 10 -> 136 W).
  Keeping the mixture profile when no effect engages (shipped with the evidence gate, 2026-10-19) took Home W1 hour-ahead 1312 -> 1243 W. Backtest: `tests/forecast_bench.py cov`.
- [ ] **Temperature evidence re-opened 2026-10-12, shipped 2026-10-19 (INS-15):** the
  temperature response and the device-state nowcast engage on a day-clustered t-test,
  `ALPHA` 0.0005, `MIN_DAYS` 4; see "Forecast covariates" under the dials. Calendars and
  attached sensors under the same rule shipped the same day on the owner's decision, though it fails three cells (see the dials).

### Confirmed, unfixed

- **The day's repair and the clock.** After 25 October (end of summer time) the day boundary sits an hour off local midnight until the next restart (`main.tz_offset_s` is set once); any 24 h window is a valid day for the cap. A restart no longer shortens the day in progress: its runs and readings are stored.

- **The detector reacts to readings that should not matter - partly fixed**
  (2026-10-04, the noise cards on 77c3159; traced and fixed in exp4, the
  overnight log): dropping one reading in ten thousand, or moving each by up
  to 1 ms, changed Home's library by up to 14 % (circuits 813 -> 828-925
  signatures) and a load's capture by up to 27 points. Two root causes, both
  a schedule counted in readings or steps from the first, so one more or less
  moved it for good: the noise and relative noise re-measured in blocks of 120
  moves (now the median at every reading, and on the clock - `NOISE_WINDOW`,
  `REL_REFRESH_S`), and the size histogram cut every 32 steps (now at every
  step - `EDGE_RECUT`). Traced on Home circuits, a reading in 10,000 dropped:
  steps differing 23,439 -> ~1,000, sessions only one replay filed 146 -> 14
  kWh, filed into another signature 36.6 -> 3.6 kWh. Left: (1) a mark counted
  in cadences falls on a beat meter's readings (the 3EMs write on whole
  seconds; three cadences of 5.003 s = 15.009 s against readings 15.002-15.012
  s after a change) - a half or a tenth of a cadence's slack fixes it under
  jitter and costs Home hidden 2-5 points of impurity, not kept
  (`exp4-fragility-beat-slack.patch`); (2) the noise learns only while no run
  is open, so a run one replay keeps open freezes it at another value for
  hours; (3) the library's caps (200 signatures, 150 edges a group) evict by a
  ranking a small difference reorders, and each eviction refounds a load
  (without the cap, sessions in another signature 7.5 -> 3.7 kWh). The start-of-window
  copies the replay fed as readings are dropped since 0005337.

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
The owner asked for it on 2026-09-22 and it was not built then (see "When the owner
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
  under no sub-meter, 34 -> 39 in its main cluster. The owner asked whether putting
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

**A fall to the idle floor closes what was open (2026-10-04,
`PhaseState._floor_stop`).** Until then such a fall DROPPED every run still
open, with no session at all - nearly all of the card's "starts never
closed": the grid's detector, SLICE=6, Home 1,212 / 1,183 / 907 runs (16.9 /
15.2 / 12.9 kWh at their start size) hidden / circuits / fed, Kozolec 40 / 12,
home-hisa 399 / 388, home-mansarda 73 / 71, the third site 161 / 169; the meters' own
detectors, fed, Home 649 runs and 81 kWh (the EVBox's tapering charges 41, the
Hiša 3EM 13, Susilna's 20-hour dehumidifier runs 12, Mansarda 11), Kozolec 60
/ 3.4, the third site's go-e 17 / 5.8. Mostly short and small (median 61 W, 42 s at
Home), a few hours long. Now the runs the fall's unpaired part covers close at
their share of it, the rest at their own size, as the conservation stop
closes a run the reading cannot carry; "starts never closed" is 0.0 % at every
site. Card against 9aa31bf, the effect in three paired cards (the plain one
and NOOP=drop:10000:1 and :2 on both sides): Home's capture up in every mode
and replicate (devices hidden 33.9 / 16.7 -> 36.1 / 16.3, circuits 44.0 / 12.9
-> 49.6 / 10.1, fed 67.3 / 7.6 -> 70.9 / 6.6; partition 49.8 / 16.5 -> 51.4 /
16.4, 62.1 / 9.0 -> 65.1 / 7.0, 73.4 / 6.9 -> 77.2 / 6.8), its impurity flat on
average; the third site fed the go-e 83.4 / 4.3 -> 92.8 / 7.7 (+3.4 impurity in all
three, the Rest's 13.6 -> 6.1); Kozolec +0.1 impurity; kiln and mat unchanged;
invariance 0. Closing only the covered runs (298 of Home's 1,212 hidden) moved
Home's hidden card nowhere. The washer-dryer's 03:56 and 05:47 cycles of
24 Sep (hidden) are filed, at 282 W for 548 s and 279 W for 55 s. Still open:
a run its stop went unseen hours before the floor is booked to the floor
(Mansarda's own detector, 09-29 21:14, a 1,439 W run 107 min, 2.6 kWh) - a
guard benched 2026-10-04 (the fall must carry the run, else it ends at the
first declared level under half its size) moved Home's card nowhere or a
little down in three paired cards, and also cut tapers (EV charges); its
stop-sized form (a fall of half its size reached that level) within 0.4
points in two paired cards - left out, patch and numbers in the overnight log
(exp6); the idle floor itself moves where a fall finds nothing
open (Home A's base 1,818 W on 09-28 evening), and a meter's own detector can
seed it wrong: Susilna's plug seeded 301 W with the dehumidifier on, so its
floor stops fire on 10 W wobbles of the 270 W run (its own runs hold 35 % of
its energy).

**A run its one-device meter owns is booked at what the meter drew
(2026-10-04, `Fleet._meter_wh`).** A session's energy is its levels times
their lengths, and a one-level run is booked at the mean of its start and stop
where they agree, else the smaller: Home's EVBox began a charge at 1,262 W -
its ramp's first plateau - and drew 3.48 kW for 8.5 hours (09-25 18:57), booked
10.7 of the 29.3 kWh it drew. A run a meter holding one device owns
(`_Open.meter`, `holds_one_device`) is now booked (`Session.wh`, per phase,
stored; `energy_wh` and the hourly statistics read it) at the meter's declared
level against just before its rise, in the grid's terms by the meter's gain
(8637651; it was the run's start scaled by that level against the rise).
Declared levels (each `declared` step carries the
level it stepped to), not readings: a spike never became a level (exp2's raw
integral booked an 88 W, 13 s run at 1,040 W). A circuit's change is every
load in it, so one-device meters only. Where the run ends does not change (a
taper below its size less the pairing tolerance still ends it, `_ended_by`).
Card against d707840 - only fed moves, hidden and circuits identical; the
same moves in two paired NOOP replicates: Home fed devices 70.9 / 6.6 ->
81.5 / 6.2 (the EVBox 74.3 / 6.9 -> 96.0 / 6.2, the hidrofor 73.8 / 11.6 ->
76.6 / 13.2, the office 58.1 / 12.8 -> 54.4 / 11.5), partition 77.2 / 6.8 ->
82.0 / 6.6; Kozolec fed 95.0 / 3.2 -> 94.8 / 2.7 (its water pump 80.7 -> 96.6 %
captured). The grid's runs of 1 kW or more the EVBox switched on with, fed:
86.2 kWh booked against the 113.8 the meter drew over them -> 113.3. Not
reached: runs no meter owns (hidden, circuits) and meters holding several
(bench.PROD_SINGLE does not declare the third site's go-e; declared, a replay's fed
devices 92.9 / 8.0 -> 98.7 / 9.0). Following a run alone on its phase at the
phase's level above its floor was benched and left out: the grid's floor holds
cycling loads, and Home's hidden Hiša lost right energy as fast as wrong
(73.9 / 19.2 -> 63.9 / 11.3).

**Every run a one-device meter owns at a moment shares what it draws (exp9,
8637651), and a step the meter's readings show is its run's (ebfe46f).**
Two runs a meter owned at once - a charge's start and a later rise of it the
meter's step was all of, or a 10-68 W wobble - were each booked at the
meter's whole level: the EVBox's 09-25 charge 39.6 kWh of owned runs for 29.8
drawn (the fixed scorer, 9b7c69c, counts the second as wrong). The meter's
change since just before the LOWEST of the runs it owns at a moment (open, or
closed in the last day, `Fleet._owned_runs`) is split among them by their
starts' sizes; the lowest, not the oldest, since a wobble opened at 3.5 kW and
left open read a restart from its own base (Kozolec 09-28 15:18, 65 Wh of 3.1
kWh). A one-reading dip is under a meter's sustain: its detector declares
nothing, the grid's faster cadence declares both edges, and the EVBox's 22
dips in that charge each closed a run by size and opened a 2.15 kW run no
meter owned (~14 kWh beside it). `Fleet._meter_read`: a grid step a one-device
meter's readings show - two of them over the step's span and the meter's
latency, the step's size apart in the grid's terms - while a run it owns is
on there before and after it is that run's: a fall no stop, a rise no run.
Fed, against the night's baseline under the fixed scorer: Home devices 61.2 /
9.2 -> 64.1 / 8.6 (the EVBox 65.3 / 8.8 -> 71.8 / 8.9), Kozolec 91.9 / 5.5 ->
91.6 / 1.6 (the EVSE 98.2 / 6.8 -> 97.4 / 1.0); the 09-25 charge booked 51.7
-> 30.5 kWh for 29.8. Left as patches in the overnight log (Exp 9): a
three-phase event whose legs' sum a one-device meter's total rise was all of
owned by it, leg by leg (#43 - the EVBox +1.6 / -0.3 but the hidrofor's 2-leg
starts taken too, its impurity 13.6 -> 27.5 %; a ramp's first plateaus, 09-29
20:24's 8.75 kW of legs against the meter's 10.46, are outside the tolerance),
and exp8's "on until the meter shows it off" on top (neutral at Home now, +0.3
impurity at Kozolec).

**A load inside a meter holding several devices is that meter's (2026-10-03).**
The owner, naming Mansarda's fridge and freezer: "if a same load is detected by
both meters, shouldn't the reading collapse into a single device anyway?" A
parent's run (the grid's, or a meter's others hang under) that a child meter
holding several devices filed a session of - same moment, same size, its own
phase (`_session_pairs`) - is that session's signature's (`Session.owner`,
`Fleet._file_as`): the parent library grows no copy of it. A one-device
meter's word stays identity, as before. Two exceptions, both so a name stays
where it was given: the run's device files into a parent signature the user
named (`Detector.device_signature`), or a name a reset carries looks like the
run (`Detector.awaited`) - Home's Peč za glino and Inkubator sit in Hiša,
Kompresor in the Delavnica, all named on the grid; moving a name is the owner's
call. A name lives on any detector's signature, (meter, id) with the grid
under "" (`Fleet.names`, `energy_by_name`, `active_by_name`, `rename`,
`adopt`, `name_descriptors` / `carry_names` - a reset hands a meter's name
back to that meter's detector only); its energy, hours and running are its
own meter's sessions', its entities per NAME as ever. Naming
(`Fleet.namable`): each meter holding several devices offers its own
signatures under its name, the grid its own under "main"; a signature a
read meter below saw is not offered where it was seen (that meter offers its
own, or is the device), nor a meter's whose twin the grid filed under a name.
Rows show the meter's own channel letters, as the detected-loads sensor's
`meters` attribute always did (Mansarda's c is the grid's A). Measured, ten
days 23 Sep - 2 Oct, card against the same code without it: invariance 0
everywhere; hidden and Kozolec identical; Home fed devices 61.6 / 12.7 ->
61.1 / 13.1 (Blaževa Soba impurity 24.2 -> 18.5, the office 43.4 / 13.4 ->
41.7 / 10.4, the workshop boiler 9.2 -> 6.2; Susilna 12.3 -> 14.8), partition
68.7 / 7.6 either way (Hiša 86.9 / 7.3 -> 86.3 / 5.5, Mansarda 63.4 / 3.8 ->
59.3 / 3.5, Delavnica 64.8 / 6.0 -> 66.4 / 7.2); circuits devices 28.5 / 16.2
-> 28.5 / 15.5, partition 58.4 / 8.7 -> 58.3 / 7.8; home-hisa fed 6.7 / 7.9
-> 10.9 / 5.0. Fed, 3,768 grid runs (96 kWh) became the meters' own, the grid
library 255 -> 235 signatures; the grid's signatures placed at a meter
holding several (Hiša, Mansarda, and the four plugs the bench's ten days
guess so) 78 (45 namable, 139 kWh) -> 47 (17, 41.5 kWh) - Mansarda's 65 W
fragment (206 runs; Home's live #929) folds into the freezer, its 50 W one
into the fridge. Those left are not shown: 69 % of their runs have a session
of the circuit's own within 15 s, 58 % of their energy one of their size -
the same loads, seen worse where the pairing failed. Open, a guess not
traced: a grid run the circuit filed no session of no longer files with the
ones it did (they no longer vote for its device's signature) - likely most
of Mansarda's 4 points of capture and Susilna's 2.5 of impurity. A device
whose runs a meter's signature took taking the rest too would be a second
mechanism; not built.

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
up (2026-09-23).

**A pass is only a pause (2026-10-02).** Production reads the recorder six
hours at a time while it catches up and a minute at a time after, and until
2026-10-02 a pass's end decided what later readings could have decided
otherwise: a change still pending was confirmed there, a start still in its
event window became an event alone, runs waiting for a partner leg were filed
in a batch, the meters below the grid were read a whole pass ahead of it, and
the fleet filed, placed and voted once a pass - so the same history gave
another library at every slicing (the scorecard baseline 500c275, fed: Home
9,790 of 15,813 sessions differed between 6-hour and 1-hour passes, 1,166
between 6-hour passes and live minutes, 14,292 against one call; Kozolec 192 /
25 / 335; the hidrofor's old 219 -> 203 at one-minute slices was this). Now
every meter is read on ONE clock (`Fleet.process` / `_run`): a meter's reading
at its own time, the grid's once the clock is the horizon past it, and every
deadline - a silence confirming a level (`PhaseState.silence_due`), an event
window (`Detector.event_wait`), the held tail, a house session's wait for its
meters (`Fleet._ready_at`), a meter session's vote (`_vote_blocked`) - fires
when its moment comes on that clock, before the next reading or in the next
pass; what is not due when a pass ends is carried, and stored (`pending_rises`,
the timed `unfiled` / `pending_main` / `unvoted` lists). Sessions are filed when
they become decidable, ties by start - one order the data fixes (the owner) - and
device merges run after each filing, never into a signature a run was kept out
of (`Signature.apart`). The horizon is the slowest meter's DECLARING lag against
the grid, learned from the steps the two share where each is the only one of
its kind within `LAG_REACH_S` on both meters (`Fleet._learn_lags`, p95 of the
last 200, believed from 20), at least `EDGE_LAG_REACH_S` with inputs fed, never
past `METER_WAIT_CAP_S` (300 s, the settings page's *Wait for slower meters*);
a meter's own levels are confirmed over three of its repeat intervals or its
learned report lag, whichever is longer - over the lag alone, Home's fed
impurity doubled (14.9 -> 24.7 %: half-caught readings founded levels, the
hidrofor's runs split over two signatures). Only a meter that has shared a
step with the grid sets the horizon (the owner: one that never does has nothing
the grid waits for - Home's Server UPS, a 60 s heartbeat, had held Home at
the 300 s cap at twice its latency); a meter with a step shared but fewer
than 20 sets it at twice its latency until its lag is believed - only the
believed ones setting it left Kozolec's first hours at no horizon and cost
its pond EVSE 99 -> 88 % captured. Ten-day horizons: Kozolec ~60 s (the pond
EVSE's 58 s declaring lag; the boiler reports within 4 s and declares within
36); Home ~190 s, set by the office plug's p95 declaring lag - suspect: a
wandering computer load matched "one of a kind" up to 109 s off (its report
p95), Mansarda's 149 s the same way; bounding a match by the two meters'
latencies is the proposal. Meters with fewer than 20 shared steps in ten days:
Home's Attic AC, Blaževa Soba, Polnilnica, Server UPS, Susilna, Workshop boiler
and charger; Kozolec's bug lamp, car charger, pastir, pond, washing machine and
well pump (19).
Result (card `64da1b1-dirty-20261002T173608Z`, with the four fixes of the
same day - a meter step the other way is netted into a grid step only where
the two spans truly overlap, a meter's stop ends only the run its own rise
started in size too, a one-level run is booked at the mean of start and stop
only where they agree and at the smaller otherwise, and a run a meter's own
step started is that meter's until the meter shows it stopped
(`PhaseState.owned`, `Fleet._meter_on` / `_meter_started` / `_meter_level`):
invariance 0 sessions / 0 kWh at every site, mode and pair of slicings - one
call, SLICE=6, SLICE=1 and LIVE=1 file the same sessions into the same
signatures, so one slicing is enough for a score and a session the
`invariance` command lists is a fault. Scorecard capture / impurity against
500c275 (SLICE=6): Home devices hidden 24.8 / 33.0 -> 22.6 / 26.9, circuits
28.8 / 22.8 -> 30.8 / 15.6, fed 25.5 / 14.9 -> 45.1 / 14.6 (Susilna 0.3 / 23.5
-> 44.9 / 11.3: its 20-hour runs survive the grid's closers now that its plug
owns them; the hidrofor 78.5 / 18.1 -> 72.7 / 17.9, the workshop boiler 85.8 /
12.7 -> 81.8 / 8.3, the office plug 15.2 / 13.6 -> 27.0 / 24.6); Home partition
hidden 50.8 / 13.0 -> 57.0 / 8.8, circuits 57.6 / 9.5 -> 65.7 / 5.7, fed 54.4 /
8.9 -> 69.6 / 5.4 (the scorer's `_started_with` now reads a meter's dip in the
ten seconds before a start, so a 2.15 kW load that drops out for six seconds
and restarts is the Delavnica's, 8.8 kWh); Kozolec devices hidden 68.0 / 9.0 ->
69.9 / 7.0, fed 86.9 / 4.6 -> 96.1 / 3.2 (the pond EVSE 91.9 / 4.3 -> 98.7 / 1.5,
the IR panel 83.0 / 6.6 -> 82.5 / 6.3, the water pump 88.4 / 33.3 -> 83.2 /
18.6; the car charger reads 128.9 % captured - two of its sessions overlap
and both are credited its draw, a scorer quirk); home-hisa hidden 8.7 / 31.7 ->
7.2 / 9.9, fed 2.4 / 31.5 -> 22.3 / 36.7; home-mansarda hidden 24.3 / 15.8 ->
27.9 / 11.2, fed 30.8 / 6.8 -> 32.3 / 6.4. A meter's rise inside a bigger start
takes its part since 2026-10-02 evening (`_split_rise`, `_meter_started`: the
plug's 265 W and a fan's 48 W switching on in one reading - the owner: "a separate
fan in all likelihood"), a leg of a multi-phase event can be a total-only
meter's, and a meter's own settling fall or a reading that dipped under the
run does not end it. Against the scorer-fixed card `0928354-20261002T175951Z`
(a meter's energy credited once across overlapping runs): Home fed devices
42.8 / 15.3 -> 51.5 / 14.0 (Susilna 44.9 / 11.3 -> 57.0 / 6.7, the office plug
23.1 / 27.9 -> 36.2 / 22.4, the hidrofor 65.3 / 20.1 -> 69.9 / 22.9), partition
67.6 / 7.6 -> 70.4 / 9.8; circuits 28.2 / 16.5 -> 29.2 / 17.8; Kozolec fed 93.0 /
5.4 -> 93.8 / 6.3; home-hisa fed 2.5 / 15.5 -> 8.4 / 20.7; home-mansarda fed
32.3 / 6.4 -> 46.8 / 22.5 (the office plug's suspect lags again). Still cut:
Susilna's 09-24 run - its 313 W start stepped down 30 W within a minute, then
multi-closed with the fan.
Replays cost more (Home ~160 s against 85-95 s:
the one-clock loop). A restart is only a pause too - see *A restart is only a
pause* below. The repeat-interval estimator stays: a meter's own transient
rejection needs a multiple of it, and the event, corroboration and crowding
windows and the sample counts read it.

**A restart is only a pause (2026-10-19, INS-13).** The same history read in
passes and stored through JSON and read back after every pass (the bench's
`RESTART=1`, with the store's rounding off, `TRIM=0`, so what is asked is
whether a restart LOSES state; the rounding is its own cost, `TRIM` on) files
the same sessions into the same signatures, at every site and mode, on the
card (`SLICINGS=all`, pairs `6~6R` and `L1~L1R`): 0 sessions differ, Home fed
2 of 29,226 (0.0 kWh). Before: Home circuits 6,345 sessions / 146 kWh, fed
2,853 / 84 kWh, hidden 37 / 0.7; Kozolec fed 5 / 3.0 kWh. What the store had
lost, found by running a held Fleet and one stored and read back after every
pass side by side and diffing their stored dicts and their memory (`LOCKSTEP`,
a throw-away patch of the replay, not kept): the declared steps and edges
beyond the last two hours (the votes of a meter first read after a restart
reach back to its first reading, so nothing is trimmed: `declared` is capped
at 4,000 a phase, `edge_at` trims itself), `step_diffs`, the open run's
`now` / `q` / `short0`, the day's runs, readings and repair mark, the size
densities and their segments, the phase each pair was learned on (its start
cluster may be gone: `_pair_index` rebuilt `?`, which moved a pair's chance of
being accepted), the devices `_merge_devices` still had to judge again (a full
walk after a restart merged what the pass-by-pass one had left apart:
`rejudge`, `rejudge_all`), the grid's voltage beside the steps, and the house's
runs the day keeps being the very ones waiting for a meter's answer (one object
in memory, two copies in the store: `Fleet.from_dict` joins them). The first
pass after a restart also reads its inputs and switches back as far as the
oldest run or session it still has to place (`detection.py`
`_read_inputs`, `_inputs_primed`), and each pass waits for the recorder to
write what it has been given before it reads (`RECORDER_FLUSH_S`, 30 s: a pass
reading rows the recorder had not yet committed was the one fault the card
could not show). `DERIVED` metadata on a field says it is a cache or a hook set
each pass; `test_every_field_is_persisted_or_derived` fails on any other
field the store leaves out, `test_a_restart_leaves_the_stored_state_as_it_was`
compares the whole stored state of a held and a restarted run. Cost: Home's
snapshot is now about 21 MB at the end of the ten days (JSON, default
separators) against about 5 MB before: the day's readings for the repair
(`day_rows` 3.7 MB at the grid, more per meter), the meters' own detectors (6.6 MB), the
meters' readings (`sub_rows`, 3.0 MB), the voltage (1.6 MB) and the day's runs
(1.3 MB). Written at most hourly (`SAVE_MAX_INTERVAL_S`). Open for the owner:
whether that is acceptable, or the day's readings are kept coarser.

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

**...and the two are not read at one moment** (exp12, 2026-10-04). Polled
together, the M1 reports part of each change of the inverter's a poll later:
least squares of its moves on the inverter's over ten days, the third site 66 % at
once, 22 % at the next 5 s poll, 8 % the poll before; Home 77-80 % at once,
17-19 % 2 s later. So every cloud shows in the house as a load for a reading,
and a ramp holds a phantom while it lasts: by day where the inverter moved
The third site's house moved 13 W a reading (4.7 where it did not; Home 19-23 against
5-14), and the idle noise learned from those moves sat at 12.5-14.5 W by day
against 10 at night (Home's A 14.5 / 10). Of the house steps declared by day,
the grid meter itself moved less than half of 11 % at the third site (613 of 5,651)
and 3.7 % at Home (1,222 of 32,899), the inverter the rest; at night 8 and 11.
Kept: the detector is handed what the inverters add to each phase of a house
built from them (`detection.array_in_house`, the bench's `|array` series) and
learns neither its idle noise, its relative noise nor its floor from a reading
the array moved under by the noise or more. Card, SLICE=6, capture / impurity,
three paired draws (plain, drop:10000:1, :2), mean move [band]: Home fed
devices +2.2 / -1.8 [2.2 / 1.0], partition +1.1 / -1.6 [0.8 / 0.8], hidden
+1.8 / -0.5 and +1.4 / -0.2 [1.7 / 2.5, 1.0 / 1.9], circuits +1.9 / -0.4 and
+2.0 / +0.7; the third site's charger -2.0 / -0.8 hidden, -2.1 / +0.6 fed [0.9 / 1.9,
0.7 / 1.5] - a trade, the third site's capture lost in every draw, its impurity
inside the band; Kozolec and the circuit sites read no inverter. Tried and dropped (patch
`exp12-dials.patch`): reading the inverter late by its measured share of a
poll (`_read_late`, 0.30 at the third site, 0.22 at Home; the house's moves fall by a
third, the day noise to the night's, but the card moves inside its band); a
house step declared only where the grid meter moved its way (a sun ramp under
a real step looks the same - the third site +3.5 to +4.5 impurity).

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

- **INS-8 (b1) `LEFTOVER_PREFER_STARTED` and (b2) `LEFTOVER_NEEDS_RISE`** (benched 2026-10-19, dropped by the
  owner the same day; the dials, the `_leftover` plumbing and the `meter_rose` hook are removed). b1: a split
  leftover never joins the start group of a meter whose own run started in the same event; b2: it joins by
  likelihood only when that meter itself stepped over its span. No clear gain anywhere: Home fed capture +0.5
  at best, Kozolec, the third site and the pump bench unchanged, Home circuits / fed impurity moved 1-2 points
  both ways. The case they targeted (Home 27 Sep 13:01, an 858 W split leftover joining the pump meter's start
  group and staying open 31 min) no longer reproduces at the current code in any variant. The idea is not
  dead (the tested implementation did not help); a similar case turning up is the reason to revisit.
- **Per weekday-and-hour slot centring of the temperature features** (INS-15, 2026-10-19; the
  plan's choice, with the leave-one-out residual; bench dial `SLOT_CENTRE=1`). Centring each
  slot on its own twelve weeks of weather adds a dozen-sample mean of noise to every slot. At
  `ALPHA` 0.0005, signals off, Home W2 `site` hour-ahead 1226.0 -> 1228.7 W (worse, 0.2 %;
  day-ahead 1207.4 -> 1197.2 and noon-tomorrow 12.889 -> 12.715 better); at 0.005, 1226.0 -> 1255.1 and
  Home W1 1241.0 -> 1243.4. Centring on the hour of day alone (what shipped) holds on every window. A global
  centre with the leave-one-out residual (`SLOT_CENTRE=3`) equals the gate alone on the whole house but
  never lets a young series engage (bee room heater: no engagement, against 7.6 days with the hour-of-day centre).
- **`ALPHA` 0.005 and 0.05** (temperature, see the dials table): the whole house's own weak response engages at
  t about 3 and costs 1-2 % hour-ahead at Home. And the test is not sharper than its nominal
  level in young windows: at G = 4 to 14 day blocks with a smooth temperature drift and AR(1) 0.7 day offsets,
  150 simulated series falsely engaged 0.7 % (4 and 5 days), 2.0 % (7), 2.7 % (14), 0 % (28, 56) at 0.0005 - above
  the nominal 0.05 %, because few clusters make CR1 sloppy (at 0.005 and iid days: 4-9 %). A delete-one-day
  jackknife variance joining the maximum (`covariates.JACKKNIFE`, simulated only) took 5.6 -> 4.2 % at G = 5: not enough to carry.
- **All-phase events read off the raw house readings** (2026-09-30, the owner's window
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
  The owner's objection (2026-09-30): the first comparison changed two things at
  once - the detector's steps carry a sustain check, the raw vectors did not.
  Re-run with each component required to hold across the next K readings
  (SUSTAIN=1): V 0.325 (K 2) and 0.374 (K 3) against 0.320 / 0.375 without,
  the detector's groups 0.477 / 0.479 on the same events. The sustain check
  is not the difference. The owner's second objection: "multi-phase" counted any
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
  matters, as the owner said (a real multi-phase load shows on its other phases at
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
  has been measured for days), never one that rides the daily swing. Shipped 2026-10-04 as a unit that only ever moves DOWN (see `EDGE_NOISE_SHARE`).
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
  two elements to neutral would read 1.0 on both. The owner expected phase to
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
  (The owner: "a couple impossible readings before we decide", 2026-09-29), checked
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
  newest-bigger-load step-down guess (`STEP_DOWN_GUESS`): the owner, 2026-09-29, run
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
  what the owner wants), `PAIR_HOME` (identical: a device's home decides first),
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
  over +-2 readings and widen only while no other phase triggers (the owner).
- **Edge grouping cut sized by each edge's own error** (2026-10-17; offline regroups of the 82bd24b
  replays, Home 10-03..12 / Kozolec 10-02..11, truth card first). The size density smoothed by every
  edge's own pe instead of the key's median (peak- or area-normalised kernels, x1 and x2, floored at the
  median or not), the angle density by each edge's own angle error or the key's median, with or without
  the segment's: the truths always lose what fine cuts hold - Home kettle 24 -> 33-83 %, Workshop boiler
  39 -> 59-84 %, E165 1.4 -> 5.6-11 %, Susilna 63 -> 69-74 % (MW 83 % with the key's angle error) -
  and the large steps barely stop fragmenting: Home >= 1 kW groups of one 1,257 -> 1,141-1,161 at the
  same kernel, 716-875 only at twice it. Cause: of those 1,257, 333 are alone after the size cut
  already and the rest are cut off by the angle (error ~0.2 deg at 1 kW); most sit within 2 combined
  errors of a group but not 1. Per-edge angle kernels alone (size as before) are within noise of the
  committed cut (E165 1.4 -> 2.8, Workshop 39 -> 43, dup 20.5 -> 19.8, singles 1,249): not kept. The
  fragmentation is the price of the truths' fine cut; singles are under-reporting, not mixing.
- **Merge pass with the groups' spreads in the error** (2026-10-15 and 17): kettle 24 -> 83 % dominated
  (all of it unlabelled Mansarda B steps: 0 % with unlabelled edges not counted, as for every variant),
  Workshop boiler 39 -> 57 %; the edges' errors alone (kept) 39 / 45.5 %.
  **The kettle merge is allowed and kept** (INS-6; the owner, 2026-10-17: "that merging is probably ok"),
  re-benched 2026-10-19 on the ten days in `data/history` with the merge off (a temporary dial) and on: the
  kettle reads 16.1 % dominated in both and 0 % with unlabelled Mansarda B edges not counted, so on today's
  days it merges nothing the truth minds; the merge's measured effect is elsewhere. Edge card, off -> on: Home
  groups 3,353 -> 3,074, duplicates 10.8 -> 7.5 %, unsure 7.3 -> 4.6, mixed 8.6 -> 8.2, mirror 96.4 -> 96.0,
  tightness 4.51 -> 4.60, >= 1 kW singles 1,323 -> 1,209; Kozolec groups 444 -> 413, duplicates 24.4 -> 21.1,
  unsure 10.5 -> 7.3, mixed 20.2 -> 20.1, mirror 89.9 -> 89.1. Truth card: Workshop boiler 51.4 -> 58.1 (the
  price), Pond EVSE 16.2 -> 18.8, IR panel 4.6 -> 5.7, E100 3.5 -> 1.8, the rest within a point.
- **A wider merge tolerance** (INS-5, 2026-10-19; `_merge` at 1.5 and 2 combined errors instead of 1, a
  temporary `EDGE_MERGE_D`; the ten days in `data/history` as of today; the sized-on-each-
  edge's-own-error merge itself is 4d46d50, already kept). Edge card at 2 (1 -> 2): Home groups 3,074 ->
  2,448, duplicates 7.5 -> 3.0 %, >= 1 kW singles 1,209 -> 907, mixed 8.2 -> 8.1, mirror 96.0 -> 95.2,
  unsure 4.6 -> 3.0, tightness 4.60 -> 4.80; Kozolec groups 413 -> 304, duplicates 21.1 -> 11.0, unsure
  7.3 -> 3.0 but mixed 20.1 -> 25.4, mirror 89.1 -> 78.7, tightness 1.12 -> 1.31. Truth card (dominated %,
  1 -> 1.5 / 2): oven E165 2.8 -> 5.6 / 4.2, E265 2.1 -> 2.1 / 0.0, E100 and MW unchanged; Workshop boiler
  58.1 -> 62.2 / 67.6, Polnilnica 76 -> 76 / 78, Susilna 27.8 -> 26.6 / 28.2; Kozolec Car charger 35 -> 45 / 45,
  Pond EVSE 18.8 -> 20.0 / 21.2; kettle 16.1 -> 33.9 / 33.9 (0 % with unlabelled edges not counted at all
  three). The truths gain nowhere and lose on five classes; the card's gain is fragmentation cured (Home's
  >= 1 kW singles fall by a quarter, the groups by a fifth), against Kozolec mixed +5 and mirror -10. Not
  shipped: no truth gain, and Kozolec's own proxies get worse. 1 combined error stays.

## Working rules behind the dials

- **Explore boldly, do not fine-tune** (2026-09-29, redesign work). Build the whole mechanism end to end on a branch, commit freely, bench it, and iterate before judging. A regression means dig further, not revert: stopping at the first dip keeps the work at the old pipeline's local maximum. Production changes still get backups and identical-or-better benches.
- **Fewest special rules** (2026-09-30). Remove overly specific rules whose effect on the end result is within bench noise (about 0.5 purity, a few kiln or pump runs, a few mat hours), with their dial and tests. Prefer a general mechanism (edge library, devices, input keys) over a rule for one load. When adding a fix, first ask whether an existing general mechanism should carry it.
- **One accurate detection over two unreliable ones** (2026-09-29). When two look-alike devices cannot be told apart reliably (Kozolec's two fridges), one accurate combined detection beats two noisy halves. Do not spend effort keeping look-alikes apart unless that also improves their combined accuracy.
- **Under-report over over-report** (2026-09-30): "Capturing 80% of actual energy a device (group) consumed is much preferred over assigning 20% over." Energy credited to a named load that it never drew is misinformation on the Energy dashboard and in forecasts. Weigh precision above recall; prefer closing runs early, gating, and keeping look-alikes out over catching every run.
- **Imbalance alone is not two loads** (2026-09-22). An unbalanced multi-phase signature may be one machine: a three-phase motor plus single-phase parts. Never split on imbalance alone. Rank the evidence: how reliably the legs co-occur first (one contactor switches them together), size second (real multi-phase loads here are large and balanced), imbalance last.
- **Old names are not worth chasing** (2026-09-30). After a library reset, names that do not find their load again are not worth engineering for, and the owner would drop them. Do not add matching logic to re-attach carried names; if the orphan-carrying mechanism (`orphan_names`, `_reclaim`, `carry_names`, `name_descriptors`) gets in the way, propose deleting it.
- **A load's power factor is its movement**, not the meter's factor (2026-09-17): how far its watts and its VAr moved when it switched. Ratios are not additive; watts and VAr are.
