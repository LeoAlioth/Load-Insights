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
# device statistic id -> the device's own state sensor (tank temperature,
# tank level...), for the nowcast of its next hours
CONF_DEVICE_STATE_SENSORS = "device_state_sensors"

# Detection: the meter's raw per-phase readings. Active power is the one
# that matters most; PF (or reactive power), current and voltage refine the
# signatures where the meter publishes them. All optional, per phase.
CONF_DETECTION = "detection"
DETECTION_KINDS = ("power", "pf", "current", "voltage")
DETECTION_INTERVAL_MINUTES = 5
DETECTION_BACKFILL_DAYS = 10
DETECTION_SLICE_HOURS = 6

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
