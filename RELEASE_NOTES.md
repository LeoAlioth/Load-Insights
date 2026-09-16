# Release Notes

## 0.1.0

### New Features

- **Consumption forecast from the Energy dashboard**: Load Insights reads the sources and individual devices you have already set up in Home Assistant's Energy dashboard - grid import/export, PV, battery, metered devices - and the recorder's hourly statistics for them, and publishes a consumption forecast: expected power over the coming hour, today's and tomorrow's kWh, and a seven-day hourly series in the same shape the PV forecast integrations use, so one dashboard card can plot both. A fourth sensor forecasts the **unmetered remainder** - consumption minus every individually metered device - which is what nothing else on the site measures. Recomputed every quarter hour from twelve weeks of history; nothing to configure beyond a name. A device added to the dashboard recently does not shorten the remainder's history: before its first statistic it counts as zero, since its energy was unmetered - and therefore part of the remainder - until then; the sensor's `remainder_complete_since` attribute says from when every device is covered.
