# Dashboard

Both files draw the same thing: one forecast-vs-actual graph per device, for
every device on your Energy dashboard.

| | when the list of devices is worked out | needs |
|---|---|---|
| `forecast-cards-live.yaml` | **every render** - a device added to the dashboard just appears | config-template-card + Plotly |
| `forecast-cards.jinja` | once, when you run the template | Plotly |

## `forecast-cards-live.yaml` - the self-updating card

Paste it into a dashboard as a **manual card**. Its `cards:` is a single
JavaScript expression that finds every sensor carrying a `detailedForecast`
attribute - exactly the Load Insights forecast sensors - and returns a card
for each. `entities:` is only what config-template-card watches to know when
to redraw; any one forecast sensor will do, since they all refresh together.

### Not auto-entities

`auto-entities` is the obvious candidate and cannot do this: it produces a
list of ENTITY configs, and its own `card_param: cards` example works only
because a grid turns `{entity: x}` into a default entity card. Hand it a whole
card config - even `{'type': 'markdown', 'content': 'hello'}` - and the item
is dropped for having no `entity`. Tested on a live instance, 2026-09-17.

## `forecast-cards.jinja` - forecast against actual, one graph per thing

Paste the template into **Developer Tools -> Template**; copy what it prints
into a dashboard's **Raw configuration editor**. It finds every Load Insights
forecast sensor on the instance - the site total, the unmetered remainder,
and one per device on the Energy dashboard - and writes one graph for each:

* **Actual** (bars): the last 48 hours as they happened, from the sensor's
  `history` attribute.
* **Forecast** (dotted): behind now, the sensor's own recorded state - its
  state IS the forecast for the coming hour, so the recorder already holds
  the forecast's history (in W, hence the /1000). Ahead of now,
  `detailedForecast`. One continuous line.
* **Likely range** (shaded): `kwh_p10` to `kwh_p90`, the spread of that hour
  across the weeks behind it. Future only - there is no spread to show for an
  hour that has already happened.

The `history` attribute also carries a `predicted` value per hour: what was
forecast for it a DAY ahead, from the scoring ledger. That is a harder test
than the recorded state (which is an hour ahead) and takes about two days
from a fresh install to fill, so the cards do not use it - but it is there
for an accuracy chart.

The window is 36 hours back and 36 hours forward, rolling - the card's
window is relative to now, not pinned to midnight.

Requires the [Plotly Graph Card](https://github.com/dbuezas/lovelace-plotly-graph-card)
from HACS. Re-run the template after adding a device to your Energy dashboard.

Device forecast sensors are recognised by carrying a `statistic_id`
attribute; every forecast sensor carries `detailedForecast`, which is what
the template selects on.
