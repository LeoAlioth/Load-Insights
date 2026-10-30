# Working on Load Insights

> **Setup note for agents:** this file holds this folder's instructions. Tools that read `AGENTS.md` (Codex and others) need nothing more. A tool with its own file name needs a file of that name here containing only a pointer to this file, if the folder does not already have one: Claude Code reads `CLAUDE.md` and needs the line `@AGENTS.md`; Gemini CLI reads `GEMINI.md`. Never copy this file's content into it.

A Home Assistant integration that finds individual appliances in whole-house
meter readings, names them, and forecasts consumption. Two live sites drive
every decision here: **Home** (3-phase, SolarEdge SE17K + M1 meter, several
Shellys) and **Kozolec** (off-grid, single-phase, Victron MultiPlus II).

## Where things are

| path | what |
|---|---|
| `custom_components/load_insights/insights/detect.py` | the pure detector - edges, sessions, signatures, merging. No Home Assistant imports; this is what the bench runs. |
| `custom_components/load_insights/insights/phases.py` | which of a device's sensors are its per-phase readings, and which phase each of a meter's channels carries (`phase_mapping`). Load Juggler carries a verbatim copy at `custom_components/dynamic_ocpp_evse/phases.py`, synced by hand: change it here, copy it there whole. Stdlib only, no Load Insights imports; `tests/test_phases.py` fails while the two differ. When you change a shared function here, say so and offer the same change to Load Juggler's copy. |
| `custom_components/load_insights/insights/model.py` | the Energy dashboard's preferences as a `SiteModel` (`PowerSpec`, `Device`, `SiteModel.from_prefs`); the bare reader, no option helpers (those live in `inputs.py` and `renames.py`). Load Juggler carries a verbatim copy at `custom_components/dynamic_ocpp_evse/energy_prefs.py`, synced by hand: change it here, copy it there whole. Stdlib only, no Load Insights imports; `tests/test_model.py` fails while the two differ. When you change it, say so and offer the same change to Load Juggler's copy. |
| `custom_components/load_insights/insights/autoname.py` + `autonaming.py` | auto-naming of confident fridges and heating elements (INS-21; plan `docs/auto-naming-plan.md`): the pure recogniser / bar / hysteresis / numbering / tombstones / Energy-dashboard list, and the HA half (store `load_insights.<entry>.autoname`, rename, words under `exceptions.autoname_*` in the translations, the ONE place Load Insights WRITES the Energy dashboard: `device_consumption`, only entries it added, `insights/model.py` stays the bare reader). Options `autoname` / `autoname_dashboard` inside the detection options, both on by default; the dashboard option covers EVERY named load (the owner's names too, with its power sensor for the power Sankey; a rename moves the entry) and does not depend on auto-naming (2026-10-25), and each sub-meter's base load as an entry under its meter (`included_in_stat`), never the grid's, whose floor holds the meters' (2026-10-30). `python3 tests/autoname_bench.py` prints the bar against the exports and replays |
| `custom_components/load_insights/insights/classify.py` | what a load might be, from power, duration, factor, name hints (EN + SL) |
| `custom_components/load_insights/detection.py` | the HA runner - reads the recorder, resolves sub-meters from the Energy dashboard, owns `DETECTOR_GENERATION` |
| `custom_components/load_insights/config_flow.py` | setup and the naming pages |
| `custom_components/load_insights/sensor.py` | entities and the diagnostic attributes |
| `tests/replay.py` | run the detector over exported CSVs, no HA |
| `tests/bench.py` | the bench every dial was chosen with: `score`, `kiln`, `surge`; Home is always read as production builds it (never the template sensor); ground-truth scoring and each site's device-meter list (`SITES`) |
| `tests/energy_bench.py` | the scorecard: capture and impurity per measured load, hidden / circuits / fed, the slicing invariance, and what each meter is worth to every other load (`card`, `invariance`, `diff`, `worth`) |
| `tests/truth_bench.py` | the cross-meter edges (`insights/edges.py`, `tests/edge_bench.py`) against known truths: Le Hottie's programs on Mansarda C, the kettle and the panini press on Mansarda B, and every plug's own steps with the plugs hidden (`card`, `diff`, `fetch`) |
| `tests/forecast_bench.py` | the forecast backtest on exported long-term statistics: hour-/day-ahead MAE and the noon "tomorrow" error. Profile only (`SITE START END`): devices left out (`OUT=`) either fitted away, subtracted as forecasts (`MODE=helper`) or never added to the house built from its parts (`MODE=parts`, `profile.assemble`). `cov SITE START END`: temperature, calendars, attached sensors and the device-state nowcast, every series, hourly origins, JSON results in `data/forecast/results/`; how to fetch its data and the acceptance rule are in `tests/AGENTS.md` |
| `custom_components/load_insights/insights/planning.py` | the SOC planning maths (target, minimum, daily excess, base consumption) from the house forecast and the sun forecast; pure, replayable (`plan_all`, `base_consumption`), plus the room and rate figures of a load (`room_to_absorb`, `absorption_rate`). Options page "SOC planning", sensors `PlanSensor`. |
| `custom_components/load_insights/insights/roommodel.py` | the room heat-loss model (INS-24; spec `docs/specs/2026-10-21-room-thermal-model.md`): hourly rows -> `fit` (C kWh/K, UA W/K, constant G, optional sun gain; unknown with the reason when the data cannot carry it), the derived room to absorb, daily energy per mode setpoint, boost share, stored rows (`merge_hours`, `load_insights.<entry>.roomrows`). Pure; bench `tests/roommodel_bench.py` (`fetch`, then `fit` / `A` / `B` / `C`). A *Room thermostat* load uses it before `thermal.py`'s energy per degree. |
| `custom_components/load_insights/insights/heatnudge.py` | the heat nudge's SHADOW MODE (INS-19; plan `docs/heat-nudge-plan.md`): COP proxy, shadow schedule, daily scorecard, hourly energy from a power / energy / once-a-day reading; pure, nothing is written to a device. Options page "Heat devices", `HeatIndexSensor` / `HeatScorecardSensor` / `HeatNudgeNow`, stored in `load_insights.<entry>.heatnudge` (`HEAT_GENERATION`). kWh is the only objective; a nudge never raises a 15-minute block's peak. The outdoor temperature is corrected first (a sun-heated sensor is trusted only in the hours learned to agree with the weather entity; plan 2a). |
| `tests/run_all.py` | every pure test file |

Keep new logic in `insights/` where it can be replayed and tested. Anything
that needs `hass` belongs in the layer above it.

## Detailed rules by folder

Not loaded automatically. Read the file for the folder you are working in:

- `tests/AGENTS.md`: the bench process, the scorecard, the replay, how to tune, and the bench-side known shortfalls. Read it before changing anything the bench scores.
- `custom_components/load_insights/AGENTS.md`: the dials, the detector internals and their measured history, storage and generations, deployment, and the detector's known shortfalls and ruled-out ideas. Read it before changing the detector, runner, classification, config flow, sensor or storage.

Reference docs (not loaded automatically):

- `dev/TODO.md`: the backlog (the same shape as Load Juggler's) and the standing decisions: priorities, the current stage of the cross-meter edges work, parked items. Read it before starting detection or forecast work.
- `docs/sites.md`: how the sites are wired, which readings can be trusted, Shelly reporting, operating notes for the Home Assistant instances. Read it before naming a cluster or building a house figure.
- `docs/bench-machine.md`: the Windows/WSL bench machine and how to reach it.
- `docs/history.md`: the dated design log, first dumps and experiment results. Long; read it to learn why something is the way it is.

## The two sites

| | Home | Kozolec |
|---|---|---|
| supply | 3-phase, grid-tied | single-phase, off-grid |
| main meter | SolarEdge SE17K inverter + M1 meter | Victron MultiPlus II 48/15000 |
| sample interval | ~6 s in the history; 2.4 s since the owner sped up SolarEdge polling (22-23 Sep) | ~6 s |
| power resolution | 1 W | 1 W |
| current resolution | **0.01 A** (2.4 VA) | **0.1 A** (23 VA) |
| measured noise | 26 / 25 / 37 W per phase | 10 W |
| library size | ~200 signatures | ~33 |
| notable loads | kiln (2-phase A+C, 5.9 kW, flat 48 s pulses), 3-phase compressor | boiler 1.8 kW, two fridges, Scala2 pump, EVSE |

The current-resolution row is why Kozolec's power factors are worthless below
~230 W and Home's are fine above ~24 W. It is the clearest example of why
nothing here should be a tuned constant.

## The one rule that matters

**A constant that describes the world must be measured from the data. A
constant that describes what to show a person may be a judgement.**

Every instrument differs, and a number tuned on one site is wrong on the
other. So resolution, noise, sampling interval and the error bars that follow
from them are measured per reading, from its own history, and the constants
around them are dimensionless *counts* of those measurements:

| measured per reading | where it comes from |
|---|---|
| `PhaseState.quantum` | `measure_quantum` — a low percentile of its changes, confirmed against a lattice |
| `PhaseState.noise` | median deviation while idle - and the array, where it is in the reading, holding still (exp12) |
| `PhaseState.interval` | its cadence: the 10th percentile of the gaps after a reading that moved (`CADENCE_GAPS`), stored with the phase |
| `PhaseState.q_quantum` | `V x dI` from the amps behind the power factor |
| `Signature.pf_mad` / `power_mad` / `duration_mad` | spread across sightings |

`PF_MIN_QUANTA`, `ENERGY_MIN_QUANTA`, `NOISE_REL_FLOOR_FACTOR` and friends are
counts, not watts. The same value means 23 VA at Kozolec's 0.1 A Victron and
2.4 VA at Home's 0.01 A SolarEdge, and neither site is configured.

Legitimately policy, and fine as plain constants: `MAX_SIGNATURES`,
`NAMING_START_ROWS`, `DEFAULT_MIN_EVIDENCE`, `YOUNG_COUNT`. These say what to
put in front of a person, not what an instrument can do.

Watch for absolute constants that should be relative. Two have been found and
fixed this way (`NOISE_REL_MIN_LEVEL`, a flat 300 W standing in for
quantisation), and a third since: `SUSTAIN_SECONDS = 5.0` sat below Home's 6 s
sampling interval, so the guard meant to reject a transitional sample could
never fire. It is now `SUSTAIN_INTERVALS`, a count of measured intervals.

## What the detector is for

**Steady-state appliances first.** A kiln, a boiler, a fridge, a switched
pump, a compressor: these repeat, and the detector should find them well.
Highly variable loads - computers (Home's NASA station), variable-speed pumps
(Kozolec's Grundfos Scala2), a UPS - are harder to detect by nature and are
the better candidates for a meter of their own. So a change that clearly
improves the steady loads and makes the variable ones somewhat worse is a
change worth taking (2026-09-23). Report the two groups separately so
the trade is visible, and do not reject a change for losses confined to the
variable ones.

## When the owner lists several things to build

Be thorough. Build every item on the list, and account for each one at the
end: built and measured, or deferred - with the reason said in so many words
and his agreement. Do not let one item's findings quietly stand in for
another, and do not decide alone that an item "belongs in its own release"
and then move on. On 2026-09-22 he said a detection on a sub-meter should
always override one on a higher level. It was scoped as "the largest
remaining piece", never built, and he believed a day later that it had been.
Keep the list in writing while working through it and tick items off
against it, not against memory.

## Standing rules

- **Personal project, not Ixtlan work.** Skip the work-context logging for this repo; never add entries for it to `/Volumes/Razvijam/work-context.md` or `open-tickets.md`.
- **Words.** Call the main detector meter the **grid meter**, never "house" (Hiša is the house circuit at Home).
- **Terms (the owner, 2026-10-23). The classes carry them (`Run`, `StepGroup`, `StepGroup.owner`, `Run.parent_signature`, since the steps-and-claims work); two older names remain in method and field names: the detector's "step" (`_classify_step`, `_step_meter`, `declared`) is an edge in these terms, and the grid's phase keys stay `a/b/c` (they are also the config fields), shown as X/Y/Z by `display_phase`.**
  - **reading**: one sensor's value at one time; several values the same sensor sends a few hundredths of a second apart are one reading.
  - **edge**: one meter's own change between readings. **rise / fall** is its direction.
  - **step**: the edges several meters saw of one change, joined into one object on the shared system phases. A step with one edge is a step too. A step records the change on every system phase (an untouched phase reads about zero), so there is no separate multi-phase step; the phases it touches are its phase set (the owner, 2026-10-23: this stays whatever the bench shows). A detected step feeds a named signature's power figures.
  - **start / stop**: the role of a step in a run. **run**: a start step joined with its stop step, so energy can be worked out.
  - **step group**: steps of one phase set, direction and owner, grouped by size. Steps are what get grouped, not edges.
  - **library**: never bare. A **step-group library** is a meter's step groups, a **signature library** its signatures; each belongs to the owner meter.
  - **signature**: a group of runs filed together by power, power factor, duration and input state; what a user names. **load**: a named signature, or the several that share a name.
  - **phase**: a meter's own phase, as it labels it (Mansarda's C). **system phase**: the grid meter's phases are **X, Y, Z**, and every other meter's phase is mapped onto them once learned.
  - **claim**: a sub-meter's edge taking over a matching edge of the next meter outward, on the same system phase within size and timing tolerance. **owner**: the innermost meter that claimed a step. **unclaimed edge**: an edge no inner meter claimed, so an unmetered load.
  - **meter**: the physical measuring unit (a 3EM, a plug); **sensor**: the HA entity it reports through; **load**: an appliance connected to a meter. Say "inner" and "outer" for which meter hangs under which; avoid "device".
  - **ramp**: an edge spread over several readings; its size is the settled difference, not two raw readings.
  - **parent signature**: the signature a grid run was handed to (the code's `s.owner`; "owner" now means the meter that claimed a step).
  - **daily rewrite**: the once-a-day re-filing of runs into signatures. The one place that sees the spacing and length of all runs, and so the one place length may separate signatures.
  - Device states and their transitions attach to every step. Run pairing is loose on duration. Diagnostics show the system phase with the meter's own label beside it, "X (L1)".
  - **site consumption**: the computed total, the grid meter plus the inverters (`combine`); the grid meter is the raw meter alone.
  - **open start** (2026-10-28, run-lifecycle brainstorm; `_Open`, `open_edges`): a start step whose stop has not come. Until it closes its only identity is its step group, size and age; it has no signature. A run is started by a step (which belongs to a step group), never by a signature.
  - **pair** (`A>B`, `Library.pairs`): a learned link from one rise step group to one fall step group: stop/start size ratio, run length (log mean and spread), how often the two came together. Used while a run is closing; internal, never shown, not a signature. Steps are what get paired (a run joins one start step to one stop step); a pair is the statistic learned from those runs, between their two steps' groups, and only from runs a fall closed directly (`direct=True`), not from closes by conservation, floor, multi-close or `end_older`. Fits one level of one device; staged devices mostly have none.
  - *Draft, run-lifecycle brainstorm 2026-10-28, not built:* pairing of individual steps into runs goes. Every step is classified into its step group; a rise group whose step was seen is **running**; a fall is matched to the running rise groups whose partner fall group it belongs to and ends that one. A fall whose group has no running partner is still classified, with no run to close - possibly a sign that the rise and fall of one load differ in size (a compressor settling lower once it has pressure), or that its rise was missed or misclassified: a fall with no running partner is a reason to **look back** in the data for that rise. A fall is scored against each running rise group on (1) size and power factor/angle fit, from the first sighting; (2) **partner evidence**: how far above chance its fall group has come while that rise group was running - order, not a fixed delay, counted whatever closed what, so a wrong close teaches nothing; (3) age against the pair's learned length, weighing less the wider its spread (a boiler's runtime follows the weather). A rise of a group already running is most often a missed fall, but can be a second identical unit (a water heater's two same-size elements overlapping, Kozolec's two fridges): idea under discussion - a running group carries a **count**, the phase reading at the second rise decides (still carrying the first: a second unit; not: a missed fall, found by looking back for where the reading dropped). Agreed: counting. And the count is evidence about the GROUP (the owner): groups may start wider, more tolerant of spread; a group whose runs never overlap is likely one device with a noisy readout, while one whose count reaches 2 may hold two similar devices (Kozolec's fridges, similar not identical) and is a candidate to be split finer - that group only, so the others are not tightened into fragments. Each overlap gives two rises that must be different devices (a cannot-link pair); a split that separates them is kept, and a group no feature splits is learned as one with several identical units (the heater's two elements). Splits are decided in the daily pass (on the readings' clock, so a rebuild's 6 h slices change nothing). The daily pass's midnight border loses runs today (a run filed after its day's repair is never repaired; the readings before midnight are dropped), so it needs overlap. Agreed: steps kept ~10 days in a store of their own; the readings for the look-back (~48 h) are read again from the recorder, which the runner reads every pass anyway (Home keeps ~4 days), not held by Load Insights. A rise group with no partner yet is closed on size and power factor alone, and TIGHTER (the owner): the tolerance comes from the two groups' own measured spreads and the phase's noise, and widens to the pair's learned ratio spread as partner evidence grows. A drifting load (rise 390 W, fall 300 W) fails the tight match at first, but its pair is still learned, because the order counting needs no close. A run with no fall to close it (the owner): (1) its fall missed - closed once the reading, less the other running groups, falls under half its size (agreed: today's 0.5 kept), the size being the pair's learned running level once the pair is known (the freezer), its rise until then; (2) its fall seen but in a non-partner group - a load whose start and stop differ (Home's old upright freezer starts well above where it stops), or a group holding more than one device; (3) the load still running - the case NOT to end; (4) a rise that was never a load - a transient, or the return of another load's dip. Agreed: a **dip** is not a pair of its own but a level change inside the running group's run (as a washer's heater-to-motor is): an unmatched fall while a group that can carry it runs is held as its possible dip; a matching rise within a few reading intervals makes both level changes of that run, nothing opens or closes; none, and the fall is an ordinary unmatched one. A staged device's stages are separate runs (the owner): that they are one physical device running in parallel is decided higher up the stack. Falls should be all-phase events like rises (the owner, "why on earth are fall steps per phase"): the per-phase fall dates from pairing each fall to one start at once. Agreed: rise and fall steps become one kind of object, an all-phase event either way. A run whose start a sub-meter owns is closed ONLY by that meter; its power, from the meter's own reading, is subtracted from the parent's reading in the parent's closing rules; and the meter's reading closes the meter's own runs left open (the 0.5 rule one level down). A running group draws its learned level (its rise, or the pair's running level once known), not the phase reading it is followed to; what the phase carries beyond the running groups is unknown (agreed; the Inkubator's 39 W pulse read 306 W by following the phase alone). Group width: today a group is cut at EVERY local minimum of the smoothed size density and wherever it is empty - the finest the data allows. Instead (the owner): keep steps together until there is evidence they are two devices - a valley deeper than the count noise explains, or overlap (cannot-link) - because one device's start varies with its conditions (a freezer restarting after a short gap starts near where it just stopped). Only a generous ceiling bounds a group, somewhere between 1.5x and under 10x from its smallest step to its largest; benched from 3x (the owner: wide enough for any device that is not multi-speed or multi-step), swept either side. At a fall, the size check compares the fall with the very rise that started the run, within the group-level match, not with the group's mean. Size: steps are not summed across meters (a step is one change, whatever meters saw it); the count is about the grid meter's (~9,750/day at Home, scorecard 9266953) plus the small steps only an inner meter resolves under the grid's noise, ~0.4 MB JSON per 10k steps before the per-meter edges.
  - **device** (filing sense): the component of start step groups a start belongs to; a run is filed into the signature most of its device's runs went to. In mixture grouping (2026-10-29, the owner) the filing key is the run's **pair** - its start group and its stop group (the stop the reliable signature): a pair is what a signature is built from, and a signature is the runs of one pair (split further by the daily rewrite's lengths and inputs, joined into one load by families and names). A run with no stop of its own takes its start's strongest partner; a start with no partner yet files alone.
  - **usual length** (`usual_length`): seconds a run of a start group usually lasts, from its accepted pairs.
  - **live power**: what a named load reads while running, from open starts (named by their start group's filed runs) plus the energy of runs filed since the last pass; capped at its signatures' strongest (`live_ceiling`).
- **Steps are declared on the site consumption.** Never on the raw grid meter alone while PV runs through it.
- **Staged work keeps every stage.** A change built in stages keeps each stage in place whatever its own bench shows; the stages are judged together (the owner, 2026-10-23).
- **One measure for every meter.** Every meter's steps come from the same detector, over the same span, on both sides of a comparison. Before adding a mechanism that relates two meters or two steps, check it measures both the same way.
- **Releases.** Home and Kozolec only, by default. The remote user's site takes a build only when the owner says so.
- **Git.** Commit each benched version locally before benching it, as a checkpoint; `dev` is fine. Never push `dev` unless the owner asks: a push to `dev` deploys to both sites. No new branches, worktrees or detached checkouts without asking. A push that fails with "Authentication failed" from the shell often works on one retry.
- **Priorities.** Detection is the focus; forecasting is parked. Read `dev/TODO.md` before starting forecast or edge work.

## Before shipping a detector change

Measure it on the bench first. The bench process and the scorecard are in `tests/AGENTS.md`.

A change that wins its bench ships as the default, with no user option and no leftover dial: a dial exists only while measuring. "Wins" is judged across the sites, not cell by cell: a big improvement at one site with only a small degradation at another is a win and ships (the owner, 2026-10-19); record both sides' numbers in the commit and the AGENTS.md entry. A large loss, or no clear gain anywhere, does not. Nor is a change dropped for a small or insignificant loss when it was discussed to work together with another change: keep it, bench the pair, and judge them together (the owner, 2026-10-19). **A cleaner, simpler, or physically more accurate solution is accepted even when the bench shows no clear win** (the owner, 2026-10-21): only a really big loss stops it (the room thermal model, INS-24, `docs/specs/2026-10-21-room-thermal-model.md`, is the first case). Write down what it lost and by how much. A failed attempt means the tested implementation was not good, not that the idea is useless; record it under "Things already ruled out" in the detector's AGENTS.md.

## Commit messages carry the evidence

Unusually for a repo this size, commit messages here record what was measured,
what was tried and rejected, and what a number came from. Keep that up. The
bodies are long on purpose — a future reader needs to know that a constant was
swept rather than picked, and which explanations were already ruled out.

State clearly when something was a judgement rather than a measurement. A
plausible story survives precisely because nobody checks it.

