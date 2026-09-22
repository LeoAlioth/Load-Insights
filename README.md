# Load Insights

Consumption forecasts for a Home Assistant site, from what its **Energy
dashboard already knows**. Companion to Load Juggler, which controls loads;
this one understands them. Fully independent - it does not need Load Juggler,
and Load Juggler does not need it.

## What it does (phase 1)

Reads the Energy dashboard's configuration - grid import/export, PV, battery,
individual devices, linked PV forecast entries - and the recorder's hourly
statistics for them, and publishes a consumption forecast:

| entity | state | attributes |
|---|---|---|
| **Consumption forecast** | expected average power over the coming hour, W | `detailedForecast`: 7 days hourly kWh with a `kwh_p10`/`kwh_p90` spread (the shape and naming the PV forecast integrations use); `history`: the last 48 hours as they happened, same shape; today/tomorrow kWh, level correction, weeks of data |
| **Grid forecast** | what the METER will do over the coming hour, W - positive importing, negative exporting | `detailedForecast` with consumption, PV, battery and SOC per hour; tomorrow's import and export totals |
| **Battery SOC forecast** | the pack's expected state of charge at the end of the coming hour, % | hourly SOC, and the day's minimum and maximum |
| **Base load** | what the site draws with nothing switched on, W | per phase, per meter, and each phase's noise floor |
| **Unmetered consumption forecast** | as the first, for consumption minus every individually metered device | `subtracted_devices`, `remainder_complete_since` |
| **\<device\> forecast**, one per individually metered device | as the first, for that device alone | `statistic_id`, plus that device's own `score` and `nowcast` |

Consumption is `grid in - grid out + PV + battery out - battery in`, the
dashboard's own signs. A device listed as included in another listed device
(`included_in_stat`) is not subtracted twice.

**One sensor per thing forecast.** The state is the prediction for the coming
hour and the attributes carry the rest, so daily totals, import and export
figures and the individual scores are attributes rather than entities of
their own - a template sensor makes one wherever a dashboard wants it.

**A metered device's forecast lives on that device.** The dashboard's
statistic id resolves through the entity registry to the Home Assistant
device behind it, and the forecast is attached there - so the boiler's
forecast sits on the boiler, beside its own sensors. Only loads found by
profiling the meter, which have no device of their own, get a new one. Where
no device can be resolved (a template or helper statistic) the forecast falls
back to a device of its own under the site. A device added to the Energy
dashboard later gets its sensor after a reload of the integration.

### Scoring

Scores are the `score` attribute on every forecast sensor: mean absolute
error and bias per lead, how often the actual landed inside the p10-p90 band,
and yesterday's day-ahead total against what the day then used.

Every hour the forecast's value for the hour one hour, one day and one week
(the horizon's last row, 167 h) ahead is remembered; when that hour arrives, prediction and actual are paired
and the errors kept for 30 days in `.storage`. They report the trailing
week. Scores need time to fill: the hour-ahead figures after a
day, the day-ahead ones after two, the week-ahead table after eight.

### Actual against forecast on one chart

`dashboard/forecast-cards-live.yaml` is a card that builds itself: paste it
into a dashboard and it draws one graph per forecast sensor - actual, forecast
and the likely range - finding them again on every render, so a device added
to the Energy dashboard just appears. Needs config-template-card beside
Plotly. The rest of this section is the hand-written version of one card.

Every forecast-type sensor carries `history` (the last 48 hours, actual) and
`detailedForecast` (the next 168, forecast) in the same shape, so a chart card
that can read attributes draws both from one entity. With
[Plotly Graph Card](https://github.com/dbuezas/lovelace-plotly-graph-card):

```yaml
type: custom:plotly-graph
title: Consumption - actual and forecast
hours_to_show: 72
time_offset: 24h
refresh_interval: 60
entities:
  - entity: sensor.kozolec_insights_consumption_forecast
    name: Actual
    type: bar
    filters:
      - fn: |-
          ({meta}) => ({
            xs: meta.attributes.history.map(p => new Date(p.period_start)),
            ys: meta.attributes.history.map(p => p.kwh),
          })
  - entity: sensor.kozolec_insights_consumption_forecast
    name: Forecast
    line:
      width: 2
      dash: dot
    filters:
      - fn: |-
          ({meta}) => ({
            xs: meta.attributes.detailedForecast.map(p => new Date(p.period_start)),
            ys: meta.attributes.detailedForecast.map(p => p.kwh),
          })
layout:
  yaxis:
    title: kWh / h
    rangemode: tozero
```

The same pair works for the unmetered remainder and for any device sensor.
Note that the forecast drawn over past hours is today's fit, which already
contains them - a picture of how well the profile explains the week, not a
test of yesterday's prediction. Scoring stored past forecasts is later work.

The forecast is recomputed once an hour from twelve weeks of hourly
statistics. The model is a recency-weighted hour-of-week profile (weight
halves every three weeks) with a damped, clamped correction from the last 24
hours. Each hour also carries a spread: the weighted 10th and 90th
percentiles of that slot's samples, so a weekday 10:00 where the car charges
on some weeks reads "0.4 to 4.0 kWh" rather than an average that never
happens. `kwh` is the mean, so daily totals add up exactly; percentiles do
not add, so the daily sensors carry no band. Link a weather entity and an outdoor temperature sensor and the forecast
responds to the weather: a temperature response (heating and cooling
degree-hours) is fitted on what the weekly profile leaves unexplained, using
the sensor's past and applied to the weather entity's coming week - and kept
only if it explains a real share of the residual, so an input that turns out
not to matter cannot make the forecast worse. Each sensor's
`temperature_response` attribute says whether it engaged and by how much.
A device can be given its own state sensor (Configure -> "A device's own
state sensor"): the boiler its tank temperature, the pump its tank level. The
sensor's recorded past is regressed, hour by hour of lead, against what the
device then did, and its live value shifts that device's forecast for the
next six hours and narrows their spread - a cold tank means the boiler is
about to run. Nothing beyond six hours is touched, because the state has no
forward source. The `nowcast` attribute on the device sensor shows the fit.

Any entity can be attached as an explanatory input - a tariff block, an
electricity price, irradiance, an occupancy sensor. It is labelled (its own
state where it has few values, a quantile band where it is numeric) and
fitted exactly like a calendar, then carried into the week where the input is
schedule-like, or held for a few hours where it is not. An input the weekly
profile already knows - anything that is a pure function of weekday and hour,
a tariff among them - will not engage, because the 168 slots already contain
it; such an input earns its place only when its schedule changes.

Link any calendars and Load Insights works out what each one means: from
its past events an existence signal is fitted on the residuals, per hour of
day - an away calendar comes out as daytime factors near 0.4 and nights near
1, a guests calendar as evenings above 1 - and applied to the week where its
future events fall. Event titles are then fitted on the calendar's own
on-hours, so a title earns a factor only for how it differs from the
calendar's average. A calendar that explains nothing is reported and ignored
(`calendars` attribute on every forecast sensor). Public holidays are scored as
Sundays, in the
history and in the week ahead, using the `holidays` library that Home
Assistant's Workday integration installs (no Workday, no holidays - the
sensor's `holidays_modelled` attribute says which). Nothing learned in the
machine sense; every number is explainable.

## Load detection (phase 2, first stage)

Point the *Load detection* page at the meter's raw per-phase readings -
active power per phase above all; power factor, current and voltage where the
meter has them - and Load Insights watches the recorder's raw states at the
meter's own cadence, incrementally, backfilling its window in slices. A
phase leaving its idle baseline opens a **session**; sustained changes inside
it are levels, so a washing machine is one session with a heater level and a
motor level; sessions that start and end together on several phases are one
multi-phase load. Closed sessions are matched to **signatures** (phase set,
watts per phase, duration, PF) and described in words - "6.1 kW on A+C, ~80 s,
every 3 min, seen 258 times". Meters below the main one need no setting up. Every device on your Energy
dashboard is resolved to its Home Assistant device and its power readings are
discovered the same way - per phase where the hardware has them, one total
where it does not - so a load that the main meter and a device's own meter
both see is **located and named** at once. `included_in_stat` supplies the
nesting, so a load seen by both the workshop's meter and the boiler's belongs
to the boiler; a signature's `location` says which, or `main` when no device
meter saw it. Two sensors: **Detected loads** (how many are on now, with the signature
library and every device meter's in its attributes) and **Unknown load
power**.

What is left over - the loads no device meter accounts for - is named by you,
once, on the *Name a detected load* page: pick one from the list, described in
words, and type a name. It then gets a **running** binary sensor and a
**power** sensor of its own. Two signatures given the same name are one device
on different settings, and the page suggests candidates for that: same phases,
same power factor, never running at once. Naming signatures, explaining them with the dashboard's known
devices, and feeding them back into the forecast are the next stages.

## What a load might be

Every signature is offered a guess at what kind of thing it is, in two layers.
The first is physics and is stated plainly; the second is a guess about houses
and is always asked as a question. Nothing in the second layer can contradict
the first - it only asks *which* motor, *which* heating element.

- **a heating element** - power factor 0.93-1.0, holds one flat level, 80 W to 20 kW. The range is wide because the family is: a towel rail and the backup resistive heat in a heat pump's air handler are the same physics two hundred-fold apart
  - **a hot water tank?** - 800 W to 4 kW, running 15 min to 5 h
  - **cooking?** - 700 W to 7 kW for a few minutes to an hour, weighted by whether it runs at meal times. A whole induction hob is commonly wired across two phases and peaks far above one ring
- **a motor** - power factor 0.35-0.93, 20 W to 4 kW, *or* a start that towers over the run. An induction motor draws several times its running current until it is up to speed, and nothing else in a house does - so where the meter catches it, it identifies a motor on its own, even one behind a variable-speed drive whose power factor would otherwise read as a heating element
  - **a pump?** - 250 W to 2.2 kW in bursts of 20 s to 15 min
  - **a fridge or freezer?** - 30 to 350 W, 5 min to an hour, flat across the day, keeping a regular interval
- **a three-phase motor** - the same factor but *balanced on all three legs*, 300 W to 9 kW. Its own family rather than a guess: nothing else in a house draws the same power on each leg at a motor's power factor
  - **a three-phase workshop machine?** - 700 W to 7 kW, seconds to minutes, mostly during working hours
  - **a pump?**
- **an appliance that varies its own power** - near-unity factor but stepping or gliding rather than holding
  - **a pump?** - 80 W to 700 W. A pump behind a variable-speed drive corrects its own factor back to near unity, so a window that fits a straight-to-line induction pump would exclude it; size and burst length carry this one
  - **cooking?** - an induction hob modulates the same way
- **an appliance running a programme** - 2.5 levels or more over at least 15 minutes. The only family that stands without a power factor at all, since a programme steps through its stages whatever its factor
  - **a dishwasher?** - 45 min to 4 h, 400 W to 2.5 kW, many levels. An eco cycle runs to the top of that
  - **a washing machine?** - 30 min to 8 h, which is a washer-dryer combination doing both programmes back to back. Neither profile asks for a heat spike, because an industrial washing machine has no heaters
  - **a tumble dryer?** - 30 min to 3 h, fewer levels
- **electronics** - power factor 0.2-0.9, 1 W to 300 W
- **a car charging** - a heating element's factor for half an hour or more, at **6 to 80 A per phase**. Measured in amps rather than watts because that is what the standard limits and what a charger is set to: 6 A is the floor in IEC 61851 (a Tesla will go to 5), 32 A is the common ceiling, and 63 A on three phases or about 80 A on one are the extremes. A band on total watts describes nothing real - it calls a three-phase charger idling at its 6 A minimum a 4.1 kW load and scores it as large, while the same 4.1 kW on one phase is 18 A and quite a different thing

Two families within 0.15 of each other are **both** named. Below the margin the
second layer says nothing rather than guessing, and no guess is ever certain:
a hair dryer and a fan heater are the same reading, and the ceiling says so.

**A family reaching further than the appliances beneath it is deliberate.** A
150 W towel rail and a 15 kW electric boiler are both heating elements and
neither is a hot water tank, so they are named as the family and nothing more.
The second layer only asks a question where a profile actually fits; where none
does it stays quiet rather than reaching for the nearest.

### Except where the meter's name says what it is

A load that turns out to sit on a device meter takes that meter's **name** over
anything inferred from its shape, and is then stated without the question mark.
This is the best evidence there is - shape can only say a load draws 1.8 kW at a
heating element's power factor, while whoever wired the site already wrote
`Boiler` on it - and it beats the shape in exactly the cases worth having: a
boiler that cycles for 70 seconds is nothing like the quarter-hour a hot water
tank is expected to run, and a pressure pump behind a variable-speed drive reads
as a heating element at 0.96.

Matched on whole words, against the friendly name and the entity id both, in
English and Slovene - so `Water Pump`, `Hidrofor` and
`sensor.kotlovnica_well_pump_energy` all say pump. Only device words: most
meters are named after **rooms**, and a room says nothing about what is plugged
into it. A name explains a reading; it does not excuse one that disagrees, so a
40 W load on a meter called `EVSE` is still not a car charging.

## What the meter will do

Consumption answers how much the house needs; the grid forecast answers what
the meter does about it. PV comes from the forecast integrations your Energy
dashboard already links to its PV source - the same data it draws as its own
dashed line - and the battery is simulated in between: surplus charges it
until full, deficit discharges it until empty, bounded by the pack's rated
power where that is known.

That model does not know your charge policy, tariff arbitrage or reserves, so
it answers "if the battery simply follows the house", which is what most
sites do most of the time. Without a state of charge or a capacity on the
dashboard no battery is simulated, and the net is reported before it -
`battery_modelled` says which. `hours_with_pv_forecast` says how far the PV
forecast reached; beyond it the hours are treated as sunless, so a two-day PV
forecast leaves the rest of the week reading as pure consumption.

## When something is half-done

Three things leave the integration working and quietly worse, and none is an
error, so each is raised as a repair notification while it is true and
withdrawn the moment it is fixed: a weather entity without an outdoor
temperature sensor or the reverse (the response needs both halves and can
never fit); a PV site with no forecast integration linked on the Energy
dashboard (the grid forecast then predicts a sunless week); and a metered
device whose hardware publishes only energy, never power, which detection
cannot see.

## Diagnostics

**Download diagnostics** on the config entry or any of its devices gives one
file with the configuration, what the Energy dashboard said, every forecast's
fit - history, level, temperature response, each signal's factors, the
nowcast, the horizon - the scoring ledger, and load detection's baselines,
signature library and recent sessions. It is the first thing to attach to a
question about a number.

## Services

`load_insights.refresh` recomputes every forecast now rather than at the top
of the hour - useful straight after changing an input, to see whether it
engaged. `load_insights.reset_detection` forgets every signature and starts
the meter's backfill again, for when a meter changed or a phase was rewired;
names are lost with the signatures.

## Requirements

- Home Assistant with the Energy dashboard configured, at least a grid source.
  Any supported core works for phase 1; the power-sensor fields that phase 2
  (load detection) will use arrived in 2025.12 / 2026.3 / 2026.6.
- Nothing else. No cloud, no extra Python packages.

## Install

HACS only reads GitHub, so the Gitea repository (the source of truth) is
mirrored to `https://github.com/LeoAlioth/Load-Insights`, and every push to
`dev` publishes a tagged pre-release with the zip on both. In HACS add
`https://github.com/LeoAlioth/Load-Insights` as a custom repository
(integration), install it, restart, then Settings -> Devices & services ->
Add integration -> Load Insights.

Every dev build can also announce itself to your Home Assistant instances so
HACS downloads it at once: the release workflow POSTs `{"version", "repository"}`
to the webhooks in the user-level `HA_WEBHOOK_URLS` secret, and one automation
per instance routes on the repository - see Load Juggler's
`dev/HA_AUTO_UPDATE.md`. Restarting is left to you.

## Development

The model lives in `custom_components/load_insights/insights/` and imports no
Home Assistant. Its tests run on bare Python:

```bash
python3 tests/run_all.py
```

Phase 2 adds load detection / classification on the unmetered remainder from
the dashboard's power sensors (per phase where CTs exist), and a test rig for
the Home Assistant layer.
