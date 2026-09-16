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
| **Consumption forecast** | expected average power over the coming hour, W | `detailedForecast`: 7 days hourly kWh (the shape the PV forecast integrations use), today/tomorrow kWh, level correction, weeks of data |
| **Consumption today** | actual for completed hours + forecast for the rest, kWh | |
| **Consumption tomorrow** | kWh | |
| **Unmetered consumption forecast** | as the first, for consumption minus every individually metered device | `subtracted_devices`, `remainder_complete_since` |
| **\<device\> forecast**, one per individually metered device | as the first, for that device alone | `statistic_id`; **disabled by default** - enable the ones you want from the device page |

Consumption is `grid in - grid out + PV + battery out - battery in`, the
dashboard's own signs. A device listed as included in another listed device
(`included_in_stat`) is not subtracted twice.

A device added to the Energy dashboard later gets its sensor after a reload
of the integration.

The forecast is recomputed on the quarter hours from twelve weeks of hourly
statistics. The model is a recency-weighted hour-of-week profile (weight
halves every three weeks) with a damped, clamped correction from the last 24
hours. Nothing learned in the machine sense; every number is explainable.

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
