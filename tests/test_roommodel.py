"""Room model: the fit recovers a known room, gives up when the data cannot carry it, and the derived figures."""
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

RM = load("insights.roommodel")
H = 3600.0
C_WH, UA, G, S_GAIN = 300.0, 35.0, 110.0, 25.0     # the room: Wh/K, W/K, W, W per kW of sun


def outdoor_at(day_frac):
    """Degrees: a daily swing on a slow drift between about 2 and 16 C."""
    return 9.0 + 6.0 * math.sin(day_frac / 4.5) + 3.0 * math.sin(day_frac * 2 * math.pi - 2.0)


def sun_at(day_frac):
    return max(0.0, math.sin((day_frac % 1.0 - 0.25) * 2 * math.pi)) * 3.0     # kW


def simulate(days, heater_w=1000.0, with_sun=True, rng=None, offset=0.0, step=0.2):
    """Hourly (temps, energy, outdoor, sun) of a thermostat room, minute steps, T&H steps of ``step``."""
    rng = rng or random.Random(1)
    t_in, on = 20.0, False
    temps, energy, outdoor, sun = {}, [], {}, {}
    for h in range(days * 24 + 1):
        key = h * H
        temps[key] = round((t_in + offset) / step) * step
        outdoor[key] = round(outdoor_at(h / 24.0) + rng.gauss(0, 0.1), 1)
        if h == days * 24:
            break
        kwh, s_sum = 0.0, 0.0
        for m in range(60):
            frac = (h + m / 60.0) / 24.0
            if t_in < 19.0:
                on = True
            elif t_in > 20.0:
                on = False
            p = heater_w if on else 0.0
            s = sun_at(frac) if with_sun else 0.0
            to = outdoor_at(frac)
            t_in += (p - UA * (t_in - to) + G + S_GAIN * s) / C_WH / 60.0
            kwh += p / 60.0 / 1000.0
            s_sum += s / 60.0
        energy.append((key, kwh))
        sun[key] = s_sum
    return temps, energy, outdoor, sun


def fitted(days=10, with_sun=True, **kw):
    temps, energy, outdoor, sun = simulate(days, with_sun=with_sun, **kw)
    rows = RM.hourly_rows(temps, energy, outdoor, sun if with_sun else None)
    why = {}
    return RM.fit(rows, RM.quantum(temps), why), why


def test_the_fit_recovers_the_room():
    m, why = fitted()
    assert m is not None, why
    assert abs(m.ua_w_per_k - UA) < 0.2 * UA
    assert abs(m.c_kwh_per_k * 1000 - C_WH) < 0.3 * C_WH
    assert abs(m.g_w - (G)) < 0.5 * G + 20
    assert m.sun_gain is not None and abs(m.sun_gain - S_GAIN) < 0.5 * S_GAIN


def test_without_a_sun_series_the_constant_takes_the_mean_sun():
    m, why = fitted(with_sun=False)
    assert m is not None, why
    assert m.sun_gain is None
    assert abs(m.ua_w_per_k - UA) < 0.3 * UA


def test_a_constant_sensor_offset_does_not_move_ua_or_c():
    a, _ = fitted(offset=0.0)
    b, _ = fitted(offset=3.0)
    assert a is not None and b is not None
    assert abs(a.ua_w_per_k - b.ua_w_per_k) < 0.1 * a.ua_w_per_k
    assert abs(a.c_kwh_per_k - b.c_kwh_per_k) < 0.1 * a.c_kwh_per_k
    assert abs((b.g_w - a.g_w) - 3.0 * UA) < 0.25 * 3.0 * UA    # a sensor reading 3 K warm: the constant takes UA x 3 K


def test_too_few_hours_is_unknown():
    m, why = fitted(days=2)
    assert m is None and "usable hours" in why["reason"]


def test_a_heater_that_never_runs_is_unknown():
    temps, energy, outdoor, sun = simulate(10)
    rows = RM.hourly_rows(temps, [(t, 0.0) for t, _ in energy], outdoor, sun)
    why = {}
    assert RM.fit(rows, RM.quantum(temps), why) is None and "heater drew" in why["reason"]


def test_a_flat_outdoor_temperature_is_unknown():
    temps, energy, outdoor, sun = simulate(10)
    flat = {t: 8.0 for t in outdoor}
    rows = RM.hourly_rows(temps, energy, flat, sun)
    why = {}
    assert RM.fit(rows, RM.quantum(temps), why) is None and "too little" in why["reason"]


def test_a_stalled_sensor_is_unknown():
    temps, energy, outdoor, sun = simulate(10)
    stalled = {t: 20.0 for t in temps}
    rows = RM.hourly_rows(stalled, energy, outdoor, sun)
    assert RM.fit(rows, 0.2, {}) is None


def test_a_room_that_changes_halfway_is_unknown():
    temps, energy, outdoor, sun = simulate(10)
    rows = RM.hourly_rows(temps, energy, outdoor, sun)
    # the second half loses heat three times as fast (a window left open): the halves disagree
    bent = [r if i < len(rows) // 2 else RM.Row(r.t, r.dt_k - 2.0 * UA / C_WH * r.d_k, r.p_w, r.d_k, r.sun)
            for i, r in enumerate(rows)]
    why = {}
    assert RM.fit(bent, RM.quantum(temps), why) is None, why


def test_derived_figures_follow_the_closed_form():
    m = RM.RoomModel(0.3, 35.0, 110.0, None, 200, 80, 0.2, 10.0, 10.0, 0.1)
    assert abs(m.coast_hours - 300 / 35.0) < 1e-9
    # 20 C inside, 10 C outside: loss 350 - 110 = 240 W
    assert abs(m.loss_w(20.0, 10.0) - 240.0) < 1e-9
    # holding 20 C for 24 hours at 10 C outdoors: 24 x 240 W, within a 1 kW rating
    assert abs(RM.daily_energy(m, 20.0, [10.0] * 24, 1000.0) - 24 * 0.240) < 1e-9
    # a mild hour (loss below zero) asks for nothing, a bitter hour is capped at the rating
    assert RM.daily_energy(m, 20.0, [19.0], 1000.0) == 0.0
    assert RM.daily_energy(m, 20.0, [-40.0], 1000.0) == 1.0
    # 2 K to go, 1 kW heater: held 0.6 kWh, net 1000 - 240 (about) so about 0.6 h of loss
    room = RM.room_to_absorb(m, 18.0, 20.0, 10.0, 1000.0)
    assert 0.6 < room < 0.8
    assert RM.room_to_absorb(m, 21.0, 20.0, 10.0, 1000.0) == 0.0
    assert RM.room_to_absorb(m, None, 20.0, 10.0, 1000.0) is None
    # a heater below the loss: it is held to the horizon at its own rate
    assert RM.room_to_absorb(m, 18.0, 20.0, -40.0, 200.0, horizon_h=12.0) <= 200 * 12 / 1000.0


def test_the_days_of_each_mode_follow_their_setpoints():
    m = RM.RoomModel(0.3, 35.0, 110.0, None, 200, 80, 0.2, 10.0, 10.0, 0.1)
    outdoor = {h * H: 10.0 for h in range(48)}
    got = RM.daily_by_mode(m, {"away": 7.0, "normal": 20.0, "boost": 24.0}, outdoor, 1000.0, [(0.0, 24 * H), (24 * H, 48 * H)])
    assert got["away"] == [0.0, 0.0]                                   # 7 C holds itself against 10 C outdoors
    assert abs(got["normal"][0] - 24 * 0.240) < 0.01 and got["boost"][0] > got["normal"][0]
    short = RM.daily_by_mode(m, {"normal": 20.0}, {h * H: 10.0 for h in range(30)}, 1000.0, [(0.0, 24 * H), (24 * H, 48 * H)])
    assert short["normal"][1] is None                                  # a day with an unknown hour is unknown


def test_the_boost_share_counts_boost_against_normal_and_leaves_away_out():
    targets = {h * H: (24.0 if h % 4 == 0 else 20.0) for h in range(48)}       # a quarter at boost
    targets.update({(100 + h) * H: 7.0 for h in range(40)})                    # away hours do not count
    assert abs(RM.boost_share(targets, 20.0, 24.0) - 0.25) < 1e-9
    assert RM.boost_share({0.0: 24.0}, 20.0, 24.0) == 0.5                      # too few hours: halfway


def test_merged_hours_keep_old_rows_take_fresh_ones_and_prune():
    old = {str(int(h * H)): [20.0, 0.1, 9.0, None, 20.0] for h in range(0, 10)}
    now = 100 * H
    merged, temps, energy, outdoor, sun, targets = RM.merge_hours(
        old, {99 * H: 21.0, 100 * H: 21.5}, [(99 * H, 0.4), (100 * H, 0.2)], {99 * H: 8.0}, {99 * H: 1.0}, {99 * H: 24.0}, now)
    assert temps[99 * H] == 21.0 and temps[100 * H] == 21.5
    assert (99 * H, 0.4) in energy and all(t < now for t, _ in energy)         # the running hour's kWh is not taken
    assert outdoor[99 * H] == 8.0 and sun == {99 * H: 1.0} and targets[99 * H] == 24.0
    assert str(int(5 * H)) in merged                                            # inside the keep window: stays
    gone, *_ = RM.merge_hours(old, {}, [], {}, {}, {}, now, keep_days=1)
    assert str(int(5 * H)) not in gone                                          # outside it: pruned


if __name__ == "__main__":
    run_main(globals())
