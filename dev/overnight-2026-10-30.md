# Overnight 29-30 Oct 2026: memory and performance

The owner's brief (29 Oct, 23:59): memory and CPU first - Load Insights, Load Juggler and Home's HA
install; HA may be restarted and integrations disabled temporarily (all re-enabled after); commit and
push allowed; stop at 85 % plan usage. Once at a floor, detection quality; code clean-up along the way.

## Home's HA install (CM4, 1.98 GB RAM; the CM5 8 GB not in yet)

- 22:28: Core 1,205 MiB (64 %), ~30 % CPU; system 84 % RAM, swap 1.9 GiB used; HAGHS: I/O pressure
  8.3 %, 75 zombie entities, DB 5.9 GB. Add-ons ~220 MiB (Matter 98 - needed for the Air Guard CO2,
  Zigbee2MQTT 43, VS Code 36, File Browser 21, Mosquitto 18, DB Stats 4, SSH 1). The owner stopped VS
  Code and File Browser.
- The Profiler's first full object count (`start_log_objects`) restarted Core (memory). Not to be run on
  the CM4 again. Profiler integration left installed, idle.
- Restarts measured (Core memory, Supervisor's figure, includes file cache):
  - all on, after a restart: 474 -> 630 -> 1,271 MiB in two minutes, then 750-1,060 settled;
  - Load Insights disabled: 364 -> 855 -> 907-972 MiB settled, CPU 1-7 %;
  - Load Insights re-enabled: 1,061 / 1,216 / 991 MiB while it loads and backfills (CPU ~28 %), then
    919-1,057 MiB, CPU 1-6 %;
  - 25 non-essential integrations disabled (HACS, Spook, Synology, Electrolux, ABRP, Steam, qBittorrent,
    Plex, Sonos, Spotify, Tronity, travel time, CO2 Signal, LocalTuya, Radio Browser, DLNA x2, IPP x2,
    WLED, UPnP, Google Assistant SDK, Roborock, NUT, Epson): 874-988 MiB against 920-1,060 - worth
    ~50-100 MiB together. All re-enabled; snapshots in HA_Configs/home/snapshots/2026-10-29.
  - So: Load Insights costs CPU and memory mainly at start / backfill; its steady load is a few percent.
    Core's ~900 MiB without it is everything else; no single integration stands out.
- Removal candidates (not removed; the owner decides): Adaptive Cover Pro's "Building Profile Wizards
  attic" (no entities), DLNA servers x2, Radio Browser, python_script's hello_world / weather_alarm (not
  called), Google Assistant SDK (if unused); a stale ignored `matter` entry. Modbus: not configured by the
  owner - loaded as a dependency, harmless. Moonraker: switched by an automation - keep.

## Load Insights

Measured: a ten-day Home replay (fed, SLICE=6) under tracemalloc holds 326 MiB of detector state (the
bench's own series and reactive dicts aside): the steps store ~36 MiB (85,282 steps), the day's readings
26 MiB (372,000 tuples), order 72,313 pairs ~24 MiB, co-runs 46,852 pairs, finished runs, the meters' rows
~12 MiB. The stored JSON is 30 MiB.

Committed and pushed (bf467f9, deployed 01:06, both sites restarted, generation 23 rebuilding):
- the recorder read in compressed rows instead of State objects (the start / backfill burst);
- the day's readings as array('d') columns (26 -> ~4 MiB);
- order pairs faded under 0.05 pruned (the store only grew).
  Two-day digest identical (7,487 runs, d73f5cf8c271).

Committed and pushed 01:25 (a674bfc) and 01:30 (2743f09):
- co_run fades and is pruned like order (Home fed +2.4, hidden -1.5, circuits -1.2 in band, Kozolec same);
- steps rows as tuples; Fleet.footprint in the diagnostics;
- forecast sensors keep all attributes out of the recorder (~20 MB of rows a day at Home).

CPU (a live pass profiled: the two-day Home slice, every meter fed, its last day in one-minute passes):
Fleet._sub_total was 97 s of 278 (its cache missed on every new reading, so each pass summed every kept
reading of each meter again), _keep_rows 20 s (a sort of every kept moment per pass), the kept voltages and
inputs ~10 s (a dict rebuilt and sorted per pass). All three made incremental; the same runs filed live and
in six-hour slices (7,484, a74c450ef786); the live two-day replay 120.7 -> 48.5 s (-60 %). Pushed.

Deploys: both sites install a pushed build and restart by themselves (their update automations; the
Supervisor logs a requested restart, no watchdog). Kozolec rebuilt generation 23 and caught up by 01:25;
Home's recorder holds 4 days, its rebuild runs at ~12 min per six-hour slice (~05:00 to catch up). The
compressed recorder read works (sessions filed during the rebuild). Open: after Kozolec caught up, the name
"Pond EVSE" (twice) is still waiting (`awaiting_name`) - no new signature is `alike` the old one yet;
likely the EVSE's runs now carry their step downs as levels. Cosmetic (a metered device's energy is its
meter's); to look at.

Quality item found (not built yet): a leg of a multi-phase grid event goes to a meter whose own rise is
"all of it" within MATCH_EDGE_REL (15 %): Home 7 Oct 18:16:06, the EVBox's phase C leg (+793) taken whole
by Hiša's own load starting with it (+689), the EVBox's ~104 W share with it - Hiša's later stop ended it.
Better: split the leg as _split_rise does a single-phase start.

## Load Juggler

One coordinator per site at a configurable interval, no recorder reads; light - not profiled yet.
