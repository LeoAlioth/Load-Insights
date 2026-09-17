# Dashboard

`forecast-cards-live.yaml` draws one forecast-vs-actual graph per device,
for every device on your Energy dashboard, and finds them again on every
render - so a device added to the dashboard just appears.

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
