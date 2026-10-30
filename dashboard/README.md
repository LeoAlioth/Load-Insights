# Dashboard

`forecast-cards-live.yaml` draws one forecast-vs-actual graph per device,
for every device on your Energy dashboard, and finds them again on every
render - so a device added to the dashboard just appears. How to paste it,
and the two HACS cards it needs, is in the file's own header.

## Running now

The Detected loads sensor carries the loads on right now as a ready-made
Markdown list in its `running_markdown` attribute: named ones first, then
unnamed, each by watts, ten at most. Paste it as a Markdown card:

```yaml
type: markdown
title: Running now
content: >-
  {{ state_attr('sensor.31b_consumption_forecast_detected_loads', 'running_markdown') }}
```

The entity id is the one your Detected loads sensor has (at Kozolec
`sensor.kozolec_pv_kozolec_insights_detected_loads`).

An unnamed load the naming page offers is a link: it opens the integration's
entry, highlighted (Home Assistant cannot link into the options page itself).
From there it is Configure -> Name detected loads -> the group named at the
end of the row (none means "main") -> the row ending in the same `#id`.

## Not auto-entities

`auto-entities` is the obvious candidate and cannot do this: it produces a
list of ENTITY configs, and its own `card_param: cards` example works only
because a grid turns `{entity: x}` into a default entity card. Hand it a whole
card config - even `{'type': 'markdown', 'content': 'hello'}` - and the item
is dropped for having no `entity`. Tested on a live instance, 2026-09-17.
