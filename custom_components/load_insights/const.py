"""Load Insights - constants."""

# The pure layer owns the words for what it measures, so they have one
# definition; the rest of the integration keeps importing from here.
from .insights.detect import (  # noqa: F401  - one definition, re-exported
    SOURCE_GENERATOR,
    SOURCE_NONE,
    SOURCE_UTILITY,
)

DOMAIN = "load_insights"

CONF_NAME = "name"
DEFAULT_NAME = "Home"

# Explanatory inputs. The weather entity supplies the horizon's temperatures,
# the sensor the history's - the same quantity, so a response fitted on one
# can be applied to the other. Either alone is useless for a week-ahead
# forecast, which is why they are a pair.
CONF_WEATHER_ENTITY = "weather_entity"
CONF_OUTDOOR_TEMPERATURE_ENTITY = "outdoor_temperature_entity"
# Calendars are linked without saying what they mean; the fit decides.
CONF_CALENDAR_ENTITIES = "calendar_entities"
# Any other entity worth trying as an explanation - a tariff block, a price,
# irradiance, an occupancy sensor. Fitted like a calendar and kept only if it
# explains something.
CONF_INPUT_ENTITIES = "input_entities"
# device statistic id -> the device's own state sensor (tank temperature,
# tank level...), for the nowcast of its next hours
CONF_DEVICE_STATE_SENSORS = "device_state_sensors"

# Detection: the meter's raw per-phase readings. Active power is the one
# that matters most; PF (or reactive power), current and voltage refine the
# signatures where the meter publishes them. All optional, per phase.
CONF_DETECTION = "detection"            # the main meter's fields, flat: power_a, pf_b, ...
# Inside that same dict, the GRID connection's per-phase power and how it
# relates to the load reading. Two readings of one site are not
# interchangeable: grid-tied, the meter carries the house MINUS what the
# inverter makes, so the load is their sum; behind a transfer switch or off
# grid, everything reaches the loads through the inverter and adding the
# grid would count the pass-through twice.
CONF_GRID_PREFIX = "grid_power_"
CONF_GRID_DEVICE = "grid_device"
# Each ROLE carries the whole electrical set, not just watts. Voltage and
# current belong to the meter that publishes them, and pairing one meter's
# amps with another meter's watts is not a power factor - it is a
# subtraction artefact. Home did exactly that: the load reading was a
# template of house consumption while the volts and amps came off the grid
# meter, and the reactive power derived from the pair turned 121 signatures
# into 199 in a single day (Anze, 2026-09-18).
ROLE_PREFIX = {"load": "", "grid": "grid_"}
# Inverters, as a LIST from the start: one site already has two - a SolarEdge
# cabled to a Deye hybrid's load port - and retrofitting a list onto a
# single-inverter config would be a migration across every site.
CONF_INVERTERS = "inverters"
CONF_INV_DEVICE = "device"
CONF_INV_ATTACH = "attach"          # which node its output feeds
CONF_INV_TOPOLOGY = "topology"
# An inverter's GRID-side reading, stored beside its output under this
# prefix. What it contributes to the house is output minus input, so a
# hybrid does not hand back the grid power it merely passed along; a PV
# string inverter has no input and contributes its output whole.
CONF_INV_INPUT_PREFIX = "in_"
ATTACH_BUS = "bus"                  # the same bus as the grid/main meter
ATTACH_LOAD_PORT = "load_port"      # behind another inverter's output
CONF_LAYOUT = "layout"
LAYOUT_AUTO = "auto"
LAYOUT_PARALLEL = "parallel"          # load = inverter output + grid
LAYOUT_SERIES = "series"              # load = inverter output alone
LAYOUTS = (LAYOUT_AUTO, LAYOUT_PARALLEL, LAYOUT_SERIES)
# What was stored before the word was borrowed from Load Juggler, which
# describes the same two wirings and had the better name for this one.
LAYOUT_ALIASES = {"separate": LAYOUT_SERIES}
# WHAT is connected there, which the topology does not say. An AC input is
# an AC input whether the utility or a generator is behind it, and Kozolec
# has the second: an off-grid site whose MultiPlus input port feeds from a
# generator, idle - and so reading zero - almost all the time (Anze,
# 2026-09-18). It changes nothing electrically and everything about what a
# shortfall MEANS: energy bought at a tariff, a generator someone has to
# start, or a load that simply goes unserved.
# Auto is the default and the honest answer nearly always: only a utility
# absorbs a surplus, and a generator is off far more than it is on, so the
# reading itself says which is there. The override exists for the one case
# the data cannot settle - a generator that has not run inside the window
# looks exactly like nothing at all.
CONF_SOURCE_KIND = "source_kind"
SOURCE_AUTO = "auto"
SOURCE_KINDS = (SOURCE_AUTO, SOURCE_UTILITY, SOURCE_GENERATOR, SOURCE_NONE)
DEFAULT_SOURCE_KIND = SOURCE_AUTO
# Meters BELOW the main one are not configured: the Energy dashboard already
# lists every individually metered device and, through included_in_stat, how
# they nest. Load Insights resolves each one to its Home Assistant device and
# discovers that device's per-phase readings, so a load seen by both the main
# meter and a device's own meter is located - and named - with nothing typed.
DETECTION_KINDS = ("power", "pf", "current", "voltage")
# How often the recorder is re-read. One minute by default since the pass
# itself costs well under a millisecond and its overheads no longer scale
# with it - one query rather than one per entity, and the state written
# hourly rather than every pass (Anze, 2026-09-18). Configurable because a
# large site, or one on slow storage, may still want it slower; it buys
# latency, not accuracy, since the same recorded edges are reconstructed
# either way.
CONF_DETECTION_INTERVAL = "interval_minutes"
DETECTION_INTERVAL_MINUTES = 1
DETECTION_INTERVAL_CHOICES = (1, 2, 5, 10, 15, 30)
DETECTION_BACKFILL_DAYS = 10
DETECTION_SLICE_HOURS = 6
# How long the detector's state may sit in memory unwritten. The snapshot is
# the whole signature library - well over a hundred kilobytes - and writing
# it every pass is what makes the polling interval expensive rather than the
# detection itself, which costs under a millisecond per pass. An hour caps
# what an ungraceful shutdown can lose; a clean one always writes.
SAVE_MAX_INTERVAL_S = 3600.0
# How far behind the detector may be before the naming page will show its
# library. A backfill part way through holds whatever happened in the first
# few days, and rows that change under the reader are worse than no rows.
# Past this it shows them anyway: a detector wedged days back should still
# offer what it has rather than nothing at all.
NAMING_MAX_STALE_S = 3600.0
# How sure the detector must be that a signature is a real repeating load
# before it is offered for naming. A house makes far more shapes than it has
# appliances, and a list of two hundred is a list nobody reads (Anze,
# 2026-09-18). Evidence is the score: how often it has been seen and how
# tightly its power and duration repeat.
CONF_MIN_EVIDENCE = "min_evidence"
# 0.7, not 0.5: evidence is 0.5*seen + 0.3*tight_power + 0.2*tight_duration,
# so anything seen five times already scores 0.5 and a bar there filters
# nothing at all - 153 of home's 153 namable signatures cleared it. At 0.7 it
# asks for a load that also REPEATS tightly, which is 99 at home and 13 at
# Kozolec (measured over ten days, 2026-09-18).
DEFAULT_MIN_EVIDENCE = 0.7
EVIDENCE_CHOICES = (0.3, 0.5, 0.6, 0.7, 0.8, 0.9)
# ...but a threshold that hides everything is worse than one set too low, so
# when too few clear it, the best of the rest come along anyway. That is the
# "lower it slowly if we are not getting good hits" without any state to
# decay: the bar is what it is, and the list simply never runs dry.
NAMING_MIN_ROWS = 5
# The smallest change to call a step, as a floor under what each phase
# measures for itself. Anze asked whether it was configurable - it was not,
# and at 100 W it was the binding constraint on both sites, which is why
# Kozolec has two fridges and detected neither.
CONF_MIN_STEP_W = "min_step_w"
STEP_CHOICES = (5, 10, 20, 40, 80)
# Bumped whenever a signature is named, so the entry reloads and the named
# load's entities appear. The names themselves live with the detector.
CONF_SIGNATURE_REVISION = "signature_revision"

SERVICE_REFRESH = "refresh"
SERVICE_RESET_DETECTION = "reset_detection"

# How much history the profile is fitted on. Twelve weeks of hourly rows is
# ~2 000 rows per statistic - cheap to refetch in full every refresh, so
# phase 1 does exactly that and nothing incremental.
HISTORY_WEEKS = 12

# The model is hourly - it can only change when a completed hour's statistic
# arrives - so the forecast is recomputed once an hour, a little after the
# mark so the recorder has compiled that hour. It was every quarter hour;
# three of those four refreshes carried nothing new (Anže, 2026-09-16).
REFRESH_MINUTES = (0,)
REFRESH_SECOND = 30

HORIZON_HOURS = 168
