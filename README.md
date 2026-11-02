# Load Insights

Consumption forecasts and appliance detection for Home Assistant, from what the **Energy dashboard already knows**. It
finds the individual loads in your whole-house meter readings (no training phase, no labelling), forecasts what the site
will use, and plans the battery around it. Companion to [Load Juggler](https://github.com/LeoAlioth/Load-Juggler), which
switches loads; this one understands them. Neither needs the other.

## What it gives you

| | |
|---|---|
| **Forecasts** | consumption, grid (what the meter will do), battery SOC, base load, and one per individually metered device - each hourly for a week with a likely range (p10-p90), the last 48 h as they happened, and a score of how right it has been |
| **Battery plan** | SOC target, SOC minimum and the day's excess, for Load Juggler's thresholds or your own automations |
| **Detected loads** | appliances found in the meter's per-phase readings, described in words and with a guess at what each is; you name one once and it gets a *running* and a *power* sensor |
| **Auto-naming** | confident fridges and heating elements are named for you and added to the Energy dashboard under their meter |
| **Weather, calendars, inputs** | link a temperature, calendars (away, guests) or any entity and the forecast uses them where they really explain something |

Everything is explainable: a recency-weighted hour-of-week profile with a damped correction, nothing trained.

## Dashboard cards

Both come with the integration; nothing to add as a resource.

- `type: custom:load-insights-forecast-card` - one forecast at a time from a dropdown: actual bars, forecast line and likely range. `include` or `exclude` limits the list.
- `type: custom:load-insights-card` - the loads waiting for a name, or what is running now, with a name field on each row.

## Install

Needs Home Assistant 2026.6 or later with the Energy dashboard set up (at least a grid source). No cloud, no extra packages.

In HACS add `https://github.com/LeoAlioth/Load-Insights` as a custom repository (integration), install, restart, then
**Settings > Devices & services > Add integration > Load Insights**. Point the *Load detection* page at the meter's per-phase
power readings to start finding loads.

## With Load Juggler

They meet only through ordinary entities: the SOC planning sensors and the sun's production start and end can feed Load
Juggler's battery thresholds, pacing and early starts (its *Battery and forecast policy* page).

## More

- [How it works](docs/how-it-works.md) - the forecast model, load detection, what a load might be, the grid forecast, repairs, diagnostics, services
- [Release notes](RELEASE_NOTES.md)

## Development

The model lives in `custom_components/load_insights/insights/` and imports no Home Assistant. Tests run on bare Python:

```bash
python3 tests/run_all.py
```

Load detection is replayed over exported history without Home Assistant - see `AGENTS.md`.

## Support

If it is useful to you and you want to say thanks: [ko-fi.com/LeoAlioth](https://ko-fi.com/LeoAlioth)

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/LeoAlioth)
