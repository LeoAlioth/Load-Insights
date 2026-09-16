"""The pure tier: no Home Assistant imports anywhere under this package.

Everything here takes plain data - the energy dashboard's preference dicts,
lists of (hour, kWh) samples - and returns dataclasses, so it runs and is
tested on a machine with nothing but Python installed. The HA layer above it
(coordinator, sensors, config flow) only fetches, converts and publishes.
"""
