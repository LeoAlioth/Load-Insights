"""Load Insights - constants."""

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
CONF_LAYOUT = "layout"
LAYOUT_AUTO = "auto"
LAYOUT_PARALLEL = "parallel"          # load = inverter output + grid
LAYOUT_SERIES = "series"              # load = inverter output alone
LAYOUTS = (LAYOUT_AUTO, LAYOUT_PARALLEL, LAYOUT_SERIES)
# What was stored before the word was borrowed from Load Juggler, which
# describes the same two wirings and had the better name for this one.
LAYOUT_ALIASES = {"separate": LAYOUT_SERIES}
# Meters BELOW the main one are not configured: the Energy dashboard already
# lists every individually metered device and, through included_in_stat, how
# they nest. Load Insights resolves each one to its Home Assistant device and
# discovers that device's per-phase readings, so a load seen by both the main
# meter and a device's own meter is located - and named - with nothing typed.
DETECTION_KINDS = ("power", "pf", "current", "voltage")
DETECTION_INTERVAL_MINUTES = 5
DETECTION_BACKFILL_DAYS = 10
DETECTION_SLICE_HOURS = 6
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
