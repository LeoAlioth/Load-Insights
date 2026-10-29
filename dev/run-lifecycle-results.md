# Run lifecycle - bench results per stage

Plan: `docs/run-lifecycle-plan.md`. Cards: `python3.14 tests/energy_bench.py card` (SLICE=6, all sites and modes),
`tests/lifecycle_check.py SITE` (meters fed, last 3 days in one-minute passes). Capture / impurity %, with kWh
captured / wrong where it matters (truth kWh in brackets).

## Stage 0 - baseline (19b9cbf, card `19b9cbf-dirty-lifecycle-base-20261028T192210Z.json`)

| | hidden | circuits | fed |
|---|---|---|---|
| Home devices (227.92 kWh) | 35.2 / 14.0 | 41.1 / 13.6 | 59.2 / 12.0 |
| Home partition (604.58) | 48.6 / 14.6 | 53.5 / 10.7 | 60.7 / 9.6 |
| Susilna (91.03) | 24.4 / 14.6 (22.2 kWh caught, 3.8 wrong) | 22.9 / 12.9 | 58.4 / 8.5 (53.2, 4.9) |
| Polnilnica (45.82) | 64.2 / 7.1 | 55.8 / 10.3 | 72.7 / 4.7 |
| Hidrofor (22.19) | 68.7 / 19.8 | 65.0 / 18.0 | 68.9 / 28.6 |
| Workshop boiler (8.84) | 13.9 / 29.1 | 77.0 / 9.7 | 91.3 / 7.3 |
| Kitchen fridge (4.80, heuristic) | 3.1 / 38.7 | 3.3 / 23.1 | 5.5 / 21.9 |
| Kozolec devices (251.22) | 84.7 / 4.3 | - | 85.2 / 3.9 |
| Kozolec partition (325.78) | 82.7 / 5.3 | - | 83.0 / 5.4 |
| home-hisa Blaževa Soba (7.31) | 31.7 / 21.4 | - | 61.2 / 15.8 |
| home-mansarda office (49.53) | 36.1 / 18.5 | - | 54.5 / 14.6 |
| third charger (322.14) | 89.3 / 6.5 | - | 92.2 / 4.6 |

Closes, Home hidden: 49,041; observed 74.5 %, started again 9.1 %, multi-close 8.1 %, floor 3.5 %, joint 3.0 %,
unseen 1.3 %; stops that closed nothing 6.5 % of steps. Kozolec hidden: 1,911; observed 80.0 %.

Lifecycle check, fed, last 3 days live:
- Home: most open starts on one phase main 14, Mansarda 13, Hiša 10; passes x phases with open starts sharing a
  group: plain 5, owned 5,880; open starts older than 3x their group's usual length 28,379 of 113,705 seen;
  Inkubator live max 444 W against a ceiling of 192 W, over it in 20 of 4,351 passes.
- Kozolec: main 8; sharing a group: plain 0, owned 300; older than 3x usual 1,085 of 12,553.

## Stage 1 - falls as all-phase events (card `nogit-lifecycle-s1-20261028T193937Z.json`)

| | hidden | circuits | fed |
|---|---|---|---|
| Home devices | 34.4 / 15.9 (-0.8 / +1.9) | 43.5 / 14.0 (+2.4 / +0.4) | 60.0 / 10.0 (+0.8 / -2.0) |
| Home partition | 47.2 / 14.7 | 54.8 / 10.9 | 61.5 / 9.7 |
| Susilna | 23.5 / 19.9 (21.4 kWh caught, 5.3 wrong) | 26.2 / 15.6 | 57.1 / 10.0 |
| Polnilnica | 62.7 / 6.5 | 62.2 / 4.4 | 74.9 / 3.9 |
| Hidrofor | 69.2 / 22.3 | 68.4 / 21.5 | 68.8 / 12.2 |
| Workshop boiler | 12.3 / 12.4 | 80.0 / 9.2 | 87.0 / 5.5 |
| Kozolec devices | 84.8 / 4.3 | - | 84.4 / 3.2 |
| home-hisa Blaževa Soba | 20.3 / 10.3 | - | 70.9 / 21.1 |
| home-mansarda office | 34.9 / 16.2 | - | 51.4 / 9.8 |
| third charger | 90.3 / 6.3 | - | 92.9 / 4.9 |

Small moves either way; the fall waits for its event (a few readings) and closes only what was open before it.
Kozolec fed: `_meter_ended` closes 10.8 % (was 3.3 %), observed 72.5 % (79.6 %).

## Stage 2 - order evidence: no effect on the card by construction (nothing reads it until stage 3)

## Stage 3 - closing by group score, dips (card `nogit-lifecycle-s3-20261028T194953Z.json`)

| | hidden | circuits | fed |
|---|---|---|---|
| Home devices | 22.4 / 13.1 | 31.2 / 13.6 | 57.x (cut) |
| Home partition | 41.0 / 12.5 | 50.5 / 10.0 | 59.x |
| Susilna | 8.9 / 13.8 | 12.9 / 20.3 | 57 |
| Hidrofor | 31.5 / 25.3 | 31.0 / 16.2 | 68 |
| Kozolec devices | 74.2 / 3.6 | - | 83.7 / 4.1 |
| home-hisa Blaževa Soba | 30.0 / 9.8 | - | 72.3 / 21.0 |
| home-mansarda office | 30.7 / 18.5 | - | 42.7 / 8.2 |
| third charger | 74.1 / 3.6 | - | 91.6 / 4.9 |

A large loss hidden. Closes matched within 10 / 20 % of their start fell (Home hidden 48.5 / 74.3 -> 41.0 / 62.4,
median off 10.4 -> 13.5 %): with no partner evidence the fall must match the run's own rise within 3 noises, and
the old match against what a run had sagged to (`o.now`) is gone. Kozolec's pond EVSE 93.6 -> 78.9 % (its
charge tapers). Added after this card: a fall that leaves the phase carrying the other runs and no more emptied
the run, whatever its size (`_score`, house readings only): Kozolec hidden alone 74.2 / 3.6 -> 80.2 / 6.9 (pond
EVSE 87.5, boiler impurity 8.8 -> 22.8). Capping that fall at the run's size made it worse (72.8 / 9.3), not kept.

## Stages 3-5 - second units, ends by the readings, learned level, emptied runs (card `nogit-lifecycle-s5-20261028T200551Z.json`)

| | hidden | circuits | fed |
|---|---|---|---|
| Home devices (227.92) | 21.0 / 18.0 | 28.4 / 14.1 | 53.2 / 8.3 |
| Home partition (604.58) | 39.1 / 14.3 | 48.9 / 9.4 | 57.8 / 8.2 |
| Susilna (91.03) | 8.8 / 29.5 (8.0 kWh caught, 3.4 wrong) | 9.0 / 23.2 | 51.8 / 8.3 |
| Polnilnica | 51.9 / 7.4 | 48.6 / 6.4 | 63.0 / 5.1 |
| Hidrofor | 30.7 / 27.1 | 29.5 / 21.0 | 68.3 / 9.7 |
| Workshop boiler | 9.5 / 16.0 | 71.0 / 13.9 | 87.5 / 6.4 |
| Kozolec devices (251.22) | 80.2 / 6.9 | - | 83.7 / 3.1 |
| home-hisa Blaževa Soba | 30.5 / 20.7 | - | 66.1 / 19.2 |
| home-mansarda office | 30.9 / 23.6 | - | 46.3 / 11.4 |
| third charger | 62.5 / 4.7 | - | 90.1 / 5.1 |

Against the baseline: a large loss hidden everywhere (Home devices -14 points, Susilna 24.4 -> 8.8, the third site's
charger 89.3 -> 62.5), fed impurity better (Home 12.0 -> 8.3). Closes within 10 / 20 % of their start's size, Home
hidden 48.1 / 73.5 -> 40.6 / 61.6. Suspected: the learned size ratio was averaged over every fall while a group ran,
other loads' falls of any size among them, so its spread - and the tolerance - grew wide. After this card the ratio
learns only from falls within PAIR_RATIO_MAX (2x) of the start: Kozolec hidden 80.2 / 6.9 -> 81.2 / 6.8.

Lifecycle check, Home fed, stages 3-5: Inkubator live max 109 W against a 137 W ceiling, over it in 0 of 4,351
passes (baseline 444 W, 20 passes over); open starts sharing a group: plain 2,201 (baseline 5 - second units the
reading accepted), owned 5,893; older than 3x their group's usual length 30,777 of 94,891 seen (baseline 28,379 of
113,705); most open on one phase main 15.

After the ratio fix (PAIR_RATIO_MAX), one Home hidden replay: devices 30.6 / 15.5 (69.7 kWh caught, 12.8 wrong of
227.92; stages 3-5 card 21.0 / 18.0, baseline 35.2 / 14.0); Susilna 20.8 / 17.9 (18.9 kWh caught; 8.8 before the
fix, 24.4 baseline); hidrofor 62.4 / 25.7; Polnilnica 59.9 / 4.2; workshop boiler 8.7 / 25.1. The polluted ratio
was most of the stage-3 loss.

Variants, one Home hidden replay each (devices capture / impurity, against 30.6 / 15.5 after the ratio fix):
PAIR_TIGHT_NOISES 8 (looser size match) 31.4 / 17.1; PAIR_SCORE_MIN none 31.7 / 14.0 (kept as the default);
dips off 29.7 / 16.5 (Polnilnica 59.9 / 4.2 -> 57.7 / 8.4: the EVBox's dips) - dips stay.

## Current (no score floor, ratio fix, dips; card `nogit-lifecycle-s6-20261028T202937Z.json`)

| | hidden | circuits | fed | baseline hidden / fed |
|---|---|---|---|---|
| Home devices (227.92) | 31.7 / 14.0 | 40.0 / 14.0 | 52.8 / 6.4 | 35.2 / 14.0, 59.2 / 12.0 |
| Home partition (604.58) | 44.8 / 13.6 | 53.5 / 9.6 | 58.4 / ? | 48.6 / 14.6, 60.7 / 9.6 |
| Susilna (91.03) | 20.5 / 11.7 (18.7 kWh caught, 2.5 wrong) | 23.6 / 12.9 | 47.5 / 2.4 (43.2, 1.1) | 24.4 / 14.6, 58.4 / 8.5 |
| Polnilnica (45.82) | 65.3 / 6.2 | 59.6 / 3.2 | 69.1 | 64.2 / 7.1, 72.7 / 4.7 |
| Hidrofor (22.19) | 64.2 / 25.6 | 67.8 / 21.7 | 69.7 | 68.7 / 19.8, 68.9 / 28.6 |
| Workshop boiler (8.84) | 13.2 / 14.5 | 64.5 / 17.1 | 88.5 | 13.9 / 29.1, 91.3 / 7.3 |
| Kozolec devices (251.22) | 82.3 / 6.2 | - | 84.4 / 3.0 | 84.7 / 4.3, 85.2 / 3.9 |
| Kozolec boiler (38.45) | 85.5 / 16.7 | - | 90.4 / 1.5 | 86.1 / 8.8, 91.1 / 2.0 |
| home-hisa Blaževa Soba | 35.5 / 19.1 | - | 67.1 / 18.4 | 31.7 / 21.4, 61.2 / 15.8 |
| home-mansarda office | 42.2 / 23.6 | - | 47.2 / 9.8 | 36.1 / 18.5, 54.5 / 14.6 |
| third charger | 87.3 / 5.7 | - | 90.7 / 3.7 | 89.3 / 6.5, 92.2 / 4.6 |

Reading: hidden capture a few points under the baseline at Home and Kozolec, impurity level or better; fed
impurity much better (Home 12.0 -> 6.4, Susilna 8.5 -> 2.4) with less captured (Home 59.2 -> 52.8). The circuit
sites (3EMs as the main meter) gain hidden (Blaževa Soba +3.8, office +6.1). Kozolec's boiler hidden impurity
8.8 -> 16.7 is the emptied-run rule's cost.

Variant VALLEY_SIGMAS 2 (GROUP_SPAN 3), card home kozolec: Home devices hidden 15.0 / 14.9, circuits 23.3 / 12.8,
fed 49.5 / 7.3; Kozolec hidden 56.2 / 5.5 (its boiler 85.5 -> 10.0 % caught: merged with other loads), fed 84.3 /
3.6. Far worse: groups wide enough to hold several loads until overlaps split them lose most of what the hidden
mode found. Not kept (0 stays the default).

Variant UNITS_NEED_HISTORY 1 (a second unit only in a group that has shown PAIR_MIN_RUNS overlaps), card home
kozolec: Home devices hidden 32.0 / 13.0 (31.7 / 14.0), circuits 39.9 / 12.9, fed 53.0 / 6.5 (52.8 / 6.4); Kozolec
hidden 81.3 / 3.2 (82.3 / 6.2; its boiler 85.5 / 16.7 -> 85.3 / 6.9), fed 84.5 / 3.1. Kept as the default.
The owner accepted the trade (fed impurity 12.0 -> 6.4 % for capture 59.2 -> 52.8 %), 2026-10-28.

Variant VALLEY_SIGMAS 1 (UNITS_NEED_HISTORY 1), card home kozolec: Home devices hidden 23.4 / 16.2 (default 32.0 /
13.0), circuits 33.5 / 17.5, fed 51.9 / 6.3 (53.0 / 6.5); Kozolec hidden 80.1 / 6.3 (81.3 / 3.2), fed 84.0 / 3.9.
Worse than 0, less than 2.

Variant VALLEY_SIGMAS 0.5: Home devices hidden 28.4 / 18.3, circuits 37.3 / 17.9, fed 52.7 / 6.2; Kozolec hidden
80.9 / 6.3, fed 84.3 / 3.2. Still worse than 0: the valley sweep is monotonic, every step away from "every dip
cuts" loses hidden capture and adds impurity (S 0 / 0.5 / 1 / 2, Home hidden 32.0 / 28.4 / 23.4 / 15.0).

Variant GROUP_MERGE 1 (the mirror of the split: neighbours joined on evidence), card home kozolec: Home devices
hidden 30.7 / 15.4 (default 32.0 / 13.0), circuits 40.3 / 13.3 (39.9 / 12.9), fed 53.0 / 6.5 (same); Kozolec hidden
80.5 / 4.8 (81.3 / 3.2; its boiler 85.3 -> 80.8 % caught), fed 84.5 / 3.1 (same). A little worse hidden, fed
unchanged - the joins it makes are few and fed runs come from the meters anyway. Not kept (0 stays).

## Stage 6 - mixture grouping (2026-10-29)

Grouping bench (tests/grouping_bench.py), Kozolec rises, groups / main share / unlabelled share: valleys -> mixture
after the three fixes (angle error from the amps' resolution, close piles by overlaps, no join on a failed fit):
boiler 6 / 98.5 / 3.3 -> 11 / 95.2 / 3.3; water pump 37 / 17.1 / 26 -> 8 / 64.6 / 38.5; IR panel 7 / 82.2 / 8.0 ->
4 / 88.9 / 13.3; pond 9 / 38.6 / 43 -> 1 / 100 / 78; 325 -> 145 groups. Home: 3,106 -> 1,701 groups; five of six
plugs more in one group (Susilna 31.8 -> 42.8 %, office 48.8 -> 69.4, UPS 26.5 -> 80.0, Blaževa Soba 53.7 -> 68.3,
workshop boiler 77.8 -> 88.9), the hidrofor worse (94.1 -> 46.4: its inrush scatters its rises).

Energy card, Home and Kozolec, mixture sweep on the code before the hidrofor fixes (devices capture / impurity):

| | Home hidden | Home partition hidden | Kozolec hidden | Home groups |
|---|---|---|---|---|
| valleys (default) | 32.0 / 13.0 | 44.8 / 13.6 | 81.3 / 3.2 | 3,106 |
| mixture far 4, join 3 | 25.7 / 24.2 | 38.3 / 17.3 | 82.7 / 3.8 | 1,701 |
| far 6 | 22.7 / 20.8 | 38.2 / 17.2 | 82.8 / 3.8 | 1,694 |
| far 8 | 22.3 / 19.9 | 37.7 / 17.8 | 82.7 / 3.8 | 1,693 |
| join 2 | 29.2 / 15.3 | 41.5 / 12.5 | 82.8 / 4.6 | 3,627 |

At Home the mixture merges too much: groups take unmetered loads' steps, which the grouping bench (plugs only)
cannot see. Hidrofor fixes afterwards (fresh, above-chance co-runs; joins across every pair in reach; rise groups
sharing a partner fall joined): its rises 46.4 -> 76.2 % in one group at step time, 93.0 % once the daily joins
apply; falls 85.6 %.

Filing by pair (Detector.device_of: start group + stop group), mixture, MIX_JOIN_WIDTHS 2 (card pair-join2):

| | valleys | mixture join 2 + pair filing |
|---|---|---|
| Home devices hidden / circuits / fed | 32.0 / 13.0, 41.1 / 13.6, 53.0 / 6.5 | 34.2 / 12.7, 37.2 / 13.3, 52.2 / 6.2 |
| Home partition hidden | 48.6 / 14.6 | 44.8 / 10.9 |
| Susilna hidden / fed | 24.4 / 14.6, 58.4 / 8.5 | 24.0 / 14.3 (21.8 kWh caught, 3.6 wrong), 44.8 / 2.5 (40.8, 1.0) |
| Hidrofor hidden / fed | 68.7 / 19.8, 68.9 / 28.6 | 63.9 / 19.2, 70.4 / 8.1 (15.6 kWh caught, 1.4 wrong) |
| Kozolec devices hidden / fed | 81.3 / 3.2, 84.5 / 3.1 | 82.5 / 3.9, 83.5 / 3.4 |

The owner, 2026-10-29: "that is absolutely great". The default is still valleys until he says to switch.

## The fall score as a probability (2026-10-29, bench dial FALL_SCORE, default legacy)

The owner's design: a fall closes the open run with the highest size likeness x pair factor x age vote, at
FALL_NOTHING_MIN 0.2 or more (fall_similarity: 1 within one noise, then a steep curve 0.5 at r = 0.75 with a
straight tail, a miss upwards x2.5; pair factor the evidence's share over chance, 0.7 with no partner, 0.4 a
partner of others; age a bell over log seconds, at most -0.3 younger, -0.5 older). With it: the 0.5 rule ends
the least likely still running first; joined groups keep their order counts (_fold_order); a fall leaving the
rest of the run's rise carried is a step down; two or three runs ending together are one candidate; a run is
scored also less the drops held since it started. Susilna's "too big" closes of compressor-sized runs 50 -> 23.

Cards (mixture, join 2), capture / impurity, legacy -> probability:

| | hidden | circuits | fed |
|---|---|---|---|
| Home devices (227.9 kWh) | 34.2 / 12.7 -> 27.4 / 9.9 (78.0 -> 62.5 kWh caught, 11.3 -> 6.9 wrong) | 37.2 / 13.3 -> 40.2 / 11.1 (84.8 -> 91.6, 13.0 -> 11.4) | 52.2 / 6.2 -> 53.8 / 6.4 (119.0 -> 122.6, 7.9 -> 8.4) |
| Home partition (604.6 kWh) | 44.8 / 10.9 -> 41.4 / 11.1 | 51.5 / 8.7 -> 51.2 / 7.8 | 56.7 / 6.5 -> 57.2 / 5.4 |
| Kozolec devices (251.2 kWh) | 82.5 / 3.9 -> 81.8 / 2.6 (207.3 -> 205.5, 8.4 -> 5.5) | - | 83.5 / 3.4 -> 83.1 / 4.1 |

Home hidden loses most at Polnilnica (70.1 -> 47.4 %, -10.4 kWh), Susilna (24.0 -> 20.9, -2.8 kWh), the office
plug and Blaževa Soba; Susilna fed gains (44.8 -> 48.7). The first probability card (no age bell, combinations or
held drops) had Home hidden at 32.9 / 10.7 and Kozolec at 78.1 / 8.3: the Kozolec fixes cost Home hidden - next
to trace. Kozolec's earlier loss was the age vote vetoing a 537 W run 21 s old (its fall then shrank an EVSE
charge), boiler + a 1 kW load falling together taken for one run, and a mid-charge drop held but not consulted
(the charge booked at its stop's 2,604 W for 3.4 h). Default left at legacy until the owner decides.

Then (the owner, 2026-10-29) two rules for the probability score: a leg of a run that rose on several phases is
ended only by a fall spanning all of them (Home's EVBox lost its A leg to a 1,751 W load's fall on A, its B leg to
a 1,372 W one), and a second rise of a running group never ends the first without a fall or the readings showing
it fell under half (the EVBox ramping 3.4 then 5.7 kW in one group lost its first part for the whole charge).
Card fall-legs-units, capture / impurity, legacy -> probability:

| | hidden | circuits | fed |
|---|---|---|---|
| Home devices (227.9 kWh) | 34.2 / 12.7 -> 36.1 / 13.3 (78.0 -> 82.3 kWh caught, 11.3 -> 12.6 wrong) | 37.2 / 13.3 -> 41.9 / 11.5 (84.8 -> 95.5, 13.0 -> 12.4) | 52.2 / 6.2 -> 54.0 / 5.6 (119.0 -> 123.1, 7.9 -> 7.3) |
| Home partition (604.6 kWh) | 44.8 / 10.9 -> 49.7 / 11.0 (270.9 -> 300.5, 33.1 -> 37.1) | 51.5 / 8.7 -> 54.9 / 9.2 | 56.7 / 6.5 -> 58.9 / 6.0 |
| Kozolec devices (251.2 kWh) | 82.5 / 3.9 -> 81.8 / 4.0 (207.3 -> 205.5, 8.4 -> 8.6) | - | 83.5 / 3.4 -> 82.9 / 4.1 |

Polnilnica hidden 70.1 / 7.0 -> 64.8 / 7.4 (47.4 before these two rules), the office plug 13.5 -> 22.7, Blaževa
Soba 18.8 -> 26.7, Susilna 24.0 / 14.3 -> 25.5 / 14.9 hidden and 44.8 -> 46.3 fed; Kozolec's boiler impurity
8.2 -> 15.0 hidden (3.1 -> 5.6 kWh wrong). The owner: "the results are good" - the probability score becomes the
only one: the legacy score, FALL_SCORE, PAIR_SCORE_MIN and UNITS_NEED_HISTORY are removed (2026-10-29); the
dials named above for earlier runs no longer exist.

## Mixture the only grouping, pair filing made real (2026-10-29)

The owner: drop the size density's groups and file by the pair. Removed: GROUPING (mixture only), the density's
cluster choice and its angle bands, split_groups / edge_cuts, merge_groups / edge_joins / GROUP_MERGE,
SPLIT_SHARE; MIX_JOIN_WIDTHS 3 -> 2 as benched. The density stays for input keying and meter placement. And a bug
found removing it: filing looked a device up by its pair (device_of) while runs were counted by start
(start_home), so no run found its device's signature - each founded one and the start's merge folded them back,
the Wash-born heater lost into a kettle's. Now pair_home is keyed by the device and holds the merge votes; a start
that learns its stop partner hands its record to the pair with its strongest partner (_promote).

Cards, capture / impurity (A = 582d4da with the mixture dials, B = density groups removed, C = + pair_home):

| | A | B | C |
|---|---|---|---|
| Home devices hidden | 36.1 / 13.3 (82.3 kWh caught, 12.6 wrong) | 37.8 / 12.0 (86.2, 11.8) | 36.1 / 12.4 (82.3, 11.6) |
| Home devices circuits | 41.9 / 11.5 (95.5, 12.4) | 38.8 / 12.8 (88.4, 13.0) | 38.9 / 13.7 (88.7, 14.1) |
| Home devices fed | 54.0 / 5.6 (123.1, 7.3) | 54.3 / 6.4 (123.8, 8.4) | 53.1 / 7.6 (121.0, 9.9) |
| Home partition hidden | 49.7 / 11.0 | 50.4 / 11.4 | 48.9 / 11.5 |
| Kozolec devices hidden / fed | 81.8 / 4.0, 82.9 / 4.1 | 81.8 / 4.0, 82.9 / 4.1 | 82.0 / 4.1, 82.9 / 4.1 |

A -> B is the split_groups cuts, which shaped the segments the input keying reads (circuits -7.1 kWh caught);
B -> C, pair filing, costs ~3 kWh of Home's 228 and is kept as the design (a device is its pair) with the bug
fixed. Two mixture weaknesses found on the way: dev/TODO.md INS-32.

## A second rise ends the first at a fall of its size; a new fall group is no evidence (2026-10-29)

Kozolec's boiler over-booked its main signature 30.24 kWh against 27.99 drawn, most of it three runs with no stop:
3 Oct 08:40 booked 2,180 Wh for 72.5 min while the boiler, pulsing 1.8 kW every 20-40 s, drew 835. The cascade:
the pond EVSE stepped 3,570 -> 2,190 W at 08:38:51 and the 0.5 rule closed the boiler for it; the boiler's next
fall went to the charge (0.662 vs 0.644), so 2.2 kW of charge sat in the reading with no run; each later boiler
rise came in a new step group (size off by up to 110 W, angle -43..+34 degrees - Kozolec's reactive from V x I
on a 4.5-6.3 kW reading) and each fall too, so the newest pulse, at FALL_NO_PAIR 0.7 and no age vote, took every
fall from the group-12 run at FALL_OTHER_PAIR 0.4 with an age vote of 0.57-0.72; and at every later group-12
rise second_rise found ~4.3 kW "left" for it and kept it. Two changes (the owner):

1. second_rise: a fall alike the first run (FALL_STEP_ALIKE) seen between the two rises that ended no run of the
   group ends the first there (PhaseState.closed_groups records each close's group; a fall that ended a run of
   the same group is that run's, so two units are still decided by the reading).
2. _score_held: FALL_OTHER_PAIR only where the fall's group is some start's partner (Library.is_partnered); a
   fall in a group nobody is partnered with scores every run at FALL_NO_PAIR.

Cards against the quantum-guard commit (capture / impurity, kWh caught and wrong):

| | before | after |
|---|---|---|
| Kozolec devices hidden | 82.0 / 4.1 (206.0, 8.8) | 86.0 / 4.7 (216.0, 10.7) |
| Kozolec Boiler hidden | 82.2 / 15.0 (31.6, 5.6) | 92.8 / 17.2 (35.7, 7.4) |
| Kozolec Pond EVSE hidden | 88.2 / 1.7 (132.4, 2.3) | 92.1 / 1.6 (138.2, 2.3) |
| Kozolec devices fed | 82.9 / 4.1 | 82.9 / 4.1 |
| Home devices hidden | 39.3 / 11.7 (89.6, 11.9) | 38.6 / 10.2 (88.0, 10.0) |
| Home devices circuits | 44.4 / 11.4 (101.2, 13.0) | 42.6 / 10.2 (97.1, 11.0) |
| Home devices fed | 53.8 / 6.3 (122.6, 8.2) | 53.9 / 6.1 (122.8, 8.0) |

Losses: Home's Polnilnica hidden 70.1 -> 62.3 (-3.6 kWh, beyond its band), Workshop boiler impurity +15 points
hidden and circuits (~1.3 kWh of 8.84), Kozolec's well pump 15.1 -> 0 (0.25 kWh). The boiler's main signature
now books 29.48 kWh against 28.11 drawn; its impurity rose because a second signature (17 runs, 5.85 kWh: 3.37
boiler, 1.1 Rest, 0.97 EVSE) now goes to it. Left: three no-stop runs (3 Oct 09:52 65 min +848 Wh, 7 Oct 12:52
+510, 3 Oct 10:59 +269), and the start of the cascade - an unmatched fall the 0.5 rule ends a run for when it
fits another run stepping down - for the owner to design.

## An unmatched fall is read two ways and a later step decides (2026-10-29)

The cascade's start on 3 Oct 08:38:51: a fall that ended nothing (the EVSE stepping down 1,437 W) left the
runs short of the reading, and the 0.5 rule closed the boiler for it - both readings fit to 25 W. Now
(_step_down, the owner): the largest other run the reading still carries once stepped down by the fall takes it
as a level, the run the 0.5 rule would have closed stays open, and PhaseState.step_pending holds the two
readings until a later step decides: that run closing (by its own fall or any way) - it was on; a rise of its
group, the stepped run stopping at its size before the step, or LOOKBACK_S (900 s) or its usual length past the
fall - it stopped at the fall after all, closed back there and the step undone. Choosing at the fall (by the
group that changes level most, the largest, the least alike) lost ~6 kWh at Kozolec and is in the detector's
AGENTS.md under ruled out, with the Susilna emptied-run cap. Removed with it: VALLEY_SIGMAS (a bench dial at 0).

Cards against 6ba2df5 (capture / impurity, kWh caught, wrong):

| | before | after |
|---|---|---|
| Home devices hidden | 38.6 / 10.2 (88.0, 10.0) | 40.9 / 9.8 (93.2, 10.1) |
| Home devices circuits | 42.6 / 10.2 (97.1, 11.0) | 42.8 / 11.8 (97.5, 13.1) |
| Home devices fed | 53.9 / 6.1 (122.8, 8.0) | 54.0 / 5.3 (123.1, 6.9) |
| Kozolec devices hidden | 86.0 / 4.7 (216.0, 10.7) | 85.0 / 3.3 (213.5, 7.3) |
| Kozolec Boiler hidden | 92.8 / 17.2 (35.7, 7.4) | 84.3 / 13.1 (32.4, 4.9) |
| Kozolec devices fed | 82.9 / 4.1 | 82.9 / 4.1 |

Polnilnica hidden 62.3 -> 77.1 (+6.8 kWh); losses: Workshop boiler hidden 33.0 -> 13.3 (-1.7 kWh of 8.84), the
Kozolec boiler's capture (-3.3 kWh, its wrong energy -2.5), Kozolec water pump impurity 7.3 -> 11.1.

## More meters must not lose energy: a meter's step counted twice, a charge's step down read as its stop (2026-10-29)

The owner: "giving the detector more meters makes some loads worse - investigate in detail". Kozolec's car
charger 28.9 kWh caught hidden, 21.7 fed; the pond EVSE 138.6 / 135.1 (wrong 1.5 -> 4.8 kWh); Home's Polnilnica
35.3 hidden, 27.6 circuits. Traced:

1. Kozolec 3 Oct 12:22:08: the charge's +2,189 W grid rise was cut into "Boiler 1,812 + 377" by metered_shares:
   the boiler meter's +1,888 W at 12:21:56, already the grid's own +1,857 step at 12:21:52, was offered again
   (_meter_totals had no guard for a same-way step the grid took as its own; _meter_started has had one). A
   phantom boiler run, the charge left at 377 W, and _meter_started's right answer ("Car charger 2,193 W, all of
   it") never asked. Now a meter step the grid took as another step of its own, ALIKE in size (_grid_took
   alike=True - a kiln pulse beside a pump's rise is the kiln's), is no share of this one.
2. A run a meter owns was ended by any meter fall that left it reading under the run's start ("cannot still be
   running on less than it started with", 2026-10-03): the pond EVSE lowering its charge 3,600 -> 2,868 W 25 min
   before it stopped (5 Oct; 4 Oct 37 min early), and the car charger's taper - closed at its first step down and
   booked at the smaller of start and stop, 148 min at 317 W, 786 Wh of 5,737. Now, on a meter holding one device
   (holds_one_device): its fall ends the run where it is about the run's level now, or the meter reads too little
   to carry the run and is back where it stood before the run started; a smaller fall the grid fell about as much
   with is a level of the run (Fleet._ended_by -> METER_LEVEL); a rise of the meter's again is that run coming
   back up (Detector._form_event, meter_single). A charge can go from 16 A to 6 A (the owner). Other meters keep
   the old rule.

Also fixed: the look back (_step_down) stepped a run from its start size while testing it at level_of (a learned
ratio can put that above the start): an EVBox leg stepped to -121 W (Home 7 Oct 18:11).

Cards against 7181bb1, devices capture / impurity (kWh caught, wrong):

| | before | rule on every meter | rises only on one-device meters | rule on one-device meters (shipped) |
|---|---|---|---|---|
| Kozolec fed | 82.9 / 4.1 (208.3, 8.9) | 90.2 / 1.4 (226.6, 3.2) | 90.2 / 1.4 | 88.3 / 1.4 (221.8, 3.1) |
| Home fed | 54.0 / 5.3 (123.1, 6.9) | 51.6 / 7.3 (117.6, 9.3) | 52.6 / 7.4 | 53.7 / 5.7 (122.4, 7.4) |
| Home circuits | 42.8 / 11.8 (97.5, 13.1) | 43.4 / 10.4 | 41.9 / 12.1 | 44.6 / 9.5 (101.7, 10.7) |
| hidden, both sites | | unchanged | unchanged | unchanged |

On every meter, Susilna's plug (fan + compressor), the office strip and Blaž's PC lost 6-12 points fed: their
devices' stops became levels of one run and their starts levels again, every compressor cycle one run. Shipped:
pond EVSE fed 90.0 -> 97.4, car charger 72.5 -> 80.2 (its strip is not guessed one device: 96.6 with the rule on
every meter), Home circuits Susilna +3.1, Hidrofor +3.0, Blaž's PC +6.1; Home fed Polnilnica 69.0 -> 66.7 (in its
band). Found on the way: with the rule on every meter Polnilnica gained 12-14 points in circuits and fed - Hiša's
channels own legs of the EVBox's three-phase charge (7 Oct 18:16:06, phase C) and its other loads' stops ended
them. Next to trace.
