# Streaming detection pipeline

*Agreed with the owner 2026-11-11 (decisions at the end). Steps 1, 2 and 3 built (dev, 0.5.3); step 4 not yet.*

## Why

Detection runs as a pass at every minute of the clock (`DetectionRunner._run`, `_on_minute`). Each
pass waits for the recorder to flush, reads every meter's, the inverter's, the reactive inputs' and
the switches' rows since the last pass, hands them to `Fleet.process`, then refreshes every entity,
persists and rewrites the Energy dashboard list. Two costs follow:

- **Latency.** A step is seen up to a minute plus the pass after it happened. Live power is a
  snapshot built once a pass, which is half of INS-30 (a 20 s pulse is either caught open at its
  full watts or added later as lumped energy; both, and it is double counted).
- **Fixed work per pass.** The recorder reads, the source resolution and the per-call set-up and
  tear-down inside `Fleet.process` are paid every minute whatever happened. The recorder share
  measured 2026-11-11 at Home: about 1 s per 2 min of Load Insights' history reads
  (`get_significant_states`, 88 calls; 4a31d4a cut the inputs' share).

The detector itself is already a stream. `Fleet._run` puts every reading and every due moment
(a pending change confirmed by silence, an event window, a held run's tail, a vote, filing, the
daily repair) in ONE order on ONE clock and feeds readings one at a time (`Detector.step`). A pass
is a pause in that stream: "the same history read in one call, in six-hour slices or a minute at a
time files the same sessions" (`Detector.process`). This design keeps that core and changes what
feeds it and what runs around it.

## The shape: layers that run when their input changes

| layer | triggered by | today |
|---|---|---|
| L0 assemble | a state change of a configured sensor | `_derive_load`, `_reactive_series`, `_align`, per pass |
| L1 edges | an assembled reading | `PhaseState.process` inside the pass |
| L2 steps | an edge, and its horizon running out (a due time) | `Fleet._run`'s horizon wait, `_form_first` |
| L3 runs | a step; and a reading, for the level rules (the 0.5 close, a meter's own fall) | `_closed`, `stand_in`, `_meter_ended` |
| L4 filing | a run released from its held tail / its meters' vote | `_decide`, `_locate`, `_vote` |
| L5 pairs, signatures, naming | a filing that changed a pair | `_link_successors` (once a pass), the library |
| L6 daily / hourly | the clock | `_repair`, `Pair.age`, `_rewrite_named`, `_persist` |

Each layer does work only when its own input arrives, so its cost follows its own event rate
(Home: readings a few a second a phase; steps ~9,750 a day; runs, filings and pair changes fewer
again). The ORDER stays shared: every layer is a handler on the existing single clock queue
(`Fleet._run`), not a loop of its own. Two layers are not purely driven from below, and keep the due
times they have now:

- **L2:** a grid edge becomes a step only when the meters have had time to report theirs
  (`Fleet._horizon`, learned per meter, capped by `wait_cap_s`). That is a due time the edge sets.
- **L3:** a run whose stop was missed closes on the reading's level (the 0.5 rule), and a meter's
  run closes on that meter's fall; both look at readings, not steps.

## What changes

### 1. The detector: persistent streams (pure, `insights/detect.py`)

`Fleet.process` becomes a thin loop over a new pair of calls, so the bench and the replay keep one
code path:

- `Fleet.feed(meter, ts, phase, watts, q=None, pv=None)`: one assembled reading into its meter's
  stream (the grid's under `""`).
- `Fleet.advance(until)`: run the queue up to `until` (the existing `_run` body).

What `process` does per call today is split by what it depends on:

| per call today | moves to |
|---|---|
| `_sync_views`, `_units`, `_bind`, the views' parents/single/horizon_skip | a configuration change (meters added, ignored, re-parented, `single`) |
| `Detector.begin` (phase bindings, `_corroborate`, sorting the pass's readings) | `_corroborate` per configuration; sorting goes (readings arrive in order, see the reorder buffer) |
| `_hold_back` (the grid held behind its meters) | gone: the horizon due time already holds a grid reading until its meters have had their say |
| `_signal_events`, `_keep_rows`, the switches' / numbers' / inputs' merge and trim | on the input's own state change; trimming on the hourly timer |
| `Detector.finish` (edge trim, `_link_successors`) | the trim on the hourly timer; `_link_successors` on L5, when a pair changed |
| pair ageing | already hourly, unchanged |

**Acceptance:** the slicing-invariance bench (`tests/energy_bench.py invariance`) and the replay
give the SAME library through `feed`/`advance` as through today's `process`, on both sites'
exports, before anything else is built. This step changes no answer, so it is judged on identity,
not on the bench.

### 2. The runner: state changes instead of recorder reads (`detection.py`)

- **Listener.** `async_track_state_change_event` on every configured entity: the grid's phases,
  inverters, sub-meters, their volts/amps/pf/var, the switches and inputs. The entity set is
  resolved once and again only on `EVENT_ENTITY_REGISTRY_UPDATED`, an Energy dashboard change, or
  the options changing (today `_resolve_solar`, `_refresh_submeters` and the device lookups run
  every pass).
- **Reorder buffer per meter.** HA delivers a meter's states in order, but a reading can carry a
  `last_updated` behind one already fed (a Shelly reporting late). Each meter keeps a short buffer
  and feeds in timestamp order; a reading older than what its meter has fed is dropped and
  counted (diagnostics), which is what the recorder path does with a row behind its pass.
- **L0 joins.** The house reading is grid + inverter; the reactive power and the PV value are
  looked up at the grid reading's timestamp. A grid reading is assembled when every series it
  joins has a reading at or after its time, or after a short join wait (the series' own measured
  cadence, `PhaseState.interval`, a few times over), using the same alignment rule as `_align` so
  that batch and stream agree.
- **The recorder stays for gaps only:** the backfill on a fresh start, and the stretch between the
  last persisted reading and the listener starting after a restart. Both go through the same
  `feed`/`advance`, in bulk.
- **Executor.** The hot path (L0-L3: assembling, edges, joining edges into steps, runs) runs once a
  second on what arrived in that second, in one executor job, not one hop per reading. A step still
  forms only once its horizon has passed (L2's due time); the 1 s loop is when that is checked.
- **Cold path on timers:** `_persist` (as now), the Energy dashboard list (on a name change, not
  per pass), the large sensor attributes (the library and detected-loads sensors, at most every
  few minutes as the library sensor already is).
- **Audit.** Once a day the runner reads one hour back from the recorder and compares it with what
  the stream fed (the pattern of `input_cache_check`), so a missed or doubled state change shows in
  diagnostics instead of silently.

### 3. Live power (`Fleet.live_power`, INS-30's snapshot half)

Today: the open runs' watts at the pass, plus the energy of runs filed since the last pass spread
over it. A pulse caught open is counted at full watts and again as filed energy.

Streamed: each named load's power is written as **the energy it drew since its last write, divided
by the time since it** - the open runs' energy accrued to now plus the runs that closed in between,
each counted once by run identity. For a steady load that is its level; for a 20 s pulse train it
is its true average over the write interval, with no double count and no lump.

Writes are throttled per load: when the value moves by more than the load's own noise, and at most
once every `LIVE_WRITE_MIN_S` (policy, proposed 5 s), at least once every `LIVE_WRITE_MAX_S`
(proposed 60 s, so a flat line still has points). Today every entity is refreshed every pass
(`async_update_listeners`); with this the writes follow what changed.

Not fixed by streaming: the ~21 % of the Inkubator's starts never counted live and the mispaired
runs (INS-30's other half). Those are the run layer's, see the run-lifecycle spec.

### 4. Upper layers gated on change

`_link_successors` walks the whole library for every named load once a pass. Under L5 it runs
when a filing changed a pair, debounced to at most once a minute. The same for anything else in
`finish` or the runner that rebuilds from the whole library.

## Latency to expect

A step is declared once its horizon has passed: about the slowest sharing meter's declare lag at
Home (Shellys, a second or a few), capped by `wait_cap_s`. So live power trails by seconds, not a
minute. Showing a load sooner than its horizon would need a provisional value corrected later;
not proposed.

## Build order

1. `feed`/`advance` in the detector, `process` on top of them; identity on invariance and replay.
2. The runner's listener, reorder buffer, L0 joins, recorder for gaps, the daily audit; the minute
   pass is removed in the same step, no transition period with both code paths (the owner).
3. Live power by energy since the last write, throttled writes.
4. L5 gating and the cold path on timers.

Each step is its own commit with its numbers: CPU per 2 min from the whole-process profile (today's
Home figures: Load Insights ~3.8 s, the recorder and its database 7.6 s), recorder queries per
2 min, and the step latency (declared time minus step time, a new diagnostic).

## Relation to the run-lifecycle work

Independent and compatible. This changes how readings reach the detector and what runs around it;
the run-lifecycle spec (`docs/specs/2026-10-28-run-lifecycle.md`) changes the run layer (L3/L4):
falls matched to running rise groups, the look-back for a missed rise, a running group drawing its
learned level. That layer was designed as reactions to single steps, which is this model. Build
this first: the look-back then reads the stream's own retained readings instead of the recorder.

## Decisions (the owner, 2026-11-11)

1. **Write throttle:** live power written at most every 5 s and at least every 60 s per named load
   (`LIVE_WRITE_MIN_S` 5, `LIVE_WRITE_MAX_S` 60), and when it moves by more than the load's noise.
2. **Hot path:** a 1 s loop for assembling, edges and joining edges into steps (and the run layer
   behind them), in one executor job per second.
3. **No fallback:** the minute pass is removed with step 2; no option, no period with both paths.
4. **No provisional live power:** left out for now. It adds latency (a step waits for its horizon)
   but every power is still shown correctly, just late.
5. **Step 2's details (the owner, 2026-11-11):** one settle lag of 1 s (`STREAM_SETTLE_S`) for ordering,
   assembling and advancing - a meter's update arrives within milliseconds (Home's SolarEdge poll ~14 ms apart, its
   3EMs ~1 ms, measured that day), and joins only ever combine readings of one update; a window never ends inside
   one (`_Stream.quiet_end`, readings under `COMBINE_SETTLE_S` apart); a change stamped behind its window is dropped
   and counted (`stream_late` / `late_dropped`); the entities, the save, live power and `Fleet.settle` once a minute
   until steps 3-4; the daily audit counts only (`stream_audit`). The recorder fills gaps an hour's slice at a time
   (every meter in one query; six hours were ~90 MB at Home).
