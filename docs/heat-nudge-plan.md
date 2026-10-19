# Heat nudge - implementation plan

Settled with the owner 2026-10-19: Load Insights (LI) is the home of the shadow; the objective is
electrical **energy (kWh) only**. Slovenia's 15-minute blocks are charged on peak POWER, and other sites
peak-shave with hybrid inverters, so there is no "cost under the 15-minute tariff" objective here (see
section 6 for what a nudge must instead never do to a block's peak).

Status: **stage 1, shadow mode, is built and committed on `dev` (not pushed, not deployed, no site
built).** Stages 2 and 3 are plans for Load Juggler (LJ) and are not started.

## 1. The idea in one paragraph

A heat pump, or an air conditioner / heat pump used as heating, makes a unit of heat for less electricity
when the outdoor air is warmer (about 2-3 % COP per degree for air-source units) and when the required
flow, tank or room temperature is lower. Humidity matters only in the cold damp band (about 0-5 C: frost
and defrost). So: use the forecast to make the device's heat in the best hours, and "nudge" it in those
hours (SG Ready state 3, a boiler's target up, a room thermostat or AC target up by about a degree).
Before anything is written to a device, LI observes, computes and publishes, so the owner can judge from
real data whether a nudge would pay.

## 2. The COP proxy (`insights/heatnudge.cop`)

`COP = ETA x T_hot / (T_hot - T_cold)` in kelvin, `T_hot = target + sink offset`, `T_cold = outdoor - 5 K`,
kept to 1..8. `ETA = 0.45` is a **judgement** (an air-source unit reaches 40-50 % of Carnot), not a
measurement. **It is a proxy, never a measured COP**, and every published object says so (`proxy`
attribute). ETA scales every COP alike, so it moves no ranking and no gap in degrees, only the size of the
percentages.

| kind | default target | sink offset | shift limit (judgement) | outdoor +1 K | target +1 K |
|---|---|---|---|---|---|
| `space_heating` | 35 C | 5 K | 4 h | +2.6 % | -2.1 % |
| `hot_water` | 50 C | 5 K | 8 h | +1.9 % | -1.5 % |
| `room` (direct room heating / AC) | 21 C (room target) | 12 K (indoor coil above room air) | 2 h | +3.1 % | -2.6 % |

(Computed at 5 C outdoors with the code; test `test_cop_proxy_slope_is_two_to_three_percent_a_degree_at_35c`.)
Humidity is a flag only: `frost` on an hour that is 0-5 C and at least 80 % RH. It never changes a COP.
The weather entity's hourly forecast supplies humidity when it carries it.

Outdoor temperature is the input LI already uses for its temperature evidence (INS-15): the outdoor sensor's
hourly statistics for the past, the weather entity's forecast for the coming hours. Both must be configured
(the Explanatory inputs page) or the heat sensors say why they are unavailable.

## 3. Deriving hourly electrical energy (the research, and its limits)

The shadow needs electrical kWh per hour. Three kinds of reading exist; `classify_energy` tells the
cumulative ones apart from their own history.

| reading | how hours come out | limits |
|---|---|---|
| **power sensor** (Home attic AC plug, Kozolec mini-split, anything with a power sensor and statistics) | the hourly **mean** statistic in W / 1000 = kWh | needs a `state_class` so statistics exist; standby counts |
| **energy sensor that moves through the day** (a plug's energy, a meter) | the hourly **change** of the long-term statistic | none beyond the sensor's own resolution |
| **a counter that moves once a day** (Andrej's MELCloud, via his utility meters) | **only the day**: `daily_from_lumps` attributes a change before 06:00 to the day before (MELCloud publishes yesterday's total after midnight, `HA_Configs/andrejg/NOTES.md`, 2026-10-04) and `spread_daily` spreads it over 24 hours | see below |

**Andrej's case, honestly.** What exists is one number per day, delivered just after the next midnight, with
no hour information at all. Hours can only be *assigned*, never measured:

1. with an optional **activity entity** (a climate's `hvac_action`, a switch, a binary sensor that says when
   the unit heats; MELCloud's climate entity polls about once a minute), the day is spread by the time the
   unit was active in each hour (`active_fractions`). `basis = "activity"`. The shape is real, the split
   inside an active hour is flat; a unit that modulates is mis-split a little.
2. without one, by **heating demand**, `max(0, 15 C - T)` per hour. `basis = "modelled"`. Then the "actual
   weighted outdoor temperature" is the outdoor temperature weighted by an assumed shape, so the gap says
   how much a nudge would gain over a demand-shaped day, **not over what the pump did**. Such days are
   counted apart (`days_modelled`) and left out of the headline gap. The owner must not judge on them.

What a daily-only device does still give honestly: the daily kWh and its relation to the day's mean outdoor
temperature (the fitted line `day_need` already uses), i.e. the heating curve of the whole system, and a
check afterwards on whether warmer days used less. The MELCloud-side knobs (a Shelly with a power meter on
the pump's supply, the unit's own flow-temperature and compressor-frequency sensors) would give real hours;
that is the practical recommendation if the owner wants Andrej's verdict rather than a modelled one
(section 9, decision 3).

Other limits: a day equal to the day before leaves no state change and is missed (his notes); the first
scored day appears one day after the first lump; hot-water and heating energy are separate meters at his
site, so each is one heat device (hot water: kind `hot_water`; heating: `space_heating`).

## 4. What is built (stage 1, shadow mode)

| piece | where |
|---|---|
| pure module: COP proxy, hourly energy derivation, `shadow_schedule`, `forecast_day`, `scorecard_day`, `summarize`, stored-state `migrate`, `parse_devices`, `publish` | `custom_components/load_insights/insights/heatnudge.py` |
| coordinator: `_heat_nudge` (reads statistics, temperatures, humidity, activity; one store) | `coordinator.py`, `InsightsData.heat` |
| options page "Heat devices" (`async_step_heat`; key `heat_devices`) | `config_flow.py`, `const.py` |
| sensors per heat device: `HeatIndexSensor`, `HeatScorecardSensor`; binary `HeatNudgeNow` | `sensor.py`, `binary_sensor.py` |
| translations en + sl, icons | `translations/`, `icons.json` |
| tests | `tests/test_heatnudge.py` (picked up by `tests/run_all.py`) |

**Shadow schedule.** For a day, expected energy by hour = the device's expected daily kWh x its hour shape.
Daily kWh (`day_need`): a straight line of past days' kWh on mean outdoor temperature when at least 7 days
spread 2 K and the line explains 20 % of them, else the median. Hour shape (`hour_shape`): the past days'
share of energy per clock hour, days near the forecast day's temperature counting most (by hour *and* by
temperature); flat with no history (then there is no schedule, only the COP index). The shadow keeps the
**heat** (`energy x COP`) and moves it, worst hour first, to a strictly better hour within the kind's shift
limit, never above max(rate, its own energy) in an hour (`rate` = the device's 95th percentile active
hour, the same `absorption_rate` the early start reads). Greedy, not optimal (`ponytail:` in the code);
a transport solution if it ever looks short. "Heat-hours needed" is thus the expected daily kWh over the
rate, implicit in the profile.

**Published per device** (all are attributes of the COP index sensor, state = the hour's COP divided by the
day's mean hour, 1.00 = average):

- hourly series for today and tomorrow: temperature, COP proxy, index, rank, frost flag, expected kWh,
  shadow kWh, `nudge` (the shadow gives that hour more than expected);
- per day: `need_kwh`, `gain_vs_day_average_pct` (shadow's energy-weighted COP against the plain mean of the
  day), `saving_pct` (electricity the same heat would save, proxy), `extra_peak_kw`, nudge hours;
- `would_nudge_now`, also as the binary sensor.

**Daily scorecard** (`HeatScorecardSensor`, one per day, stored): the outdoor temperature weighted by when
the electricity was used (`actual_t`), the same figure for the shadow schedule built from the same energy
(`shadow_t`, real temperatures = a perfect forecast), `gap = shadow_t - actual_t` (K, positive = a nudge
would have used warmer hours), and the **forecast variant** `gap_forecast`: the schedule picked from
*yesterday evening's forecast* temperatures (stored from 18:00, 14 days), judged at the real ones - what a
nudge could really have done. Also `saving_pct`, `extra_peak_kw` (the shadow's highest hour less the actual
one, hourly, so only a floor of any 15-minute block's peak), `days_measured` / `days_modelled`, energy-
weighted means, and the last 30 days. State = the newest day's gap. No heat-output meter is needed.

**Storage.** `load_insights.<entry>.heatnudge`: `{generation, days: {device: {date: card}}, forecasts:
{device: {date: [24 temps]}}}`, bounded (400 days, 14 forecast days, a few hundred bytes a day). The days of
an hourly device are recomputed from the recorder for the whole 12-week window every refresh, so only the
older days and the forecast temperatures are irreplaceable. `heatnudge.HEAT_GENERATION` (1) and `migrate`
follow the storage rule in `custom_components/load_insights/AGENTS.md`: bump it and add a step whenever a
stored field changes meaning; an unknown generation starts empty. `DETECTOR_GENERATION` is untouched.

Not yet exercised against a live Home Assistant (none is installed in the pure tier and none was called):
the coordinator method, the options page and the entities are read-checked and unit-tested only through
their pure parts; the first live look belongs to the first site that configures a device.

## 5. Order of work

1. **Shadow (done).** Configure a heat device per site once the owner chooses (Home attic AC, Kozolec
   mini-split when it exists, Andrej's two utility meters). Let it run at least four weeks of the heating
   season, nothing written anywhere.
2. **Judge** from the scorecard (section 8).
3. **Boiler and room-thermostat devices in LJ**: in a best hour, raise the target by a comfort band
   (below). These devices exist in LJ already (`hot_water_tank`, `room_thermostat`, with their boost setpoint
   and thermostat entity; read-only for this plan). The LJ work is one new input per device: LI's `would
   nudge now` entity and a band; nothing in LI changes.
4. **SG Ready state 3** (Recommended on) once JUG-24 is live: the same input joins the early-start verdict
   and the battery-above-target rule that already select Recommended on.

Each stage needs the owner's go; the next is judged on real data from the previous one.

## 6. Nudge limits (for stages 3 and 4; the shadow already reports what it can)

1. **Comfort band per device.** Boiler: target up by at most a few degrees, never above the tank's own
   maximum; room thermostat / AC: about +1 K above the comfort target, never above comfort + band; the band
   is a per-device setting in LJ. Returned to the base target when the hour ends.
2. **SG Ready: 10-minute minimum per state** (JUG-24's rule), so a nudge never flaps the relays; a nudge window
   shorter than that is skipped.
3. **Never during a Block**, and never against a solar-surplus run. **Solar surplus comes first**: when the
   site has actual or forecast excess (the early-start verdict, Excess trigger, battery above target) LJ
   already commands Recommended on / Force on or heats for free; the nudge fills only the remaining hours.
4. **Never raise a peak.** A nudge raises the device's draw. It must not push a 15-minute block's average, or
   the site's peak-shaving / max-import limit, higher: the nudge **yields to Block, to the Max Import Power
   headroom and to any peak-shaving limit** (the headroom LJ already computes; the nudge is allowed only
   when the device's rate fits inside it). The scorecard reports `extra_peak_kw` per day, derivable from
   hourly energy only (a floor of the 15-minute peak); the first live nudge also needs LJ's own 15-minute
   figure, which LI cannot see.
5. **The gain must beat the penalty.** A higher setpoint costs COP in the moment (table in section 2: about
   -1.5 to -2.6 % per kelvin of target, in that hour) plus storage and standing losses for heat held
   (tank / floor / room). A nudge is worth it only when the COP gain of the receiving hour over the hour it
   replaces exceeds `band x penalty per K + storage loss`. The shadow's `saving_pct` is before that
   penalty; section 8 applies it.
6. **Energy only.** The objective is kWh. No tariff, no price signal.

## 7. Interface between Load Insights and Load Juggler

LI **publishes**, LJ **consumes**; nothing flows back.

- Binary sensor `Would nudge now` per heat device (`binary_sensor.<device>_would_nudge_now`): on while the
  hour under way is a shadow hour. Unavailable when LI has no schedule (no history, no temperature, no
  forecast); LJ treats unavailable as off.
- Sensor `COP index` per heat device, attributes `series` (hourly: `start`, `temp`, `cop`, `index`, `rank`,
  `nudge`, `expected_kwh`, `shadow_kwh`, `frost`), `today` / `tomorrow` (`nudge_hours`, `saving_pct`,
  `gain_vs_day_average_pct`, `extra_peak_kw`, `need_kwh`), `would_nudge_now`, `kind`, `target_c`, `proxy`.
- LJ reads them through an optional entity field on a device, as it already does for LI's `Room to absorb`,
  `Absorption rate` and `Base consumption`; LJ owns the comfort band, the minimum state times, the Block /
  headroom yield and the surplus priority. LI never reads LJ's state for this feature and never calls a
  service on any device.

## 8. How success is decided

From the scorecard over **at least four weeks of the heating season, and at least 20 scored days with
`basis` measured or activity** (modelled days do not count), per device, energy-weighted:

- `mean_gap_forecast` (the realistic figure, not the perfect-forecast `mean_gap`) at least **+1.0 K**, which
  is about 2.5 % kWh at 35 C targets (about 2 % for hot water);
- positive on at least 70 % of the scored days;
- `mean_saving_pct` (proxy) greater than the penalty of the band the owner would allow (for +1 K at 35 C
  about 2.1 %, plus an allowance for storage loss; see section 6.5) - in words: the shadow must save at
  least about 4-5 % to leave a net 2 %;
- `mean_extra_peak_kw` no higher than the device's headroom on the site (section 6.4).

Judged per site: a device that clears it on one site and misses on another is a decision for that site
(the repo's rule of ship a big win with a small loss elsewhere applies to code, not to a device's
nudge). Pass: build stage 3 for the devices that pass. Fail or ambiguous: leave the shadow running or
remove the device; a fail means this schedule was not good, not that the idea is dead (record the
numbers). The gap is **not** a measurement of a COP and a heat meter would sharpen it; if the pass is
marginal that meter (or a power sensor on a daily-only unit) is the next step.

## 9. What the owner must decide

1. **Which devices to configure first** and with which target (Home attic AC as a `room` device is the
   one that exists today; Andrej's two utility meters need decision 3; Kozolec has nothing yet).
2. **The judgements in the code:** `ETA` 0.45; the sink offsets (5 / 5 / 12 K); the shift limits (hot water
   8 h, space heating 4 h, room 2 h); the comfort band per device for stage 3; the success thresholds in
   section 8 (+1.0 K, 20 days, 70 %, ~4-5 % saving).
3. **Andrej's pump:** accept a modelled verdict (day totals only), or add an activity entity, or a real
   hourly source (a power meter on the pump, the unit's compressor sensors) before judging. Recommendation:
   an activity entity at least, and a power meter if his result is to decide anything.
4. **Whether a daily-only device is worth scoring at all** on the demand-shaped basis (it is counted apart
   and never in the headline).
5. **Stage 3 needs LJ work in another repo**: who builds it, and whether the nudge input belongs on the
   boiler / room-thermostat entry or on the hub.
6. **Slovenian wording** of the new strings (listed in the build report); the terms are mine, not the
   owner's decided ones.

## 10. Known limits and not done

- The coordinator method, options page and entities are untested against a live HA. First live check: that
  the heat devices appear, that the options page saves, that the sensors read, and what
  `classify_energy` says about the real sources.
- The 15-minute figure behind `extra_peak_kw` is not derivable in LI (hourly statistics only).
- No humidity in the COP, by decision; no defrost model.
- No per-hour comfort model: the shift limit is a flat judgement per kind, not a thermal model of the tank,
  floor or room (`insights/thermal.py` holds kWh per degree for LJ thermostat loads and could replace the
  shift limit for them later).
- Hot water and heating of one pump are two heat devices; no sharing of a compressor's schedule between
  them.
- A device renamed on the options page gets a new id and a new, empty scorecard.
