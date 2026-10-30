# INS-24: a heat-loss and heat-capacity model for room thermostats (spec, for review)

Status: draft 2026-10-21, not built; the owner answered the open questions the same day (section 8). Decided with the owner (2026-10-21): (1) **rooms first, tanks included in INS-24** (revised later the same day: the model also covers water heaters, section 10; INS-2's figure stays as the fallback until a tank's model is known); (2) the model **replaces** INS-2's figure for rooms: it is the cleaner approach, so it ships even when it loses to the current method, as long as the loss is not big (the bar is in section 7); (3) design agreed here before any code.

## 1. Why

INS-2 learns `k = E / rise` over heating runs, and for a room that figure is not a property of the room: a run's energy also pays for the heat lost *while* it heats, so `k` grows with run length and with how cold it is outside. Kozolec's Bee Room Heater reads 0.656 kWh/K from six runs (spread 0.4); the room's own heat capacity is about 0.3 kWh/K. Three numbers describe the room and nothing else is needed:

    C dT/dt = P - UA (T_in - T_out) + S + G

| symbol | meaning | unit |
|---|---|---|
| `C` | heat capacity of what answers in hours (air, furniture, wall surfaces) | kWh/K |
| `UA` | heat loss to the outside | W/K |
| `P` | the heater's electrical power; all of it becomes room heat | W |
| `S` | sun on the room, `s x` (an optional sun series) | W |
| `G` | a constant for everything else: sensor height and position, the ground, neighbours, small loads | W |

`C / UA` is the room's time constant. Everything INS-2 and the planner want is derived from these three, at any run length and any weather.

## 2. What was measured (Kozolec Bee Room, 2026-10-21, 10 days of recorder data)

Room 2 x 4.5 m, floor insulated about 10 cm, hives empty, LED light and phone / laptop chargers only; T&H on a shelf at 1.4 m on the long wall, under 1 m from a corner, the heater at the far short side blowing away from it. Script: `HA_Configs/kozolec/bee_room_thermal.py` (+ `bee_pv.py`, scratchpad).

| fit | C kWh/K | UA W/K | G W | residual K |
|---|---|---|---|---|
| 30 min windows, outdoor only | 0.30 | 35 | 182 | 0.227 |
| 60 min windows | 0.36 | 36 | 184 | 0.333 |
| night only (18-04 UTC) | 0.31-0.39 | 39-40 | 179-196 | |
| + PV now (sun) | 0.29 | 32 | 114 | 0.208 |
| + PV now + 3 h earlier | 0.29 | 34 | 146 | 0.204 |
| + the apartment as a second cold side | 0.30 | 34 | 176-184 | no gain |

- **UA 32-36 W/K and C 0.29-0.36 kWh/K hold under every variation** (about +-15-20 %). G does not: it is the part the model leaves out.
- **Sun gain is real** (t = 8-10): 25-38 W per kW of PV. It takes about a third of G (182 -> 114 W).
- **The weather station is not the cause of G**: against Open-Meteo it reads 0.8 K *warmer* at night and 0.3 K cooler by day (230 hours, no scale error).
- **G is the same with the heater off (107 W) and on (118 W)**, so the heater does not heat the sensor.
- **The heater's meter is right**: 36.9 kWh integrated against 37.1 kWh on its own counter; it averaged 154 W, while the mean loss is 327 W. About 110-140 W of constant gain remains. Floor and ground account for 10-30 W, small loads 10-30 W; the likely rest is the sensor reading warm at 1.4 m (a 3 K bias is 100 W at 34 W/K). The owner moves a second sensor to table height to test that; the result goes into this spec's bench (section 7).
- A constant offset in any sensor lands in `G`, not in `UA` or `C`. That is why `G` is a fitted term and not an assumption.

## 3. Fit

A pure `insights/roommodel.py` (stdlib only, no Home Assistant imports, replayable).

**Inputs per room**, hourly, on the same grid INS-2 uses: the load's energy per hour (statistics, kWh), the thermostat's `current_temperature` at the top of each hour (`coordinator._hourly_attribute`), the outdoor temperature per hour (`temps_hist`, already the *corrected* series of INS-19 / INS-20), and optionally a sun series per hour (the measured PV power where the site has it; the sun forecast the planner already holds otherwise; none -> the `S` term is dropped).

**Windows.** One row per hour: `dT = T(h+1) - T(h)`, `P` = the hour's energy as mean watts, `d` = the mean of `T_in - T_out` over the hour (mean of the two ends), `sun` = the hour's mean. The 60-minute bench above is the resolution LI has; it agrees with the 30-minute one.

**Estimate.** Least squares of `dT / 1h = a P - b d + g + s sun` (normal equations, four unknowns). Then `C = 1 / a`, `UA = b C`, `G = g C`, `s' = s C`. The most recent window of days, with a floor on hours.

**It is unknown (and the room falls back to INS-2 as today) when:**
- fewer than `MIN_HOURS` usable hours (hours with all of temperature, outdoor and power known), or fewer than `MIN_HEATED` hours with the heater drawing, or an outdoor spread (max - min of `d`) below a floor: with no variation in `d` the loss and the constant cannot be told apart;
- a coefficient is not physical (`a <= 0` or `b <= 0`);
- the coefficients are not significant: `t(a)` and `t(b)` below a critical value, from the residual variance (the same kind of test INS-15 uses);
- the fit is unstable: fitted on the first and on the second half of the window (day blocks) the two `UA` or `C` differ by more than a spread limit (the equivalent of INS-2's `max_spread`).
Counts and limits are measured in units of the sensor's own steps and the data's own spread where they describe the world (the repo's one rule); `MIN_HOURS`, `MIN_HEATED` and the spread limit are judgements, benched, not tuned by eye.

**History.** The recorder holds the thermostat's attributes for ten days only (`keep_days`). Ten days was enough for the Bee Room, but a window that short sees one season. Open question 3.

## 4. What is derived

- **Room to absorb.** From now at `T_now` to the boost `T_b` over `H` hours at power `P` (the load's absorption rate): `C (T_b - T_now) + (UA (T_mean - T_out) - G - S) H`, never below zero. `H` is `(T_b - T_now) C / (P - loss)` when the heater out-runs the loss; a heater that cannot reach the boost (`P` below the loss) has room until the plan's horizon, capped. This replaces `room_from_thermal(k, ...)` for rooms.
- **Daily energy for the plan.** Hour by hour from the weather forecast: `max(0, UA (T_set - T_out_h) - G - S_h)` summed over the day, bounded by the heater's rating. It replaces the history-spread (`planning.spread_evenly`) for a room thermostat that has a model; the load keeps the spread when its model is unknown.
- **Coasting time** `C / UA` (hours) and the *unblocked temperature at hour h*, offered as attributes for Load Juggler's block decisions (whether it is safe to hold the heater off for the next peak). Nothing in Load Juggler changes in this spec.
- **Sensor attributes**: `ua_w_per_k`, `c_kwh_per_k`, `g_w`, `sun_w_per_kw` (when used), `hours`, the fit's residual and the half-window agreement, and `source` (`room_model` / `energy_per_degree` / `history`). The `Room to absorb today` sensor says which source it used, as INS-2's does today. English attributes only; nothing to translate.

## 5. Integration

- New pure module `insights/roommodel.py`; `thermal.py` keeps `heating_runs` / `kwh_per_degree` (tanks, and the fallback for a room whose model is unknown).
- `coordinator._thermal_rooms`: for a *Room thermostat* load it builds the hourly rows and calls the model first, then falls back; the rest of the function (device matching through the power sensor, the boost entity, the notes in `_thermal_note`) is unchanged.
- `planning.py`: a per-room daily-energy function the plan uses in place of `spread_evenly` for that load.
- Store: the fitted figures with their time, so the figure survives a restart and can be compared day to day.
- Generation / options: no new option. The outdoor and sun series are the ones already configured (Explanatory inputs, the Energy dashboard's solar).

## 6. Tests

- `tests/test_roommodel.py` (pure): a synthetic room with known `C`, `UA`, `G`, `s`, noise and 0.2 K quantisation: the fit recovers them; unknown with too few hours, a flat outdoor temperature, a heater that never runs, a stalled sensor, and a non-physical fit; derived room and daily energy checked against the closed form; half-window agreement catches a regime change.
- `run_all.py` picks it up.

## 7. Bench and the ship rule

Replay on exported history (`tools/hist.sh`; Kozolec's Bee Room is the only room thermostat with enough history today; any other room thermostat that exists on a site joins):

- **A. Daily energy.** For each day the heater ran, the forecast made the evening before (outdoor forecast where `forecast_bench` stored it, the day's own outdoor series as an upper bound), the room model's against the current spread-of-history, against the heater's actual kWh: MAE and bias.
- **B. Room to absorb.** At each hour the heater started, the model's room against the energy the heater actually took to the boost, against INS-2's `k x rise`.
- **C. Stability.** The same fit on the first and second half of the data and on 5-day blocks.
- **D. After the second sensor** (the owner, table height, 2026-10-21): the refit's `G`, and whether `UA` and `C` move.

**Ship rule (the owner, 2026-10-21):** a cleaner, simpler and physically more accurate approach is accepted even when the bench shows no clear win; it ships even if it loses, as long as the loss is *really big*. There is no single number for "big" (the owner cannot put one on it): the bench reports MAE and bias for A and B, per day and in total, with the current method beside them, and the owner judges the loss. A loss that is clearly large on both A and B, or that comes from a failure the model should have caught (a wild fit, a day far off), counts as big and is treated as an implementation fault: it goes under "Things already ruled out" with what was tried. Anything smaller is recorded in the commit and the AGENTS.md entry and the model ships. (The general rule is in the repo's `AGENTS.md`, "Before shipping a detector change".)

## 8. Decisions (the owner, 2026-10-21)

1. **Size of a "big" loss**: no single figure; the owner judges from the reported numbers (section 7).
2. **Sun term**: the site's measured PV power where it exists, the planner's sun forecast otherwise, none when neither exists. Agreed.
3. **History beyond ten days**: yes, store each finished day's hourly rows (temperature, power, sun, outdoor; 24 rows a day) so the window can reach thirty days or more and cover a season.
4. **Setpoint** for the daily-energy forecast: read from Load Juggler the way the boost is read today. Agreed.
5. **One heater per room**: yes. (The owner's note about a second element is about the Kozolec *boiler*, not a room: a 1 kW element mounted lower in the tank, to be used with the existing one as a single heater with three power levels. A tank is outside this spec; it is a Load Juggler matter, and INS-2's energy per degree for the boiler is unaffected by it, since the tank's heat capacity stays the same. Not in INS-24.)

## 9. Modes, setpoints and the forecast range (owner, 2026-10-21)

A thermostat load runs at one of several **setpoints**: *away* (well below), the *set* temperature, and *boost*. The model takes the setpoint as an input, so each mode is one more call of `daily_energy` (and `room_to_absorb`); no new fitting.

- **Per-mode series inside the forecast.** A thermostat load's forecast is built per mode (away, set, boost), internally and in the forecast sensor's *attributes* only; the sensors' state and names do not change. The range follows: **low** = the set temperature (away is shown as its own series, not the low), **high** = boost, **expected** between.
- **Expected = weighted.** `expected = low + w (high - low)`, `w` the share of the load's hours spent at boost (the stored hourly rows with the mode recorded: the thermostat's target temperature or preset at the top of each hour, read as an hourly attribute the way `current_temperature` is). `w = 0.5` until the stored days say otherwise (the minimum days: a judgement, benched). Away hours are left out of `w`: they are shown by their own series.
- **Where the mode comes from.** The thermostat's target temperature (three setpoints: set, boost, away) from Load Juggler's load, found the way the boost is found today; Load Juggler writes nothing new.
- **Loads without a model** (an EVSE, whose output differs by charge mode): the same split, but from history only: the mode per hour from Load Juggler's mode entity, a series per mode, and only when enough days of each mode were recorded. Not part of INS-24's build; the attribute shape is shared, so the EVSE joins it later.
- **The SOC plan keeps using the expected series** (owner): the low / high series are attributes. The house forecast carries hourly p10 / p90 bands (`Forecast.bands`), but the plan uses the point forecast only (`planning.balance`), so **there is no range for the target or the minimum today**. If one is wanted, it is computed from the consumption ranges: the balance run once with the low and once with the high consumption, giving target and minimum bands. (Summing hourly p10 / p90 over a day overstates the spread - the hours are not perfectly correlated - so a band built from the per-mode series is the better base for rooms and tanks.) Not in INS-24; a follow-up if the owner wants it.

## 10. Tanks (included; owner, 2026-10-21)

The same model fits a water heater: a tank's `UA` from its standby loss and `C` from its heating, with kWh/K following. The new part is **hot-water use**: a draw takes heat out and is not measured, so the constant term would not be constant. Build order: the room model first (sections 3-9), then the tank on the same module with a draw term, and INS-2's figure stays the tank's value until its model is known and has passed the same bench. Open: how to handle draws (fit `UA` on hours without a drop in temperature and let the draws be the residual, or learn a draw pattern per hour of day); decided when the tank is built, benched on Kozolec's boiler (energy + temperature history).
