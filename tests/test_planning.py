"""SOC planning: the weighted hourly balance and its three readings."""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

PL = load("insights.planning")
TZ = ZoneInfo("Europe/Ljubljana")
NOW = datetime(2026, 10, 17, 10, 20, tzinfo=TZ)
KEYS = PL.hour_keys(NOW, 168)
FLAT = PL.Spec(PL.Side(1e9, 0), PL.Side(1e9, 0))      # no derate


def day_sun(peak):
    """Production by hour: a triangle from 07:00 to 17:00, ``peak`` kWh at noon."""
    out = {}
    for k in KEYS:
        h = datetime.fromtimestamp(k, TZ).hour
        out[k] = max(0.0, peak * (1 - abs(h - 12) / 5)) if 7 <= h <= 17 else 0.0
    return out


def test_the_weight_is_full_then_falls_per_hour_and_stops_at_zero():
    s = PL.Side(24, 0.9)
    assert PL.weight(0, s) == 1.0 and PL.weight(24, s) == 1.0
    assert abs(PL.weight(48, s) - (1 - 0.009 * 24)) < 1e-9
    assert PL.weight(24 + 112, s) == 0.0


def test_both_sides_are_weighted_each_with_its_own_numbers():
    use = {k: 1.0 for k in KEYS}
    prod = {k: 2.0 for k in KEYS}
    spec = PL.Spec(PL.Side(0, 50), PL.Side(1e9, 0))        # use halves per hour from hour 0
    b = PL.balance(NOW, 6, prod, use, spec, production_scale=0.5, consumption_scale=2.0)
    assert [round(v, 6) for v in b.net[:4]] == [1 - 2.0 * 1.0, 1 - 2.0 * 0.5, 1.0, 1.0]


def test_a_missing_hour_gives_none_so_the_caller_keeps_its_last_value():
    use = {k: 1.0 for k in KEYS}
    prod = {k: 1.0 for k in KEYS[:60]}                     # forecast ends 60 h out
    assert PL.balance(NOW, 120, prod, use, FLAT) is None
    assert PL.balance(NOW, 40, prod, use, FLAT) is not None
    gap = {k: v for k, v in prod.items() if k != KEYS[5]}  # a hole inside its span is a zero (night) hour
    assert PL.balance(NOW, 40, gap, use, FLAT).net[5] == -1.0
    assert PL.balance(NOW, 40, prod, {k: v for k, v in use.items() if k != KEYS[5]}, FLAT) is None


def test_target_is_full_less_the_peak_and_minimum_is_the_floor_plus_the_dip():
    use = {k: 0.5 for k in KEYS}
    cap = 50.0
    strong = PL.balance(NOW, 120, day_sun(3.0), use, FLAT)           # 15 kWh of sun a day against 12 of use
    t, ta = PL.target(strong, 100.0, cap, TZ)
    assert t == round(100 - ta["peak_surplus_kwh"] / cap * 100) and ta["peak_surplus_kwh"] > 0 and ta["peak_at"]
    weak = PL.balance(NOW, 120, day_sun(0.5), use, FLAT)             # 2.5 kWh a day: the pack only runs down
    m, ma = PL.minimum(weak, 10.0, cap, TZ)
    assert m == round(10 + ma["dip_kwh"] / cap * 100) and ma["dip_kwh"] > 0 and ma["dip_at"]
    # nothing ever above now: the target is full
    t3, a3 = PL.target(weak, 90.0, cap, TZ)
    assert t3 == 90 and a3["peak_at"] is None
    # raw: a surplus bigger than the pack takes the target below zero
    assert PL.target(PL.balance(NOW, 120, day_sun(40.0), use, FLAT), 90.0, 20.0, TZ)[0] < 0


def test_excess_is_each_days_peak_less_what_the_pack_still_takes():
    use = {k: 0.0 for k in KEYS}
    sun = day_sun(4.0)
    bal = PL.balance(NOW, PL.hours_until_day(NOW, 3), sun, use, FLAT, under_way=True)
    state, att = PL.excess(bal, 3, 10.0, TZ)
    day = lambda n: sum(v for k, v in sun.items() if k >= NOW.replace(minute=0).timestamp()  # noqa: E731
                        and datetime.fromtimestamp(k, TZ).date() == NOW.date() + timedelta(days=n))
    under_way = sun[NOW.replace(minute=0).timestamp()] * (1 - 40 / 60)     # 10:00's hour is 40 minutes from over
    assert len(att["days"]) == 3 and state == att["excess_kwh"][0]
    assert abs(att["excess_kwh"][0] - (day(0) - under_way - 10.0)) < 0.06
    assert abs(att["excess_kwh"][1] - (day(0) - under_way + day(1) - 10.0)) < 0.06


def test_the_hour_under_way_counts_only_for_the_part_still_ahead():
    use = {k: 0.0 for k in KEYS}
    full = PL.balance(NOW, 3, {k: 1.0 for k in KEYS}, use, FLAT)
    part = PL.balance(NOW, 3, {k: 1.0 for k in KEYS}, use, FLAT, under_way=True)
    assert full.net[0] == 1.0 and abs(part.net[0] - 40 / 60) < 1e-9 and part.net[1] == 1.0


def test_spread_evenly_keeps_the_energy_and_moves_it_round_the_clock():
    use = {k: 3.0 for k in KEYS[:24]}
    tank = {k: (2.0 if i == 12 else 0.0) for i, k in enumerate(KEYS[:24])}     # one boost hour
    out = PL.spread_evenly(use, [tank])
    assert abs(sum(out.values()) - sum(use.values())) < 1e-9
    assert abs(out[KEYS[12]] - (3.0 - 2.0 + 2.0 / 24)) < 1e-9 and abs(out[KEYS[0]] - (3.0 + 2.0 / 24)) < 1e-9


def test_production_ends_at_the_last_sunlit_hour_of_the_first_day_with_any_left():
    prod = day_sun(3.0)
    assert PL.production_ends_at(NOW, prod, 1.0, TZ).startswith("2026-10-17T17:00")
    late = NOW.replace(hour=20)
    assert PL.production_ends_at(late, prod, 1.0, TZ).startswith("2026-10-18T17:00")


def test_plan_all_gives_three_readings_and_holds_nothing_back_when_complete():
    cfg = {"days": 5, "production_scale": 0.95, "consumption_scale": 1.0, "floor": 10,
           "weights": {"target": ((24, 0.9), (24, 0.9)), "minimum": ((48, 0.5), (48, 0.5)), "excess": ((24, 0), (24, 0))}}
    use = {k: 0.5 for k in KEYS}
    out = PL.plan_all(NOW, use, day_sun(3.0), 50.0, 60.0, 90.0, cfg)
    assert set(out) == {"target", "minimum", "excess"} and all(out.values())
    assert out["excess"]["value"] == out["excess"]["attributes"]["excess_kwh"][0]
    assert len(out["excess"]["attributes"]["days"]) == 5 and out["target"]["attributes"]["production_ends_at"]
    # sun that stops after 3 days: the 5-day readings are None, so the caller keeps its last value
    short = {k: v for k, v in day_sun(3.0).items() if k < KEYS[0] + 72 * 3600}
    assert PL.plan_all(NOW, use, short, 50.0, 60.0, 90.0, cfg) == {"target": None, "minimum": None, "excess": None}
    # no pack size, no readings; no SOC, no excess
    assert not any(PL.plan_all(NOW, use, day_sun(3.0), None, 60.0, 90.0, cfg).values())
    assert PL.plan_all(NOW, use, day_sun(3.0), 50.0, None, 90.0, cfg)["excess"] is None


if __name__ == "__main__":
    run_main(globals())
