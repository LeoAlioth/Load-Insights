# Tests and the bench

## Never ship a detector change unmeasured

**The local history is the last ten days** (2026-09-30): the sites'
recorders keep ten days, production's reset re-reads ten, so a longer replay
measures a library production can never hold. `data/history/<site>/` holds
Home 19-28 Sep and Kozolec 20-29 Sep; older CSVs moved to
`data/history/<site>-older/`. Every bench number in this file from before
2026-09-30 15:00 was measured on 20-22 days and is not comparable with later
ones; the ten-day baseline is stated where it was first measured below.

**Ten-day results on `phase-events` (2026-09-30 evening): starts as all-phase
events (EVENT_WINDOW_INTERVALS 3, EVENT_BALANCE 0.2), a device its start
cluster, runs filed on their own phases, an event closed after the window
plus the sustain time:** Home purity 80.9 %, wconc 56.9 % (hidrofor 72 % in
one signature of 62), the mat 42.6 h / 1.8 h outside heating / caught 53 %,
kiln 454 / 53, pump 452 clean / 17 missing of 665; Kozolec 99.9 / 93.7 %,
fridges 68 of 89; energy Home 674 kWh detected, the pump's signature 1,024
runs at 30 % pump (its cluster still holds another ~900 W single-phase load
on A), Susilna 17 % caught (7 before). The compressor is one three-leg
cluster (315 events, 2.5 kW) and the kiln one A+C cluster (490). Against the
ten-day baseline below: purity +5.4, wconc -1.7, kiln single-legs 65 -> 53,
pump 443 -> 452 clean. With the event closed at the window alone (a leg at
the edge not yet declared): 82.5 / 59.7, kiln 455 / 47, pump 444 - within a
point or two, and the wait is right by construction.

**Ten-day baseline (2026-09-30 afternoon, Home 19-28 Sep with the thermostat,
Kozolec 20-29 Sep), steps per phase, filing on the run's own phases:** Home purity 75.5 %, wconc 58.6 %
(hidrofor 71 % in one signature of 42), the mat 43.6 h counted once against
76.4 h of heating, 1.8 h outside it, caught 55 %; kiln 441 full-size / 65
single-leg (272 pulses in 2 firings); pump 443 clean, 69 long, 43 short, 38
multi, 58 wrong size, 14 missing of 665; Kozolec 99.9 / 93.3 %, fridges 75 of 91
lengths within 25 %, purity 87 %; energy Kozolec 73.4 / 74.1 %, Home 41 / 3 %.
The 22-day numbers (86.2 / 86.0) came from a long warm-up production never
gets: on ten days the pump's runs land in a 4,586-run blob on A (17 % pump by
energy) instead of a signature of their own. Before the own-phases rule the
same ten days gave 76.2 / 46.3 and a C-phase signature carrying A runs.

There is a bench. Use it — replaying costs minutes and a live site costs a
deploy cycle plus a ten-day rebuild.

```bash
python3 tests/run_all.py                                  # 17 files, all must pass
python3 tests/energy_bench.py card [DIAL=V ...]           # THE score: capture / impurity per measured load
python3 tests/energy_bench.py invariance SITE FOLDER      # what the pass slicing changes, session by session
python3 tests/energy_bench.py diff A.json B.json          # two scorecards, load by load
python3 tests/energy_bench.py worth SITE [DIAL=V ...]     # what each meter is worth to every other load
python3.14 tests/forecast_bench.py SITE START END STEP_H [OUT=ids] [MODE=helper|parts] [SINCE=date] [module.CONST=V]  # forecast backtest: hour-/day-ahead MAE, noon-tomorrow kWh; data/forecast/<site>-{prefs,stats}.json from energy/get_prefs + recorder/statistics_during_period (hour, change, kWh) via HA_Configs/tools/ws.mjs
python3 tests/bench.py score kozolec FOLDER [DIAL=V ...]  # purity and concentration (diagnostics)
python3 tests/bench.py score home FOLDER HOUSE=prod [DIAL=V ...]
python3 tests/bench.py kiln FOLDER HOUSE=prod [DIAL=V ...]  # the unmetered kiln
python3 tests/bench.py score SITE FOLDER SUBS=prod        # feed production's meters to the Fleet
python3 tests/bench.py attrib SITE FOLDER                 # where each device's sessions are placed
python3 tests/bench.py pump FOLDER HOUSE=prod             # every hidrofor run, run by run
python3 tests/bench.py lengths KILN_FOLDER PUMP_FOLDER HOUSE=prod   # lengths vs real ones
python3 tests/replay.py data/history/kozolec              # the raw detector output
```

`data/` is gitignored twice over; site history never reaches the remote. Each
site is one folder of per-day CSVs exported from the History panel.

For anything touching clustering or attribution, score it against **sub-meter
ground truth** rather than counting signatures — fewer signatures is also what
over-merging looks like. **The headline is the scorecard** (`energy_bench.py
card`, below): capture and impurity per measured load, hidden, circuits and fed
side by side, and the invariance count - how many sessions the pass slicing changes.
Those numbers belong in the commit message. `tests/bench.py` keeps two
session-count diagnostics, for finding out WHY a score moved:

- **purity** — does one signature hold one device
- **concentration** — does one device land mostly in one signature

A change that raises concentration while dropping purity is a trade, not a
win.

Beware the metric's own trap: changing detection changes the *session set*, so
percentages are over different populations. Only devices with 100+ labelled
sessions are readable; ignore anything with single digits. Better still,
report the ABSOLUTE size of each device's dominant cluster beside the
percentage: if a change removes sessions, a share can rise with nothing
improved, but a dominant cluster that GROWS while the total shrinks means
spurious sessions went and real ones consolidated.

### Sub-meters' reactive power in the replay

Production derives a reactive power for every sub-meter from what it
publishes beside its power; the replay handed sub-meters none, so on the
graphs every step of Home's two 3EMs sat at "no power factor" (the owner,
2026-09-30). The export now pulls the 3EMs' per-phase power factor and
apparent power (`shellys-pf` group; the recorder held them from 2026-09-20 only,
earlier days came back empty), and `replay.py` derives
each `--sub-phases` meter's reactive power from the power factor found beside
its power (same pseudo-device) and hands it to the Fleet as `sub_q`. Device
meters fed with `--sub` still get none: their Shellys publish no factor.
The bench now PINS the house power roles to the site's configured entities
(`bench.SITES[site]["main"]`) instead of letting the replay guess: with
the 3EMs' factors in the history the guess took Hiša's power for the house's
and every count halved (caught in the recapture, 2026-09-30).

### The scorecard (`tests/energy_bench.py`)

One question per MEASURED load - how well is it estimated from the main meter
- in two numbers (2026-10-02; a boiler that used 10 kWh, 9.5 kWh
attributed to it, 9 rightly):

- **capture** = energy rightly attributed to the load / its measured energy - 9 / 10 = 90 %
- **impurity** = energy wrongly attributed to it / all attributed to it - 0.5 / 9.5 = 5.3 %

Measured energy is the meter's above its idle floor (the level it holds a
tenth of the time, by time; the floor's kWh is printed beside it). Attributed:
every session in a signature the load owns - holds >= 50 % of its energy, the
one that would be named after it. A session's energy is the load's where its
meter switched on with it and as far as it drew above its floor meanwhile.
Per site: capture weighted by energy, impurity pooled (wrong / attributed).
Under-reporting is the lesser evil: weigh impurity above capture.

Two truth sets per site, each one owner per signature: **devices** - every
meter that holds no other (Home's seven plugs under the grid connection plus
Blaževa Soba inside Hiša and the office inside Mansarda; Kozolec's ten) - the
headline; and the **partition** - the meters directly under the main and the
remainder no meter reads (Home: Hiša, Mansarda, the seven plugs and the
Delavnica = house - all of them, each held; Kozolec: the ten plugs and Rest),
everything the main reads, once. Three modes: **hidden** (`SUBS=none`, no meter
fed - the main-meter estimate), **circuits** (`SUBS=circuits`, Home's two 3EMs
only) and **fed** (`SUBS=prod`, every meter production reads); the meters are
the truth in all three, read from the history. And two circuit sites,
`home-hisa` and `home-mansarda`: a 3EM as the main meter, scored on the device
inside it (replayed without reactive power: the 3EMs publish no volts or amps).
And **the third site** (`third`, since 2026-10-04): the house production
builds (`bench.PROD_HOUSE`: a third of the inverter less the M1, per phase),
one measured load - the go-e charger's own total (`sensor.goe_216841_nrg_11`, W
every ~5 s; production's OCPP reading has 154 rows in ten days) - its partition
the charger and Rest, hidden and fed. The charger is portable: its truth is
masked over any charge the house did not draw (`_away`: a span at 6 A or more
on a phase whose energy the house, half a minute before to a minute after,
drew under half of) - over 23 Sep - 2 Oct nothing; the trip reported for 24
Sep 14:30-16:50 does not show (every pulse there the house drew at 0.9-1.3x).
The third site's Rest holds a second ~10.7 kW three-phase load for hours (09-27,
09-28, 09-29 ...), likely the second charger (`charger_nova`), unfetched.

**Units** (2026-10-04): the CSVs carry no unit, and until then every reading
was replayed as W - Home's EVBox (OCPP, `Polnilnica`, kW) was a 10.8 W
"charger" with 0.14 kWh of truth, and fed mode handed the Fleet a meter
production never sees. `fetch_history` now writes `units.json` beside a site's
CSVs (each electrical entity's unit from its live state) and `replay.read_csv`
scales by it as production does (`detect.unit_scale`); a folder of symlinks
reads its site's. kW: Home's EVBox and the third site's two OCPP chargers; %: the
SolarEdge M1s' power factors (unread by the replay); everything else W, A, V,
VA, var. Every card before e7f17b3 read Polnilnica as nothing.

`card` replays every site in every mode at SLICE=6, eight at a time
(`BENCH_PARALLEL=4` when another bench shares the Mac); `card SLICINGS=all`
replays each at SLICE=0, 6 and 1 and LIVE=1 as well - four times the replays -
and prints one figure where the slicings agree and each one's (0|6|1|L1) where
they do not, with the invariance count (since 2026-10-02 they agree, so that is
a check now and then, not every card). Either way it prints the share-credited
pair (below) and how runs were closed (the pairing:
share closed by an observed stop vs each inferred kind, how near an observed
stop comes to its start's size, starts never closed - apart from those a run
took in as the rest of its ramp, absorbed (`absorb_until`) - and stops that
closed nothing), and writes `data/scorecard/<commit>-<utc>.json` (`LABEL=x`:
`<commit>-x-<utc>.json`); `diff A B` compares two, every move beside its noise
band. `invariance SITE FOLDER` compares the slicings two by two - 6 vs 1, 6 vs
LIVE=1, and 0 vs 6 on a line of its own - and lists every session one files
and the other does not, or files with another start, end, size or signature,
with its distance to the nearest pass boundary; the list is saved to
`data/scorecard/`, and `card` writes one per site and mode from its own replays. Since
2026-10-02 the slicings agree (*A pass is only a pause*, Known shortfalls):
one call, SLICE=6, SLICE=1 and LIVE=1 file the same sessions into the same
signatures - a difference is now a fault to find, and one slicing is enough
for a score. Before, one call was the most distorted (the sub-meters run over
everything before the grid, nothing filed until the end, so no phase votes
and no meter splits). `energy_bench.py SITE FOLDER [DIALS]` is one replay with
the full per-load detail; `PLANT=watts:on_s:every_s:phase` (or `PLANT=set1`)
adds a square-wave load to the house reading and the grid meter to score a
load no meter watches.

**First scorecard (2026-10-02, 500c275, the ten days; the baseline is
`data/scorecard/500c275-20261002T114540Z.json`), capture / impurity at SLICE=6,
hidden -> circuits -> fed:** Home's devices 24.8 / 33.0 -> 28.8 / 22.8 -> 25.5 /
14.9 % (the workshop boiler 81 / 35 -> 83 / 9 -> 86 / 13, the hidrofor 69 / 22 ->
71 / 26 -> 79 / 18; Susilna, the office, Blaževa Soba and the UPS under 20 %
caught in every mode); its partition 50.8 / 13.0 -> 57.6 / 9.5 -> 54.4 / 8.9
(Delavnica 59 / 9 -> 61 / 8 -> 57 / 7, Hiša 64 / 14 -> 77 / 10 -> 74 / 10).
Kozolec's devices 68.0 / 9.0 hidden -> 86.9 / 4.6 fed (the pond EVSE 56 -> 92 %
caught). A 3EM as the main meter, hidden: Blaževa Soba 8.7 / 31.7 from Hiša, the
office 24.3 / 15.8 from Mansarda. Among SLICE=6, 1 and LIVE=1 the site figures
move up to 3 points; one call is far off (Home fed impurity 33 %). Invariance,
fed, sessions / kWh two slicings file differently: Home 6~1 9,790 of 15,813 /
379, 6~LIVE=1 1,166 / 42, 0~6 14,292 / 521; Kozolec 192 / 49, 25 / 4.6, 335 /
101. Pairing, fed: Home 78 % of runs closed by an observed stop (8.9 % by a
start of the same cluster, 8.1 % in a multi-close), 75 % of observed stops
within 20 % of their start, 2.2 % of steps starts never closed and 7.6 % stops
that closed nothing; Kozolec 88 %, 86 %, 0.9 and 6.6 %.

**The noise band** (2026-10-04): the card is chaotic for small changes - a
signature crossing the >= 50 % ownership line moves a load by its whole share
(the hidrofor -41 in exp1), and the detector itself reacts to readings that
should not matter. So a move is read against the band: `card NOOP=drop:N:seed`
(each reading dropped with chance 1/N) or `NOOP=jitter:ms:seed` (each moved by
up to +-ms) perturbs every series the replay reads, never the truth; `noise
BASE NOOP1 ...` writes each load's spread - max less min at SLICE=6 - to
`data/scorecard/noise-<commit>-<utc>.json`, and `diff` prints `[band~]` beside a
move inside it, `[band!]` beyond (the newest band, or a third argument). The
band is measured on its code: re-measure it after a change that alters the
detector broadly.

**The share-credited pair**, printed beside the headline ("share" c/i): every
signature counts for a load by its share of the signature's energy instead of
all-or-nothing - attributed the load's energy in every signature, rightly that
times its share. Equal to the headline where signatures are pure, smooth
across the line, but it measures something else where they are mixed (Home fed
Delavnica 46.5 % captured, 86.8 % share-credited; Susilna hidden owns nothing
and its share impurity is 96 %). Over the noise cards it trims the band's tail
(per load, a kWh or more: capture max 26.5 -> 12.8 points, impurity 43.5 ->
15.3) but not its middle (capture median 0.7 -> 1.4). The headline stays the
headline: it is what a name would show; read it with the band.

**Overnight baseline (2026-10-04, 77c3159, 23 Sep - 2 Oct; units fixed,
The third site added; `data/scorecard/77c3159-overnight-baseline-20261003T225349Z.json`,
band `noise-77c3159-20261003T231940Z.json` over it and NOOP=drop:10000:1, 2, 3
and jitter:1:1), capture / impurity, hidden -> circuits -> fed, every slicing
agreeing and invariance 0:** Home's devices 19.7 / 15.9 -> 32.9 / 16.1 -> 53.9 /
11.0 (Polnilnica, 137.42 kWh: 30.6 / 12.2 -> 37.3 / 16.5 -> 56.5 / 14.2; the
hidrofor 62.8 / 27.2 -> 64.3 / 26.2 -> 47.0 / 17.9; the workshop boiler 3.1 ->
85.0 -> 85.3 captured; Susilna 0 -> 0 -> 50.8 / 1.5); partition 34.6 / 16.4 ->
53.8 / 12.1 -> 61.8 / 9.1 (Delavnica, 123.62 kWh with the EVBox out of it,
49.2 / 24.7 -> 51.2 / 21.4 -> 46.5 / 16.3). Kozolec devices 89.3 / 3.0 -> 95.0 /
3.2 fed, partition 86.7 / 4.4 -> 91.7 / 3.9. home-hisa 1.4 / 27.9 -> 10.9 /
5.0, home-mansarda 39.6 / 15.1 -> 54.0 / 10.1. The third site's charger 75.9 / 3.0 ->
83.4 / 4.3, partition 77.2 / 13.7 -> 80.2 / 9.0. The band, points of capture /
impurity for the site: Home devices 3.2 / 7.3, 2.6 / 1.9, 5.4 / 3.3 (hidden,
circuits, fed), partition 0.5 / 0.9, 0.8 / 1.5, 3.5 / 1.7; Kozolec <= 0.2;
home-hisa 0.1-2.0, home-mansarda 8 / 1-5; the third site's charger 7.5 / 0.9 hidden,
7.4 / 1.6 fed. Per load at Home it reaches 17-27 points of capture fed (the
hidrofor, Susilna) and 30-44 of impurity on loads with little attributed (the
workshop boiler hidden, the UPS fed): one reading in ten thousand dropped
turned Home circuits' 813 signatures into 828-925, and at Kozolec moves
nothing beyond 2 points but the water pump's partition figure (14.5).

**What a meter is worth** (`energy_bench.py worth SITE`, the owner, 2026-10-02: "how
much adding a sensor to device X improves the prediction for device Y"): none
fed, all fed, and per meter X only X fed (add-one) and all but X (remove-one),
every target scored each time at SLICE=6; two matrices, targets Y by meters X,
capture / impurity points - add-one less none, remove-one less all - beside
the two baselines, written to `data/scorecard/worth-<site>-<commit>-<utc>.json`.
Targets beyond the card's: Home's **bathroom fan** and **blinds** (Shellys
behind Mansarda, West Blinds on the grid connection - so it leaves the
Delavnica; `bench.EXTRA_SUBS`, read from `data/history/home-extra` by the
`EXTRA=` dial, scored from their first reading, 22 Sep; the bathroom fan is a
meter X too), its **ceiling fan** - a MODELLED truth, the watts of the speed
HA assumes for an RF fan (2.3 / 3.5 / 6.3 / 11.2 / 18.3 / 27.0 W at speeds 1-6)
- and Kozolec's **two fridges** - a HEURISTIC truth, `bench._fridge_runs`' runs
off the house reading credited to the session starting within 30 s, scored
over the sessions those runs can judge, pooled as "Fridges" too. The truth is
the same in every replay, so the deltas hold whatever its own error.
`FEED=name` feeds one meter (any number of them) under `SUBS=prod`.

**First worth (2026-10-02, 5779c19, SLICE=6, points of capture / impurity):**
Kozolec - the boiler's meter is worth +40 capture to the pond EVSE (its 3.5 kW
runs no longer boiler-shaped; -40 / +3 when removed) and -19 impurity to the IR
panel; the water pump's meter costs the car charger 27 points of capture
(-27 / +0 added, +27 back when removed) and the pump itself 41 added alone, yet
removing it from all loses 48 / -16 - the meters interact; the well pump is
found only with every meter fed (73 %, impurity 46 %), and no meter helps the
fridges beyond a point or two (Fridges 74 % caught, 26 % impure hidden; Fridge A
0 % in every mode - its surge start never pairs, Fridge B 75 %). Home - the
Mansarda 3EM is worth +22 capture to Mansarda itself and +13 / -6 to the office
(remove-one -11 / +4); the Hiša 3EM +4 / -6 to Hiša, -28 impurity to the
workshop boiler (+23 back when removed) and -16 to Susilna; the hidrofor's own
meter +11 / -7 (remove -9 / +8). The bathroom fan (27 Wh, ~15 W for ~2 h) is
caught in no mode, and its meter changes no other load by a point - nor does
the office plug change it: at 15 W it sits under the detector's floor, with the
ceiling fan (22 Wh at 2.3-3.5 W, 0 % everywhere). The blinds (19-28 Wh, 100 W
for seconds) score 0-13 % and their deltas are noise. Susilna LOSES 7 points
of capture under any one meter fed and 11 under its own: fed, its sessions
leave the main-meter signatures the hidden run credited. Runtimes: Kozolec 22
replays in 21 s, Home 26 in 21 min (python3.9, beside another bench).

The old energy score was this with fewer loads: its precision and recall are
1 - impurity and capture, identical on the same replay (checked 2026-10-02;
exact ties - two meters each claiming a whole session - now break in
production's meter order, which moved 0.3 kWh from the workshop boiler to the
office).
Its earlier numbers: 2026-09-30, 20 days, Kozolec 72.7 / 67.5 % (the boiler
68 -> 82 % precise, the water pump 13 -> 52 % caught); Home 55.8 / 10.1 % - the
blob, one signature of 16,876 runs and ~470 kWh holding 4-9 % of every device.

### How to tune without fooling yourself

- **Sweep the dial; a single trial is not a verdict.** A degradation says that
  one setting is wrong, not that the mechanism is. Best-fit pairing, the
  `alike` spread and the sustain guard were each dismissed once on a single
  bad point; two had interior optima and one was the biggest win found.
- **Solve each site on its own, then compare.** Where both agree, the value is
  real. Where they disagree, look for the measured quantity that explains the
  difference before averaging them. `NOISE_REL_FLOOR_FACTOR` was found this
  way: Kozolec's best floor and Home's best factor back-solved to the same 1.5.
- **Tune on one window, validate on days the tuning never saw.** Fetch the
  newest days with `tests/fetch_history.py` (it skips days already on disk)
  and keep them out of every sweep until the choice is made.
- **Dials interact.** Once a large change is decided, re-sweep the small ones
  on top of it rather than against the old baseline.
- **Ground truth only sees loads that have a sub-meter.** Home's kiln has none,
  so a change can raise every score while wiping it out. Check unmetered loads
  of interest separately at every point of a sweep.
- **The pass slicing is no longer noise** (2026-10-02, see *A pass is only a
  pause*): run `invariance` after any change to the stream, the filing or the
  fleet; a session it lists is a fault. Before, it was the noise floor
  it. On the same code the pump's clean runs move 482-490 and Home's energy
  precision 76-88 % between SLICE=4 and 8 (*A meter's cadence*, 2026-10-02).
  `card` shows each slicing's figure where they differ; a difference inside
  that spread is not a result. `invariance` lists the sessions behind it.
- **Read a move against the noise band** (`diff` marks it, 2026-10-04). At
  Home a single dropped reading in ten thousand moves a load up to 27 points;
  a per-load move inside the band is not a result, and the site figures are
  the readable ones. Kozolec's band is a fraction of a point.
- **Point sweeps at a fixed folder.** Build tuning and hold-out folders of
  symlinks and never replay `data/history/<site>` while a fetch is writing to
  it - runs started a few seconds apart will read different days.
- The laptop runs a Home replay in about two minutes on 0.1 GB, and ten at
  once. The sites are Raspberry Pis and a rebuild takes a ten-day backfill.
  Tune here; deploy only to confirm. What a replay builds from the CSVs alone
  is kept in `~/.cache/load_insights_bench` (`replay._cached`): the parsed
  series, Home's production house reading and the scorecard's truth series,
  each keyed by the files it came from and the source that builds it - a
  hidden Home replay 2-3 min -> 26 s; `python3.14` runs it identically and
  faster still.

## Known shortfalls

### Confirmed, unfixed

**The bench read a slightly different input than production at Home.** The
replay read the owner's template sensor; production sums grid and inverter itself.
Values agree to 0.1 W, but timestamps and change-only recording differ, and
the scores did too (purity 67.6 vs 68.4 %). Replay Home through
`combine()` - `bench.py`'s `HOUSE=prod` - so it matches production. Checked
against the live generation-12 rebuild (2026-09-23): replayed from the same
start instant, the bench's kiln signatures came out x338 / x39 against live's
x335 / x38. From midnight instead they came out x319: the library's history
before a load shows up moves signature counts by tens. Compare runs over the
same span only.

## Bench rules from working notes

- **Judge groupings on known truth, too** (from the owner, 2026-10-15). Every edge or grouping change is also scored against the known-truth set next to the edge card: Le Hottie's HA program windows (microwave vs oven elements, Mansarda C), the kettle mornings and the 12 Oct panini-press run (Mansarda B), and plugs replayed hidden from the detector so their real steps label the grid's edges. Report the truth-set results first and the proxy scores second. Compare methods at equal group counts: "mixed" favours methods that split into many groups or ignore the property that decides, so a pattern grouping that looks purer may only be fragmenting. Edge-pattern snippets lost on this set (2026-10-15); keep the scalar surge and settle.
- **Stage 1 is edges and grouping only** (2026-10-14). While the cross-meter edges work is in stage 1, judge changes on the edge card alone (conservation, coverage, invariance, tightness, mixed, duplicates, count, mirror), and do not run or cite the energy card as pass or fail. A device-assignment problem (a meter step claimed twice, say) is recorded as "later", not proposed as the next step. See `dev/TODO.md`.
- **CPython, not PyPy** (2026-10-02). PyPy was tried on a Home replay and declined: it stops at 3.11, and the gain was not worth it. Benches run on python3.14; speed comes from the bench and detector code.
- **The bench PC is the owner's desktop** (2026-10-04). When he says he is using it, start no new jobs there: run cards on the Mac (python3.14, `PYTHONHASHSEED=0`, at most about 8 processes in total) and tell running agents the same. Go back to the bench PC only when he says it is free. See `docs/bench-machine.md`.
