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
| `custom_components/load_insights/insights/autoname.py` + `autonaming.py` | auto-naming of confident fridges and heating elements (INS-21; plan `docs/auto-naming-plan.md`): the pure recogniser / bar / hysteresis / numbering / tombstones / Energy-dashboard list, and the HA half (store `load_insights.<entry>.autoname`, rename, words under `exceptions.autoname_*` in the translations, the ONE place Load Insights WRITES the Energy dashboard: `device_consumption`, only entries it added, `insights/model.py` stays the bare reader). Options `autoname` / `autoname_dashboard` inside the detection options, both on by default. `python3 tests/autoname_bench.py` prints the bar against the exports and replays |
| `custom_components/load_insights/insights/classify.py` | what a load might be, from power, duration, factor, name hints (EN + SL) |
| `custom_components/load_insights/detection.py` | the HA runner - reads the recorder, resolves sub-meters from the Energy dashboard, owns `DETECTOR_GENERATION` |
| `custom_components/load_insights/config_flow.py` | setup and the naming pages |
| `custom_components/load_insights/sensor.py` | entities and the diagnostic attributes |
| `tests/replay.py` | run the detector over exported CSVs, no HA |
| `tests/bench.py` | the bench every dial was chosen with: `score`, `kiln`, `surge`, and `HOUSE=prod` to read Home as production does; ground-truth scoring and each site's device-meter list (`SITES`) |
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

