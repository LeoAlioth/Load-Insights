"""Which setup fields sit under which heading (see sections.py).

Plain data and no Home Assistant import, so the pure tests can check it against
the translations: each group is (section key, field keys, collapsed), and the
section key is its translation key.
"""
_PHASES = ("a", "b", "c")


def _abc(prefix: str) -> tuple:
    return tuple(f"{prefix}_{p}" for p in _PHASES)


_PLAN = [
    ("scope", ("plan_days", "plan_production_scale", "plan_consumption_scale"), False),
    ("battery", ("plan_full_soc", "plan_full_soc_entity", "plan_minimum_floor"), False),
] + [
    (sensor, tuple(f"plan_{sensor}_{side}_{what}" for side in ("consumption", "production")
                   for what in ("full_hours", "derate")), True)
    for sensor in ("target", "minimum", "excess")
] + [("devices", ("plan_spread_evenly",), False)]

_GRID = [
    ("meter", ("grid_device", "source_kind"), False),
    ("power", _abc("grid_power"), False),
    ("voltage", _abc("grid_voltage"), False),
    ("current", _abc("grid_current"), False),
    ("pf", _abc("grid_pf"), False),
]

_INVERTERS = [
    ("inverter", ("device", "topology"), False),
    ("output", ("power",) + _abc("power"), False),
    ("grid_side", ("in_power",) + _abc("in_power"), False),
    ("voltage", _abc("voltage"), False),
    ("current", _abc("current"), False),
    ("pf", _abc("pf"), False),
]

_HEAT = [
    ("device", ("name", "source", "kind"), False),
    ("goal", ("target", "target_entity", "activity_entity"), False),
    ("removal", ("remove",), False),
]

_INPUTS = [
    ("weather", ("weather_entity", "outdoor_temperature_entity"), False),
    ("other", ("calendar_entities", "input_entities", "battery_capacity_entity"), False),
    ("forecast", ("forecast_exclude",), False),
]

_DETECTION = [
    ("waiting", ("meter_wait_s", "horizon_skip"), False),
    ("meters", ("single_device", "ignore_meters"), False),
    ("naming", ("autoname", "autoname_dashboard"), False),
]

OPTIONS = {"plan": _PLAN, "grid": _GRID, "inverters": _INVERTERS, "heat": _HEAT,
           "inputs": _INPUTS, "detection": _DETECTION}
CONFIG = {"user": _INPUTS}
