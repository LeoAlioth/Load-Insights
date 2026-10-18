# Load Insights - TODO

Completed items are removed - history lives in `git log` and `RELEASE_NOTES.md`. One file for the whole backlog (the former `docs/roadmap.md` lives here now), the same shape as Load Juggler's `dev/TODO.md`. Dated decisions by the owner; check "Standing decisions" at the bottom before starting anything. Entries are numbered (INS-n) so we can name the one we are working on in conversation: a number stays with its entry, is never reused, and gaps are fine. Entries are grouped by what can be done in one go.

## To close: review and test on a real site

*Built and live; each needs only a look on a real site to be removed.*

- [ ] **INS-1 [FEATURE] Room to absorb today: judge the P90 ceiling** *(built and live on Kozolec 2026-10-18 for detected loads and every Energy-dashboard device; Home has no battery or planning sensors, so it does not apply there)* - the ceiling is the 90th percentile of a load's complete past days, a judgement (the owner, 2026-10-18). After a few days of real values, check it against what the boiler and heaters actually take: is room 0.0 when they are full, and not 0.0 while they could still absorb? Spec `docs/specs/2026-10-17-soc-planning-and-pacing.md` section 10. INS-2 would replace it for thermostat loads.

## To build

### Planning and early start (Load Juggler reads these)

*The room sensor's two refinements feed Load Juggler's early start; do them as one stretch.*

- [ ] **INS-2 [FEATURE] Energy per degree for thermostat loads** *(the owner, 2026-10-18)* - learn the kWh a tank or heater needs per degree of change; with the current temperature and the boost setpoint it gives the room directly (kWh = degrees below boost x kWh per degree), better than the P90 ceiling for exactly these loads. Needs a design talk first.
- [ ] **INS-3 [FEATURE] Rate of absorption beside the room** *(the owner, 2026-10-18)* - how fast a named load can take energy now, so Load Juggler's early-start group power is what the loads can really pull instead of their rated minimums.

### Edges, stage 1 (one bench session)

*Judge on the edge card only (see Standing decisions).*

- [ ] **INS-5 [FEATURE] Edges: grouping tolerance sized on each edge's own errors** *(the owner: "try the tolerance sized on the edges errors")* - follows 82bd24b (per-property measured errors, weighted distance, unsure flag), whose cost was 1 kW+ steps fragmenting (Home single-edge >= 1 kW groups 680 -> 1,257). Judge on the edge card only (see Standing decisions).
- [ ] **INS-6 [FEATURE] Edges: allow the kettle merge** *(the owner: "that merging is probably ok")* - the kettle's groups may merge with the matching unlabelled Mansarda B steps (likely boils the 90-240 s truth pairing misses).
- [ ] **INS-7 [FEATURE] Voltage slope per group** *(found 2026-10-14 on Home's 3D edge page)* - the same loads draw at ~237 V and ~244-245 V on sunny afternoons (SE17K export); across a group's edges the slope of log P on log V is ~2 for resistive and ~0 for constant power, a load-type property. Don't group on R alone (it splits constant-power loads into sunny/cloudy groups). Group level, not per edge.

### Detector checks (report first, the owner decides)

*Check and report; the missizing answers wait for a Friday night.*

- [ ] **INS-8 [FEATURE] Check two 2 Oct detector ideas against current code (82bd24b+)** - only check and report, then the owner decides: (a) settled start size - Home pump (Hidrofor) start sized from its first ~3.3 s (909 W) though it settles ~820 W; follow a young run's drift while no other step is declared on its phase; (b) a split leftover opening a run on a one-device meter that did not rise (Home 27 Sep 13:01, 858 W landed in pump start cluster 184 and stayed open 31 min); tie-break by which run the meter started, or keep split leftovers out of a one-device meter's start group when that meter did not rise.
- [ ] **INS-9 [FEATURE] Missizing answers - ask the owner on a Friday night** - may the Fleet gain learning in `detect.py` change (learn from falls too, median, not gated); hidrofor start decay / pre-stop drift as part of its run's steps; same-span comparison fix in `edges.py`. Study: scratchpad `missize/missize-report.md`.

### Forecast (parked)

*Only when the owner brings it up.*

- [ ] **INS-10 [FEATURE] Manual "ignore these dates" forecast option** - open for the owner; an automatic abnormal-day rule did not win (neutral to worse on both sites, see git log 2026-10-16), nor did other level dampings.

### Plans

*Run when the owner says WHEN, in the run order of `docs/plans/2026-10-12/README.md` (interleaved with Load Juggler's plans: see JUG-2, JUG-10, JUG-11, JUG-22). Plans lj-3 (verification) and lj-7 (tank flapping) are done.*

- [ ] **INS-13 [FEATURE] Plan li-2: slicing invariance** *(`docs/plans/2026-10-12/li-2-slicing-invariance.md`, run order 2; not started)* - re-verify the detector gives the same sessions at every slicing (last 0 differing on 2026-10-04), fix what differs, flush the recorder before each pass, add the `RESTART=` dial. About 30-45 min of `SLICINGS=all` card on the Mac; doubles as INS-14's slicing card.
- [ ] **INS-14 [FEATURE] Plan li-1: daily repair, re-bench on today's code** *(`li-1-daily-repair.md`, run order 3; the repair itself shipped in c689283)* - re-bench it on current code, add tests and docs; `REPAIR_SLACK_NOISE=1e9` switches it off for the ablation. Shares the `_day_rows` persistence with INS-13.
- [ ] **INS-15 [FEATURE] Plan li-3: temperature term engages on statistical evidence** *(`li-3-temperature-evidence.md`, run order 4; not started: `covariates.MIN_HOURS = 336` is still the fixed rule)* - lean on the correlation instead of a fixed two weeks; the plan's defaults were approved (ALPHA 0.005 swept 0.05 / 0.0005, 3 % floor dropped unless its variant wins). Forecast work re-opened for this and li-4 (done) only.
- [ ] **INS-17 [FEATURE] Plan li-5: `running_markdown` attribute** *(`li-5-running-markdown.md`, run order 6; not started)* - running loads (named and unnamed, with links) as one attribute, `#id` on the naming page, named-load on/off sensors move onto per-load runs; English only. Replaces the template the Kozolec Running-now card builds by hand.

## Other

- [ ] **INS-12 Home's detected-loads dashboard: To-name sub-list wording** - the Running now card is removed (2026-10-18); the wording of the sub-list under the To-name tile is not decided.

## Standing decisions

**Priorities.** Detection is the focus; forecasting is parked (the owner, 2026-10-03: "put that on the back burner ... Detection is our focus"). Don't start forecast work or propose forecast fixes unless he brings it up; route new forecast findings to the "To explore or discuss" list in `AGENTS.md`. Re-opened 2026-10-12, these two only: the temperature term engages on statistical evidence, and the whole-house forecast is built from its parts (built 2026-10-17, `PART_MIN_WEEKS` 6; signed own part clamped only in the sum). The three planning sensors are built (`planning.py`, options page *SOC planning*).

**Parked.** Ramping loads (an inverter heat pump, a slow EV ramp, where no single start step exists): don't start until he asks (2026-10-04).

**Current stage: cross-meter edges** (`docs/specs/2026-10-14-cross-meter-edges.md`). While in stage 1, keep to edge detection and grouping. Problems in how runs and steps are assigned to devices or meters (a meter step claimed twice, Kozolec 10-05) are a later stage: note them in one line, don't offer them as next steps. Judge stage-1 changes on the edge card only (conservation, coverage, invariance, tightness, mixed, duplicates, count, mirror); don't run or cite the energy card as pass/fail until the edges become the detector's. The owner, 2026-10-14: "that is not an edge detection problem, but a device assignment problem."

**Decided.** Pond = no special rule (angle score ~0 already); Shelly 5 % rule dropped; chosen loads can be left out of the consumption forecast in Load Insights itself (built 2026-10-16, Inputs page *Not in the house forecast*).
