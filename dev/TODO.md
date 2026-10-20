# Load Insights - TODO

Completed items are removed - history lives in `git log` and `RELEASE_NOTES.md`. One file for the whole backlog (the former `docs/roadmap.md` lives here now), the same shape as Load Juggler's `dev/TODO.md`. Dated decisions by the owner; check "Standing decisions" at the bottom before starting anything. Entries are numbered (INS-n) so we can name the one we are working on in conversation: a number stays with its entry, is never reused, and gaps are fine. Entries are grouped by what can be done in one go.

## To close: review and test on a real site

*Built and live; each needs only a look on a real site to be removed.*

- [ ] **INS-19 [FEATURE] Heat nudge, shadow mode: configure a device and judge the scorecard** *(built 2026-10-19, not deployed, not exercised on a live HA; full plan `docs/heat-nudge-plan.md`)* - `insights/heatnudge.py`: a COP proxy (Carnot x 0.45, never a measured COP), the shadow schedule (the same heat in better hours, shift limit and rate cap), a daily scorecard (consumption-weighted outdoor temperature, actual vs shadow, gap; real and yesterday-evening-forecast temperatures); options page *Heat devices*, per device `COP index`, `Heat nudge scorecard` and `Would nudge now`. Objective is kWh only (no tariff); a nudge must never raise a 15-minute block or a peak-shaving limit. Left to do: the owner picks the devices (Home attic AC, Andrej's two utility meters need an activity entity or a power meter, plan section 3), then four weeks of scorecard against the plan's success rule (section 8); then Load Juggler boiler / room-thermostat nudge, then SG Ready state 3 after JUG-24. The owner decides: the judgements in plan section 9. **2026-10-20: the outdoor temperature is corrected** (plan 2a): a sun-heated sensor (Andrej's pump) is used only in the hours of the day learned to agree with the weather entity, else the weather entity at the sensor's night level; scorecard days carry `t_basis`, only `corrected` days count in the headline; weather observations are stored (`obs`). Andrej, activity days: hot water 8.6 -> 3.6 K (saving 18 -> 7 %), heating 7.9 -> 3.0 K. **Open: INS-15 reads the raw sensor still** (plan 2b: nothing engages today, latent cooling-term risk at Andrej); recommendation: give it the corrected series after a Home/Kozolec bench; not done. Slovenian wording: none new (attributes are English).
- [ ] **INS-3 [FEATURE] Absorption rate beside the room: verify on Kozolec** *(built 2026-10-18; replay on real data only)* - `DeviceRateSensor` (kW, `planning.absorption_rate`: the 95th percentile of a device's active hours) for every Energy-dashboard device, and Load Juggler's optional *Absorption rate* field replaces a load's minimum power in the early-start group (`early_rate_w`). Replay on Kozolec: the boiler 1.53 kW (element 1.89 kW; P90 read 1.11, P98 1.85), the Bee Room Heater 0.52 kW (rating 1.03 kW; it cycles). Left to do: point the Kozolec loads' *Absorption rate* field at the new sensors (the owner's say-so; changes when early start fires). The rate for detected named loads is built too (`NamedLoadRate`, 2026-10-18: `planning.rate_from_wh` of `Fleet.hourly_by_name`, the hourly energy `NamedLoadRoom` reads, 11 days, so five active hours before it reads); look at its values on Kozolec beside the devices' (a named load's 11 days are fewer hours than a device's statistics).
- [ ] **INS-2 [FEATURE] Energy per degree for thermostat loads: verify on Kozolec** *(built and live 2026-10-18; plan `docs/specs/2026-10-18-energy-per-degree.md`)* - a Load Juggler tank or room thermostat's *Room to absorb today* is `(boost - temperature now) x learned kWh per degree` (`insights/thermal.py`, source attribute `thermal`; the kind comes from Load Juggler's device type). **Live on Kozolec 2026-10-18:** the boiler reads `source: thermal`, 0.312 kWh/K from 8 runs (about a 260 l tank; the owner says 330 l with the element above the bottom), room 0.41 kWh at 73.7 of 75 C against 0.0 from history. The Bee Room Heater, a room, reads 0.724 kWh/K from 6 runs (spread 0.33; runs 0.37 to 1.2, following the weather) once a room's runs may dip. The Bathroom IR Panel probably never gets a clean figure (the owner: its thermostat is in another room and the place is drafty, so above its set temperature it is practically a limitless load): leave *Room to absorb today* empty on its Load Juggler entry so early start assumes it can take what is planned (check what that entry has set). Check after a few days: the heater's figure settles, room 0.0 only at boost, the early start's kWh agrees.
- [ ] **INS-1 [FEATURE] Room to absorb today: judge the P90 ceiling** *(built and live on Kozolec 2026-10-18 for detected loads and every Energy-dashboard device; Home has no battery or planning sensors, so it does not apply there)* - the ceiling is the 90th percentile of a load's complete past days, a judgement (the owner, 2026-10-18). After a few days of real values, check it against what the boiler and heaters actually take: is room 0.0 when they are full, and not 0.0 while they could still absorb? Spec `docs/specs/2026-10-17-soc-planning-and-pacing.md` section 10. INS-2 would replace it for thermostat loads.

## To build

### Planning and early start (Load Juggler reads these)

*The room sensor's two refinements feed Load Juggler's early start; do them as one stretch.*


### Edges, stage 1 (one bench session)

*Judge on the edge card only (see Standing decisions).*


### Detector checks (report first, the owner decides)

*Check and report; the missizing answers wait for a Friday night.*

- [ ] **INS-8 [FEATURE] Check two 2 Oct detector ideas against current code (82bd24b+)** - only check and report, then the owner decides: (a) settled start size - Home pump (Hidrofor) start sized from its first ~3.3 s (909 W) though it settles ~820 W; follow a young run's drift while no other step is declared on its phase; (b) a split leftover opening a run on a one-device meter that did not rise (Home 27 Sep 13:01, 858 W landed in pump start cluster 184 and stayed open 31 min); tie-break by which run the meter started, or keep split leftovers out of a one-device meter's start group when that meter did not rise.
- [ ] **INS-9 [FEATURE] Missizing answers - ask the owner on a Friday night** - may the Fleet gain learning in `detect.py` change (learn from falls too, median, not gated); hidrofor start decay / pre-stop drift as part of its run's steps; same-span comparison fix in `edges.py`. Study: scratchpad `missize/missize-report.md`.

### Forecast (parked)

*Only when the owner brings it up.*


### Plans

*Run when the owner says WHEN, in the run order of `docs/plans/2026-10-12/README.md` (interleaved with Load Juggler's plans: see JUG-2, JUG-10, JUG-11, JUG-22). Plans lj-3 (verification) and lj-7 (tank flapping) are done.*


## Other

- [ ] **INS-18 The day's repair costs Home 2.5 points of capture on the devices table** *(re-benched 2026-10-19, `data/scorecard/jobs/repair-2026-10-12.md`)* - impurity is down everywhere, but Home fed devices 67.5 -> 65.0 is beyond the plan's two points (and its band). Live since 10-04, so unchanged; the owner decides: keep, or sweep `REPAIR_SLACK_NOISE` 1 / 2 / 4 and `REPAIR_FLOOR_SHARE` 0.05 / 0.1 / 0.2.
- [ ] **INS-20 [FEATURE] Feed the INS-15 temperature evidence the corrected outdoor series** *(queued by the owner 2026-10-20)* - the heat-nudge shadow now corrects a sun-heated outdoor sensor with the weather entity (INS-19, `docs/heat-nudge-plan.md` 2a/2b). Andrej's heat-pump sensor passes the 24 C cooling hinge in 111 hours on 22 of 30 days (the official station: 9 hours on 3 days); heating degree-hours are 26 % short; his forecast is unharmed today (the temperature term engages on no series) but the two sun-following EV chargers are where a false cooling term would engage. Do: give the INS-15 evidence the same corrected series (and its basis), confirm on Home and Kozolec under the INS-15 bench rule (`forecast_bench cov`; their sensors should come out trusted in every hour, so the bench is a confirmation), then ship.

## Standing decisions

**Priorities.** Detection is the focus; forecasting is parked (the owner, 2026-10-03: "put that on the back burner ... Detection is our focus"). Don't start forecast work or propose forecast fixes unless he brings it up; route new forecast findings to the "To explore or discuss" list in `AGENTS.md`. Re-opened 2026-10-12, these two only: the temperature term engages on statistical evidence, and the whole-house forecast is built from its parts (built 2026-10-17, `PART_MIN_WEEKS` 6; signed own part clamped only in the sum). The three planning sensors are built (`planning.py`, options page *SOC planning*).

**Parked.** Ramping loads (an inverter heat pump, a slow EV ramp, where no single start step exists): don't start until he asks (2026-10-04).

**Current stage: cross-meter edges** (`docs/specs/2026-10-14-cross-meter-edges.md`). While in stage 1, keep to edge detection and grouping. Problems in how runs and steps are assigned to devices or meters (a meter step claimed twice, Kozolec 10-05) are a later stage: note them in one line, don't offer them as next steps. Judge stage-1 changes on the edge card only (conservation, coverage, invariance, tightness, mixed, duplicates, count, mirror); don't run or cite the energy card as pass/fail until the edges become the detector's. The owner, 2026-10-14: "that is not an edge detection problem, but a device assignment problem."

**Decided.** Pond = no special rule (angle score ~0 already); Shelly 5 % rule dropped; chosen loads can be left out of the consumption forecast in Load Insights itself (built 2026-10-16, Inputs page *Not in the house forecast*).
