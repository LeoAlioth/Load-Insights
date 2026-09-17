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
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

Sample = Tuple[datetime, float]


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
          soc_min: float = 0.0, soc_max: float = 100.0) -> GridForecast:
    """``consumption`` is the hourly forecast; ``pv`` maps hour key to forecast
    PV kWh for that hour. ``soc`` is the pack's state of charge NOW."""
    pv = pv or {}
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
        net_before = c - p
        battery = 0.0
        if model_battery:
            if net_before < 0:                       # surplus: charge
                room = max(0.0, hi - level)
                take = min(-net_before, room)
                if max_charge_w is not None:
                    take = min(take, max_charge_w / 1000.0)
                level += take
                battery = -take
            else:                                    # deficit: discharge
                have = max(0.0, level - lo)
                give = min(net_before, have)
                if max_discharge_w is not None:
                    give = min(give, max_discharge_w / 1000.0)
                level -= give
                battery = give
        rows.append(GridHour(
            when=t, consumption_kwh=c, pv_kwh=p,
            net_before_battery_kwh=net_before, battery_kwh=battery,
            net_kwh=net_before - battery,
            soc=(100.0 * level / capacity_kwh) if model_battery else None,
        ))
    return GridForecast(hours=tuple(rows), battery_modelled=model_battery, pv_hours=seen_pv)
