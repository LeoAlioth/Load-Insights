# Steps and claims - bench results per stage (2026-10-23)

Plan: `docs/steps-and-claims-plan.md`. Nothing decided between stages; the owner reads this when all is built.
Scorer: `scratchpad/dw/dw_bench.py` on Home 13-22 Oct, `SUBS=prod HOUSE=prod`, the five AEG states fed,
`INPUT_RARE_SHARE=1.1`; only sessions the grid meter or Mansarda filed, 1.2 kW or more.

## Scorer fixes made first (the baseline moved twice)
- Sessions from other meters (Susilna's 1.4 kW heater, same grid phase A) no longer count. (first fix)
- A session's signature is read through the sub-meter's merge map (`_current`); before, a merged
  signature counted as several. (second fix) - every number below is on the fixed scorer.

## Baseline (no change, fixed scorer)
| | bursts | hit | sessions | signatures | best signature | purity |
|---|---|---|---|---|---|---|
| dishwasher | 9 | 8 | 17 | 13 | Mansarda#22 (3) | 0.23 |
| Sock Eater | 50 | 34 | 50 | 30 | Mansarda#777 (9) | 1.00 |

`Mansarda#22`: 24 runs, 1879 W on C, 16 with the Sock Eater Running / 8 Off; holds 3 dishwasher, 8 Sock
Eater, 1 oven, 1 other. `Mansarda#847` (2 dishwasher runs) was born in Sock Eater=Off: the state split
works only for runs filed after the input counts matured (INPUT_MIN_RUNS); the earlier runs stay mixed in #22.
The 22 Oct 10:40 burst is four sessions: the grid's 607 s on C and 65 s on C are the second load that started
2 s earlier, Mansarda#847's 735 s on A is the dishwasher, a 283 s on A at 10:48 is its next heater pulse.

## Stage 1 - system phases X, Y, Z (display only; keys stay a/b/c)
Golden replay (`tests/relabel_check.py`): Home 24,435 sessions, Kozolec 1,839 - 0 differ. No bench numbers
change by construction.

## Stage 2 - claims
- `insights/steps.py` + `tests/test_steps.py` (7 tests, pass): the pure inside-out claiming.
- `tests/steps_bench.py home`: of 1,952 grid edges >= 300 W, Hiša claims 8.8 %, Mansarda 8.7 %, the
  Hidrofor 4.1 %, Susilna 0.3 %, the workshop boiler 0.2 %; 77.9 % stay the grid's (unclaimed).
- Falls placed at their meter (`PLACE_FALLS=1`, dial): dishwasher unchanged (8/9, 17 sessions, 15 signatures
  on the first-fix scorer); Sock Eater 33/50 hit (34 before), 35 signatures (33). No gain.

## Stage 3 - duration
`MATCH_DURATION_FACTOR` 6 and 12: identical to 3 on every figure. Duration does not fragment these loads.

## Stage 3 - device states at the step
- Falls placed (`PLACE_FALLS=1`, fixed scorer): dishwasher 8/9, 17 sessions, 13 signatures, best Mansarda#22
  purity 0.25; Sock Eater 33/50, 32 signatures. No gain either way.
- All five states joined into ONE key per step (the earlier `STATE_TAG=2`): Mansarda#22 still 3 dw / 8 se.
  Cause found: `_by_density` keyed a step by the whole tuple of its inputs' states, and no exact five-way
  combination piled up above chance - 43 rises of 1.9 kW sat in the plain group, three state-keyed groups of
  3 steps beside it.
- Per-input state keys (`_by_state`): each input's state judged on its own in the size segment, the one
  furthest above chance keys the group. Golden replay with the keys off: Kozolec 1,839 / 0 differ.
  Result: see below.

## Scorer fix 3: grid phase a only (the dishwasher is Mansarda C = grid A; the 1.3 kW load on grid C
beside it is another). Every number from here is on THIS scorer.

| dials | dw hit | dw sessions / sigs | dw best purity | se hit | se sessions / sigs |
|---|---|---|---|---|---|
| none | 8/9 | 12 / 8 | 0.23 | 31/50 | 37 / 18 |
| STATE_TAG=2 (per input) | 7/9 | 11 / 8 | 0.50 | 31/50 | 38 / 23 |
| STATE_TAG=2 PLACE_FALLS=1 | 8/9 | 12 / 8 | 0.50 | 30/50 | 37 / 23 |

## Stage 2 - why the grid keeps dishwasher runs Mansarda saw (`unowned2.py`, every detector's `_file` hooked)
Mansarda filed 3,818 sessions, 191 of 1.2 kW+. Of the dishwasher-window grid runs on phase a, 6 are owned
by Mansarda. The unowned:
- fragments: the grid's run cut by other loads' edges - 13 Oct 13:25:10 (66 s) before the owned 780 s run;
  15 Oct 10:29:12 (46 s) and 10:30:07 (272 s) after the owned 358 s run; 13 Oct 11:34:34 cut at 314 s while
  Mansarda's ran 867 s;
- timing: a grid run that ends before Mansarda's is DECIDED before Mansarda's session exists (handed over at
  its end), so it can never be owned; the late `_locate` only counts a location;
- size: 13 Oct 12:53:12 grid 1711 W vs Mansarda 2010 W - beyond MATCH_POWER_REL 10 %, although the start was
  placed at Mansarda by `_step_meter`.
Built (`CLAIM_PLACED`): `Session.placed` carries the start's placement (`_Open.meter`); `_ready_at` waits
for the placed meter's open run; `_session_pairs` pairs with that meter on start time and the cross-meter
duration factor alone, no size test.

| dials | dw hit | dw sessions / sigs | dw best (runs) purity | dw conc. | se hit | se sessions / sigs | se best purity |
|---|---|---|---|---|---|---|---|
| CLAIM_PLACED=1 | 8/9 | 12 / 6 | Mansarda#21 (5) 0.29 | 0.42 | 30/50 | 36 / 16 | 0.53 |
| CLAIM_PLACED=1 STATE_TAG=2 PLACE_FALLS=1 | 8/9 | 12 / 6 | Mansarda#21 (5) 0.56 | 0.42 | 29/50 | 36 / 23 | 1.00 (#785 Steam, 9) |

The 11:34:34 (timing) and 12:53:12 (size) runs are Mansarda's now. Still unowned: the re-opened pieces
inside Mansarda's running session (15 Oct 10:29:12, 10:30:07), the 66 s piece before 13 Oct 13:25:47, and
22 Oct 10:48:16 (Mansarda filed no session there). Built for the first: `CLAIM_PLACED=2` - a grid run
starting and ending inside a run a several-device meter already owns, at about its size, is that run opened
again (`Fleet._owned`, `_decide`).

| dials | dw hit | dw sessions / sigs | dw best (runs) purity | dw conc. | se hit | se sessions / sigs | se best purity |
|---|---|---|---|---|---|---|---|
| CLAIM_PLACED=2 | 8/9 | 12 / 6 | Mansarda#21 (6) 0.33 | 0.50 | 30/50 | 36 / 16 | 0.50 |
| CLAIM_PLACED=2 STATE_TAG=2 PLACE_FALLS=1 | 8/9 | 12 / 6 | Mansarda#21 (6) 0.60 | 0.50 | 29/50 | 36 / 23 | 1.00 (#785 Steam 9; #716 Dry 4) |

Against the baseline (8 signatures, best 3 runs at 0.23, concentration 0.25): half the dishwasher's runs in
one Mansarda signature at 0.60 purity. Still unowned: 15 Oct 10:29:12 (46 s, 31 s after Mansarda's start -
a guard, since dropped), 13 Oct 13:25:10 (66 s, 1231 W, before Mansarda's run), 22 Oct 10:48:16 (no
Mansarda session).

Golden replays with every dial off, after all the code above: Home 24,435 / 0 differ, Kozolec 1,839 / 0.

## Site-wide scorecard (`energy_bench.py card home kozolec`, the sites' default ten-day folders, SLICE=6)
Baseline `data/scorecard/96b16f6-dirty-sc-base-20261023T094504Z.json`, capture / impurity, all loads:

| | hidden | circuits | fed |
|---|---|---|---|
| Home devices (230 kWh) | 32.6 / 14.1 | 43.9 / 13.1 | 66.1 / 10.7 |
| Home partition (607 kWh) | 46.8 / 14.5 | 55.1 / 11.2 | 62.5 / 9.3 |
| Kozolec devices (251 kWh) | 84.4 / 5.3 | - | 86.8 / 2.1 |
| Kozolec partition (326 kWh) | 82.1 / 6.1 | - | 84.3 / 3.9 |

All stages (`CLAIM_PLACED=2 STATE_TAG=2 PLACE_FALLS=1`, `96b16f6-dirty-sc-all-20261023T095635Z.json`):

| | hidden | circuits | fed |
|---|---|---|---|
| Home devices | 32.6 / 14.1 | 42.7 / 12.1 | 63.5 / 12.8 |
| Home partition | 46.8 / 14.5 | 54.2 / 9.5 | 62.7 / 10.5 |
| Kozolec devices | 84.4 / 5.3 | - | 86.9 / 2.2 |
| Kozolec partition | 82.1 / 6.1 | - | 84.3 / 4.5 |

Hidden is identical (no meter fed: nothing to claim). Fed Home devices -2.6 / +2.0, beyond its band; `diff`
per load: Polnilnica (EVBox) capture 83.9 -> 74.6 [band 4.3, beyond], Hidrofor 78.0 -> 73.1 [within 5.6],
Susilna impurity 13.2 -> 17.8 [beyond 1.3]; Mansarda partition capture 51.7 -> 57.6 (+6.0, beyond its 1.6
band - the target), impurity 9.3 -> 12.0. Circuits mode: Hiša impurity -2.6, partition impurity 11.2 -> 9.5.
Cause of the EVBox loss: `_session_pairs`' time-only pairing for a placed run consumed the plug's long
session for a short grid piece. The placed claim is now limited to meters holding several devices (a plug
pairs by identity and energy already) - a judgement, re-carded (`sc-all2`): Home devices fed 63.5 / 12.7,
partition 62.8 / 10.5, Kozolec 86.9 / 2.2 and 84.2 / 3.9 - and Polnilnica STILL 83.9 -> 74.9, so the EVBox
loss is not the placed pairing's. The restriction may be lifted (the owner's call); the loss is isolated
per dial below. Open alternative that keeps claims on plugs: let one meter session be shared by several
grid pieces instead of consumed by the first.

## The Home fed loss isolated per dial (`energy_bench.py home data/history/home SUBS=prod HOUSE=prod`)
| dial alone | devices all | Polnilnica | Hidrofor | Susilna imp. | Mansarda (partition) |
|---|---|---|---|---|---|
| none | 66.1 / 10.7 | 83.9 / 4.6 | 78.0 / 12.2 | 13.2 | 51.7 / 9.3 |
| STATE_TAG=2 | 66.1 / 10.7 (identical: no inputs fed on the card) | | | | |
| CLAIM_PLACED=2 | 66.1 / 10.7 | 83.9 | 77.7 | 13.0 | 52.7 / 10.1 |
| PLACE_FALLS=1 | 63.3 / 12.5 | 75.2 / 3.4 | 72.8 / 11.5 | 17.8 | 57.0 / 10.4 |

Placing falls is the whole loss AND Mansarda's whole gain. The plugs' falls are placed only some of the
time (a 60 s plug's fall rarely matches the grid's span), so their stop groups split between "placed" and
"plain" and the start>stop pair statistics (PAIR_MIN_RUNS) fragment: Polnilnica observed closes 45.5 ->
44.6 %, multi-close 17.2 -> 19.7 %. Remedy: `_step_meter` places a fall at a several-device meter only
(a plug's stops are its run's owner's anyway, `_meter_stop`).

| PLACE_FALLS=1, several-device meters only | devices all | Polnilnica | Hidrofor | Susilna imp. | Mansarda (partition) |
|---|---|---|---|---|---|
| | 64.3 / 10.6 | 80.3 / 3.8 | 72.8 / 9.3 | 14.4 | 56.7 / 9.7 |

Against the baseline: site -1.8 capture, impurity -0.1; Mansarda +5.0 capture; Polnilnica -3.6 (band
4.3) and the Hidrofor -5.2 (band 5.6) - both inside their bands, though the same way. The plugs still move
because the circuit meters' placed falls leave the grid's plain stop histogram, and its segments are cut
anew for every load. The dishwasher scorer is unchanged by the restriction (12 / 6, best 6 at 0.60).
The owner's rule (big win at one load, small loss at another ships) would take it; his call.

## Final card, every stage as it stands (`sc-all3`, `96b16f6-dirty-sc-all3-20261023T102658Z.json`)
| | hidden | circuits | fed |
|---|---|---|---|
| Home devices | 32.6 / 14.1 | 42.7 / 12.1 | 64.9 / 11.1 (base 66.1 / 10.7) |
| Home partition | 46.8 / 14.5 | 54.2 / 9.5 | 63.4 / 10.1 (base 62.5 / 9.3) |
| Kozolec devices | 84.4 / 5.3 | - | 86.9 / 2.2 (base 86.8 / 2.1) |
| Kozolec partition | 82.1 / 6.1 | - | 84.2 / 3.9 (base 84.3 / 3.9) |

## Stage 4 - code names
`Session` -> `Run`, `EdgeCluster` -> `StepGroup`, `EdgeCluster.where` -> `owner`, `Session.owner` ->
`parent_signature`; stored keys ("owner", "where") unchanged, read as before. Not renamed: the detector's
"step" in method and field names (`_classify_step`, `_step_meter`, `declared`, `edge_hist`...) - an edge
in the agreed terms; `edges` already names the StepGroup list, so that rename needs a design of its own.
Golden replays after the rename, dials off: see the end of this file.

## What stays as dials (none ships by default until the owner decides)
`STATE_TAG` (0; 2 = every input's state at the step keys the group, per input), `PLACE_FALLS` (0; 1 =
falls placed at several-device meters), `CLAIM_PLACED` (0; 1 = a placed run waits for and pairs with its
meter's session on time; 2 = also a re-opened piece inside an owned run). The dishwasher scorer and the
card above were run with `CLAIM_PLACED=2 STATE_TAG=2 PLACE_FALLS=1`.

## Checks after everything (2026-10-23)
- `python3 tests/run_all.py`: every file, 0 failures (after the rename).
- Golden replays with every dial off: Home 24,435 sessions / 0 differ, Kozolec 1,839 / 0.
- Dishwasher scorer after the rename, all stages: identical (12 / 6, best 6 at 0.60, Sock Eater best pure).
- The scorer, its truth builder and truth, and the ownership diagnostic are in `tests/dishwasher/`
  (`dw_bench.py`, `truth.py`, `truth.json`, `unowned2.py`); run from that folder with the dials above.
- Nothing committed; `git status` lists the files.

## The owner's decisions (2026-10-23) and what shipped as default
- States at the step: always on, every state (the Sock Eater splitting by cycle phase is welcome).
- Falls placed: on, flag removed. At several-device meters only: with every meter's falls placed, Home fed
  devices 64.7 / 11.2 -> 63.1 / 12.7, Polnilnica 79.5 -> 74.0 (band 4.3) - a plug's fall is placed only
  when its 60 s report matches the grid's span, so its stop groups split. The owner may overturn (one line
  in `_step_meter`).
- The claim: unified on every meter - a placed pair no longer consumes the meter's session, later grid
  pieces of the same run pair through the covered rule and `_file_as` (a plug's identity signature, a
  3EM's own). Home fed with the claim alone: 65.6 / 10.8 (base 66.1 / 10.7), Polnilnica 83.8, Hidrofor
  78.2, Mansarda +1.9. `CLAIM_PLACED = 2` by default (two levels, kept as a constant).

Defaults, single replays: Home fed devices 64.7 / 11.2, partition 63.6 (Mansarda 57.9 / 11.4, Susilna
imp. 15.0, Polnilnica 79.5 / 3.3, Hidrofor 73.2 / 10.3); Kozolec fed devices 86.8 / 2.2, partition
84.2 / 4.6. Dishwasher on defaults: 12 / 6, best 6 at 0.60, Sock Eater best pure (unchanged).
Card on defaults (`47d0e99-dirty-sc-defaults-20261023T110645Z.json`), capture / impurity:

| | hidden | circuits | fed |
|---|---|---|---|
| Home devices | 32.6 / 14.1 | 42.5 / 12.1 | 64.7 / 11.2 (base 66.1 / 10.7) |
| Home partition | 46.8 / 14.5 | 54.2 / 9.6 | 63.4 / 10.3 (base 62.5 / 9.3) |
| Kozolec devices | 84.4 / 5.3 | - | 86.8 / 2.2 (base 86.8 / 2.1) |
| Kozolec partition | 82.1 / 6.1 | - | 84.2 / 4.6 (base 84.3 / 3.9) |

`tests/run_all.py` on the defaults: 0 failures. `DETECTOR_GENERATION` 20 -> 21 (a ten-day rebuild at
both sites on deploy).

## Falls at every meter (the owner, 2026-10-23: remove the asymmetry; single-run swings are noise)
The guard in `_step_meter` is gone. Home fed (replay `eb_def_fallsall`): devices 63.1 / 12.7, Mansarda
57.7 / 12.1, Polnilnica 74.0 / 3.1, Hidrofor 73.3 / 12.6, Susilna imp. 17.8. Looked closer: the EVBox's
pairing is unchanged (observed closes 44.6 % both ways) and its attributed energy is ~10 charges of
2-6 kWh, so 5 points is one charge changing shape; the Hidrofor's capture is unchanged (73.2 -> 73.3),
its signature #22 went 91 -> 88 % pure (775 runs, ~0.5 kWh of other runs). Kozolec fed with every
meter's falls placed: 86.8 / 2.2, partition 84.2 / 4.6 - identical. Read as neutral.

## The last dials (the owner, 2026-10-23: values alike -> the most unified code, option removed)
- `CLAIM_PLACED` removed, level 2 kept: identical (Home fed 63.1 / 12.7; dishwasher 12 / 6 at 0.60).
- INS-9 gain dials `GAIN_FALLS` / `GAIN_MEDIAN` / `GAIN_UNGATED` removed, all three behaviours kept
  (falls learn - they already went through `_step_meter`; every clean step teaches its meter; the median
  of the ring; a pre-ring library reads its mean). Home fed devices 63.1 / 12.7 -> 64.0 / 10.4
  (Polnilnica 77.1 / 3.3, Hidrofor 71.8 / 13.1, Mansarda 56.5); Kozolec 86.8 / 2.2 unchanged; dishwasher
  12 / 6 at 0.60 unchanged, Sock Eater 30/50 hit (29). Suite 0 failures.
- Left as they are: `INPUT_EVIDENCE` (the bench flips it off as a comparison baseline, `bench.py:699`),
  `TRIM` (the card's restart instrument), `FRIDGE_PHASES` (a count).

## Inside-out claiming in the Fleet (the owner, 2026-10-23: "it has to be built and benched")
Built: a placement builds a `Step` (`Fleet._placed_step`: the grid's edge, the owner's own edges and
those of every meter it hangs under, the owner's reading in the grid's terms as the change on the phase,
the grid's net on the other phases that moved); the step group and the open run are booked at the
OWNER's size and the group at the owner's moment (`Detector.placement_step`, `_classify_step`,
`_form_event`); a placed stop books its group at the owner's fall and the stop PAIRS at the owner's
fall too (`Detector.placed_fall`, `PhaseState._declare`). The ramp-window rule keeps precedence.

| | Home fed devices | Hidrofor | Polnilnica | Susilna | Mansarda (part.) | Kozolec fed | dishwasher | pump clean / long / wrong size / missing |
|---|---|---|---|---|---|---|---|---|
| before (gain shipped) | 64.0 / 10.4 | 71.8 / 13.1 | 77.1 / 3.3 | 66.0 / 12.9 | 56.5 / 11.2 | 86.8 / 2.2 | 12 / 6 at 0.60 | 640 / 34 / 78 / 19 |
| owner-sized starts only | 62.7 / 13.5 | 72.3 / 28.5 | 85.3 / 4.1 | 61.2 / 14.9 | 53.9 / 10.6 | 86.0 / 3.1 | 12 / 6 at 0.60, SE 32/50 | 634 / 47 / 69 / 21 |
| + owner-sized stops | 63.4 / 12.7 | 72.4 / 26.3 | 83.8 / 4.0 | 62.6 / 13.4 | 54.0 / 11.6 | 86.0 / 3.1 | 12 / 6 at 0.55, SE 33/50 | 637 / 46 / 70 / 21 |

The Hidrofor: the same 766 runs in its signature, booked 20.1 kWh instead of 16.4 - 5.4 kWh of time the
pump was off, runs closing late (long 34 -> 46) although the stops' sizes agree better (within 10 %:
44 -> 54 %): an owner-sized fall sometimes closes another open run of the same size. Polnilnica +6.7,
the Sock Eater 30 -> 33 bursts. Ships by the owner's rules (physically right, the site not significantly
worse); the Hidrofor's late closes are the thing to look at next. Suite 0 failures.

## The daily rewrite by length (the owner's order, item 2)
Built: `Signature.learn_split` at the day's repair - the largest gap in the run memory's log-lengths
(RUN_MEMORY 12), at least SPLIT_MIN_RUNS=4 runs each side, the two runs either side of the cut and the
sides' medians at least MATCH_DURATION_FACTOR apart; the side with more runs stays, a run on the other
side files apart (`takes_run` at both `files_in` gates), and split siblings are neither `alike` nor
device-merged. Stored (`split_s`, `split_side`). Live filing keeps its loose duration rule.
Bench: Home fed 63.4 / 12.7 -> 63.5 / 12.6, Kozolec 86.0 / 3.1 -> 86.0 / 3.2, dishwasher unchanged
(Mansarda#21 6 dw / 2 se / 1 oven / 2 other): neutral. Why it does not touch #21: its contaminants are
one or two runs among the twelve the memory holds, under the four a side needs; a longer run memory, or
the day log's runs added to the memory's, is what the split needs to see the dishwasher's case. Kept
(the owner's rule: the mechanism in the right place, no loss); suite 0 failures.

## The misses (the owner's order, item 3; `tests/dishwasher/misses.py`)
- Dishwasher: 1 of 9 (13 Oct 12:02, 112 s): Mansarda's own detector filed a 9,527 s run at 1,348 W over
  it - its own library merged the burst into something else; the grid filed 59-196 s pieces under 1 kW.
- Sock Eater: 15 of 50 - NINE are zero-length truth entries (the truth builder's artefact; the scorer now
  drops bursts under 20 s), the six real ones are 26-152 s bursts where Mansarda filed its own session
  (1.3-2.0 kW) but the grid's piece was netted under 1.2 kW (1,070 / 1,148 W) or absent, so the start was
  never placed and the claim never applied. The thing to build: a grid piece SMALLER than the owner's
  step over the same span (netted against another load) is still the owner's - today `_step_meter` wants
  the meter's step to be all of the grid's.

## The under-sized grid piece, looked at in the readings (2026-10-23, with the owner)
- 21 Oct 13:36:59 (the Sock Eater's 1.84 kW on Mansarda C): the grid's phase A rose 440 -> 1965 -> 2326 ->
  3260 W over twelve seconds, three declared edges of +1525, +359, +930. Hisa held flat on all three
  phases. The +930 is the HIDROFOR (its plug 0 -> 1050 W at 13:37:10, a plug directly under the grid):
  two metered loads 2-10 s apart, and the grid cut Mansarda's one edge into +1525 and +359 across two
  polls as its reading settled. Neither piece is "all of" Mansarda's 1842 x 1.004 within tolerance, and
  the ramp window (+2820) is not either, so nothing was placed. The rule: an owner's edge consumes the
  grid's edges inside its own span up to its gain-scaled size (1525 + 359 = 1884 ~ 1849); what is left
  (+930) stays for the next owner's claim (the pump's). The learned gains: Mansarda x1.004, Hisa x1.000,
  Susilna x1.003, Hidrofor x0.973 (778 clean steps).
- 22 Oct 13:35:32 (+1.07 kW on Mansarda C): the inverter swung 8.3 -> 10.0 -> 8.0 kW in twenty seconds;
  the grid's piece read ~650-900 W. The combine's phantom steps are a third of the inverter's change for
  the second or two between the meters' stamps (50-200 W), not a kilowatt; the +-1 kW bouncing from
  13:36:30 on is the Sock Eater's own element chattering every two seconds (Mansarda reads 1172 / 25 /
  1172 / 25 W within one second). Stamp offsets, inverter to M1, 21-22 Oct: 95.0 % within 0.1 s (and the
  same within 0.3, 0.5, 0.8, 1.0, 1.5 s), 98.5 % within 2.0 s, 99.8 % within 2.5 s: the 5 % are a whole
  M1 poll late, so a wider settle buys nothing under 1.5 s and swallows real readings above 2 s.
  COMBINE_SETTLE_S stays 0.3.
- The inverter IS balanced: every inverter change shows on M1's B and C as a third each within 20-40 W.
  The V x I share differs from a third by 6 W at the median, 24 W at the 90th percentile, 136 W at a 15 kW
  noon (a 2 V phase-voltage spread). Its phases against the M1's, by voltage correlation over two days:
  inverter a <-> M1 b (0.981), b <-> a (0.982), c <-> c (0.969), 0.75-0.77 elsewhere - wired a <-> b.
  Built: phases.voltage_votes + share_split, Fleet.inverter_map (stored, votes summed per pass,
  INVERTER_MAP_MIN_VOTES / INVERTER_MAP_CLEAR), detection._inverter_split, the bench's _prod_house, the
  map shown in the diagnostics and the sensor ("X (L1)" per inverter phase). Load Juggler's phases.py
  copied.

  | thirds -> V x I share through the map | before | after |
  |---|---|---|
  | Home fed devices (Sep folder) | 63.5 / 12.6 | 62.1 / 14.4 |
  | Mansarda (partition) | 54.7 / 11.x | 55.7 / 11.x |
  | Susilna | 62.6 / 13.4 | 60.5 / 17.5 |
  | Polnilnica | 83.8 / 3.9 | 80.3 / 3.0 |
  | Hidrofor | 72.4 / 26.3 | 72.0 / 27.9 |
  | grid edges, phase a / b / c | 3168 / 3580 / 3674 | 3068 / 3651 / 3446 |
  | ...of them 11-14 h | 755 / 511 / 557 | 728 / 405 / 506 |
  | measured idle noise a / b / c | 145 / 10 / 10 W | 145 / 10.4 / 10 W |
  | dishwasher (Oct folder) | 8/9, 12 runs / 6 sigs, best 6 at 0.55 | 9/9, 13 / 8, best 5 at 0.42; Sock Eater 33 -> 31 of 38 |

  Physically right and the midday edge count drops on every phase (B by a fifth), but the card is a small
  loss on every table (site -1.4 / +1.8, inside Home's fed band 5.4 / 3.3) and the dishwasher's best
  signature is less pure. The idle noise does not move: it is the instrument's (and phase A's 145 W is
  its loads never holding still, fourteen times B and C - worth its own look). Ships by the owner's rule
  (physically accurate, no big loss), with the loss written down.

## The under-sized piece, traced in the detector (2026-10-23, `tests/dishwasher/trace.py`)
In the detector the grid declared ONE edge of +1,879 W at 13:37:01 (its sustain merged the chart's
1,525 + 359) and +913 at 13:37:11. `_meter_totals` found both owners - Mansarda share 1,854 (timing
confidence 0.30), the Hidrofor 816 (0.10: its plug reported 9 s later, its 10 s cadence overlapping the
span) - but `metered_shares` walks the meters in dictionary order, Hidrofor before Mansarda, so the
pump's 816 was carved out of the 1,879 first and placed at the pump, and Mansarda's 1,854 no longer
fitted the 1,063 left: unplaced. Fix: `_meter_steps` serves the shares by fit (timing confidence, then
size); Mansarda takes the whole edge, the pump's edge claims the +913 ten seconds later. Two dials to
bench both ways (the owner): `CLAIM_POOL_FLOOR` (a grid piece a meter's bigger clean step explains is the
meter's down to this share of the step, 0.5 / 0) and `SHARE_SAME_DIRECTION` (1 / 0).

| dishwasher scorer (Oct) | dw hit | dw runs / sigs, best, purity | SE hit | SE sessions / sigs, conc. |
|---|---|---|---|---|
| before (fad2a96) | 9/9 | 13 / 8, best 5 at 0.42 | 31/38 | 38 / 20, 0.26 |
| fit ordering, floor 0.5, direction on (A) | 9/9 | 13 / 8, best 5 at 0.36 | 34/38 | 43 / 21, 0.23 |
| floor 0 (B) | 9/9 | identical to A | 34/38 | 44 / 22, 0.23 |
| direction off (C) | 9/9 | 13 / 8, 0.38 | 33/38 | 44 / 24, 0.18 (308 sessions for 275) |
| floor 0 + direction off (D) | 9/9 | 13 / 8, 0.38 | 32/38 | 45 / 24, 0.18 |

The floor makes no difference: the dial goes, the simpler branch (no floor) stays. The direction check
off is slightly worse everywhere (more sessions, lower concentration, a burst lost): it stays. Fit
ordering: three more Sock Eater bursts, but Mansarda#21 back to 5 dw / 6 se. Home fed card with A:
devices 62.1 / 14.4 -> 61.1 / 13.3, Mansarda +1.2, Susilna imp. -0.5, Hidrofor imp. -0.5, Polnilnica
80.3 -> 70.7 - isolated below (branch off vs on).

| Home fed | devices | Polnilnica | Hidrofor | Susilna |
|---|---|---|---|---|
| before fit (fad2a96) | 62.1 / 14.4 | 80.3 / 3.0 | 72.0 / 27.9 | 60.5 / 17.5 |
| fit ordering only (branch off) | 61.4 / 15.4 | 74.8 / 4.5 | 72.5 / 27.2 | 60.6 / 18.9 |
| + the bigger-step branch, floor half | 61.1 / 13.3 | 70.7 / 2.4 | 71.7 / 27.4 | 61.9 / 13.9 |
| + the branch, no floor | 60.9 / 13.3 | 68.5 / 3.8 | 70.9 / 12.8 | 64.6 / 17.2 |

Shipped: fit ordering, the branch WITH the half floor, the direction check. No floor read best for the
Hidrofor but fails the Kozolec blip test (a 263 W blip a second before the EVSE's 3.6 kW fall taken as
that fall): the half is the same half `_meter_stop` demands, so it stays as a fixed rule, not a dial.
The EVBox loses at every step (80.3 -> 70.7): its OCPP reading is 337 rows in ten days, one every ~40
min, so a "clean step" of its plug is forty minutes stale and, served by fit, it comes last - its own
item. One call (SLICE=0) against 6-hour slices on the scorer: 365 s against ~10 min, the same figures,
the "3 of 275 sessions differ" first noted was two runs on different code (the floor removed between
them), not slicing: `energy_bench.py invariance home data/history/home-1022` with the five inputs fed,
0 sessions differ of 23,386 across SLICE=0 / 6 / 1 / LIVE=1; the restart variants 3-4 (0.12 kWh), as
before. Scorer and single replays run at SLICE=0 from here (the owner); the card keeps SLICE=6 and the
invariance run is the occasional guard after larger changes.
Card after the fit ordering (`fdf4bd7-dirty-sc-fit-20261023T161342Z.json`): Home devices hidden 32.9 /
13.3, circuits 43.5 / 11.9, fed 61.1 / 13.3; partition fed 61.6 / 10.3; Kozolec devices fed 86.1 / 3.2,
partition 83.6 / 4.9.
Kozolec fed after the fit ordering: 86.8 / 2.2 -> 86.1 / 3.2 (pond EVSE 94.1 / 2.4 -> 92.7 / 3.4, boiler
91.2 / 2.2 -> 91.4 / 4.0, water pump 82.6 / 3.7 -> 84.0 / 1.2) - small, and real against Kozolec's band.

## The incubator's fragments with the washer's states fed (2026-10-23, `tests/dishwasher/hisa_sigs_inputs.py`, `hisa_twins.py`)

Hiša's 50-70 W signatures on c with 5+ runs, October folder, SLICE=0: 7 with no inputs (#2 the 60 W
incubator, 6,241 runs of 23 s every 109 s; #233 a 154 s load every ~70 min, 820 runs), 20 with the five
washer / oven inputs fed (24 with the bench dial `INPUT_RARE_SHARE=1.1`; the default 0.25 scores the same
or better on the dishwasher scorer - 7 @ 0.44 against 5 @ 0.36 - so the dial is dropped). Two mechanisms:
`_by_state` keys the incubator's step groups by "Sock Eater Running" / "Wash" (5,558 + 567 + 205 runs),
and `INPUT_SPLIT` births signatures of it in Spin, Dry, Paused, End Of Cycle, Conventional Cooking.

- A LIFT floor on the state keying (`n >= INPUT_MIN_LIFT x expected` in `_keyed_above_chance`): the keys go
  (16-19 signatures, all unkeyed) but the Sock Eater's one clean signature goes with them (10 runs at purity
  1.0, born in Steam -> Mansarda#21 mixed, 9 runs at 0.47-0.53). Reverted; ruled out.
- The born-in fragments were all `judged True` by episode coverage (ran in >= 50 % of the value's episodes):
  a load stepping every 109 s runs in EVERY episode of every value. Their pair lifts against #2 were
  1.1-2.3, and `Signature.alike` said alike - the "chance-born fragment goes back into its twin" rule of
  2026-09-29 lived only inside `alike`, and nothing but orphan naming calls `alike` any more. Built the
  takeback into `_judge_born`: at judgement, an alike ordinary twin (pair lift < `INPUT_SPLIT_LIFT`) swallows
  the fragment; the moved-signature bookkeeping is shared with `_merge_devices` (`_absorb`).
  Two more things were needed, found by the synthetic test (`test_an_all_day_load_born_in_a_rare_value_
  goes_back_to_its_twin`: a fridge every 25 min, a 15-minute "Spin" a day, 14 days): (1) `input_lift`
  answered None while the born-in signature had under `INPUT_MIN_RUNS` runs, and its input rows decay
  (8 runs count 5.5), so a small fragment could never be judged by lift - now it answers when the runs
  seen OR the runs expected reach `INPUT_MIN_RUNS` (the twin's expected runs in the value are the
  evidence); (2) the judgement waits while an alike twin's lift cannot be measured yet, instead of
  sealing the fragment by coverage. Twins are found with alike's born-in gate aside, since the lift is
  decided in `_judge_born` itself.

| inputs fed, SLICE=0 | Hiša 50-70 W sigs | #2 runs | 154 s load | Sock Eater best | dishwasher best |
|---|---|---|---|---|---|
| before | 20 | 5,558 | 216 + 114 + 43 | #706 10 runs @ 1.0 | 7 @ 0.44 |
| first takeback (alike as it was) | 17 | 6,370 | 412 | #707 10 runs @ 1.0 | 7 @ 0.44 |
| shipped (lift evidence, judgement waits) | **11** | 6,410 | 464 | #700 10 runs @ 1.0 | 7 @ **0.78** |

What stays: the `_by_state` keyed groups (#2 itself keyed Spin, #361 Wash), #846 born Steam with lift 17
(real by its own measure), Conventional Cooking (lift 11.4, 59 runs of 33 s every 9 min while the oven
cooks - may be real), End Of Cycle (lift 1.6 but 66 s against #2's 41 s length split, so not alike).
Suite: 0 failures. Invariance (October folder, five inputs): 0 sessions differ of 23,386 across SLICE=0 /
6 / 1 / LIVE=1; the restart variants 3 / 4 (0.12 / 0.13 kWh), as before. Home fed (`data/history/home`, production slicing, no inputs fed): identical to the
baseline to the last decimal - devices 61.1 / 13.3, partition 61.6 / 10.3 - as it must be, since nothing
is born in a value without an input fed. Kozolec has no inputs either; not rerun.

## Open
- Wire `build_steps` into the Fleet (plan task 5), stored phase maps (task 6), step groups by owner (task 7).
- The mixed `Mansarda#22` is the real target: re-file old runs by input state in the daily rewrite, or split
  the signature once the lift is known.
