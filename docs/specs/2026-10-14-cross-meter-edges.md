# Cross-meter edges - design (2026-10-14)

Agreed with Anze in conversation, 2026-10-14. Stage 1 of reworking what an
edge is: **edge detection and grouping across meters only**. How well the
groups match real devices comes later, and so does sharing a parent meter's
change between coinciding edges.

## What an edge is

An edge is a **moment of change**. It holds one **entry per fed meter and
phase** - the grid's three phases, each circuit meter's channels, each plug -
the same way a multi-phase edge already holds each phase separately.

- An entry is that meter's **own declared step** (its own detector, the same
  measure on every meter - "one measure for every meter"), with its
  properties:
  - ΔP, raw watts (no 230 V sizing - not every load follows voltage)
  - ΔQ and the reactive angle atan2(ΔQ, ΔP)
  - surge ratio (first readings' excursion past the new level ÷ the step;
    1 where there is none; recorded for every step, rises and falls)
  - ramp (readings and seconds between the old level and the new)
  - settling (the on-side level's change over its first / last minute ÷ the step)
  - wobble (the on-side spread minus the off-side spread ÷ the step)
  - voltage before and after (the meter's own where it has one)
  - its time and its reporting interval then
- An entry is **0** where the meter reported through the moment and declared
  nothing, and **unknown** where it did not report within its latency.
- **Nothing is overwritten**: every meter's entry is kept as that meter saw it.
  Choosing between entries is the grouping's business.
- **Place**: the innermost meter with a non-zero entry - for rises and falls
  alike (today only rises are placed).
- **The same moment**: each meter is read over its own learned latency
  (`Fleet._learn_lags` / `PhaseState.latency`). No new window rule.
- **Separate branches are separate edges.** Steps at the same moment on meters
  neither of which hangs under the other (the kettle under Mansarda, the
  hidrofor on its own plug) are two edges. A parent's entry that both reach
  into is kept on both, marked **shared** (with the edges it is shared with);
  attributing it between them is a later stage.
- An edge can be **born on any meter**, including a step only a sub-meter saw
  (NASA's ~15 W under Mansarda, below the grid's noise).
- A plug's channel joins the grid phase its learned phase map gives
  (`Fleet.phase_map`); an unmapped channel may join any phase.

Kozolec and Andrej have no circuit meters: there an edge is the grid's step
plus the plugs' entries.

## Grouping (stage 1)

Edges are grouped per (place, direction, phase pattern on the place meter),
by the place entry's size density (valleys on the measurement-error scale,
`edge_scale` / `valley_segments`, as today) and then its angle (as today).
The other entries and properties are recorded on each group (median, spread)
but do not split it yet - weights over them are a later experiment.

## The scorecard (no devices)

Edges:

1. **Conservation** - in each edge, every parent's entry against its
   children's entries plus the unmetered rest, within both meters' noise.
   Share that adds up, per meter pair; the ones that do not are the worklist.
2. **Coverage** - per meter, the share of entries unknown (the meter too slow
   for its latency), and the share of edges where an ancestor of the place
   shows 0 though the place stepped clearly above the ancestor's noise.
3. **Slicing invariance** - the same edges (time, place, entries) replayed in
   one call, in 6-hour slices and in 1-minute passes.

Groups:

4. **Tightness** - per group, the place entry's spread ÷ that meter's
   measurement error at that size, weighted by edges (1 = as tight as the
   meter allows).
5. **Mixed** - share of edges in groups where a recorded property (angle,
   surge, settling, wobble, ramp) splits into two piles each holding at least
   20 % of the group (should have been split).
6. **Duplicates** - share of edges in groups with a same-key neighbour whose
   size and angle agree within measurement error (should have been merged).
7. **Count** - groups; groups with >= 5 edges in the ten days; share of edges
   in smaller ones; groups seen on >= 3 different days.
8. **Mirror** - share of rise groups (by edges) with a fall group of the same
   place and phase whose size and angle agree within error.

4-8 pull against each other (splitting more helps 4-5, hurts 6-8), so they
are read together, like capture and impurity.

Every figure is also computed for today's edges (the grid's steps and their
EdgeClusters) where it applies, as the baseline.

## Scope of stage 1

- `insights/edges.py` (pure, no Home Assistant): the Edge record, the
  streaming assembly from every meter's declared steps (carried between
  passes, decided only once every meter is past the moment plus its
  latency), the grouping, and the properties computed from a meter's own
  readings.
- The detector is unchanged: pairing, runs and devices still use today's edges.
  The assembly is fed alongside it in replays; production wiring comes with
  the stage that makes these edges the detector's.
- `tests/edge_bench.py`: the scorecard at Home, Kozolec and Andrej (fed
  meters, SUBS=prod), invariance over SLICE=0 / 6 / LIVE=1, and a `diff`.
- `tests/test_edges.py`: assembly (nested meters -> one edge; separate
  branches -> two edges, the parent shared; a sub-meter-only step is an
  edge; 0 versus unknown), slicing invariance on a small case, and the
  scorecard's arithmetic.

## Later

- Attributing a shared parent change between coinciding edges.
- Weights over the recorded properties in the grouping (the "A versus B"
  bench of 2026-10-14).
- Each group's voltage response k (P ∝ V^k), from its edges' ΔP and voltage.
- Making these edges the detector's (pairing, runs, devices), and scoring
  against devices again.
