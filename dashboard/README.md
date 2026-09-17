# Dashboard

Two ways to get a forecast-vs-actual graph per device. Both draw the same
thing; they differ in when the list of devices is worked out.

| | when the device list is resolved | needs |
|---|---|---|
| `forecast-cards-auto.yaml` | **live**, every time the card renders | auto-entities + Plotly |
| `forecast-cards.jinja` | once, when you run the template | Plotly |

## `forecast-cards-auto.yaml` - the self-updating card

Paste it into a dashboard as a **manual card**. `auto-entities` builds the
list of cards from a template each render, so adding a device to the Energy
dashboard makes a graph appear on its own - nothing to re-run.

`card_param: cards` is the part that matters: it tells auto-entities to fill
a `vertical-stack`'s **cards** rather than an entity list, so each generated
item is a whole card rather than a row.

The template writes the cards as literal YAML, which is auto-entities' own
documented style. It is not decoration: Jinja's `tojson` filter is
HTML-safe, so it escapes `>` and `'` - and the plotting functions are full of
`=>`, which comes out the other side as `=\u003e` and no longer parses.


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
