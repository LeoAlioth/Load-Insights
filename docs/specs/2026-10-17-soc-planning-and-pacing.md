# SOC planning in Load Insights, and pacing in Load Juggler (design draft, 2026-10-17)

Personal project (Load Insights + Load Juggler + Kozolec HA). Never log to a work-context file.
Status: BUILT AND DEPLOYED 2026-10-17 (the owner's answers are section 8, which overrides sections 4, 5 and 7). Load Insights: house forecast from its parts (2e5ca5f), planning sensors (f310628, 74a244c), production starts / ends (403c8b4), review fixes (b32a721, 8e874d6), audit (4342cac, 3a0ab73). Load Juggler: pacing (319a958), early Excess (e28efba), catch-up (e95be19), review fixes (12cb18a, 058af50, 84f503d). Where the build differs from sections 4 and 5: pacing takes the planned excess energy and the sunset from two hub entities (no battery capacity option), and the early-Excess floor is a hub option (minimum / target / fixed). Forecasting was re-opened by the owner on 2026-10-17 for this
work only; update `docs/roadmap.md` when the draft is approved.

## 1. What the owner wants

- The battery SOC target and minimum are computed in **Load Insights** (the forecast owner), not in Home Assistant templates and not in Load Juggler. They are configurable.
- The consumption forecast is **additive**: unknown loads (the unmetered remainder) plus the chosen known loads, each fitted on its own series. A load can be left out of the total.
- Far-out forecasts count less, **on both sides equally**: hour i's net is `w_i x (factor x production_i - use_i)`, `w_i = max(1 - drop x max(i - full_h, 0), 0)`.
- The planning sensors publish the **raw value**, negative included. Consumers clamp (Kozolec's SOC controller already does).
- **Load Juggler** stays forecast-free. It reads a target entity as it does now, and gains **pacing**: use the energy above target over the time left, not all at once (GitHub issue 22, LeoAlioth/Load-Juggler).
- Home keeps EVBox and Workshop Charger as they are.

## 2. Where it stands today

- `coordinator.py` fits `cons_fc` on the site series, `rem_fc` on the remainder, and `device_fc[...]` for every device. Today the total is fitted on its own and does not equal remainder + devices.
- **Plan LI-4 Part A (`docs/plans/2026-10-12/li-4-house-from-parts.md`) already specifies the additive total**: own part = a meter's series minus its mature parts, built bottom-up, the whole's own fit supplying the band, a maturity rule (`PART_MIN_WEEKS`). Reuse it as written; it is not repeated here.
- Commit `5357bad` added `forecast_exclude` ("Not in the consumption forecast"): excluded devices come off the fitted series. Set on Kozolec only (Pond EVSE, Power Strip, Well Pump).
- Kozolec HA has three template helpers doing the planning: Forecast SOC Target (`01KRS8XKGEFYKZRCNX688KBGE4`), SOC Minimum (`01M4GSP187G4Z08WHMZ851S4CH`) and Daily Excess (`01M4HBZZSZDXXBWC54CZKCNWR2`), read by the Home Battery SOC Controller automation. Since 2026-10-16 the target helper weights both sides (drop 0.9 %/h after 24 h); the **minimum and excess helpers still count the use in full** and the minimum still has the old midnight guard.
- LI already builds `BatterySocForecastSensor` and the grid forecast through `build_grid(cons_fc.hourly, pv, soc, capacity)`, and reads the solar forecast from the Energy dashboard's forecast integrations (`_solar_forecast`, arrays summed).

## 3. Part 1 - the additive total (LI-4 Part A, plus the exclusion)

Implement LI-4 Part A unchanged, with one addition: **an excluded device** (the owner's "not in the total") is never added back. Its energy always comes out of its parent's own part, even while it is too young to count as a mature part (otherwise a young excluded load would leak into the total). `forecast_exclude` keeps its key (stored keys are byte-stable); its label becomes "Not in the house forecast" and it replaces the fit-time subtraction of 5357bad, which goes away.

Checks: the built total against today's fitted total on both sites (day totals must agree within the fit noise), `tests/forecast_bench.py` hour-ahead / day-ahead / noon-tomorrow, with and without exclusions; the Kozolec numbers from 16 Oct (excluded loads: 330.0 W hour-ahead) are the bar.

## 4. Part 2 - the planning sensors

Two sensors per site, `soc_target` and `soc_minimum` (device class battery, %), state = the raw value.

Per hour i over the next 120 h (hours that LI has a production forecast for; all 120 must be present, else the sensor keeps its last value, as the helper does since 2026-10-16):

- production_i: `_solar_forecast` (both arrays summed), times `factor`
- use_i: the built house forecast (Part 1), left-out loads excluded
- net_i = w_i x (factor x production_i - use_i); S_i = running sum
- **target** = `full - max(0, max_i S_i) / capacity x 100`; `w` uses `full_h = 24`, `drop = 0.9 %/h`
- **minimum** = `floor + max(0, -min_i S_i) / capacity x 100`; today's helper has `floor = 10`, `full_h = 48`, `drop = 0.5 %/h` - port these as the defaults and apply the same two-sided weighting (decision 1 below)

Options (LI options flow, a new "SOC planning" page; defaults in brackets): forecast factor [1.0; Kozolec 0.95], target / minimum drop and full-hours, minimum floor [10], full SOC (an entity: LJ's "Home Battery SOC Full" number, or a fixed value), battery capacity (existing `battery_capacity_entity`).

Attributes: `peak_at`, `peak_surplus_kwh`, `dip_at`, `dip_kwh`, per-day net (kWh and points), the inputs used, and `production_ends_at` (the last hour with a meaningful forecast, for pacing). The coordinator's own cadence (hourly, on recompute) replaces the helpers' per-refresh updates; no extra smoothing is added.

**Tanks spread evenly.** Kozolec's helpers keep the boiler's and bee-room heater's energy but spread it evenly over their window (their boost moves it to midday, a dull spell needs it round the clock). Carry this as a per-device flag "plan with its energy spread evenly" used only by the planning sensors, default off, ticked for those two at Kozolec. (Open: whether it belongs in the sensors at all or in the fit.)

**Swing check.** Replay Kozolec's history hour by hour and compare with the helper's outputs: number of changes per day, largest one-hour move, and the day-to-day move when a forecast is revised by a given amount. Target: a typical revision moves the target about as much as its net energy (a 3 kWh net change is about 5 points), not tens of points.

## 5. Part 3 - Load Juggler pacing

In `_calculate_solar_surplus` (`calculations/target_calculator.py`) the battery's contribution above target is `discharge_potential = max_discharge - actual_discharge` (strictly above the hysteresis-latched target, `engine/hub_calculation.py::_apply_soc_hysteresis`). Pacing replaces it, when a horizon is configured, with

`min(max_discharge - actual_discharge, (SOC - target)/100 x capacity / hours_left)`

(hours_left floored at 1 h; converted to current with the site voltage). Needs two hub options: a **battery capacity** entity and a **horizon** (a timestamp entity; LI's `production_ends_at`). Without a horizon nothing changes. The target is still read from an entity, so no dependency on LI. Because the target rises during the day, the paced rate re-adjusts itself.

Tests: pure-Python unit tests of the pacing term, and YAML scenarios in `dev/tests/scenarios/` with `human_verified: false`. Release note, the config flow and `en` / `sl` strings (the owner reviews every Slovenian string). New optional keys with defaults need no migration step, but check `async_migrate_entry` and `MINOR_VERSION`.

**An EVSE cannot run below its minimum current.** When the paced pool is under `min_current x voltage` the load is off, and starts again only when the pool has risen past the minimum by a margin (a start threshold with hysteresis, so it does not chatter around the minimum).

**Not in scope:** the "Sun Only" mode through `sun_pool` (kept as a later option for the requester's buffer case), a manual "ignore these dates" option, the grid forecast's treatment of left-out loads.

## 6. Order

1. Part 1 (additive total, exclusion) and its bench.
2. Part 2 sensors, benched against the Kozolec helpers.
3. Kozolec switch-over: the SOC controller reads LI's sensors; the three helpers are deleted after a day of both running (snapshot, NOTES entry each step).
4. Part 3 pacing, with scenarios; a release is the owner's call.

## 7. Open decisions

1. **Minimum and excess helpers.** They still count the use in full. Weight them the same way now (a Kozolec change), or only in LI?
2. **Pacing after the horizon.** `docs/design-decisions.md` says a Solar Only load may run from the battery above target after sunset, by design. Pacing "until the horizon" contradicts that. Choose: after the horizon, fall back to the old behaviour (use down to target), or offer nothing from the battery. Re-ask before changing a settled decision.
3. **Horizon**: sunset, or the last hour with production forecast above a threshold? (The draft uses the forecast, `production_ends_at`.)
4. **Where the "spread evenly" flag lives.**
5. **Capacity**: LJ has no capacity field today; a new hub option or read from the LI sensor's attributes?

## 8. The owner's answers, 2026-10-17 (these override sections 4, 5 and 7 where they differ)

- **Part 1: approved as drafted** (LI-4 Part A plus the exclusion that is never added back).
- **Part 2: three planning sensors, not two.** Target, minimum AND the daily-excess list (today .. day Z-1, ported from helper `01M4HBZZSZDXXBWC54CZKCNWR2`, keeping its hour-under-way weighting and its `battery_energy_until_full` subtraction). Every sensor takes the SAME set of options, each with its own values:
  - **horizon in days Z**: ONE option for the whole planning page (default 5, i.e. 120 h);
  - **consumption**: full-weight hours X_c, then derate Y_c % per hour;
  - **production**: full-weight hours X_p, then derate Y_p % per hour (X and Y are separate for consumption and production, and for each sensor);
  - **scale for production** (today's `forecast factor`, default 1.0, Kozolec 0.95) and **scale for consumption** (new, default 1.0), shared by the three sensors.
  - Defaults are the owner's values: target X = 24 h, Y = 0.9 %/h on both sides; minimum X = 48 h, Y = 0.5 %/h on both sides, floor 10 %; excess: its current behaviour (no derate, X = 24 h, Y = 0) unless the owner says otherwise.
  - Minimum and excess now weight BOTH sides, like the target (their helpers still count the use in full).
- **Part 3: pacing is a per-load setting, Excess mode only for now, none for Solar Only.** Not a hub-wide switch: each load has a "pace" option (default off). Mechanism to implement (owner's intent: use the planned energy leisurely until the horizon instead of racing): a hub-level pair of optional entities - the energy still to use (kWh; LI's daily-excess for today) and the horizon end (timestamp; LI's `production_ends_at`) - and a paced load in Excess mode is capped at `energy / hours_left` (hours_left floored at 1 h); no entities or the option off = today's behaviour. This replaces the (SOC - target) x capacity formula in section 5 and needs no capacity option. Check it against how Excess mode builds its pool (`_calculate_excess_available`, the Excess branch of `_source_limit`) and say so in the report if the code points to a better place for the cap. An EVSE's minimum current still needs a start threshold with hysteresis.
- **Open decisions 7.1-7.5**: 7.1 answered (weight them); 7.2 not needed (no Solar Only pacing); 7.3 horizon = the forecast's last hour with production (`production_ends_at`); 7.4 and 7.5 are the implementer's call, said in the report.
