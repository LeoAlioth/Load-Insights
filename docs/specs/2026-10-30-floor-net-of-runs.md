# The idle floor learns only what the open runs do not explain

Status: agreed 2026-10-30, not built yet.

## The problem

Each phase keeps an idle floor (`PhaseState.baseline`): the level it reads with nothing known running.
It follows quiet stretches slowly (`BASELINE_EMA`). A load that runs for hours is, to that learning,
just a higher quiet level, so it is taken into the floor:

- Kozolec's pond pump: 46 W, on 84 % of the time, 149 switches in 16 days, steps well above the 7-10 W
  noise - 14.6 kWh, caught 0 % in every mode.
- Susilna's compressor (~20 h runs): its runs closed as "emptied" when the phase drops back to a floor
  that already holds it (43 of 60 closes of one week's runs).

## Finding before building (2026-10-30)

The floor already learns only while no run is open (`PhaseState._idle()`: `baseline` follows the reading
only with nothing believed running, `BASELINE_EMA`). So a long-running load is not absorbed while its run
is open: its run must end first (some close - the 0.5 rule, `_floor_stop`, the emptied-run rule, an
unmatched fall, `MAX_OPEN_EDGES` eviction), after which the phase reads idle with the load still on and the
floor climbs to it. Next: trace the pond pump's runs at Kozolec (hidden) - is the start declared, and what
ends each run - and fix that path; "floor net of open runs" below then matters only where a run is open
and the floor still moves (none today) and becomes the guard, not the fix. The base load as a detected
load stands as designed.

## The change

The floor learns from the reading **less the open runs' levels** (`level_of`), never from the raw
reading. A load whose start step was seen stays a run until its stop; the floor under it is what is
left. Where no run is open nothing changes.

## What if the base load really changes

| case | what happens | guard |
|---|---|---|
| A new always-on load starts **without** a step (ramps in, or starts in a gap) | the residual rises; the floor follows, as today | none needed |
| A base load **goes away** without a step | the residual falls; the floor follows down, as today | the floor never goes below zero on a house reading (as today) |
| A new always-on load starts **with** a step (a server plugged in) | it is a run that never stops; booked for ever as a load | **adoption**: a run open longer than `FLOOR_ADOPT` (48 h; by time alone - a load that only rises never has a pair, so no usual length; the owner, 2026-10-30) with no stop seen becomes a step up of the meter's base load (below); booked as a run until then, as base load after |
| A run left open wrongly (its stop missed) | the floor under it is learned too low; later runs read too big | the existing ends still apply (the 0.5 rule, the look back, a rise of its group); plus a **contradiction check**: the residual under the floor by more than the noise for `FLOOR_CONTRADICT` (proposal: 10 readings) ends the least likely open run, before the floor may move down |
| Slow drift of a running load (a fridge warming up, a charger tapering) | the run's level is its group's learned level, not the reading; the drift shows in the residual and moves the floor a little | accepted - the same as today's error, now on the floor instead of the run |
| A restart or a rebuild | open runs and the floor are stored together; a rebuild starts with no runs and the floor seeds as today | none needed |

## What it touches

- `PhaseState`'s floor update (where `baseline` follows idle readings), `_floor_stop`, the
  emptied-run rule in `_score_held` (it compares with the floor), `open_scale`.
- New: `FLOOR_ADOPT` (a judgement, seconds), `FLOOR_CONTRADICT` (a count of readings).

## How it is judged

Cards at both sites, every mode; the pond pump and Susilna as the targets (truth, caught, wrong in kWh);
the Kozolec fridges and Home's always-on loads (Server UPS, the office strip) as the risk - they must
not lose; slicing invariance (live vs six-hour slices identical).

## Base load as a detected load (the owner, 2026-10-30)

Each meter's floor is offered as a load of its own - "Base load", or per phase "Base load X/Y/Z" -
booked as floor x time, optional per meter and per phase in the configuration. Adoption is then a level
change of that load, a floor drift without a step its slow change, a base load going away its step down;
nothing is thrown away. Check first whether the forecast side's BaseLoadSensor can carry it.

Decided (the owner, 2026-10-30): on by default for every meter, as the meter's total; split by phase
off by default (an option per meter); a base load under `BASE_LOAD_MIN_W` (a couple of watts - a
policy of what to show, not a measurement) is not offered.

Status: design agreed; to build, bench on the cards (targets and risks above) and ship by the usual rule.
