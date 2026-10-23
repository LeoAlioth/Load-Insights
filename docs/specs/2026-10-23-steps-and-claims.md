# Steps, claims and system phases (design, 2026-10-23)

Agreed with the owner in chat on 2026-10-23. Vocabulary: the Terms entry in `AGENTS.md` (Standing rules).

## Why

Home's dishwasher and Sock Eater share a circuit (Mansarda C = grid phase A) and a power; on the corrected scorer
(`scratchpad/dw/dw_bench.py`, Home 13-22 Oct, `SUBS=prod HOUSE=prod`, all AEG states fed, `INPUT_RARE_SHARE=1.1`)
the dishwasher's 17 filed sessions sit in 15 signatures (8 of 9 bursts hit, best signature 2 sessions), the Sock
Eater's 50 in 33 (34 of 50 hit). About 11 of the 17 dishwasher sessions are in the grid's own library although
Mansarda's meter saw the burst cleanly. Causes found: one burst is filed by several meters with separate libraries;
only rises are placed at a meter (`_step_meter`, falls only teach the gain and `GAIN_FALLS` is off), so a stop is
grouped plain; the grid's noisy size founds the group; duration enters at filing (`MATCH_DURATION_FACTOR = 3`).

## Design

**Stage 1, system phases.** The grid meter's phases, in meter order, are X, Y, Z (`PHASES` in `detect.py`, today
`("a", "b", "c")`). Every other meter's phase maps onto them with the existing `phase_map`; edges, step groups and
signatures record the system phase once it is mapped, else the meter's own label as today. Pure rename: replay output
is identical session for session. Diagnostics show "X (L1)". Saved libraries change through a `DETECTOR_GENERATION` bump.

**Stage 2, claims.** Each meter detects its own edges by the same measure. Meters are taken inner to outer. An outer
edge is claimed by an inner meter's edge of the same direction, size within noise plus `METER_CAL_SLACK` (gain
learned), timing within the union of both spans (today's `_step_meter` tests, reused), on the mapped system phase
or any candidate while unmapped; a meter that only held its value cannot claim. Linked edges are one step recording
the change on every system phase (untouched ones about zero; the phases it touches are its phase set). Owner =
innermost claimer; size, power factor and timing come from it, outer edges cross-check. Falls are claimed like
rises, so a stop is an owned step and `_meter_stop`'s lookup through the run's owner goes away. An unclaimed outer
edge, and an inner edge nobody confirms, are steps with one edge. Claims are made once the outer meter is complete
through the horizon. The grid's libraries hold only unclaimed steps.

Phase maps: wiring is static, so a map is learned once, stored and re-checked daily, replaced only on strong
evidence. With no stored map (new install, generation bump) a first pass over the recorder history claims on size
and timing alone and every claim votes. Claims in that pass are provisional.

**Stage 3, step groups.** Step groups are keyed by owner, phase set and direction, grouped by the owner's measured
size. The device states and their transitions at each step join the key by the existing above-chance rule with all
states fed; `STATE_TAG` (edge-level tagging, no bench gain) is removed. Run pairing is loose on duration; filing
loosens `MATCH_DURATION_FACTOR` (value from the bench); the daily rewrite uses spacing and length to split what is left.

## Standing decisions

- Steps are declared on the site consumption (`combine`), never on the raw grid meter while PV runs through it.
- A step records every system phase; this stays whatever the bench shows.
- Stages are built in order and each stays in place whatever its own bench shows; judged together.
- Not in this plan: renaming the code (`Session` to run, `EdgeCluster` to step group, `where` to owner); done after
  stage 3 on the owner's go.
