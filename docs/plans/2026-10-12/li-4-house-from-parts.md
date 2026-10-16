# LI-4 - The house forecast built from its parts; Kozolec's minimum SOC without the over-target loads (plan, 2026-10-12)

Personal project (Load Insights + Kozolec HA). Never log to any work-context file.
Repo `/Users/anzek/Projects/Load_Insights`, branch `dev`, head `482aa9a`, tree clean.
Plan only: nothing below has run except the read-only reads and template renders marked **(read)**.
Part B does not depend on Part A and can run first.

---

## Part A - consumption = the sum of its parts

### A0. Where it stands (coordinator.py 223-309)

- `consumption = combine(series, site.consumption_terms())`. `cons_fc = fit(consumption, cal_signals)` is fitted on its own.
- `remainder = subtract_all(consumption, series, remainder_ids)`. `rem_fc` is fitted with the unlinked signals only.
- Every device with rows gets `device_fc[d.energy]`. That includes the nested devices, each fitted on its own meter.
- `cons_fc` feeds four things:
  - `ForecastPowerSensor("consumption")`: the state is `next_hour_w` (W, measurement, so it has long-term statistics); the attributes are detailedForecast, today/tomorrow, score, level, temperature, signals.
  - `build_grid(cons_fc.hourly, ...)`, which feeds `GridForecastSensor` and `BatterySocForecastSensor`.
  - `overview.py` (next hour, today/tomorrow) and `diagnostics.py`.
  - The `SITE_KEY` ledger.
- LI writes no statistics from forecasts. `detection.py`'s `async_import_statistics` is for named loads only.
- Kozolec live **(read, 12 Oct ~21h)**, tomorrow:
  - fitted total 20.21 kWh
  - from the top-level parts: remainder 0.17 + Inverter (L1) 17.12 + losses 2.60 = 19.89
  - inside L1, the 14 plugs sum to 15.53 (bee room heater 5.25, 1.2 weeks of history), so L1's own part is about 1.6 kWh.
- Nesting:
  - **Kozolec** has 2 levels: L1 holds 14 plugs; inverter_losses has nothing inside it.
  - **Home** has 3 levels:
    - Mansarda holds 11 devices, including Pisarna, which holds EcoFlow.
    - Hiša holds 6.
    - Delavnica holds 7, including Centralna, which holds Obtočni črpalki.
    - Attic AC, Polnilnica, Workshop Charger and West Blinds are top-level and have nothing inside them.

### A1. Decisions

1. **Leaves and parents.**
   - A *part* of X (X = None means the site) is a listed device whose `included_in` is X. A device whose `included_in` names an unlisted statistic counts as top-level, which is what `remainder_devices()` does today.
   - A *leaf* has no parts. A *parent* has at least one.
2. **Own part.**
   - own(X) = X's series − its **mature** parts, computed with `subtract_all`. That function already counts a part as 0 before its first row and never goes below 0.
   - The own part is fitted as its own series, exactly as the remainder is today.
   - **The site's own part IS the remainder.** The remainder sensor becomes consumption − mature top-level devices. While a top-level device is young it stays inside the remainder, just as it did before it was listed. Today no top-level device is young at either site, so the remainder does not change.
3. **Built(X)** = own(X) + Σ built(part) over X's mature parts.
   - It is computed bottom-up.
   - Every parent with at least one mature part is built, whether or not the parent itself is mature.
   - A leaf, or a parent with no mature parts, keeps its own fit.
4. **Short history: the maturity rule.**
   - A part joins its parent's sum only once its own fit's `span_weeks >= PART_MIN_WEEKS`.
   - Start at `PART_MIN_WEEKS = 3.0`, which equals `HALF_LIFE_WEEKS`. By then the part's own hours carry about half of the recency weight of the parent's own-part profile.
   - Before that, the young part's energy stays in the parent's own part over the whole window. That keeps the history consistent: the own part's profile neither double-counts the young device nor drops it.
   - The sweep {0, 1, 3, 6} in A4 picks the value. The winning value becomes the constant, with no toggle.
   - Today this leaves out only the bee room heater (1.2 weeks). Both fridges (3.3 weeks) join.
5. **Centre.** Sum of the parts' hourly means. Means add, so the day totals stay exact.
   - Then cap at the whole's own largest hour, the same rule as `profile.forecast`.
   - The cap can break the exact sum only in an hour above anything the meter has ever done.
6. **Band.** Take the whole's own fitted spread and move it to the parts' centre:
   - lo = c − (w − w_p10)
   - hi = c + (w_p90 − w)
   - then clamp to [0, cap].
   - The band always contains the centre, because the whole's fitted band already contains its own centre (b173e48).
   - Why: the parts are correlated. Occupancy moves everything together; at Kozolec, LJ moves the heater, boiler and EVSE together. RSS of the parts' half-widths assumes they are independent and would make the band too narrow. The whole's own band is the measured spread of the sum, correlation included.
   - A4 benches RSS beside it on day-ahead coverage (target 0.8). If RSS covers better, it ships instead.
   - So the whole is still fitted independently, but it now supplies only the band.
7. **Other Forecast fields** come from the whole's own fit, via `dataclasses.replace`: level, temperature, signals, span, history.
   - Recomputed from the built hours: `hourly`, `bands`, `next_hour_w`, `today_kwh`, `tomorrow_kwh`.
   - New field `parts: tuple = ()` holds the labels that were summed. A young part is listed as `"<label> (in own part)"`.
8. **Signals for an own part:** the inputs linked to nothing, plus those linked to X itself (`within(t)[0] == X`).
   - Inputs linked to a part belong to that part.
   - For X = None this is exactly today's remainder scoping (unlinked only).

### A2. What every consumer gets

| consumer | change |
|---|---|
| `sensor.*_consumption_forecast` | Same meaning (house forecast) and same unit. Its values are now built. Its level / temperature / signals attributes describe the whole's own fit, which now supplies only the band; add one docstring line saying so. New attr `parts`. Its long-term statistics simply carry on (no migration). |
| `sensor.*_unmetered_consumption_forecast` | Becomes "consumption − mature top-level devices". `subtracted_devices` lists exactly those. No change today at either site. |
| parent device sensors (Kozolec `sensor.inverter_forecast`; Home Mansarda, Hiša, Delavnica, Centralna, Pisarna) | **Meaning changes**: now own part + mature parts, so the nesting adds up on the sensors. New attr `parts`. The own part is not a sensor; it is in diagnostics only. Add a sensor if Anze wants one. |
| leaf device sensors | unchanged |
| `GridForecastSensor`, `BatterySocForecastSensor` | Values change through `build_grid(cons_fc.hourly)`. Meaning unchanged. |
| overview page, diagnostics | Overview reads the built forecast. Diagnostics add `"consumption_fitted"` (the independent total) and each `own:<stat>`, so the two can be compared live. |
| ledgers | `SITE_KEY` and each parent's ledger score the built forecast against their own actual. The remainder ledger scores the own part. The 7-day metrics mix the old and new forecasts for one week after the deploy; say so in the release note. |

### A3. Code (shortest diff)

1. **`insights/model.py`**
   - Add `SiteModel.parts_of(parent: Optional[str]) -> tuple`: listed devices whose effective parent is `parent`, with an unlisted `included_in` counting as None.
   - `remainder_devices()` becomes `return self.parts_of(None)`.
   - Add `own_scoped(signals, links, within, stat)`: `[g for g in signals if not links.get(g.entity) or (stat is not None and any(within(t)[:1] == [stat] for t in links[g.entity]))]`.
2. **`insights/profile.py`**
   - Add `PART_MIN_WEEKS = 3.0` and `Forecast.parts: tuple = ()`.
   - `from_parts(whole, parts, actual, now) -> Forecast` covers A1.5-A1.7: index-aligned (same `now`, same horizon), the cap computed from `actual` the way `forecast` does it, and `day_total_kwh` / `next_hour_watts` for the totals.
   - `assemble(rows_of, fitted, kids_of, fit_own, now) -> (built, own)` is the bottom-up walk of A1.3-A1.4, written once.
     - It is pure and stdlib-only.
     - `fit_own(stat, rows)` is a callable, so the coordinator and the backtest run the **same** walk (one measure for every meter).
     - `own[stat] = (rows, fc)` is returned for scoring and diagnostics.
3. **`coordinator.py`**
   - Fit the devices first (the existing loop, without its `score` call).
   - Then run one executor job: `assemble(...)`, with `fit_own = lambda stat, rows: forecast(rows, now, HORIZON_HOURS, hols, temps_hist or None, temps_fc or None, own_scoped(cal_signals, links, within, stat) or None)`.
   - Then:
     - `cons_fc = built.get(None, cons_fit)`
     - `rem_rows, rem_fc = own[None]`, falling back to today's remainder when there are no parts
     - `device_fc.update({k: v for k, v in built.items() if k})`
   - Score: `SITE_KEY` takes `cons_fc`, `REMAINDER_KEY` takes `rem_rows`, each device takes its (built) forecast.
   - `build_grid` is unchanged; it takes `cons_fc.hourly`.
   - Delete the standalone `rem_fc = await fit(remainder, ...)`, because `own[None]` is the remainder.
4. **`sensor.py`**
   - Add the `parts` attr (consumption, parent devices).
   - The remainder's `subtracted_devices` comes from the walk's mature list.
5. **`diagnostics.py`**: the two extra entries from A2.
6. **Tests** (one per piece of new logic):
   - `test_model.py::test_parts_of_walks_three_levels`: Home's tree, including Centralna→Obtočni and Pisarna→EcoFlow, plus an unlisted `included_in` that counts as top-level.
   - `test_profile.py::test_from_parts_sums_means_moves_the_whole_band_and_caps`
   - `test_profile.py::test_assemble_leaves_a_young_part_inside_its_parent`: a 1-week child is not summed, and its energy stays in the own part's rows.
   - `tests/run_all.py` passes.
7. `RELEASE_NOTES.md`: one line under 0.4.1. The AGENTS.md "Where things are" table gets a row for `tests/forecast_bt.py`.

### A4. Backtest (independent vs from parts)

The scratchpad `fc/bt.py` that AGENTS.md mentions is **gone**: `fc/home` and `fc/koz` are empty, and only stale code copies remain. Rebuild it as **`tests/forecast_bt.py`** (committed, so it cannot be lost again). Its cache is gitignored under `data/forecast/<site>.json`.

1. **Fetch** (read-only), using `node /Users/anzek/Projects/HA_Configs/tools/ws.mjs <site> cmds.json`:
   - `energy/get_prefs`
   - `recorder/statistics_during_period` with `{period: "hour", units: {energy: "kWh"}, types: ["change"]}`, covering every id in `SiteModel.all_statistic_ids()`
   - the outdoor temperature (`types: ["mean"]`); its entity is the consumption sensor's `temperature_response.temperature_entity` attribute
   - span: 12 weeks before the window start through to now
   - Save the output to `data/forecast/<site>.json`. Tokens stay inside ws.mjs.
2. **Run**: for every hour `now` in the window, 29 Sep 00:00 - 12 Oct 00:00 local (312 runs per site):
   - Cut every series at `now`.
   - Fit each device and the whole: `forecast(rows, now, 168, hols, temps, temps)`. The forecast temperatures are the actual ones, which flatters both arms equally. No calendars or inputs: the same for both arms, and the site's signals already score worse than none (AGENTS, 1237 vs 1070 W).
   - Then `assemble(...)` with the same `fit_own`.
   - Arms:
     - A = `fitted[None]`
     - B(N) = `built[None]` with `PART_MIN_WEEKS` = 0, 1, 3, 6 (a bench-only override)
     - B(3)-RSS = B(3) with the band from the RSS of the parts' half-widths (bench-only)
3. **Metrics per site** (total vs `consumption`, in W):
   - MAE at lead 1 (hour ahead) and lead 24 (day ahead), the ledger's leads
   - MAE over 1-5 h and 18-29 h, comparable with the older figures
   - |noon `tomorrow_kwh` − actual day| (kWh)
   - p10-p90 coverage at lead 24
   - For each arm pair: the paired mean difference, and the share of runs where B beats A
   - The same table for each parent: Kozolec L1, and Home's five parents
4. **Acceptance**: in each site, B's total MAE at lead 1 **and** at lead 24 is ≤ A's, and the daily error does not rise.
   - Pick N among the B(N) that pass, by lead-24 MAE.
   - The band rule is whichever covers closer to 0.8.
   - If no B passes at a site, stop and bring the table to Anze. No per-site switch.
5. **Where to run**: libench if Anze is not using it, otherwise the Mac (`python3.14`). Both sites in parallel.
6. **Commits**: commit the result locally, with the table in the commit body (A vs B per site, per lead). **Do not push** until Anze says. A push to dev auto-deploys to Home and Kozolec, and both restart by themselves.

### A5. Ship (when Anze says)

1. Push dev. Home and Kozolec install and restart. Andrej's site is not included.
2. Read back on Kozolec:
   - `sensor.kozolec_pv_kozolec_insights_consumption_forecast` `tomorrow_kwh` = remainder + `sensor.inverter_forecast` + `sensor.inverter_losses_forecast`, within rounding.
   - `parts` lists the bee heater as "(in own part)".
   - The grid and SOC forecasts are live.
3. After 7 days, compare the ledgers' `score.leads` against the backtest.

---

## Part B - Kozolec minimum SOC: the Pond EVSE and Power Strip out, everything else as forecast

### B0. What is live (read)

- `sensor.kozolec_forecast_soc_minimum` is template entry `01M4GSP187G4Z08WHMZ851S4CH`.
- The options flow was opened and aborted. The fields are `state`, `unit_of_measurement` ('%'), `device_class`, `state_class`, `device_id`, `additional_options`.
- The live `state` is byte-identical to `kozolec/changes/2026-10-12/forecast_soc_minimum_after_minimum_discount.txt`.
- **It already subtracts three loads**: `sensor.pond_evse_forecast`, `sensor.power_strip_forecast`, `sensor.well_pump_forecast`.
- **It also spreads the tanks evenly**: `sensor.boiler_forecast_2`, `sensor.bee_room_heater_forecast`.
- **Anze's decision (2026-10-12):** the well pump is counted again, so only the Pond EVSE and the Power Strip are subtracted. The tanks are no longer spread evenly; every remaining load counts as forecast, hour by hour.
- Renders now **(read, /api/template)**: live **18 %**, new text (B2) **16 %**.
- Per-device forecast sensors (DeviceForecastSensor, unique id `<entry>_device_<statistic>`):
  - `sensor.pond_evse_forecast` ← `sensor.pond_evse_energy`
  - `sensor.power_strip_forecast` ← `sensor.power_strip_energy`
  - Hourly series: attribute **`detailedForecast`**, a list of `{period_start, kwh, kwh_p10, kwh_p90}` with 168 rows, the same shape as the house sensor's.
- Once Part A ships, the house hour is the exact sum of its parts, including both plugs (both mature, 12 weeks). The subtraction then removes exactly their share, and the clamp at 0 never bites, short of the total's cap clipping a sum.

### B1. Steps

1. `node /Users/anzek/Projects/HA_Configs/tools/snapshot.mjs kozolec before-minimum-no-evse-strip`
2. Save the live `state` to `kozolec/changes/2026-10-12/forecast_soc_minimum_before_no_evse_strip.txt`. Read it from the options flow and abort the flow, as `tools/read_helper_options.py` does.
3. Render the new text (B2) via `POST /api/template`. It must be a number 10-100. Write the before and after renders down. The expected value (12 Oct ~21h) is 16, against 18 now.
4. Save it:
   - Open the options flow for `01M4GSP187G4Z08WHMZ851S4CH`.
   - Set `state` to the new text and keep every other non-None field as it was. This is the `tpl_fix.py` pattern from the scratchpad (`soc/tpl_fix.py`).
   - POST to the flow and expect `type: create_entry`.
5. Write the new text to `kozolec/changes/2026-10-12/forecast_soc_minimum_after_no_evse_strip.txt`.
6. Check that `states('sensor.kozolec_forecast_soc_minimum')` equals the render, and that LJ's minimum slider follows at the controller's next run (it reads the helper).
7. `node tools/snapshot.mjs kozolec after-minimum-no-evse-strip`
8. Add a line to `kozolec/NOTES.md`. It should cover:
   - the date
   - "Minimum SOC counts all but the over-target loads" (Anze)
   - the Pond EVSE and Power Strip subtracted; the well pump and the tanks (boiler, bee room heater) now counted as LI forecasts them, with no even spread
   - minimum 18 → 16
   - the snapshot names and the changes/ file names
   - "with LI-4 the house forecast is the sum of its parts, so the subtraction is exact"
   - the target and daily-excess helpers left as they are (they still subtract three loads and spread the tanks)

### B2. New template text (the full `state`)

```jinja
{# Minimum SOC: 10 % plus the deepest the battery's balance (S) dips below where it is now over the next 120 h. #}
{% set factor = states('input_number.kozolec_forecast_factor') | float(0.8) %}
{# the further out, the less of the forecast sun is counted: full weight for full_h hours, then drop % less
   per hour (Anze, 2026-10-12: 48 h, 0.5 %); the use is counted in full #}
{% set full_h = states('input_number.kozolec_minimum_full_hours') | float(48) %}
{% set drop = states('input_number.kozolec_minimum_drop_per_hour') | float(0.5) / 100 %}  {# share of the Open-Meteo forecast we count on #}
{% set flat = states('input_number.kozolec_base_consumption') | float(300) / 1000 %}
{% set pack = states('sensor.kozolec_battery_capacity') | float(0) %}  {# kWh: installed Ah x SOH x 51.2 V #}
{# Use per hour: Load Insights' house forecast less the two loads that run only over the target - Pond EVSE and
   Power Strip (Anze, 2026-10-12); everything else as Load Insights forecasts it, the tanks and the well pump
   included; input_number.kozolec_base_consumption for an hour Load Insights does not cover. #}
{% set li = namespace(base = {}) %}
{% for p in state_attr('sensor.kozolec_pv_kozolec_insights_consumption_forecast', 'detailedForecast') or [] %}
  {% set li.base = dict(li.base, **{ (as_timestamp(p.period_start, 0) | int) | string: p.kwh | float(0) }) %}
{% endfor %}
{% for e in ['sensor.pond_evse_forecast', 'sensor.power_strip_forecast'] %}
  {% for p in state_attr(e, 'detailedForecast') or [] %}
    {% set k = (as_timestamp(p.period_start, 0) | int) | string %}
    {% if k in li.base %}{% set li.base = dict(li.base, **{k: [li.base[k] - p.kwh | float(0), 0] | max}) %}{% endif %}
  {% endfor %}
{% endfor %}
{# S: the battery's running energy balance from this hour on, 120 h of factor x Open-Meteo (both arrays) less the use #}
{% set start = now().replace(minute=0, second=0, microsecond=0).timestamp() %}
{% set ns = namespace(e = 0, s = [], ok = pack > 0) %}
{% for d in ['today', 'tomorrow', 'd2', 'd3', 'd4', 'd5'] %}
  {% set a = state_attr('sensor.energy_production_' ~ d, 'wh_period') %}
  {% set b = state_attr('sensor.energy_production_' ~ d ~ '_2', 'wh_period') %}
  {% if a is mapping and b is mapping %}
    {% for k in a | list | sort if start <= as_timestamp(k, 0) < start + 120 * 3600 %}
      {% set w = [1 - drop * ([(ns.s | count) - full_h, 0] | max), 0] | max %}
      {% set ns.e = ns.e + factor * w * (a[k] + b.get(k, 0)) / 1000 - li.base.get((as_timestamp(k, 0) | int) | string, flat) %}
      {% set ns.s = ns.s + [ns.e] %}
    {% endfor %}
  {% else %}
    {% set ns.ok = false %}
  {% endif %}
{% endfor %}
{% set ns.ok = ns.ok and ns.s | count >= 100 %}
{{ (10 - ([ns.s | min, 0] | min) / pack * 100) | round(0) if ns.ok else none }}
```

The target helper (`sensor.kozolec_forecast_soc_target`) and the daily-excess helper (`sensor.kozolec_forecast_daily_excess`) are left as they are. Their use term still subtracts the Pond EVSE, Power Strip and Well Pump, and still spreads the tanks. Say so in the NOTES line.

Scratch copies: `/private/tmp/claude-1773206459/-Users-anzek-Projects/4b9e35e1-1c3c-4305-92d3-4b8a8a8f0980/scratchpad/li4/` (`minimum_live.j2`, `minimum_new.j2`, `read_live.py`, `render.py`).
