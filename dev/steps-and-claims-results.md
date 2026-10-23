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

## Open
- Wire `build_steps` into the Fleet (plan task 5), stored phase maps (task 6), step groups by owner (task 7).
- The mixed `Mansarda#22` is the real target: re-file old runs by input state in the daily rewrite, or split
  the signature once the lift is known.
