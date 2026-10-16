# LI 3 - covariates engage on statistical evidence, not after a fixed two weeks (plan, 2026-10-12)

Personal project (Load Insights). Never log to any work-context file.
Repo `/Users/anzek/Projects/Load_Insights`, branch `dev`, head `482aa9a`, tree clean.
Plan only: nothing below has run. Forecast work is re-opened for this item (and li-4) only; the rest of
the 2026-10-03 back-burner stays parked.

---

## 0. Where it stands (read)

| gate | where | value | applies to |
|---|---|---|---|
| `MIN_HOURS` | `insights/covariates.py:26` | 336 rows with residual + temperature | temperature response |
| `MIN_EXPLAINED` | `covariates.py:27` | 0.03 of residual variance | temperature response |
| `MIN_HOURS` | `insights/nowcast.py:25` | 336 rows per lead | device-state nowcast (`_fit_lead_joint`) |
| `MIN_EXPLAINED` | `nowcast.py:26` | 0.03 at the best lead, and per added state | nowcast forward selection (`fit_joint`) |
| `MIN_ON_HOURS` / `MIN_OFF_HOURS` | `insights/calendars.py:23-24` | 24 / 24 | calendars AND attached sensors (`CalendarSignals.from_labels`) |
| `MIN_EXPLAINED` | `calendars.py:26` | 0.03 | same |
| `MIN_HOURS` | `insights/inputs.py:48` | 168 labelled hours (`usable()`) | attached sensors, before they become signals |

- **Same kind of gate everywhere**: a fixed amount of time plus an explained-share floor. `covariates.py`
  itself holds only temperature; the banded/categorical attached entities are `inputs.py` ->
  `CalendarSignals.from_labels` -> `calendars.fit_factors`, i.e. the calendar machinery. So the new rule
  covers all three (temperature, nowcast, calendars + attached sensors). It is ONE helper and ONE
  critical value.
- **The fixed-time gate is not the only reason a young series never engages.** `_settled`
  (`profile.py:127`) gives each hour's residual against the slot mean that INCLUDES that hour. A slot
  seen once has residual exactly 0, so a 192 h series (bee room heater, 8 days) has a non-zero residual
  only on the one weekday seen twice: 48 of 192 hours. Lowering the hour floor alone would change nothing
  for it. Fix: the fit's residual is **leave-one-out** (LOO). Step 2.
- **The temperature centring double-counts the daily cycle (found while reading).** Features are centred on
  ONE global weighted mean (`h_mean`, `covariates.py:91`). The 05:00 slot mean was learned at 05:00's
  usual cold, but `delta()` adds `a * (hdh(05:00) - global mean) > 0` every night, even on an ordinary
  night. In the fit, the between-hour HDH offsets add variance to x with no matching residual, which pulls
  `a` toward 0. That may be part of Kozolec's whole-house 0.7 %. Fix: centre each feature **per slot**,
  the way the profile centres consumption. Step 2. It gets its own backtest column, so its effect is
  measured apart from the gate.
- Kozolec's inputs: temperature `sensor.weather_station_temperature`, horizon from the weather entity.
  Home's: `sensor.outside_t_h_temperature`. Series history: `HISTORY_WEEKS = 12` (`const.py:160`), long-term
  statistics.
- **The backtest script is gone.** AGENTS.md:927 points to "scratchpad fc/ (bt.py)". The scratchpad's
  `fc/` now holds only code copies (`old/ band/ at50/ scope/ strength/ listorder/ oldtest/`) and two empty
  dirs (`home/`, `koz/`); `bt.py` is nowhere on disk. Step 6 writes a new one inside the repo so it
  cannot be lost again.

---

## 1. The rule (one helper, every covariate)

**Rows.** Each row is `(day, w, x, u)`:
- `day` = `int(hour_key // 86400)`. UTC days; the 1-2 h offset from local midnight does not matter for
  clustering.
- `w` = the profile's recency weight.
- `x` = the k centred features.
- `r` = the residual; `u` = r minus the fitted value (defined below).

**Fit** (unchanged estimator):

```
A = Σ w x xᵀ   (+ JOINT_RIDGE on the diagonal for nowcast, as now)
β = A⁻¹ Σ w x r
u = r − βᵀx
```

**Honest variance.** Day blocks (cluster-robust), plus one day of Newey-West lag. Hours inside a day are
correlated in any way they like. Consecutive days (a cold spell, a level shift) are covered by the lag
term, which can only make the test stricter:

```
g_d   = Σ_{i∈d} w_i x_i u_i                                 (k-vector per day block)
c     = G / (G − 1)                                         (CR1 small-sample factor; G = day blocks)
M     = c · Σ_d g_d g_dᵀ
M_nw  = M + c · ½ · Σ_{d, d+1 both present} (g_d g_{d+1}ᵀ + g_{d+1} g_dᵀ)       (Bartlett, lag 1)
var_j = max( [A⁻¹ M A⁻¹]_jj , [A⁻¹ M_nw A⁻¹]_jj )
t_j   = β_j / sqrt(var_j)
```

**Kept** when `|t_j| ≥ t_crit(G − 1, ALPHA / L)`:
- `L` = the number of candidates the term was chosen as the best of. That is 1 for temperature and
  calendars, and `LEADS` = 6 for the nowcast's best lead (Bonferroni).
- `t_crit` is Student's t quantile, computed in stdlib (no table, no scipy). The Cornish-Fisher expansion
  is accurate to under 4 % at df 3 and under 0.5 % from df 8:

  ```
  z = NormalDist().inv_cdf(1 − p)
  t ≈ z + (z³+z)/(4ν) + (5z⁵+16z³+3z)/(96ν²) + (3z⁷+19z⁵+17z³−15z)/(384ν³)
  ```

  Unit-check it against known values: t(3, .995) = 5.841, t(10, .995) = 3.169, t(30, .995) = 2.750.

**Floors (same for every covariate):**
- `G ≥ MIN_DAYS`. A day block counts toward G only if it has ≥ 12 rows, so a history that starts at
  23:00 does not buy a degree of freedom. Thinner blocks still enter the sums.

**Removed:** both `MIN_HOURS = 336`, all three `MIN_EXPLAINED`, `MIN_ON_HOURS` / `MIN_OFF_HOURS`, and
`inputs.MIN_HOURS`. The `explained` share is still computed and published; it just no longer gates. It
also still drives `Nowcast.band_shrink`.

### Constants (one place: `covariates.py`; `nowcast.py` and `calendars.py` import them)

| constant | value | reason |
|---|---|---|
| `ALPHA` | 0.005 | False-engagement rate per term per fit. A site has ~20-30 series × 1-3 terms, refit every 15 min, so 0.005 keeps chance engagements to roughly 0.1-0.3 terms per site at any time. The bar it sets: t ≥ 5.84 at 4 days, 3.50 at 8 days, 2.98 at 15, 2.75 at 31, 2.6 at 12 weeks. Every one of these is well above t = 1, which is where a term starts lowering out-of-sample error (expected gain ∝ Var(β̂)·Var(x)·(t² − 1)). So a kept term is expected to pay for itself. Swept in step 7 (0.05 / 0.005 / 0.0005). |
| `MIN_DAYS` | 4 | The test needs ≥ 3 degrees of freedom to say anything; at G = 3 even t = 9.9 is the bar at this α. 4 days = 96 h, against 336 h now (Anze suggested ~72 h). A strong real response passes in 4-5 days; a weak one waits for its evidence, which is the point. |
| `MIN_SPREAD_C` | 1.0 °C | **Temperature only.** It is the only covariate with a physical unit whose horizon value (a forecast cold snap) can sit far outside what the fit saw. A coefficient is fitted only if its slot-centred feature's weighted SD ≥ 1 °C (`sxx ≥ MIN_SPREAD_C² · W`). That is far below an ordinary week's day-to-day variation, and above a fog week's (±0.3 °C), so "a flat week cannot pass". The nowcast needs no such floor: a state that does not vary gets a huge SE and fails the t-test by itself. Calendars use "on and off each on ≥ MIN_DAYS days" instead (step 4). |
| `HEATING_BASE_C` / `COOLING_BASE_C` | 15 / 24 | unchanged |
| `JOINT_RIDGE`, `MIN_PER_HOUR_OF_DAY`, `FACTOR_CLAMP`, `MIN_RESIDUAL_MS`, `MIN_RESIDUAL_SHARE` | unchanged | Numerical guards and table fallbacks, not engagement gates. |

**`MIN_EXPLAINED` decision: dropped as a gate.**
- A term with honest t ≥ ~2.6 lowers expected error whatever its hourly R².
- Hourly R² also understates the day-level value. Kozolec's whole house at 0.7 % hourly could still move
  the day totals by a real amount.
- Step 7 runs the final variant again with `MIN_EXPLAINED = 0.03` put back. If that is better at either
  site, it stays as a practical-size floor; the result is reported, not decided alone.

---

## 2. Temperature (`covariates.py`, `profile.py`)

1. `covariates.py`:
   - Add `ALPHA`, `MIN_DAYS`, `MIN_SPREAD_C`, `t_crit(df, p)` (the 3-line Cornish-Fisher) and
     `day_block_t(rows, beta, A_inv) -> (t list, G)`, about 25 lines.
   - Delete `MIN_HOURS` and `MIN_EXPLAINED`.
2. `fit_temperature_response(rows)` takes rows `(key, w, r, x_h, x_c)`: the features come already
   slot-centred from the profile (step 2.4), so the function no longer computes `h_mean` / `c_mean`.
   1. Drop a feature whose spread < `MIN_SPREAD_C`. If both are dropped:
      `_no("temperature varied by X °C over the fit, 1.0 needed")`.
   2. Fit the remaining one or two. Clip negatives, as now. If a coefficient is clipped or fails its t,
      refit the other alone and re-test it, so the reported t belongs to the coefficient actually applied.
   3. `G < MIN_DAYS`: `_no("N days have both a residual and a temperature, 4 needed")`.
   4. Nothing kept: `_no("heating t = 1.8, 3.5 needed at 8 days")` (wording by example).
   5. `TemperatureResponse` gains `t_heating`, `t_cooling` and `days`. `hdh_mean` / `cdh_mean` become the
      centring tables (step 2.4).
3. `Profile` keeps its per-tier sums. `fit_profile` already computes `sw/swx/hw/hwx/tw/twx`; store them
   on the frozen dataclass. Add `Profile.loo_kwh(dt, w, v)`: `(swx − w·v)/(sw − w)` at the slot, falling to
   hour-of-day and then overall when the slot has nothing left without this hour. `_settled` yields
   `e_loo` beside `e`:
   - **Every covariate fit uses `e_loo`**: temperature, calendars and nowcast residuals.
   - `level_correction` keeps `e`. It judges the last 24 h and is not a fit.
4. Per-slot centring. In `forecast()`, build `hdh`/`cdh` weighted sums per slot, per hour-of-day and
   overall, over hours that have a temperature, with the same `slot_of(t, holidays)` and `_weight`.
   - **Fit**: `x = hdh(T) − LOO tier mean of hdh`, matching `e_loo`.
   - **Apply**: `TemperatureResponse.delta(temp, dt)` uses the FULL tier mean at `dt`'s slot, same
     fallback order as `Profile.slot_kwh`. It is `a·(hdh(T) − centre_h[slot]) + b·(cdh(T) − centre_c[slot])`.
   - Change the four call sites in `profile.py` to pass `t`: `level_correction`, `residual_rows`, the
     nowcast residuals and the horizon loop.
   - `ponytail:` a slot's consumption mean also counts hours without a temperature, while its centre counts
     only hours with one. It matters only while the temperature sensor is younger than the series. Add a
     temperature-hours-only slot mean if the bench shows it.

## 3. Nowcast (`nowcast.py`): yes, the same kind of gate

1. `_fit_lead_joint`:
   - Delete the `MIN_HOURS` check.
   - Rows carry `day` (`build_rows_joint` has `k`).
   - Return `(c, explained, t list, G)` through `day_block_t`, using the ridged `A`.
2. `fit_joint`:
   - Order the states by single-state best-lead |t|, not by explained.
   - Add a state if, fitted jointly with those already in, its own coefficient's |t| at its best lead
     ≥ `t_crit(G − 1, ALPHA / LEADS)`.
   - Engaged if at least one state is kept. A near-copy of a kept state gets a large joint SE, so it is
     not added (keeps `test_a_second_reading_of_the_same_thing_is_not_added` meaningful).
   - Delete `MIN_HOURS` and `MIN_EXPLAINED`. `Nowcast` gains `t` (per kept state, best lead) and `days`.

## 4. Calendars and attached sensors (`calendars.py`, `inputs.py`)

1. `fit_factors`: the existence fit is the WLS of `a = e·(c₁ + c₂·on)` with row weights `w/e`.
   - Its solution is exactly today's ratios: `c₁ = r_off`, `c₁ + c₂ = r_on`.
   - Test `c₂` (on vs off) with `day_block_t`, using x = `[e, e·on]` and weights `w/e`. No new maths.
   - Titles: the same, on the calendar's on-hours with `[e, e·title_on]`.
   - Gate: on-hours in ≥ `MIN_DAYS` day blocks AND off-hours in ≥ `MIN_DAYS`, then |t(c₂)| ≥ crit.
     The per-hour-of-day tables ride on the single contrast passing, as the single pair rides on the guard now.
   - Delete `MIN_ON_HOURS`, `MIN_OFF_HOURS` and `MIN_EXPLAINED`. `Factors` gains `t` and `days`.
2. `inputs.py`: delete `MIN_HOURS`. `usable()` = more than one distinct label; the day floor lives in the fit.
3. This is also what the parked "signals worse than without" (1237 vs 1070 W) was about. Ten days of an
   input reading as a ×2.1 evening must now clear a day-clustered t, not a 3 % share. Step 7 reports
   with-signals against without; fixing that item is not this plan's acceptance.

## 5. Attributes (`sensor.py`)

- `temperature`: add `t_heating`, `t_cooling`, `t_needed`, `days_fitted`. `residual_explained` and
  `reason` stay.
- `nowcast` and each calendar/input: add `t` and `days_fitted`.
- All are nested inside dicts that are already unrecorded. Check `tests/test_sensor_attributes.py`.

---

## 6. Tests (`tests/`, stdlib, `run_all.py`)

`tests/test_covariates.py`, new:
1. `test_a_true_response_engages_within_four_days`
   - 5 days of hourly history: `pattern(t) + 0.5·hdh(T) + N(0, 0.3)`.
   - T = daily mean drawn from 2-12 °C, plus a ±4 °C diurnal cycle.
   - Expect: engaged, `heating_kwh_per_degh` within 15 % of 0.5, `days ≥ 4`.
   - Same series cut to 3 days: not engaged, reason names "4 needed".
2. `test_noise_does_not_engage_over_eight_weeks`
   - Consumption independent of T. Residuals AR(1) hourly (ρ = 0.9) plus a day offset AR(1) (ρ = 0.7).
   - T with a slow multi-day drift: the case an iid t-test passes falsely.
   - Fit at days 4, 5, 7, 14, 28 and 56: never engaged. Fixed seeds, 5 of them.
3. `test_a_flat_temperature_week_does_not_engage`
   - 7 days at 10 ± 0.3 °C, consumption = `pattern + 0.5·hdh` (a real response, no spread).
   - Expect: not engaged, reason names the spread.
4. `test_a_temperature_that_follows_the_clock_is_no_response`
   - Diurnal T, no response. Expect: not engaged.
   - Even engaged with a real response: the forecast at a slot's own mean temperature equals the slot
     mean (per-slot centring, `delta ≈ 0`).
5. `test_t_crit_matches_students_t`: the three reference values above, within 4 % at df 3 and 0.5 % from df 8.
6. Rewrite:
   - `test_a_response_that_does_nothing_says_why`: the "336 needed" assertion becomes "4 needed", and rows
     become `(key, w, r, x_h, x_c)`.
   - `test_too_few_hours_with_temperature_means_no_fit` (100 constant hours): still not engaged, now by
     the spread.

`tests/test_nowcast.py`:
- `test_too_little_state_history_means_no_fit`: 200 h now *would* engage on a real relation. Make it 48 h
  (2 days).
- Add `test_a_true_state_relation_engages_within_four_days`.
- Existing noise and second-reading tests stay as they are.

`tests/test_calendars.py`:
- `test_too_few_on_hours_means_no_fit` -> on-hours on 2 days: no fit.
- Add `test_an_away_calendar_on_four_days_engages`.
- `test_a_meaningless_calendar_does_not_engage` stays.

Gate: `python3.14 tests/run_all.py` passes before step 7.

---

## 7. Measurement: the forecast backtest

### 7.1 Data (read-only, `data/forecast/<site>/` - `data/` is gitignored)

- **Config.** `GET /api/diagnostics/config_entry/<entry>` gives the options verbatim (temperature entity,
  weather entity, calendars, inputs, links). Entry ids are in `HA_Configs/tools/li_flow.py`: Home
  `01M2MHR02E73618M7Y0JY5GEEK`, Kozolec `01M2TB111THS8W6GF66PRKYDM8`. Tokens are in `data/<host>`.
- **Energy prefs and statistics** via `node /Users/anzek/Projects/HA_Configs/tools/ws.mjs <site> cmds.json`:
  - `energy/get_prefs`
  - `recorder/statistics_during_period`, `period: "hour"`:
    - `types: ["change"], units: {energy: "kWh"}` for `SiteModel.from_prefs(prefs).all_statistic_ids()`
    - `types: ["mean"]` for the temperature sensor and every numeric linked input
    - `start` = window start − 12 weeks, so every origin sees what production would
  - The websocket `start` is in **ms**; divide by 1000.
- **Calendars**: `GET /api/calendars/<id>?start=&end=`. No archived weather forecasts exist, so the
  horizon temperature is the **measured** one (a perfect forecast). This states an upper bound for the
  day-ahead gain, the same for old and new.

**Windows:**
- **W1 (tune)**: 2026-09-28 → 10-11 (14 days, origins every hour).
- **W2 (hold-out)**: a heating-season fortnight, if the statistics and both temperature sensors reach it
  (check first; e.g. 2026-01-12 → 01-25). Otherwise the earliest 14 days that have 12 weeks before them.
- AGENTS' ten-day rule is about the detector's recorder; the forecast reads 12 weeks of long-term
  statistics in production, so this is production's input.

### 7.2 Script: `tests/forecast_bench.py` (new, tracked)

- Builds the series exactly as `coordinator.py:186-229` does: `_rows_to_samples` logic,
  `combine(series, site.consumption_terms())`, `subtract_all` for the remainder, every device, `scoped()`
  signals per series, and `holiday_dates`.
- Each origin calls `profile.forecast(...)` and feeds `scoring.Ledger.record/settle`, so the metrics are
  production's own: hour-ahead (lead 1) and day-ahead (lead 24) MAE in W, the noon "tomorrow" kWh error,
  and p10-p90 coverage.
- Variants are temporary bench dials (module constants, set by `DIAL=V` like `bench._apply`):
  - `covariates.EVIDENCE`, `covariates.SLOT_CENTRE`, `nowcast.EVIDENCE`, `calendars.EVIDENCE`: 0 = today's
    code path.
  - `MIN_EXPLAINED_FLOOR`.
  - `SIGNALS` = 0/1.
- Output: one JSON per run in `data/forecast/results/`, plus a per-series table on stdout.
- `python3.14`; series × origins in a `multiprocessing.Pool`.
- ≤ 8 at once on the Mac. libench only if Anze is not using it.

### 7.3 Runs

| run | dials |
|---|---|
| OLD | all 0 |
| GATE | `covariates.EVIDENCE=1` (gate only: global centring, in-sample residual) |
| CENTRE | GATE + `SLOT_CENTRE=1` (LOO + per-slot centring) |
| NOW | CENTRE + `nowcast.EVIDENCE=1` |
| CAL | NOW + `calendars.EVIDENCE=1`, `SIGNALS=1`; compared with OLD `SIGNALS=1` |
| sweep | CAL with `ALPHA` = 0.05 / 0.005 / 0.0005, and with `MIN_EXPLAINED_FLOOR=0.03` |
| young | Bee room heater from its first hour (2026-10-04); the whole house of both sites cut to its last 4 / 7 / 14 days. First origin it engages, its t and coefficient, and day-ahead MAE against `temps=None` |

Run every row on W1, then the chosen row and OLD on W2.

### 7.4 Acceptance (deterministic replay - no noise band; ties count as "not worse")

1. Per site, whole-house series: hour-ahead MAE, day-ahead MAE and noon-tomorrow kWh MAE are each
   NEW ≤ OLD, on W1 **and** W2.
2. Per site, the sum over all series of day-ahead MAE: NEW ≤ OLD.
3. List every series whose hour- or day-ahead MAE rises by > 1 % and > 5 W. For each newly engaged one,
   find out why (dig, do not revert, per "explore boldly"). Do not ship with a newly engaged series worse
   by > 5 % and unexplained.
4. Report for each site, every series and each run:
   - engaged (OLD / NEW)
   - coefficient in W/°C, or per unit, or the factor
   - t and t_needed
   - days
   - explained share
   - MAE h-a / d-a, OLD → NEW
   - coverage
   Name explicitly: the bee room heater, both whole houses, and which terms engage under NEW that did not
   under OLD.
5. CENTRE against GATE is reported apart: the double-count fix ships only if it holds (1)-(2) by itself.

---

## 8. Docs (same commit as the code)

- **AGENTS.md**:
  - Under `## The dials`, a new `### Forecast covariates (covariates.py, nowcast.py, calendars.py, inputs.py)`
    table with the same columns: `ALPHA`, `MIN_DAYS`, `MIN_SPREAD_C`, `HEATING_BASE_C` / `COOLING_BASE_C`,
    `JOINT_RIDGE`, `MIN_PER_HOUR_OF_DAY`, `FACTOR_CLAMP`, `HALF_LIFE_WEEKS`, `LEVEL_*`.
    - Each row gets its kind and its tested-by: step 7's numbers.
    - One line lists the removed gates (`MIN_HOURS` ×2, `MIN_EXPLAINED` ×3, `MIN_ON/OFF_HOURS`,
      `inputs.MIN_HOURS`).
  - "Where things are": add the `tests/forecast_bench.py` row and how to fetch its data (7.1).
  - The back-burner item (line 919): add "temperature evidence re-opened 2026-10-12: <result>". Replace
    the dead `bt.py` pointer with `tests/forecast_bench.py`.
  - Under "Never ship ... unmeasured": add one paragraph saying forecast changes are measured with
    `forecast_bench.py` on W1 + W2 under the 7.4 rule.
- **RELEASE_NOTES.md**, `## 0.4.1` (still the open section: dev builds are tagged `0.4.1-dev.*`), New
  Features, user wording, with the measured numbers filled in. Draft:
  - **The weather counts as soon as the data proves it does.** The temperature response, a device's own
    state and attached calendars and sensors no longer wait two weeks (or a week). Each one engages once
    its effect is statistically clear. The test counts days, not hours, so a run of similar hours cannot
    fake it. A new heater can follow the cold within days; a weak link waits until the data is clear. The
    response is also measured against each hour's usual temperature, so a cold night is no longer counted
    as both a night and a cold spell. Backtest: <site: hour-ahead / day-ahead before → after>.
  - Removed: "the fixed two-week minimum and the 3 % explained-share floor (the share is still shown)".

---

## 9. Commit and deploy

1. Bench checkpoint commits, local, as you go:
   - "Bench dials EVIDENCE/SLOT_CENTRE ... (0 = unchanged)"
   - After step 7: the final commit with the dials removed (proven changes ship without toggles) and the
     evidence in the message (AGENTS "Commit messages carry the evidence").
   - `python3.14 tests/run_all.py` passes at each commit.
2. **Do not push.** Pushing `dev` auto-deploys. Wait for Anze's word.
3. When he says push:
   - HA_Configs snapshot before and after on **Home and Kozolec only**. Andrej's site gets nothing unless
     he says.
   - Sites install the dev build and restart.
   - Forecast only: no `DETECTOR_GENERATION` bump, no reset.
4. Verify on the sites after the first refresh (read-only):
   - Kozolec bee room heater and whole-house `temperature.engaged`, `t_heating`, `t_needed`, `days_fitted`.
   - The same at Home, plus the nowcast and signals attributes.
   - The ledger's hour-ahead and day-ahead MAE over the next 7 days against the 7 before, in the score
     attributes.
   - Note the results in AGENTS.md's back-burner line.

---

## Open for Anze (defaults in brackets, the plan runs on them)

1. ALPHA [0.005; the sweep decides between 0.05 / 0.005 / 0.0005].
2. MIN_EXPLAINED [dropped; it comes back only if the 0.03 variant wins at either site].
3. Per-slot centring and LOO [in this plan, as their own measured column. They are what lets a young
   series engage at all, and they fix the night-time double count].
4. Calendars and attached sensors under the same rule [yes; step 4 is its own column (CAL), so if it
   loses, temperature and nowcast can still ship without it - his call].
