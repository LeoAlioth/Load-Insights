# Dashboard

`forecast-cards-live.yaml` draws one forecast-vs-actual graph per device,
for every device on your Energy dashboard, and finds them again on every
render - so a device added to the dashboard just appears. How to paste it,
and the two HACS cards it needs, is in the file's own header.

## Not auto-entities

`auto-entities` is the obvious candidate and cannot do this: it produces a
list of ENTITY configs, and its own `card_param: cards` example works only
because a grid turns `{entity: x}` into a default entity card. Hand it a whole
card config - even `{'type': 'markdown', 'content': 'hello'}` - and the item
is dropped for having no `entity`. Tested on a live instance, 2026-09-17.
