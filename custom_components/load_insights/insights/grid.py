"""What the METER will do, from what the house will use and the sun will give.

Consumption alone answers "how much will we need". The number decisions turn
on is the meter's: whether the evening imports, whether noon exports, whether
the battery carries the night. That is consumption minus PV, and then the
battery in between.

The battery model is deliberately the simplest one that is honest: surplus
charges it until full, deficit discharges it until empty, both bounded by the
pack's rated power where that is known. It does not know the site's charge
policy, tariff arbitrage, or reserves - so it answers "if the battery simply
follows the house", which is what most sites do most of the time. Where the
pack's capacity or state of charge is unknown, no battery is simulated and
the net is reported before it.

Powers are watts; energies are kWh per hour, which is kW.

Two facts about the site change the arithmetic rather than the wording.

WHERE THE BATTERY SITS. Series, or DC-coupled: the arrays land on the DC
bus in front of the inverter, so charging is DC to DC and nearly free, and
everything the house draws pays the inverter conversion once - whether it
came from the pack or off the arrays a second earlier. Parallel, or
AC-coupled: the arrays are already AC and feed the house at no cost, and
only the surplus pays, twice, in and back out. On a site that runs its
whole night off the pack that is the dominant term, and the model used to
have no losses at all (Anze, 2026-09-18, about Kozolec).

WHAT IS AT THE AC INPUT. The residual after the battery is grid flow only
where there is a grid. Off grid a deficit is a load that goes UNSERVED and
a surplus is generation the arrays CURTAIL; on a generator the deficit is
fuel someone has to burn. Same number, three different meanings, and
Kozolec was being shown a weekly import figure for a site with no utility
connection.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

Sample = Tuple[datetime, float]

# Round-trip efficiencies. Deliberately plain single figures: a real pack's
# curve depends on rate, temperature and state of charge, and pretending to
# that precision inside an hourly forecast would be false rigour.
DC_CHARGE = 0.98        # array to pack on a shared DC bus
DC_DISCHARGE = 0.94     # pack out through the inverter
AC_CHARGE = 0.95        # AC bus into the pack, one conversion in
AC_DISCHARGE = 0.94     # and one back out
INVERTER = 0.94         # DC bus to AC loads, the series path's standing cost

TOPOLOGY_SERIES = "series"
TOPOLOGY_PARALLEL = "parallel"


@dataclass(frozen=True)
class GridHour:
    when: datetime
    consumption_kwh: float
    pv_kwh: float
    net_before_battery_kwh: float      # + import, - export
    battery_kwh: float                 # + discharging to the house, - charging
    net_kwh: float                     # + import, - export, after the battery
    soc: Optional[float]               # % at the END of the hour


@dataclass(frozen=True)
class GridForecast:
    hours: tuple = ()
    battery_modelled: bool = False
    pv_hours: int = 0                  # horizon hours with a PV forecast

    def slice_day(self, day, field: str) -> float:
        return sum(getattr(h, field) for h in self.hours if h.when.date() == day)

    @property
    def import_kwh(self) -> float:
        return sum(max(0.0, h.net_kwh) for h in self.hours)

    @property
    def export_kwh(self) -> float:
        return sum(max(0.0, -h.net_kwh) for h in self.hours)


def build(consumption: Sequence[Sample], pv: Optional[Dict[float, float]] = None,
          soc: Optional[float] = None, capacity_kwh: Optional[float] = None,
          max_charge_w: Optional[float] = None, max_discharge_w: Optional[float] = None,
          soc_min: float = 0.0, soc_max: float = 100.0,
          topology: str = TOPOLOGY_PARALLEL) -> GridForecast:
    """``consumption`` is the hourly forecast; ``pv`` maps hour key to forecast
    PV kWh for that hour. ``soc`` is the pack's state of charge NOW.

    ``topology`` says where the pack sits relative to the conversion, which
    is where the losses land - see the module docstring."""
    pv = pv or {}
    # Everything below is in AC-DELIVERED terms, so the topology shows up
    # only as efficiencies. On the series path the arrays are DC and reach
    # the house through the inverter, so a kWh of array is worth INVERTER
    # kWh at the socket - and a kWh of AC-equivalent surplus never paid that
    # conversion, so it stores MORE than one kWh in the pack. Round trip
    # works out at 98% DC-coupled against 89% AC-coupled, which is right:
    # the DC path avoids a conversion the AC path cannot.
    series = topology == TOPOLOGY_SERIES
    charge_eff = (DC_CHARGE / INVERTER) if series else AC_CHARGE
    discharge_eff = DC_DISCHARGE if series else AC_DISCHARGE
    model_battery = soc is not None and capacity_kwh is not None and capacity_kwh > 0
    level = (soc / 100.0) * capacity_kwh if model_battery else 0.0
    lo = (soc_min / 100.0) * capacity_kwh if model_battery else 0.0
    hi = (soc_max / 100.0) * capacity_kwh if model_battery else 0.0

    rows: List[GridHour] = []
    seen_pv = 0
    for t, c in consumption:
        p = pv.get(t.timestamp())
        if p is not None:
            seen_pv += 1
        p = p or 0.0
        if series:
            p = p * INVERTER            # DC array, as it arrives at the loads
        net_before = c - p
        battery = 0.0
        if model_battery:
            if net_before < 0:                       # surplus: charge
                offered = -net_before
                if max_charge_w is not None:
                    offered = min(offered, max_charge_w / 1000.0)
                # what reaches the pack is less than what is offered to it
                stored = min(offered * charge_eff, max(0.0, hi - level))
                level += stored
                battery = -(stored / charge_eff if charge_eff else 0.0)
            else:                                    # deficit: discharge
                wanted = net_before
                if max_discharge_w is not None:
                    wanted = min(wanted, max_discharge_w / 1000.0)
                # and more leaves the pack than arrives at the house
                drawn = min(wanted / discharge_eff if discharge_eff else 0.0,
                            max(0.0, level - lo))
                level -= drawn
                battery = drawn * discharge_eff
        rows.append(GridHour(
            when=t, consumption_kwh=c, pv_kwh=p,
            net_before_battery_kwh=net_before, battery_kwh=battery,
            net_kwh=net_before - battery,
            soc=(100.0 * level / capacity_kwh) if model_battery else None,
        ))
    return GridForecast(hours=tuple(rows), battery_modelled=model_battery, pv_hours=seen_pv)
