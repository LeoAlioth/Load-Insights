# LI 1 - daily retroactive repair pass: ship it (plan, 2026-10-12)

Personal project (Load Insights). Never log to work-context.md.
Repo `~/Projects/Load_Insights`, branch `dev` at 482aa9a (= origin/dev).

## 0. Finding: exp15 is not lost - it is already on dev, always on, and pushed

- **Commit c689283** (2026-10-04 17:24, "Once a day each phase's runs of the finished day are held to ...
  (exp15 ...)"), a direct ancestor of dev; first build tag `0.4.1-dev.20261004.1524`, so every dev build
  since - including today's pushed 482aa9a - carries it. Home and Kozolec auto-install dev, so production
  has run it since 2026-10-04 (to confirm, step 6).
- **No dial.** It runs unconditionally: `Fleet.process` -> `_run` due rank 6 at
  `_repaired_to + REPAIR_DAY_S + REPAIR_WAIT_S` -> `Fleet._repair(a, b)` -> `_cap_day(det, log, rows, a, b)`
  per detector (detect.py: constants ~L788-800, `Detector._day_from/_day_log/_day_rows` ~L3827,
  row capture ~L3896, run log ~L4177 and ~L6370, `Fleet._repaired_to` ~L5416, seeding ~L5507,
  due ~L5601/L5643, `_repair` ~L5651, `_booked/_hour_of/_unbook/_floor_of/_cap_day` ~L7403-7490).
- **Statistics path already wired**: the repair lowers `Session.wh` and `Signature.hourly/hour_wh/day_wh`;
  `detection.py` `_hour_compiled` (EVENT_RECORDER_HOURLY_STATISTICS_GENERATED) -> `_check_named` ->
  `async_backfill_statistics(name, force=False)` -> `plan_rewrite` + `first_change` -> `_write_statistics`
  (`recorder.async_import_statistics` into `Statistics` and `StatisticsShortTerm`, then
  `async_adjust_statistics` for the shift). No new code needed.
- **Already done in c689283**: RELEASE_NOTES 0.4.1 entry ("A day's loads never add up to more than the
  phase drew."), two tests (`test_the_days_repair_gives_back_what_the_phase_never_drew`,
  `test_the_days_repair_runs_once_a_day_on_the_readings_clock`), the bench's kitchen-fridge heuristic truth.
- **Distance from current dev**: 19 commits on top (exp20 identity hold, phase_map vote counter, density
  cache, driver regression dropped, HELD_DROPS 0 semantics, angle always on bd93df0, refactors 5362664 /
  788b60f, multi-close remembers 482aa9a). `git log -S_repair` shows only c689283: the code is untouched,
  but **it has never been re-benched on top of those changes**. Its scorecards (26575f8 four-way card,
  the overnight the third site numbers) are not in `data/scorecard/`.
- **What is actually missing**: (a) a measurement on current dev; (b) the slicing test is vacuous for the
  repair - checked today: its kiln scenario books 12,544.47 Wh with the repair and with it off
  (`REPAIR_SLACK_NOISE=1e9`), i.e. the repair cuts nothing there; (c) AGENTS.md still lists it under
  "To explore", unticked, and the dial tables have no `REPAIR_*` rows.

So "ship it" = measure it on today's dev, close the test gap, fix the docs. No detector change, no dial
to add or remove, no generation bump, no deploy beyond what the owner already pushed.

## 1. Ablation - no code needed

`REPAIR_SLACK_NOISE=1e9` turns the repair off exactly (`over = active - max(drew - floor + slack, 0)` is
always negative, nothing is taken); `bench._apply` accepts it (checked). This is the "dial" for the bench;
nothing is committed for it.

Prereqs: `git status` clean, `python3.14 tests/run_all.py` passes, nothing writing to `data/history/*`
(no fetch running), libench only if the owner is not using it - otherwise the Mac with `BENCH_PARALLEL=8`.
Data: the ten days in `data/history/<site>/` as they are (2-11 Oct).

```bash
cd ~/Projects/Load_Insights
export BENCH_PARALLEL=8
python3.14 tests/energy_bench.py card LABEL=repair-on
python3.14 tests/energy_bench.py card REPAIR_SLACK_NOISE=1e9 LABEL=repair-off
python3.14 tests/energy_bench.py diff data/scorecard/482aa9a-repair-off-*.json data/scorecard/482aa9a-repair-on-*.json
```

Noise band: the newest one is `noise-8da9de1-dirty-20261004T094503Z.json` - before the repair, the
angle and the rest. Re-measure on 482aa9a (repair on), then re-run the diff against it:

```bash
for n in drop:10000:1 drop:10000:2 drop:10000:3 jitter:1:1; do
  python3.14 tests/energy_bench.py card NOOP=$n LABEL=noise-${n//:/-}; done
python3.14 tests/energy_bench.py noise data/scorecard/482aa9a-repair-on-*.json data/scorecard/482aa9a-noise-*.json
python3.14 tests/energy_bench.py diff data/scorecard/482aa9a-repair-off-*.json data/scorecard/482aa9a-repair-on-*.json data/scorecard/noise-482aa9a-*.json
```

Once, repair on: `python3.14 tests/energy_bench.py card SLICINGS=all LABEL=repair-slicings` - every
slicing must agree and invariance stay 0 (the repair is due on the readings' clock; this proves it on
real data, which the unit test did not).

Cost: 7 cards (2 + 4 noise + 1 four-slicing card = ~4x). Run the two plain cards first; read them; only
then the noise and slicing cards.

## 2. Acceptance (off -> on)

Pass when all hold:

1. **Impurity down** (pooled, the headline) at every site and mode where something is attributed:
   Home devices and partition - hidden, circuits, fed; Kozolec devices and partition - hidden, fed;
   The third site charger and partition - hidden, fed; home-hisa, home-mansarda. "Down" = lower, or within its
   band where the exp15 run showed no gain (Kozolec fed).
2. **Capture**: site figure loses about a point or less (exp15: ~1); any loss beyond 2 points on a site
   figure, or beyond its band, is a stop-and-ask.
3. **No load collapses**: no per-load capture drop marked `[band!]` that also exceeds 10 points on a load
   with >= 1 kWh measured. Check by name: Home - Polnilnica (EVBox), hidrofor, workshop boiler, Sušilna,
   office, Blaževa Soba, Server UPS, Kitchen fridge (heuristic table); Kozolec - boiler, pond EVSE, car
   charger, water pump, well pump, IR panel, Fridges (heuristic); the third site - go-e charger. Long-running and
   tapering loads (chargers, boiler, Sušilna's 20 h runs) are the ones the cap can cut wrongly - read them
   first.
4. **Ownership flips**: list every signature whose owner changed (exp15 flipped the office plug's,
   47.0/6.8 -> 54.9/10.5). A flip is acceptable only if the new owner's impurity is not worse by more
   than its band.
5. **Pairing block and session counts identical** on and off (the repair changes Wh, never filing); a
   difference is a fault to trace before anything else.
6. `SLICINGS=all` card: one figure per cell, invariance 0.

Fail -> do not change anything alone. Already live since 10-04, so the options go to the owner: keep; sweep
`REPAIR_SLACK_NOISE` 1 / 2 / 4 and `REPAIR_FLOOR_SHARE` 0.05 / 0.1 / 0.2 (bench dials, one card each); or
remove (revert c689283's detect.py hunks, generation unchanged - Wh lowered in libraries already rebuild
away within ten days).

## 3. Production concerns (checked in code; actions in brackets)

- **What the rewrite touches**: only named loads that are not a metered device; only the hours whose
  `Signature.hourly` moved - yesterday's, from the first changed hour (`first_change`) on, hour rows plus
  that span's five-minute rows, and one `async_adjust_statistics` shift for every later row. Unnamed
  signatures change only in the library.
- **Recorder load**: once a day per named load, ~24-48 hour rows + ~300-600 five-minute rows, at the first
  hourly compile after 00:25 on the readings' clock (REPAIR_WAIT_S = MATCH_PATIENCE_S + METER_WAIT_CAP_S =
  1,500 s). The same path already runs every hour for name changes. [none]
- **Restart mid-day**: `_repaired_to`, `_day_log`, `_day_rows` are not persisted. After a restart the day
  in progress is repaired with only the runs filed and rows read since the restart; a run is capped only
  where rows exist; the floor is the 10 %-by-time level of the rows since the restart. Restart late in a
  busy evening -> the floor can read a running load's level -> that day's unmetered runs capped low.
  Errs towards under-report (the accepted side). [Add the restart test, step 4; measure nothing more
  unless the owner asks. Upgrade path if it ever shows: persist `_repaired_to` and skip a phase whose rows
  cover under half the day - not now, fewest special rules.]
- **Reset / generation bump**: the 10-day backfill passes through every day's due, so each finished day is
  repaired during the re-read and `_rewrite_named` writes them all at its end. [none]
- **DST**: `main.tz_offset_s` is set once at setup/reset; after 25 Oct the day boundary sits an hour off
  local midnight until the next restart. Any 24 h window is a valid day for the cap. [note only]
- **Storage / generation** (AGENTS "Storage and generations"): nothing persisted changes meaning -
  `Session.wh` and `Signature.hourly` keep their meaning, just smaller values; no new stored field.
  **No `DETECTOR_GENERATION` bump** (c689283 made none - correct).
- **Upgrade path for existing installs**: Home and Kozolec already run it (since the 10-04 builds); days
  before their first repaired day keep the old hours, as RELEASE_NOTES says. No reset needed. The third site: not until the owner releases there.

## 4. Tests to add (`tests/test_detect.py`; only after the bench passes)

1. Make the slicing test bite: `test_the_days_repair_is_the_same_at_every_slicing` - one phase, a run
   whose stop goes unseen (a start with no matching stop, closed late) beside a kiln, three days; assert
   (a) the repair took something (`sum(energy_wh)` with it < with `REPAIR_SLACK_NOISE=1e9`, restored in
   `finally`), (b) `_as_filed` and every signature's `hourly` equal at one call vs 3,600 s passes vs a
   600 s pass, (c) no run's Wh above its unrepaired Wh (never adds). Reuse `series`, `kiln`, `_passes`,
   `_as_filed`; no new helpers.
2. `test_the_days_repair_after_a_restart` - same history, `Fleet.from_dict(fleet.to_dict())` at 18:00 of
   day 2: day 1 and day 3 repaired as without the restart (equal `_as_filed` for runs outside day 2);
   day 2 never above its unrestarted Wh.
3. Run `python3.14 tests/run_all.py`.

## 5. Docs (only after the bench passes)

- **AGENTS.md**
  - "Known shortfalls -> To explore or discuss": remove the "Explore: a daily retroactive repair pass"
    bullet (the in-stream candidates' "moved 5 runs" and the #64 kitchen-fridge note go with it into the
    new section).
  - New `### The day's repair` under "The dials" (after "Library housekeeping"): three lines on the
    mechanism (`Fleet._repair` / `_cap_day`, due 25 min past local midnight on the readings' clock, cap
    = drew above the day's floor + slack, unmetered runs first, longest open first, never adds; the
    hourly statistics check carries it to the named loads), then the table:

    | dial | value | what it does | kind | valid range | tested |
    |---|---|---|---|---|---|
    | `REPAIR_DAY_S` | 86400 | the repaired span | policy | - | not tested |
    | `REPAIR_FLOOR_SHARE` | 0.1 | the day's floor: the level a phase held this share of the day, by time - the scorecard's idle floor | ratio | 0.05-0.2 | exp15 only |
    | `REPAIR_SLACK_NOISE` | 2.0 | slack above the drawn level, in measured noise | count | 1-4 | exp15 only; `1e9` = off (the bench's ablation) |
    | `REPAIR_WAIT_S` | MATCH_PATIENCE_S + METER_WAIT_CAP_S | wait past the day's end so its last runs are filed | derived | - | - |

    plus the shipped history: c689283 (2026-10-04) numbers, and the 2026-10-12 re-bench on 482aa9a
    (off -> on, per site, with the new band file name).
  - Under "Confirmed, unfixed": one bullet - a restart repairs the day in progress from the restart only
    (floor from the rows since then; under-reports, never over); DST shifts the day boundary until a
    restart.
- **RELEASE_NOTES.md**: the 0.4.1 entry exists. Change only the bench figures in its parentheses to the
  re-bench's (e.g. "bench, 2-11 Oct: impurity Home x -> y with meters, Kozolec ..., the third site ...; about a
  point of capture"); add nothing new - it is not a new feature of a later build.
- **Job note** `data/scorecard/jobs/repair-2026-10-12.md` in the angle note's format: data range, the
  on/off diff per site and mode, band file, ownership flips, verdict.

## 6. Commit, then deploy only when the owner says

1. One local commit on dev: tests + AGENTS.md + RELEASE_NOTES.md, message carrying the re-bench evidence
   ("The day's repair re-benched on 482aa9a (REPAIR_SLACK_NOISE=1e9 off vs on, 2-11 Oct): Home ...,
   Kozolec ..., the third site ...; band noise-482aa9a-...; slicings agree, invariance 0. Tests: the repair at
   three slicings and across a restart. AGENTS: out of To explore into The dials. run_all passes.").
   No new branch, no worktree, **no push**.
2. Production check (read-only, any time): on Home and Kozolec read the installed Load Insights version
   (>= 0.4.1-dev.20261004.1524) and, after the next 01:00 local hourly compile, the log line "Rewrote
   <name> ... kWh less than recorded" for a named load - the repair reaching the Energy dashboard.
3. Push dev only on the owner's word. It auto-deploys to Home and Kozolec (HACS webhook) - docs and tests only,
   so no restart needed for behaviour, no reset, no generation bump. Not the third site.

## Order of work

1 two plain cards -> read -> 2 acceptance -> (pass) noise cards + SLICINGS=all -> 4 tests -> 5 docs and
job note -> 6.1 local commit -> report to the owner -> 6.2/6.3 on his word.
