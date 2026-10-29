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
