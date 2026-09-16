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
| **Consumption today** | actual for completed hours + forecast for the rest, kWh | |
| **Consumption tomorrow** | kWh | |
| **Unmetered consumption forecast** | as the first, for consumption minus every individually metered device | `subtracted_devices`, `remainder_complete_since` |
| **Forecast error / bias, day ahead** and **hour ahead** | trailing 7-day mean absolute error and signed mean error of the site forecast, W | per-lead table (hour, day and week ahead), `band_coverage_day_ahead` (share of actuals inside p10-p90; honest is about 0.8), the last 48 scored hours |
| **Yesterday's day-ahead error** | actual minus the "tomorrow" total the forecast showed at noon the day before, kWh | |
| **Unmetered forecast error / bias, day ahead** | the same for the remainder | |
| **\<device\> forecast**, one per individually metered device | as the first, for that device alone | `statistic_id`; **disabled by default** - enable the ones you want from the device page |

Consumption is `grid in - grid out + PV + battery out - battery in`, the
dashboard's own signs. A device listed as included in another listed device
(`included_in_stat`) is not subtracted twice.

A device added to the Energy dashboard later gets its sensor after a reload
of the integration.

### Scoring

Every hour the forecast's value for the hour one hour, one day and one week
(the horizon's last row, 167 h) ahead is remembered; when that hour arrives, prediction and actual are paired
and the errors kept for 30 days in `.storage`. The score sensors report the
trailing week. Device scores are the `score` attribute on each device
forecast sensor. Scores need time to fill: the hour-ahead figures after a
day, the day-ahead ones after two, the week-ahead table after eight.

### Actual against forecast on one chart

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
Public holidays are scored as Sundays, in the
history and in the week ahead, using the `holidays` library that Home
Assistant's Workday integration installs (no Workday, no holidays - the
sensor's `holidays_modelled` attribute says which). Nothing learned in the
machine sense; every number is explainable.

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
