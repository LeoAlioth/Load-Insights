"""Load Insights - constants."""

DOMAIN = "load_insights"

CONF_NAME = "name"
DEFAULT_NAME = "Home"

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
