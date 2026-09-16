"""Load Insights - constants."""

DOMAIN = "load_insights"

CONF_NAME = "name"
DEFAULT_NAME = "Home"

# How much history the profile is fitted on. Twelve weeks of hourly rows is
# ~2 000 rows per statistic - cheap to refetch in full every refresh, so
# phase 1 does exactly that and nothing incremental.
HISTORY_WEEKS = 12

# The forecast is recomputed on the quarter hours - the grid the tariff blocks
# live on - and a little after the mark so the recorder's hourly and 5-minute
# statistics for the period just closed are already compiled.
REFRESH_MINUTES = (0, 15, 30, 45)
REFRESH_SECOND = 30

HORIZON_HOURS = 168
