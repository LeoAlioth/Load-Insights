# Dashboard

`forecast-cards.jinja` writes one forecast-vs-actual graph per device, for
every device on your Energy dashboard. Run it again when that list changes.

### Why it is a generator and not a self-updating card

A card that rebuilds its own list would be nicer, and `auto-entities` looks
like the tool for it - it is what the "devices with low batteries" cards are
built from. It cannot do this one. auto-entities produces a list of ENTITY
configs; `card_param: cards` works in its own example only because a grid
turns `{entity: x}` into a default entity card. Hand it a full card config -
even `{'type': 'markdown', 'content': 'hello'}` - and the item is dropped,
because it has no `entity`. Tested on a live instance, 2026-09-17.

`config-template-card` can generate cards from a template and would be the
tool to try, at the cost of another custom card.

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

The window is 36 hours back and 36 hours forward, rolling - the card's
window is relative to now, not pinned to midnight.

Requires the [Plotly Graph Card](https://github.com/dbuezas/lovelace-plotly-graph-card)
from HACS. Re-run the template after adding a device to your Energy dashboard.

Device forecast sensors are recognised by carrying a `statistic_id`
attribute; every forecast sensor carries `detailedForecast`, which is what
the template selects on.
