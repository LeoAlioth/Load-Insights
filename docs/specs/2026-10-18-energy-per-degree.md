# INS-2: energy per degree for thermostat loads (plan, for discussion)

Status: built 2026-10-18 (`insights/thermal.py`, `coordinator._thermal_rooms`), answers below. Decided with the owner: (1) the temperature, boost setpoint and matching come from Load Juggler's tank/heater load, matched to the dashboard device through the load's power sensor; (2) the boost is Load Juggler's boost slider; (3) tank and room are separate kinds with their own defaults (tank: 12 runs, spread 0.35; room: 6 runs, spread 0.6), a device is a room when marked on the inputs page; (4) runs where the temperature falls are dropped, and the figure is total energy over total rise so long runs weigh most.

## Goal

Today "Room to absorb today" is the P90 of a load's complete past days minus
today's kWh (spec 2026-10-17, section 10). For a tank or heater the physics
gives the room directly:

    room_kWh = max(0, boost_setpoint - current_temp) x kWh_per_degree

`kWh_per_degree` is the load's thermal mass (a tank: litres x 1.163 Wh/l/K, a
heater: whatever the room and its walls hold). Learn it from the load's own
history instead of asking for litres, and the room is exact at any hour, not
only after 3 clean days.

## What is learned

Pairs of (energy the load used, temperature change it caused) over heating
runs. For a run from t0 to t1:

    k = (E(t1) - E(t0)) / (T(t1) - T(t0))      [kWh per K]

- **Inputs per load:** its energy series (LI already has it for every Energy
  dashboard device and named load) and one temperature series (new, see Q1).
- **Run selection:** the load draws continuously, the temperature rises by at
  least a floor in K (measured from the sensor's own resolution, per the "one
  rule": a count of quanta, not degrees) and no hot-water draw interrupts it.
- **Estimate:** the median of k over the last N runs (robust to a draw inside a
  run, which lowers the rise and so raises k), kept with its spread (MAD).
  Unknown below a minimum number of runs; the room sensor then falls back to
  the P90 ceiling, exactly as today.
- **Losses:** a tank also loses heat while idle, a room heater loses to the
  outside. Both make k look larger than the pure thermal mass. Measuring over
  short, hot runs keeps the loss small against the input; a later refinement
  is fitting k with a loss term (INS-3 territory).

## What it changes

- New `DeviceRoomSensor` source: `thermal` when k is known and both the
  temperature and the boost setpoint are readable, `history` (P90) otherwise.
  The sensor's attribute says which, so Load Juggler's `early_room_kwh` and the
  owner can see it.
- Load Juggler's `thermal_headroom_k` and its early-start blocker already use
  the headroom in kelvin; with k both stay consistent (K blocks, kWh sizes).
- INS-3 (rate of absorption) shares the run selection: power during a run is
  the rate, so build the run finder once for both.

## Open questions (need the owner)

1. **Where does the temperature come from?** (a) a new optional "temperature
   sensor" next to the load in LI's options, (b) LI reads it from the Load
   Juggler load that wraps the same device (no new option, but couples the two
   integrations), (c) Load Juggler computes k itself, since it already holds
   both the temperature and the boost setpoint, and LI only supplies the energy
   series. Recommendation: (a) for independence, with (b) as a default when a
   Load Juggler load exists.
2. **Boost setpoint:** read from the same number entity Load Juggler uses
   (one more optional field), or ask once per load in LI?
3. **Room heaters** (Bee Room Heater): the room's k moves with the outdoor
   temperature. Learn it anyway and show its spread, or leave heaters on the P90
   ceiling and do tanks only first? Recommendation: tanks first, heaters once
   the tank result is judged.
4. **Hot-water draws:** a draw during a run cools the tank. Skip runs where the
   temperature ever falls, or keep them and rely on the median?

## Testing

- Pure `insights/thermal.py`: `heating_runs(energy, temp)`, `kwh_per_degree(runs)`,
  `room_from_thermal(k, temp, boost)`; replayed in `tests/test_thermal.py` on
  synthetic runs (clean, with a draw, with a stalled sensor, too few runs).
- Bench on Kozolec's boiler (energy + temperature history, exported with
  `tools/hist.sh`): compare the thermal room with the P90 room against the
  energy the boiler actually took the rest of each day.
- Release notes and the TODO entry move to "to close" once live on Kozolec.
