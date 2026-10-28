# Run lifecycle: runs by step group - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Runs start and end by step group instead of by pairing individual steps, so a start is closed by its own load's stop, an open start never takes the phase's other loads, and the daily pass sees across midnight.

**Architecture:** Seven stages, each kept whatever its own bench shows and judged together. (1) Falls become all-phase events like rises. (2) Running groups with a unit count, and partner evidence by order, learned beside today's closing. (3) Falls close runs by a group score; dips and second rises handled. (4) Runs with no fall end by the readings; owned runs only by their meter. (5) A running group draws its learned level. (6) Wider groups, split on significant valleys and overlap. (7) A 10-day steps store and a daily pass that overlaps midnight.

**Tech Stack:** Python 3.14, `custom_components/load_insights/insights/detect.py` (pure detector), `custom_components/load_insights/detection.py` (HA runner), `tests/`.

**Spec:** `docs/specs/2026-10-28-run-lifecycle.md`

## Global Constraints

- Git: `git add` is fine; commit, push, branch only when the owner confirms in the session. Commit each benched version locally before benching it, on his go. Never push `dev` unless he asks (a push deploys to Home and Kozolec). No new branches or worktrees.
- Tests and benches: ask the owner before running them; run only on his go. Tests: `python3.14 tests/run_all.py` (the default `python3` is too old for `bisect(key=)`).
- Benches on the Mac: `python3.14`, `PYTHONHASHSEED=0`, at most about 8 processes in all; `BENCH_PARALLEL=4` when another bench runs.
- Vocabulary: the Terms entry in `AGENTS.md`. Code names are not renamed in this plan.
- Tolerances and windows are counts of measured quantities (noise, interval, spread), never fixed watts or seconds (`AGENTS.md`, "The one rule that matters").
- A bench dial exists only while measuring; the winner ships as the default with no option. Big win at one site with a small loss at another ships; cleaner or more physical ships unless the loss is really big.
- Commit messages carry the measured numbers, kWh beside percent (captured, wrong, truth), both sites, before / after.
- `phases.py` and `model.py` are shared with Load Juggler: untouched here.
- Saved libraries change shape: one `DETECTOR_GENERATION` bump at the end (Task 13), not per stage.
- Baseline: the card at the commit that holds the live-power fix (Task 0). Data: the card's default folders, as scorecard `9266953`.

## Review Focus

- Kozolec's two fridges both running: the second rise is a second unit, not a missed fall, and each fall closes one of them (Task 5).
- A three-phase load whose legs fall in readings one interval apart: one fall event closes its run on all legs (Task 1).
- A plug-owned run while the grid sees an unrelated fall of the same size: the run stays open until the plug shows it off (Task 6).
- A restart in the middle of runs: running counts, cannot-link pairs and order counts survive `to_dict` / `from_dict` (Tasks 2 and 9).
- The pass slicing: SLICE=6, SLICE=1 and LIVE=1 file the same runs at every stage (`energy_bench.py invariance` in each stage's bench step).

---

## Stage 0 - baseline

### Task 0: Checkpoint and lifecycle check

**Files:**
- Create: `tests/lifecycle_check.py`
- Modify: none in `custom_components` (the live-power fix already in the working tree is committed as it is, on the owner's go)

**Interfaces:**
- Produces: `python3.14 tests/lifecycle_check.py SITE FOLDER [MODE=none|prod] [DIAL=V ...]` printing, per detector: closes by kind (as the card's pairing line), unmatched falls per day, the most open starts on one phase, open starts sharing a group, open starts older than 3x their group's usual length, and per name the highest live power against `live_ceiling`. One replay at SLICE=6 with the last 3 days at LIVE=1 (as `scratchpad/inkpower2/trace.py` did).

- [ ] **Step 1:** Ask the owner to commit the live-power fix (`LIVE_STALE_FACTOR = 3.0`, `LIVE_STALE_QUANTILE`, `LIVE_CAP_FACTOR`, `live_ceiling`, the two tests) and `tests/nilm_ref_bench.py`.
- [ ] **Step 2:** Write `lifecycle_check.py` on `bench._run`; for Home name Hiša's phase-C 10-100 W / 10-60 s signatures "Inkubator" as the trace did.
- [ ] **Step 3:** On the owner's go: `energy_bench.py card` (LABEL=lifecycle-base) and `lifecycle_check.py` for home and kozolec, hidden and fed. Write the numbers to `dev/run-lifecycle-results.md` (Susilna hidden capture / impurity in kWh, Kozolec fridges, the Inkubator's live maximum, closes by kind).

## Stage 1 - one kind of step

### Task 1: Falls as all-phase events

**Files:**
- Modify: `detect.py` - `PhaseState._process` (~2384, the fall split and `_declare` call), `PhaseState._declare` (~2488), `Detector.note_rise` / `_form_first` / `_form_event` (~4983-5103), `_classify_step` (~5169)
- Test: `tests/test_detect.py`

**Interfaces:**
- Produces: `Detector.note_step(state: PhaseState, since: float, watts: float, var: Optional[float], surge: float, new_level: float, quality: float) -> None` (rises and falls; replaces `note_rise`); `_form_first` forms an event of either direction (members of one sign within `event_window()`, `EVENT_BALANCE` as for rises) and, for a fall, closes through `PhaseState._pair` per member phase with the event's group id as `stop_cluster`. A fall `StepGroup` has the event's phase pattern.

- [ ] **Step 1:** Tests: `test_a_three_phase_fall_one_reading_apart_is_one_event` - a 3x2 kW rise on X/Y/Z then falls on X, Y, Z within two intervals: one fall group with pattern "XYZ", the run closes on all three phases at once, no single-leg run is filed. `test_a_single_phase_fall_still_closes_its_run` - the `_named_kettle` fixture files as before (count and watts within 5 %).
- [ ] **Step 2:** Implement. `_floor_stop` and `_unseen_stop` run when the fall's event forms, not at declaration.
- [ ] **Step 3:** On the owner's go: tests, then card + invariance + lifecycle_check; numbers to the results file; checkpoint as a patch file (`dev/patches/run-lifecycle-1.patch`) unless the owner confirms a commit.

## Stage 2 - running groups and partner evidence (learned, not yet used)

### Task 2: Running counts and order counts

**Files:**
- Modify: `detect.py` - `Detector` (new fields beside `pairs`), `Detector.to_dict` / `from_dict` (~6296, ~6340), the fall path from Task 1
- Test: `tests/test_detect.py`

**Interfaces:**
- Produces:
  - `Detector.running: Dict[int, List[_Open]]` - group id to its open starts (the count is `len`); derived from the phases' `open_edges`, not stored twice.
  - `Detector.order: Dict[str, List[float]]` keyed `"A>F"`: `[falls while A ran, sum of log seconds since A's start, sum of squares]`; `Detector.run_time: Dict[int, float]` seconds each rise group has been running (count >= 1), and `Detector.fall_n: Dict[int, float]` falls per fall group.
  - `Detector.partner_evidence(rise: int, fall: int) -> float` - `0.0` while `above_chance(n, expected)` fails, else `log(n / expected)`; `expected = fall_n[F] * run_time[A] / span` with `span` the seconds since the group pair was first possible.
  - `Detector.pair_length(rise: int, fall: int) -> Optional[Tuple[float, float]]` - mean and spread of log seconds from `order`.
- [ ] **Step 1:** Tests: `test_order_counts_a_fall_against_every_group_running_then` (two groups running, one fall: both pairs counted once); `test_a_wrong_close_teaches_no_pair` (a fall closing the wrong run still counts only against the groups running); `test_partner_evidence_is_zero_until_above_chance` (8 falls of F, each while A runs, A running a tenth of the time: positive; F at random times: 0); `test_order_counts_survive_a_restart` (`to_dict` / `from_dict` equal).
- [ ] **Step 2:** Implement; fade `order`, `run_time`, `fall_n` with `EDGE_TAU_S` as the group histograms fade. Today's `pairs` stays until Task 3.
- [ ] **Step 3:** On the owner's go: tests; card and invariance must be unchanged (nothing reads the new counts yet) - any moved session is a fault to find.

## Stage 3 - closing by group

### Task 3: A fall closes the best-scoring running group

**Files:**
- Modify: `detect.py` - `PhaseState._pair` (~2759-2903), `_pair_by_model` (~2933, deleted), `_close` (~2968, `note_pair` call), `Detector.note_pair` / `_pair_index` / `_pair_model` / `partners` / `usual_length` (~5851-5966), `PAIR_TIE_BAND`, `MATCH_EDGE_REL` uses in pairing
- Test: `tests/test_detect.py`

**Interfaces:**
- Consumes: `partner_evidence`, `pair_length` (Task 2).
- Produces: `PhaseState._score(o: _Open, fall: float, fall_var: Optional[float], fall_group: Optional[int], at: float) -> Optional[float]` - `None` when the size test fails, else a score combining (a) the fall's distance from the run's own rise in units of the two steps' measured noise - or, once `partner_evidence > 0`, from the pair's learned ratio in units of its spread; (b) `partner_evidence`; (c) the run's log age against `pair_length`, its weight `1 / (1 + spread)`. `Detector.usual_length(rise: int) -> Optional[float]` now from `pair_length` over the group's partners.
- Tolerance with no evidence: `PAIR_TIGHT_NOISES` noises of the two steps combined (dial, bench 2 / 3 / 4); the closing floor: dial `PAIR_SCORE_MIN` (bench three values). Both open points in the spec; the owner sees the sweep before a value is kept.

- [ ] **Step 1:** Tests: `test_a_fall_closes_its_partner_not_the_newest_of_its_size` (A 190 W running 2 h with evidence, B 190 W started 3 min ago with none, a 190 W fall of A's partner group closes A); `test_a_drifting_load_is_closed_once_its_ratio_is_learned` (rise 390, fall 300: unmatched for the first falls, closed after 8 above chance); `test_a_fall_far_from_every_run_closes_nothing` (unmatched, counted in `fall_n`).
- [ ] **Step 2:** Implement; keep the settle, multi-close and joint-stop branches after the score as today. Delete `PAIR_TIE_BAND`, `_pair_by_model` and the recency tie; `pairs` and `note_pair` go once `usual_length` reads `order`.
- [ ] **Step 3:** On the owner's go: tests, card, invariance, lifecycle_check; the closes-by-kind line shows the new split. Results file; patch checkpoint.

### Task 4: Dips are levels of a running run

**Files:**
- Modify: `detect.py` - `PhaseState._pair` unmatched branch (`held_drops`, ~2897), the rise path in `_form_event`
- Test: `tests/test_detect.py`

**Interfaces:**
- Produces: `PhaseState.dips: List[Tuple[float, float, int]]` - (at, watts, open start index) held as possible dips; a rise of matching size (Task 3's tight test) within `DIP_INTERVALS` reading intervals of one appends two levels to that run and opens nothing. Dial `DIP_INTERVALS`, bench 2 / 3 / 5.

- [ ] **Step 1:** Tests: `test_a_dip_and_its_return_open_no_run` (EVBox-like: 10.6 kW running, -2.1 kW for one reading, +2.1 kW: one run, two extra levels, no 2.1 kW run filed); `test_a_late_return_is_a_new_run` (the return after 10 intervals opens a run).
- [ ] **Step 2:** Implement.
- [ ] **Step 3:** On the owner's go: tests and the stage-3 bench together with Task 5.

### Task 5: A second rise of a running group

**Files:**
- Modify: `detect.py` - `PhaseState.end_older` (~2705, replaced), `_form_first` (~5018 call), `PhaseState.declared` look-back
- Test: `tests/test_detect.py`

**Interfaces:**
- Produces: `PhaseState.second_rise(o_new: _Open, cid: int, others: List[_Open]) -> List[Run]` - when the reading at the new rise, less the other running groups' levels (`o.watts`; Task 6's `level_of` once it exists), carries the old run's level within its noise, the count goes up and nothing closes; else the old run ends at the newest declared fall between the two rises that fits its size by Task 3's test, else at `min(new rise, start + usual_length)`. Owned runs are exempt (Task 6). The cannot-link pair `(old.since, new.since)` is recorded for Task 9.

- [ ] **Step 1:** Tests: `test_two_fridges_overlapping_count_two` (Kozolec numbers: 83 W and 60 W groups merged into one group, the second rises while the first is on: count 2, both runs closed by their falls, a cannot-link pair recorded); `test_a_second_rise_without_the_first_carried_ends_the_first_at_its_missed_fall` (a declared fall between the two rises ends it there); `test_a_second_rise_under_the_noise_is_a_missed_fall`.
- [ ] **Step 2:** Implement.
- [ ] **Step 3:** On the owner's go: tests, card, invariance, lifecycle_check (same-group open starts at 0 outside counts of 2). Results file; patch checkpoint.

## Stage 4 - ends without a fall

### Task 6: The readings end a run; owned runs only by their meter

**Files:**
- Modify: `detect.py` - `PhaseState._unseen_stop` (~2642), `_floor_stop` (~2526), `owned` (~2084), `Fleet._meter_stop` / `_meter_ended` (~7873, ~7959), `live_runs` (~6128)
- Test: `tests/test_detect.py`

**Interfaces:**
- Produces: `PhaseState.level_of(o: _Open) -> float` - `o.watts`, or the meter's reading for an owned run (Task 7 makes it the learned level). `_unseen_stop` closes a run once `level - baseline - sum(levels of the other running runs) < 0.5 * level_of(o)` (0.5 is today's factor, kept); for an owned run the parent's rules skip it and its meter's reading less the meter's other runs decides by the same rule. `live_runs` stales per run on `LIVE_STALE_FACTOR * usual_length(o.cluster)` (falls back to the name's quantile while the group has none).

- [ ] **Step 1:** Tests: `test_a_run_ends_when_the_reading_less_the_others_falls_under_half` (1 kW run beside a 2 kW boiler: the reading falls to 2.4 kW: closed); `test_a_plug_run_survives_an_unrelated_grid_fall_of_its_size`; `test_a_plug_reading_closes_its_own_runs_left_open`; `test_age_alone_ends_nothing` (an open start 10x its usual length the reading still carries stays open, and is not in `live_runs`).
- [ ] **Step 2:** Implement. `MAX_OPEN_S` unchanged (spec open point).
- [ ] **Step 3:** On the owner's go: tests, card, invariance, lifecycle_check. Results file; patch checkpoint.

## Stage 5 - what a running group draws

### Task 7: Learned level, the rest unknown

**Files:**
- Modify: `detect.py` - `PhaseState._process` (~2262-2271, `o.now` following the phase), `_close` (~2988-2995), `open_scale` (~3035), `Detector.unknown_power` (~6103), `live_runs`
- Test: `tests/test_detect.py`

**Interfaces:**
- Consumes: `level_of` (Task 6).
- Produces: `level_of` extended: the pair's learned running level (rise x learned ratio) once `partner_evidence > 0` for its best partner, else `o.watts`; the meter's reading for an owned run. `o.now` is no longer set from the phase level; `o.lo` / `o.hi` stay (they describe the phase while it runs alone).

- [ ] **Step 1:** Tests: `test_a_lone_open_start_does_not_take_the_phases_other_loads` (a 39 W start, the phase then carrying 300 W more with no step: live 39 W, unknown about 300 W); update `test_a_named_load_reads_its_power_while_its_run_is_still_open` only if its expected watts change (say so in the commit).
- [ ] **Step 2:** Implement.
- [ ] **Step 3:** On the owner's go: tests, card, invariance, lifecycle_check (the Inkubator's live maximum against the baseline). Results file; patch checkpoint.

## Stage 6 - group width

### Task 8: Cut only at significant valleys, within a span

**Files:**
- Modify: `detect.py` - `_valley_cuts` / `_valley_cuts_near` (~4144-4195), `_valleys` (~5385), `_by_density` (~5270)
- Test: `tests/test_detect.py`

**Interfaces:**
- Produces: `_valley_cuts(dens, lo, peak, counts)` cuts at a local minimum only when both neighbouring peaks exceed it by more than `VALLEY_SIGMAS` times the Poisson spread of the counts there (`sqrt` of the summed weight), and always where the density is empty; a segment wider than `GROUP_SPAN` (largest / smallest step, bench 2 / 3 / 5, start 3) is cut at its deepest minimum. Dials `VALLEY_SIGMAS` (bench 1 / 2 / 3) - the significance test is a spec open point: the owner sees the sweep before a value is kept.

- [ ] **Step 1:** Tests: `test_three_steps_are_one_group` (100, 104, 98 W: one group); `test_a_real_valley_splits_once_enough_steps_show_it` (300 steps at 80 W and 300 at 120 W: two groups; 3 of each: one); `test_a_group_never_spans_more_than_group_span` (steps from 100 to 500 W evenly: no group wider than 3x).
- [ ] **Step 2:** Implement. The incremental `_valley_cuts_near` must give the same segments as the full cut (keep its equality test).
- [ ] **Step 3:** On the owner's go: tests, card at each `GROUP_SPAN` and `VALLEY_SIGMAS` value, invariance at the chosen one. Results file; patch checkpoint.

### Task 9: Cannot-link pairs and the nightly split

**Unit count:** a spec open point; build two or three variations of how a group learns and uses its count, bench them, and leave the choice to the owner. This task is written for the split.

**Files:**
- Modify: `detect.py` - `StepGroup` (~3964: `cannot: List[Tuple[float, float, float, float]]`, the two rises' sizes and angles), `Fleet._repair` (~6814), `StepGroup.to_dict` / `from_dict`
- Test: `tests/test_detect.py`

**Interfaces:**
- Consumes: the cannot-link pairs Task 5 records.
- Produces: `Detector.split_groups() -> int` - in the daily pass, for each group with at least `PAIR_MIN_RUNS` cannot-link pairs, the size (then angle) threshold that puts the most pairs on opposite sides; split when it separates `above_chance` of them; the group's runs of the day are re-filed. Returns the number split.

- [ ] **Step 1:** Tests: `test_overlapping_similar_fridges_split_into_two_groups` (83 W and 75 W rises in one group, 10 overlaps: two groups after the daily pass); `test_identical_units_are_not_split` (two 2 kW elements, 10 overlaps, same sizes: one group); `test_cannot_link_pairs_survive_a_restart`.
- [ ] **Step 2:** Implement.
- [ ] **Step 3:** On the owner's go: tests, card, invariance, lifecycle_check (Kozolec's fridges). Results file; patch checkpoint.

## Stage 7 - the daily pass across midnight

### Task 10: The steps store

**Files:**
- Modify: `detect.py` - `Detector` (`steps: List[list]`, each `[since, phase pattern, signed W, var, group id, owner]`, kept `STEPS_KEEP_S = 10 * 86400`), `detection.py` (~384: a third `Store` `f"{DOMAIN}.{entry.entry_id}.detection.steps"`, saved after each daily pass and on `HISTORY_SAVE_INTERVAL_S` as `_history` is)
- Test: `tests/test_detect.py`

**Interfaces:**
- Produces: `Fleet.steps_to_dict() -> dict`, `Fleet.load_steps(d: dict) -> None` - out of `to_dict`, so the library store does not grow.

- [ ] **Step 1:** Tests: `test_steps_older_than_ten_days_are_dropped`; `test_steps_round_trip_through_their_own_store` (not in `Fleet.to_dict`).
- [ ] **Step 2:** Implement.

### Task 11: Re-read the readings; re-book what is not final

**Files:**
- Modify: `detect.py` - `Fleet._repair` (~6814), `_cap_day` (~9106), `_recover_day` (~9311), `Detector._day_log` / `_day_rows` (~4410); `detection.py` (the pass that crosses `fleet._repaired_to + REPAIR_DAY_S + REPAIR_WAIT_S` first reads the recorder for `[a - 86400, b + REPAIR_WAIT_S]` and hands it in); `tests/replay.py` / `tests/bench.py` (hand the same slice from the CSVs)
- Test: `tests/test_detect.py`

**Interfaces:**
- Produces: `Fleet.repair_rows: Optional[Dict[str, Dict[str, list]]]` - detector name to phase to rows, set by the caller before the pass; `_repair(a, b)` re-books every run that ended after `a - 86400` and was not final (including runs filed after the previous repair), then marks them final. `_day_rows` goes.

- [ ] **Step 1:** Tests: `test_a_run_across_midnight_is_repaired` (23:50-02:00 run filed after the repair fired: re-booked the next night); `test_the_repair_sees_the_readings_before_midnight`; `test_a_run_is_repaired_once` (final runs are not re-booked again).
- [ ] **Step 2:** Implement.
- [ ] **Step 3:** On the owner's go: tests, card, invariance (`6~6R` and `L1~L1R` too: a restart across the boundary), lifecycle_check. Results file; patch checkpoint.

### Task 12: Look back for missed falls and missing rises

**Files:**
- Modify: `detect.py` - `Fleet._repair`, new module-level `_look_back(rows, run, level, unit, a, b)` beside `_find_cycles` (~9253)
- Test: `tests/test_detect.py`

**Interfaces:**
- Produces: `_look_back(...) -> Optional[float]` - the time the reading, less the day's other runs, fell by the run's level within Task 3's tight test, or `None`. Used for runs ended by `usual_length` or `MAX_OPEN_S` (end moved to the drop) and for unmatched falls whose fall group has a partner (a rise of that partner's size before it: a run booked, `quality` as `QUALITY_UNSEEN_STOP`).

- [ ] **Step 1:** Tests: `test_a_run_ended_by_its_usual_length_moves_to_its_drop`; `test_an_unmatched_fall_finds_its_netted_rise` (the rise netted against another load's fall in one reading).
- [ ] **Step 2:** Implement.
- [ ] **Step 3:** On the owner's go: tests and the stage-7 bench (cards, invariance, lifecycle_check). Results file; patch checkpoint.

## Finish

### Task 13: Generation, docs, results

**Files:**
- Modify: `detection.py` (`DETECTOR_GENERATION` +1, its comment naming this change), `custom_components/load_insights/AGENTS.md` (the dials added and their benches; the replaced mechanisms under their entries), `AGENTS.md` Terms (the draft notes become the terms as built, pointing to the spec), `dev/TODO.md` (INS-29 and INS-30 updated)

- [ ] **Step 1:** Write the docs from `dev/run-lifecycle-results.md`; delete every dial not kept.
- [ ] **Step 2:** On the owner's go: `python3.14 tests/run_all.py`, then commits as he decides; ask before any push.

## Execution rules

Stages in order; each stage's bench goes to `dev/run-lifecycle-results.md` against the Task 0 baseline; nothing is dropped between stages (staged work keeps every stage). The owner, 2026-10-28: build the whole plan in one run while he is away, commit at milestones (each stage, its numbers in the message), run the benches as needed, carry on when a stage's numbers are worse (they are expected to be until the plan is complete), and stop when everything is built up to his decisions or when a plan usage window reaches 65 %. The open points get variations benched and wait for his choice; the bench dials stay in place until then.
