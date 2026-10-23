# Steps, claims and system phases - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Join what several meters saw of one change into one owned step, so a dishwasher burst is filed once, under the meter closest to it.

**Architecture:** Three stages, each kept whatever its own bench shows. (1) The grid's phases become X, Y, Z. (2) Sub-meter edges claim the matching edges of the next meter outward, rises and falls alike, into steps that record every system phase. (3) Step groups are built on steps, with device states at each step and duration loosened.

**Tech Stack:** Python 3.14, `custom_components/load_insights/insights/detect.py` (pure detector), `tests/` benches.

**Spec:** `docs/specs/2026-10-23-steps-and-claims.md`

## Global Constraints

- Git: `git add` is fine; commit, push, branch only when the owner confirms in the session. Commit each benched version locally before benching it. Never push `dev`. No new branches or worktrees.
- Tests and benches: ask the owner before running them; run only on his go.
- `phases.py` and `energy_prefs.py` are shared with Load Juggler: do not change them here without saying so and offering the same change.
- A bench win ships as the default with no leftover dial. Big win at one site with a small loss at another ships; cleaner or more physical ships unless the loss is really big (`AGENTS.md`, "Before shipping a detector change").
- Commit messages carry the numbers measured (before / after, both sites).
- Vocabulary: the Terms entry in `AGENTS.md`. Code names are renamed in no task here.
- Replay data: `data/history/home-1022` (Home, 13-22 Oct). Scorer: `scratchpad/dw/dw_bench.py`, run with `SUBS=prod HOUSE=prod INPUT=sensor.aeg_sock_eater_appliancestate INPUT=sensor.aeg_sock_eater_cyclephase INPUT=sensor.aeg_sock_eater_cyclesubphase INPUT=sensor.aeg_le_hottie_appliancestate INPUT=select.aeg_le_hottie_program INPUT_RARE_SHARE=1.1`. Baseline: dishwasher 8/9 bursts hit, 17 sessions in 15 signatures; Sock Eater 34/50, 50 in 33.

## Review Focus

- Two loads of one size starting together on different phases of one meter: the claim must not swap them.
- A slow inner meter (plug, 60 s cadence) reporting after the grid edge: claimed once the horizon passes, not missed or double-booked.
- Two overlapping loads on an outer edge: the claim takes the inner meter's share, the rest stays unclaimed.
- A meter that held its value through a change (recorder keeps only changes) must not claim.
- A wrong early phase vote must not stick: a stored map is replaced by a strictly better one.

## Stage 1 - system phases

### Task 1: Golden replay of today's output

**Files:**
- Create: `tests/relabel_check.py`

**Interfaces:**
- Produces: `golden(folder: str, site: str) -> list[tuple]` - every filed session as `(start, end, phase, watts, signature id)`; `python3 tests/relabel_check.py save|compare SITE FOLDER`.

- [x] **Step 1:** Implement `golden` on `bench._run` output; `save` writes `data/scorecard/golden-<site>.json`, `compare` prints the sessions that differ after mapping `X,Y,Z` back to `a,b,c`.
- [x] **Step 2:** On the owner's go, run `save` for `home` and `kozolec` on the unchanged code. Expected: files written.

### Task 2: Rename the grid's phases

**Files:**
- Modify: `custom_components/load_insights/insights/detect.py:43` (`PHASES = ("X", "Y", "Z")`) and its other literals (`grep -n '"a"' detect.py`: 4 places), `Fleet.phase_map` (~7680) to pass `phases=PHASES` to `phase_mapping`; `tests/bench.py`, `tests/replay.py` where they name `a/b/c`.
- Test: `tests/test_detect.py::test_grid_phases_are_xyz`, `::test_unmapped_meter_keeps_own_label`

**Interfaces:**
- Produces: `PHASES = ("X", "Y", "Z")`; `display_phase(grid_label: str, system: str) -> str` returning `"X (L1)"`.

- [x] **Step 1:** Write the two tests: a replayed three-phase fixture files sessions on `X/Y/Z`; a sub-meter with no stored map keeps its own letter.
- [x] **Step 2:** Make the rename; `phases.py` stays untouched (the default argument is overridden at the call).
- [ ] **Step 3:** On the owner's go, run `relabel_check.py compare` for both sites. Expected: zero differing sessions.

### Task 3: Saved libraries and display

**Files:**
- Modify: `custom_components/load_insights/detection.py:203` (`DETECTOR_GENERATION = 21`), `custom_components/load_insights/sensor.py` (attributes that print phases)
- Test: `tests/test_sensor_attributes.py::test_phase_shown_with_meter_label`

- [x] **Step 1:** Test: a session on grid phase `X` with meter label `L1` prints `"X (L1)"`.
- [x] **Step 2:** Implement with `display_phase`; bump the generation (stored libraries are rebuilt by the history pass, `older` is read as today).
- [ ] **Step 3:** On the owner's go, `python3 tests/run_all.py`. Expected: all pass. Commit locally after he confirms.

## Stage 2 - claims

### Task 4: Pure claiming

**Files:**
- Create: `custom_components/load_insights/insights/steps.py`
- Test: `tests/test_steps.py`

**Interfaces:**
- Produces:
  - `@dataclass Edge: meter: str; phase: str; since: float; size: float; var: Optional[float]; noise: float; held: bool`
  - `@dataclass Step: edges: list[Edge]; owner: str; since: float; changes: dict[str, float]` (system phase -> signed W)
  - `build_steps(edges: Mapping[str, Sequence[Edge]], depth: Mapping[str, int], gain: Callable[[str], float], slack: float = METER_CAL_SLACK) -> list[Step]` - meters taken from the largest `depth` (innermost) outward.

- [x] **Step 1:** Tests, using the 22 Oct numbers: Mansarda C `+1940` at 10:40:46 and grid X `-1925` (export fell) at 10:40:44 form one step owned by `Mansarda`; the grid edge on Z `+2100` at 10:40:42 with no inner edge stays a one-edge step owned by `grid`; two overlapping loads (grid `-3000`, Mansarda `-1940`) leave a `-1060` unclaimed edge; a `held` edge claims nothing; two equal edges on two phases of one meter claim their own phases (swap check); a fall claims like a rise.
- [x] **Step 2:** Implement `build_steps`; reuse the tolerance and union-of-spans tests of `Fleet._step_meter` by extracting them into functions both call.
- [ ] **Step 3:** On the owner's go, run `python3 tests/test_steps.py`. Expected: pass.

### Task 5: Fleet uses steps; stops are claimed

**Files:**
- Modify: `detect.py` `Fleet._step_meter` (~7451), `Fleet._meter_stop` (~7007), `PhaseState` placement (~4728-4748: `where` from the step's owner, falls too), `GAIN_FALLS` becomes the behaviour.
- Test: `tests/test_detect.py::test_stop_is_owned_like_start`, `::test_late_plug_edge_is_claimed_after_horizon`

**Interfaces:**
- Consumes: `build_steps`, `Edge`, `Step` from Task 4.
- Produces: `EdgeCluster.where` set from `Step.owner` for rises and falls; `_meter_stop` removed.

- [x] **Step 1:** Write the two tests (a plug whose edge arrives 40 s after the grid's is claimed once; a Mansarda fall closes the run its rise opened with no owner lookup).
- [x] **Step 2:** Replace placement and stop lookup; keep the `meter_held` rule.
- [x] **Step 3:** Commit locally on the owner's go, then run the scorer. Record dishwasher / Sock Eater numbers and `energy_bench.py card home kozolec`, and `invariance`, in the commit message.

### Task 6: Stored phase maps and the history pass

**Files:**
- Modify: `detect.py` `Fleet.phase_map`, `detection.py` (first run with no stored map)
- Test: `tests/test_detect.py::test_map_learned_from_claims`, `::test_stored_map_replaced_only_by_better`

**Interfaces:**
- Produces: `Fleet.learn_phase_maps(history_rows) -> dict[str, dict[str, str]]` (meter -> own phase -> system phase), persisted with the library.

- [ ] **Step 1:** Tests: claims by size and timing alone on a fixture where Mansarda C moves grid X give `{"Mansarda": {"c": "X"}}`; a later weaker vote does not replace it, a strictly better one does.
- [ ] **Step 2:** (not built - see dev/steps-and-claims-results.md) Implement: with no stored map, one pass claims unmapped and votes; the daily pass re-checks with `phase_mapping(votes, PHASES, current=stored)`.
- [ ] **Step 3:** On the owner's go, print the learned maps for Home and Kozolec and show him.

## Stage 3 - step groups

### Task 7: Group steps, with device states

**Files:**
- Modify: `detect.py` `EdgeCluster`/`_group` (~3708, 3840), `_by_density` (~4776), the step-tagging code carrying `STATE_TAG` / `STATE_SUFFIX` (remove)
- Test: `tests/test_detect.py::test_step_group_key_is_owner_phases_direction`, `::test_state_transition_splits_group_above_chance`

**Interfaces:**
- Consumes: `Step`. Produces: `_group(phases: frozenset[str], up: bool, owner: str) -> str`; each step stores `states: dict[str, str]` and `transitions: dict[str, tuple[float, str, str]]` (input -> when, from, to).

- [x] **Step 1:** Tests: steps with the same size under different owners fall in different groups; a state seen only with one step cluster splits it, a chance split does not.
- [x] **Step 2:** Implement; delete `STATE_TAG`, `STATE_SUFFIX` and their `_keyed_above_chance` branch.
- [x] **Step 3:** Commit locally; scorer and scorecards as in Task 5.

### Task 8: Loose duration, daily rewrite

**Files:**
- Modify: `detect.py` pairing (`MATCH_DURATION_FACTOR` uses ~5373, ~8457), the daily re-booking (~890)
- Test: `tests/test_detect.py::test_pairing_ignores_duration_within_limit`, `::test_daily_rewrite_splits_by_spacing`

- [x] **Step 1:** Tests: a stop 10x the usual length still pairs; two runs of one signature at distinct spacings split in the rewrite.
- [ ] **Step 2:** (swept: 3, 6, 12 identical; nothing changed) Implement; sweep `MATCH_DURATION_FACTOR` with the bench dials and keep the best (a dial exists only while measuring).
- [x] **Step 3:** Final numbers (scorer, cards, invariance) in the commit message and an entry in `custom_components/load_insights/AGENTS.md`; ask the owner before any push.

## Stage 4 - code names (the owner, 2026-10-23: add it)

### Task 9: Rename the code to the agreed terms

**Files:**
- Modify: `detect.py`, `detection.py`, `sensor.py`, `edges.py`, `tests/*.py`, `translations/*.json`, `AGENTS.md` files (the Terms entry loses its "code still uses" note)

**Interfaces:**
- Produces: `Session` -> `Run`; `EdgeCluster` -> `StepGroup`; `EdgeCluster.where` -> `owner`; today's `s.owner` -> `parent_signature`; `edges`/`step` where they mean a single meter's change -> `edge`; stored keys keep their old names with a read alias, so no second generation bump.

- [x] **Step 1:** Rename `s.owner` to `parent_signature` first (frees the word), then `where` to `owner`, then the classes; one `grep -rn` per old name must come back empty (comments and the Terms entry excepted).
- [x] **Step 2:** Replay golden equality (Task 1's `compare`) after each rename. Expected: zero differing sessions.

## Execution rules (the owner, 2026-10-23)

Built natively in one run, stages in order. Bench results are gathered as each stage lands and written to `dev/steps-and-claims-results.md` (scorer, cards, invariance, per stage, against the baseline above); nothing is decided or dropped between stages. The owner reads the file when everything is built and then decides what to skip. Stage checkpoints are saved as patch files, not commits, until he confirms commits.
