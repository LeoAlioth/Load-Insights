# Run lifecycle: runs by step group (design, 2026-10-28)

Agreed with the owner in chat on 2026-10-28 (brainstorm after the Susilna and Inkubator investigations). Vocabulary:
the Terms entry in `AGENTS.md`, which carries the same decisions as draft notes.

## Why

- Susilna (Home, the dehumidifier room) is caught 22 % hidden. About 50 kWh of the leak is compressor starts closed
  minutes in by an unrelated fall of the same size: `PhaseState._pair` closes the newest open start that fits by
  size, with no regard to age.
- Home's Inkubator (a ~60 W, ~22 s pulse) read up to 574 W live. A replay of 3-13 Oct showed one open start
  following the phase's whole level (`_Open.now = level - baseline` while it is the only open start: a 39 W pulse
  read 306 W), and starts counted live long after their group's usual run (270 of 1,024 older than 3x it).
- The learned pairs (`Library.pairs`, `A>B`) are taught only by the closes they steer, so a wrong close teaches the
  pair; their learned run length is stored and never used (`_pair_by_model` checks size only).
- Falls are per phase while rises are all-phase events, which leaves a three-phase load's legs to close one by one
  (the kiln's single-leg fragments).
- The daily pass sees one day: a run filed after its day's repair is never repaired, and the readings before
  midnight are dropped (`Fleet._repair`).

## Design

**Steps and groups.**
1. Rise and fall steps are one kind of object: an all-phase event either way, with the same event window. A fall
   group has a phase pattern as a rise group does.
2. A step group keeps steps together until there is evidence they are two devices. Benched (2026-10-28):
   cutting the smoothed size density only at significant dips (VALLEY_SIGMAS) is monotonically worse, and the
   method cannot separate two loads closer than about two smoothing widths at all. Redesigned with the owner: a
   group is a bell curve over size and reactive angle, its width fitted from its own steps but never under the
   meter's step error (sqrt(2) x its noise); one or two is decided by BIC, overlaps (5) entering as cannot-link
   pairs. At Home a group's size is corrected to 230 V by its own exponent k (P ~ V^k, fitted in the daily pass
   from each step's on-side voltage, stored with the step - before and after); where the voltage hardly moves
   (Kozolec's inverter) there is no k and sizes are taken as measured. Current is no dimension; it stands in for
   the voltage where a meter reports current and no voltage. Grouping is benched on its own (fragmentation and
   merging per metered device), not only through the energy card. Plan: Stage 6, Tasks 8b-8e.

**Running groups.**
3. A rise group whose step was seen is running, with a count of units. A run is still one start step joined to one
   stop step, with its levels, for energy; what changes is how the stop is found.
4. A rise of a group already running is a missed fall unless the phase reading, less the other running groups,
   still carries the first unit at the second rise: then it is a second unit and the count goes to 2. When the
   reading cannot tell (a load under the phase's noise), it is a missed fall. A missed fall ends the old run where
   the readings dropped by its size (look-back), else at its usual length, at most the new rise.
5. Each overlap (count 2) is a cannot-link pair: two rises that are different devices. Groups collect them; the
   daily pass splits a group along a feature (size, angle, surge) that separates them. A group no feature splits
   is learned as having several identical units (a water heater's two equal elements). How a group's unit count is
   learned and used is to be discussed with the owner before it is built.

**Closing a run.**
6. Steps are no longer paired individually by size and recency. A fall is classified into its fall group, then
   scored against each running rise group on its phase set by: (a) size and power factor/angle fit, comparing the
   fall with the very rise that started that run; (b) partner evidence; (c) age against the pair's learned length,
   weighing less the wider its spread (a boiler's runtime follows the weather). The best candidate above a floor
   closes; none, and the fall is unmatched.
7. Partner evidence is order, not a fixed delay: how far above chance the fall group has come while the rise group
   was running. Counted at every fall against the groups running then, whatever closed what, so a wrong close
   teaches nothing. Chance is the fall group's rate times the share of time the rise group runs.
8. With no partner evidence the size and power-factor match is tight (the two steps' measured noise, not today's
   15 %); it widens to the pair's learned ratio spread as evidence grows. A drifting load (rise 390 W, fall 300 W)
   fails the tight match at first, its pair is still learned by order, then the learned ratio closes it.
9. A dip is a level change inside a running run, not a pair of its own: an unmatched fall while a group that can
   carry it runs is held; a matching rise within a few reading intervals makes both level changes of that run and
   nothing opens or closes; none, and it is an ordinary unmatched fall.
10. A staged device's stages are separate runs. That they are one device is decided higher up.

**Ending a run with no fall.** The readings decide where a run ended.
11. Missed fall: closed once the reading, less the other running groups, is under half its running level (the
    pair's learned running level once known, its rise until then).
12. Fall seen in a non-partner group (a load whose start and stop differ, Home's old upright freezer; or a group
    holding two devices): handled by 7, 8 and 5.
13. Load still running: the case not to end. No rule may end a run the reading still carries.
14. A rise that was never a load (a transient, a dip's return): handled by the sustain guard, the surge and 9.
15. Age alone ends nothing; it only stops a run counting in live power, at `LIVE_STALE_FACTOR` (3) times its group's
    usual length. The 24 h `MAX_OPEN_S` stays as today's last safety net (the owner, 2026-10-28).
16. A fall with no running partner is a reason to look back for its rise (missed, or misclassified into a
    neighbour group), in the daily pass over the readings: a rise netted against another load's fall in one
    reading was never declared, so live there is nothing to search.

**Sub-meters.**
17. A run whose start a sub-meter owns is closed only by that meter. Its power, from the meter's own reading, is
    subtracted from the parent's reading in the parent's closing rules (11). The meter's reading closes the
    meter's own runs left open (11 one level down).

**Power.**
18. A running group draws its learned level (its rise, or the pair's running level once known), not the phase
    reading it is followed to. What the phase carries beyond the running groups is unknown. This sets live power
    and booked energy alike. The live cap of 1.25x the name's strongest signature (`live_ceiling`) stays.

**The daily pass.**
19. On the readings' clock, as today, so a rebuild's 6 h slices change nothing.
20. Steps are kept about 10 days in a store of their own (`load_insights.<entry>.detection.steps`), saved after each
    daily pass and on a slow timer. A step is one change, whatever meters saw it: about the grid meter's count
    (~9,750 a day at Home) plus the small steps only an inner meter resolves; ~0.4 MB of JSON per 10,000 steps
    before their per-meter edges.
21. The readings for the look-back (~48 h) are read again from the recorder (Home keeps ~4 days), not held by the
    detector. The runner reads them before the pass that crosses a day boundary, so the repair still runs at its
    fixed point in the stream.
22. Each night re-books every run not yet final, including those crossing midnight, decides group splits on all
    the cannot-link evidence collected (fading with the group histograms, 10 days), and looks back for missed falls
    and missing rises.

## Unchanged unless the bench says otherwise

The settle rule (a pump's start decaying), the multi-close (several loads stopping in one fall) and the joint stop
(drops held since a start) were not discussed; they stay, re-judged on the bench at the stage that replaces `_pair`.

The settle rule stays for groups whose pair is not yet known; the multi-close is not a dip (no return follows) and
stays (the owner asked, 2026-10-28).

## Open points (the owner, 2026-10-28: build a few variations, bench them, then discuss)

- How a group's unit count is learned and used (5).
- The significance test for a valley (2).
- The dip window, in reading intervals (9), and the closing floor in (6).

## Standing decisions

- Stages are built in order and each stays in place whatever its own bench shows; judged together (`AGENTS.md`).
- Steps are declared on the site consumption (`combine`), never on the raw grid meter while PV runs through it.
- One measure for every meter: every meter's steps, groups and runs by the same code.
- Tolerances are counts of measured error, never fixed watts (`AGENTS.md`, "The one rule that matters").
