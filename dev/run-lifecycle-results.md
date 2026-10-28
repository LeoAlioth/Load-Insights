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
