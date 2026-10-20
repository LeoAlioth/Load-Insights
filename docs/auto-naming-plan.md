# Auto-naming of confidently detected loads (INS-21)

Owner, 2026-10-20: when a load is detected with high certainty and a good guess of what it is, name it
automatically ("Fridge 1", "Heater 1") and put it on the Energy dashboard. Built the same day, **not
deployed, not exercised on a live Home Assistant**. Code: `insights/autoname.py` (pure: recogniser, bar,
hysteresis, numbering, tombstones, the dashboard's list), `autonaming.py` (Home Assistant: store, rename,
words, dashboard write), hooks in `detection.py` (`DetectionRunner.autonamer`, called hourly from
`_check_named`), the two options on the detection page, `auto_named` on the detected-loads sensor.
Tests `tests/test_autoname.py`; the calibration table is `python3 tests/autoname_bench.py`.

## Decisions (owner)

1. Kinds: a FRIDGE or FREEZER (named "Fridge N" - not told apart) and a HEATING ELEMENT ("Heater N"). Nothing else.
2. The bar is set from real data (below), not guessed.
3. Two options, both ON by default and separate: **Auto-name confident loads** and **Add auto-named loads
   to the Energy dashboard** (meaningful only while the first is on; with it off, nothing is auto-named; with
   the second off, auto-names are made but nothing is written, and turning it off removes only the entries
   Load Insights itself added).

## Design rules

- **Marker.** Our names live in a store of their own (`load_insights.<entry>.autoname`: `marks` name ->
  kind, descriptor, since), shown as the sensor attribute `auto_named` and as a line on the load's naming
  page. A name an owner gave is never in it.
- **The owner always wins.** Only a signature with NO name is ever named; an existing name is never
  overwritten and is taken as taken when numbering (also meters' and dashboard devices' names). Once an hour
  `reconcile` compares our marks with the names the library holds (a name a reset is carrying counts as held):
  a name of ours that nothing wears - renamed away, forgotten, joined elsewhere - leaves the marks and a
  **tombstone** (kind, phases, watts, duration, name) is stored; a load alike (same phases, within 15 % in
  watts and a factor 2 in length) is never auto-named again, and the old name is not reused. A detection
  reset clears the candidates (ids mean nothing any more); `forget_names` clears the marks too; tombstones stay.
- **An owner's load claims its pieces.** The detector splits a load over signatures. A candidate is skipped
  when an owner-named signature of the same phases and size (within 25 %) exists: for a fridge only if that
  name says fridge (`classify.appliance_from_name`), for a heater any owner name. (Andrej's "Hladilnik A 70 W".)
- **Numbering** per kind per site, lowest free n, in the HA language of the entry: `Fridge {n}` / `Heater {n}`;
  Slovenian `Hladilnik {n}` / `Grelnik {n}`, stored under `exceptions.autoname_fridge|heater.message` in the
  translations (read with `translation.async_get_translations`, English if missing). Look-alikes (same phases,
  within 25 % in watts) share ONE name, because the detector splits one fridge over several signatures
  (Kozolec's two over about thirty): a second look-alike is never "Fridge 2", only a load of another size or
  phase is. This under-counts two equal fridges rather than inventing fridges; the owner renames.
- **Hysteresis** (once an hour, a day counts once): a load is named after clearing the bar on `DAYS_TO_NAME` = 3
  distinct days; a name is taken back (the signature unnamed, no tombstone) only after `DAYS_TO_REVOKE` = 7
  consecutive days of missing it; a clearing day ends the streak. Never while history is re-read (`refiling`)
  or the detector has not caught up.
- **What is named**: only signatures the naming page would offer (`Fleet.namable`: unnamed, seen twice, 50 Wh,
  not inside a meter that holds one device) - a metered device is never touched.
- **Energy dashboard.** Verified against Home Assistant's `homeassistant/components/energy/data.py`:
  `EnergyManager.async_update({"device_consumption": [...]})` merges the given keys and saves (60 s delayed
  save), does NOT validate, and replaces the whole list, so the list is read from `manager.data`, changed in
  the pure `plan_dashboard` and written once (`write_dashboard`, tested with a fake manager). An entry is
  `{"stat_consumption": <the named load's energy entity id>, "name": <name>}`: no `stat_rate` (the owner's
  own entries for detected loads, the kiln, are energy-only as far as is known; adding the power entity is a
  later choice). `added` records exactly the statistics we added; only those are ever removed (the name went,
  or an option is off); an entry the owner made is never touched; no duplicates (a statistic already listed by
  anyone is left); an entry of ours the owner took out goes to `declined` and is never put back. Nothing is
  written while the dashboard has no preferences at all, or while a named load's sensor is not made yet.
  `energy_site` already leaves Load Insights' own entities out of the meters, so the entry is not read back as
  a meter. **Not changed: `insights/model.py`** (the reader; Load Juggler's `energy_prefs.py` copy stays in sync).
  The earlier statement that Load Insights only reads the dashboard no longer holds: it now also writes the
  device list (this and `AGENTS.md`).

## The bar (every number is policy; calibrated 2026-10-20)

Shared: evidence >= 0.70 (the naming bar `DEFAULT_MIN_EVIDENCE`). Fridge: one phase, 30-400 W, runs 6-90 min,
start-to-start 10 min - 3 h, run/period 0.1-0.85, >= 40 runs, >= 6 cycles a day while seen, >= 6 hours of the
day with energy, under 1.6 levels, interval jitter <= 0.6 where known; the power factor is NOT used (untrusted
at 4 of the 5 fridge-like signatures). Heater: the classifier's own guess is `a heating element` with
confidence >= 0.6, >= 500 W, >= 8 runs, seen over >= 5 days, one level, and the appliance guess is not
cooking or a workshop machine (a kettle or an oven would be "Heater 1").

Data: the live exports of 2026-10-20 (Home 563 signatures, Kozolec 200, Andrej 200) with the owner's names, and
production-style replays of Home's and Kozolec's last ten days (every meter's signatures, no names). The
exports carry no first/last-seen or level count (read from the description, or skipped).

### Hand-named loads (export)

| site | load | clears? | why not |
|---|---|---|---|
| Home | Le Hottie (kiln pieces), 1.7 kW, 10 runs, evidence 0.89, 6.5 days | **heater** | - |
| Home | Le Hottie, 5 runs / 5 runs / 8 runs / 39 runs | no | 5 runs and under 5 days; evidence 0.53; the 39-run piece has an untrusted power factor (no guess) |
| Home | Hladilnik, Zamrzovalnik (Mansarda#1, #11) | not in the export (sub-meter library); their replay twins (Mansarda #3, #4) **clear as fridge** (evidence 0.85 / 0.71, 66 / 47 W, 19 / 22 min every 41 / 90 min, 24 busy hours) | - |
| Home | Kompresor, Sock Eater, Kettle, EcoFlow, ... | no, and meant not to | not fridges or heaters |
| Kozolec | Bathroom IR Panel (517 W, evidence 0.73) | no | power factor untrusted (pf 0.92 +- 0.12): the classifier gives no guess |
| Kozolec | Boiler (1.27 kW, 1353 runs) | no (in the fresh replay, evidence 0.78 and a trusted power factor, #12, it **clears**) | in production evidence 0.50 and pf 0.92 +- 0.16: no guess |
| Kozolec | Apartment Fridge, Bottom Fridge (Inverter#3, #6) | not in the export; unmetered, the detector splits them over ~30 signatures of evidence 0.50-0.67 (#33, #8, #80, #15, #25, #63: 52-83 W, 35-90 min, 20-81 runs) | none clears, by design |
| Andrej | Hladilnik A 70 W (#8) | no | the signature holds 2 runs (the real fridge, 106 runs of 7.5 min every 21 min, is #72, unnamed) |
| Andrej | the two "Neznano" | no, and meant not to | not fridges or heaters |

Why the Boiler and the IR Panel cannot clear: the heater test needs the classifier's guess, which needs a
trusted power factor. Loosening it to "mean pf >= 0.9, steady, >= 100 runs" would also name Home's Hidrofor
(902 W, pf 0.93 +- 0.14, 1452 runs): it is indistinguishable from the Boiler on every field the export carries.
Left to the owner (a better power-factor error bar in the detector would give both a guess).

### Unnamed loads that clear (export; the replay's in brackets), with what they look like

- Home, export: 6 heaters, all 1.2-6 kW pulses of 25-120 s repeated 8-46 times over 7-21 days: #3537 and #3569
  (A+C, 6.0-6.1 kW, 25 s pulses - probably the clay kiln, which is named only inside Hiša; a judgement, not a measurement), #3421 (B+C 1.6 kW), #1513 (A+B
  1.3 kW), #3079 (C 1.9 kW), #3098 (B+C 1.2 kW) - daytime programme elements, probably like Le Hottie's, split
  over signatures. In use the two kiln pieces share "Heater 1" and the rest become Heater 2-5; an owner-named
  load of the same phases and size would claim them.
- Home, replay (every meter): 2 fridges (the Mansarda pair, hand-named in production, so claimed), 10 heaters:
  Hiša #22 / #67 (C 580-638 W, 125 and 313 runs: probably the floor heating, "Talno gretje", named in production),
  Mansarda #6 / #64 / #29 (1.5-1.7 kW pulses: probably the oven's), main #89 (650 W), #148 (B 2.2 kW), #175 / #1330 (A 1.7
  kW: probably Le Hottie), #2251 (B 2.2 kW).
- Kozolec: 0 in the export; the replay's Boiler (#12, hand-named in production, so claimed).
- Andrej: the real fridge #72 (72 W, 450 s every 1237 s, 106 runs; hours 20-03 only, the sun hides the day) - claimed
  by "Hladilnik A 70 W" (83 W, same phase), so NOT named; and a heater #125 (C 1.8 kW, 52 s, 12 runs, 5.2 days, at night).

So at the three sites, after the owner's names claim their pieces, a handful at most: Home's kiln and oven
elements (named Heater N), Andrej's #125.

## What is left / for the owner

- Not deployed, not run on live HA. Check on a site: the hourly check names only after `caught_up`; the
  entity of the new name appears at once (`_follow_names`) and the dashboard entry follows the next hour.
- A named fragment is only a fragment: naming one of Kozolec's fridge pieces would give a "Fridge 1" meter of part
  of the fridge's runs. That is why Kozolec's fridges are not named (evidence under 0.7); the detector's split of
  a fridge is the cause (`tests/AGENTS.md`, the fridge bench).
- The heater bar's reach (Boiler, IR Panel) needs a trusted power factor first (above).
- Load Juggler: nothing needed (`model.py` unchanged).
- Not built: the rate statistic (`stat_rate`) on the dashboard entry; a freezer told from a fridge.
