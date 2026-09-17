# Dashboard

## `forecast-cards.jinja` - forecast against actual, one graph per thing

Paste the template into **Developer Tools -> Template**; copy what it prints
into a dashboard's **Raw configuration editor**. It finds every Load Insights
forecast sensor on the instance - the site total, the unmetered remainder,
and one per device on the Energy dashboard - and writes one graph for each:

* **Actual** (bars): the last 48 hours as they happened, from the sensor's
  `history` attribute.
* **Forecast** (dotted): the coming week, from `detailedForecast`, clipped by
  the window.
* **Likely range** (shaded): `kwh_p10` to `kwh_p90`, the spread of that hour
  across the weeks behind it.

The window is the last 24 hours and the next 48 - yesterday, today and
tomorrow, rolling rather than pinned to midnight, because the card's window is
relative to now.

Requires the [Plotly Graph Card](https://github.com/dbuezas/lovelace-plotly-graph-card)
from HACS. Re-run the template after adding a device to your Energy dashboard.

Device forecast sensors are recognised by carrying a `statistic_id`
attribute; every forecast sensor carries `detailedForecast`, which is what
the template selects on.
