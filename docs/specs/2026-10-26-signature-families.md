# Signature families: one named load above its signatures (spec, for review)

Status: draft 2026-10-26, not built. Decided with the owner (2026-10-26): (1) a **family** is stored and is what gets **named**, not a signature; (2) everything in one family spans the **same phases** (a device with parts on several phases is joined by giving the families the same name); (3) two families that are very probably one load may be joined **automatically**, beside joining them by naming them alike; (4) named-load entity ids and the Energy dashboard entries stay; (5) the spec is written first and read during the build; (6) a family is per meter, as signatures are (one signature reads the same reading across meters); (7) `name_load` takes a **family id only**: every signature has a family, most families have one signature; (8) the step groups keyed by state stay as they are; (9) migration also joins the several signatures of one name into one family when they are otherwise alike; (10) the weight thresholds are the existing ones to start with and **will need tuning**; (11) a brand-new family born in a rare value stays born-in until established (section 4.4).

## 1. Why

Home, 22-25 Oct 2026. A rare value of a setting in force (the washer's cycle phase: Dry, Wash, Rinse) makes an ordinary signature refuse a run (`Signature.files_in`), so a run made then is born into a new signature keyed to the value, and that fragment is judged only after the value has come round `INPUT_MIN_EPISODES` (3) more times. For a washer used once a day that is days. Measured:

- The kiln's firing on the evening of 25 Oct (local time was UTC+1 from 03:00 that day, so the evening hours are CET) drew about 17 kWh by 24:00 UTC: 191 pulses of about 89 Wh, 7.8 kWh seen on Hiša's A and 9.2 on C (3EM, pulses over 2 kW, integrated). `sensor.pec_za_glino_energy` recorded 2.4 kWh. Live Hiša holds 7.6 kWh of the firing in 9 signatures; the rest (about 9 kWh) is in no signature at all - a detection shortfall separate from fragmentation, to be looked at after this work. A replay of 22-25 Oct files its 5.7 kW pulses into five Hiša signatures, four born in `sensor.aeg_sock_eater_cyclephase` = Dry / Wash / Rinse (#606, #611, #618, #619); none holds the firing.
- The same rule splits the fridge and freezer runs of 24 Oct (a third of normal energy), and left 193 signatures born under `fan.ceiling_fan_rf=33 %` after the generation-22 rebuild.

The detector learns, per signature and per setting, how many runs fell in each value (`n`) against how many chance gives (`e`): `Signature.inputs`, `input_of`, `input_lift`. That is the right measure of whether a setting matters to a load: the washer's cycle phase matches the washer's own heater and motor edges cleanly and means nothing to the kiln. It is used too late (after the fragment exists) and on one signature at a time (so a load's evidence is cut into pieces). A grouping above the signature fixes both.

## 1a. How filing really works (read from the code, 2026-10-26: this supersedes the sketch in section 4)

A run is filed by its start cluster, its **device** (`Detector.device_of`): `_device_signature` returns the signature most of that device's runs went to on the run's phases that may take a run in the value in force (`files_in`). None means a new signature, born in the value. `_merge_devices` then merges the signatures of one device. There is no power-and-duration lookup in the filing. So a family is, in the code, **the signatures of one start cluster on one phase set** (plus several signatures sharing a name that are otherwise alike): `Detector._join_families`. The step-1 `alike` lookup of section 4 is dropped; `alike` is used only to join same-name signatures.

## 2. The model

A **family** is one load as one meter sees it: signatures that differ only by a setting's value or by length. Stored on the detector (per meter, as signatures are): `Signature.family` (every signature has one), `Detector.next_family`, `Detector.family_eps`. The family's member list and its pooled weights are derived from the signatures' stored `inputs` rows, not stored twice; its name is the one every member wears (naming a family writes it to all members, and a new member inherits it), so signatures keep a `name` for the code that reads it.

| field | meaning |
|---|---|
| `id` | stable, never reused |
| `phases` | the phase set every member spans (one per family) |
| `name` | optional; the one place a name lives |
| `members` | signature ids; each signature belongs to exactly one family |
| `weights` | per setting: `{value: {n, e}}`, pooled over every member's runs (the same decayed counts as `Signature.inputs`, kept on the family) |
| `default` | the member that takes a run when no setting splits the family |

A signature keeps its power, duration, power factor, energy by hour and run memory. It loses `name` (the family has it) and, in time, `born_in` / `takes_in` / `born_judged` (section 4).

Not families: the **load** of the terms list ("a named signature, or the several that share a name") becomes "a named family". Several families sharing a name stay possible (the same-name join across phases).

## 3. Weights

For a family F and a setting S, per value v: `n` = F's runs filed while S read v (time-faded as `note_input`), `e` = the value's share of the time summed over the same runs. The **weight** of S for F is the lift of its best value, `n / e`, with its share `n / runs` and its **coverage**: the share of v's episodes F ran in (`ep_seen / since`, as `_judge_born` counts). S **splits** F on v when F has `INPUT_MIN_RUNS` runs, the share is at least `INPUT_MIN_SHARE`, the lift at least `INPUT_MIN_LIFT` and the coverage at least `INPUT_MIN_COVERAGE`: today's thresholds, now read on the pooled runs. The kiln's lift on the washer phases is about 1; the washer heater's is far above 3. No new constant.

## 4. Filing a run

1. Find the family: the best `alike` match among families of the run's phase set, **ignoring every setting**. (`alike` loses its born-in gate; the length split and power / pf / duration tolerances stay.)
2. Inside the family, the settings that split it (section 3) choose the member: one member per value of a splitting setting, one default member for the rest. A member for a value is made when the first run in it arrives; its power, duration and pf are its own.
3. A setting that does not split the family is recorded (its `n`, `e` grow) and does not choose a member.
4. No family matches: make one, with one member. If the run is in a rare value, the member is **born in it** (as today) until the family has `INPUT_MIN_RUNS` runs and its weights say whether the value matters: a first washer heater run still gets its own family, the first run of a brand-new load never merges into an unrelated one. Once established, section 3 decides.
5. A family that shows a dependence it did not have (its weight crosses the thresholds later) **splits** the default member's later runs on that value; earlier runs stay where they are. A dependence that disappears **joins** the members back (`swallow`, as `_judge_born` does for twins).

This replaces `files_in`'s refusal of runs in a rare value, the born-in judging by episode count and the `apart` / twin lift test; it keeps `input_lift`'s regime-change guard (a value that came once and stayed is no dependence: a twin that stopped before the fragment began).

## 5. Joining families

By name: giving two families the same name makes them one load (any phases). Automatically: two families **of the same phase set** join when they are very probably one load. The test is the existing twin test applied to families (`alike` on the default members, within the tolerances `_judge_born` uses for twins) **and** one of:
- one of them stopped before the other began (the 25 Oct fridge fragments, `input_lift`'s rule 1), or
- they alternate in time with no overlap and their pooled weights show no setting that tells them apart.

Two named families with different names never join. A join keeps the older id and the name; the other's id is recorded in `_moved` as signatures' are. The threshold is a count of measurements (runs, episodes), never an absolute number of watts or seconds.

## 6. Storage and migration

`DETECTOR_GENERATION` is bumped. A rebuild reads the recorder as now, files every run through section 4 and keeps names through `orphan_names` (a named signature's name goes to the family its signature lands in). Existing stored signatures are loaded as one-member families (phases from the signature, name from `Signature.name`); signatures that share a name join one family when they are otherwise alike (same phase set, `alike`), the rest by section 5 on the next daily repair, so a library that has not rebuilt yet still works. `to_dict` / `from_dict` carry `families`; a stored file without them reads as above.

## 7. Where it touches (to be verified against the code during the build)

- `detect.py`: `Signature.name`, `alike`, `swallow`, `files_in`, `_judge_born`, `_file`, `_device_signature` (a sub-meter's `prefer`), `_merge_devices`, `_reclaim` / `orphan_names`, `_by_state` and `STATE_SUFFIX` (the step groups keyed by state: unchanged, the owner called their splitting "expected and welcome", 2026-10-23), `_recover_day` (a regular cycler's members are its family's), the daily repair.
- `Run.signature_id`, `Run.parent_signature`, `start_home` / `_vote`: stay signature ids; the family is looked up through the signature.
- `detection.py` (`name_load`, named-load sensors, the Energy dashboard writer, `autoname.py`): key on the family; entity ids stay.
- `classify.py`, `planning.py`: read a family's members' pooled energy and hours.
- `__init__.py` / `services.yaml`: `name_load` takes a family id only.

## 7a. Verified before building (2026-10-26, live Home diagnostics + replay)

- Kiln: live Hiša holds `#572` (5.85 kW, 134 runs, takes `fan=33 %` and cyclephase `Wash`) and nine fragments born in `fan.ceiling_fan_rf=33 %`, cyclephase Rinse / Spin / Dry (#1513, #1536, #1543, #1547, #1552, #1569, #1592, #1602). The name sits on `#561` (last seen 21 Oct); the firing's 4.28 kWh went to the unnamed `#572`. 114 of Hiša's 194 and 140 of Mansarda's 198 signatures are born in a value.
- Fridge / freezer, 24 Oct: the named #1 and #2 drew 77 and 117 Wh; unjudged fragments #940 and #957, born in `select.aeg_le_hottie_program=Bread Baking`, drew 97 and 124 Wh. That explains about 220 Wh of the shortfall, not all of it (the rest is not fragmentation).
- Pooled weights from the stored `inputs` rows: kiln family (#572 + fragments) vs the washer's cycle phase: lift 1.5, share 0.18 (no split); fridge 1.2 and 2.4; freezer 2.4. The washer's own loads read lift 15-65 at share 0.8-1.0 (Delavnica #285 / #283 / #296 / #402, Mansarda #75 / #76 / #101): the weight separates them cleanly. A single kiln fragment alone read lift 7.4 at share 1.0 (#1552): pooling is what makes it right.
- Risk: `fan.ceiling_fan_rf=33 %` reads lift 6.8-9.8 on the kiln and fridge families; only the share (0.55 and 0.18-0.20, under 0.6) and the regime-change guard keep it from splitting. The kiln's 0.55 is close: tune with the bench.
- Mansarda #245 'Sock Eater' (67 W, 1040 s, born in `fan.air_purifier=11 %`, last seen 18 Oct) has the fridge-freezer's shape: probably a wrong name; to be looked at, not part of this work.
- Touch points by reference count: `born_in` 30, `takes_in` 10, `files_in` 3 (all `detect.py`); `.name` / `"name"` in `detect.py` 35, `config_flow.py` 19, `sensor.py` 4, `autonaming.py` 5, `insights/autoname.py` 5, `detection.py` 2, `__init__.py` 3; `successor_id` `detect.py` 7 and `overview.py` 1.

## 8. Bench and acceptance

- Truth: the kiln firing of 25 Oct (`HA_Configs/tools/ws.mjs home` + `history/history_during_period` on `sensor.hisa_phase_{a,c}_active_power`, 10 kWh per phase; the replay is `data/history/home-1026`) and 24 Oct's fridge and freezer energy: the family holds them, the named load's energy is within the measured.
- The plug scorecard (`energy_bench.py card`), hidden, circuits and fed, Home and Kozolec: capture and impurity of the named devices must not get worse beyond the noise band; the washer heater test (`test_a_heater_that_runs_only_while_washing_gets_its_own_signature`) and the fridge fragments test keep passing.
- Signatures born under `fan.ceiling_fan_rf=33 %` after a rebuild: far fewer than 193.
- Invariance 0 at every slicing and across a restart (`RESTART=1`).

## 9. Stages

1. Families stored, weights read on pooled runs, filing by section 4, names migrated, generation bumped. The old born-in rules stay in place until stage 2 (staged work keeps every stage; judged together).
2. Remove `files_in` refusal and episode judging; automatic joining (section 5).

## 10. Not decided

- Whether a family's `default` member is rebuilt when its runs thin out.
- How the diagnostics show a family (one row with its members, or a row per member).
- The UI wording ("load" / "family") on the naming pages.

## 11. Addition to the daily re-process: run length and time of day as hints (decided with the owner 2026-10-26; not built)

The daily re-process (`Fleet._repair`) is the one place that sees all of a day's runs, their lengths and their clock hours at once. It gets two **hints**, neither a split by itself:

1. **Run length.** Per family, the lengths of its runs over a longer memory than the signature's last `RUN_MEMORY` (12): a small log-length histogram kept with the family, decayed like the input rows. The repair asks whether the lengths are two populations (a mixture on log-length, medians at least `MATCH_DURATION_FACTOR` apart, each with at least `SPLIT_MIN_RUNS` runs of weight) and gives each run a soft label for the long mode. The present `learn_split` looks at the last 12 runs and takes the largest gap; a minority load (1 run in 13) never has 4 runs in the window, and on all of a signature's runs the largest gap is an outlier, not the minority's mode (below).
2. **Time of day.** Per family, the clock hours its runs start in, kept per length mode: a mode that sits in a few hours (the boiler, 12:00-15:00) while the rest spreads over the day is a second hint.

A hint only **proposes**: the repair splits a family (a new member signature for the minority mode, as `learn_split` does for the length side) when the hints together clear the evidence bar, with the family's input weights (section 3) counting as a third hint where an input exists (Load Juggler's tank status, below). One hint alone never splits. The repair re-files only its own day's runs into the new member; learning (count, power, runs) is untouched, as in `_cap_day` and `_recover_day`. Stored per family and persisted with it.

What was measured (Home 15-25 Oct, hidden, grid phase Y, the signature at about 2.06 kW; the boiler plug's own runs as truth): 145 runs, 11 the boiler's (273-992 s, median 472 s), 122 Hiša's element on phase b (median 140 s, 80 % of them 110-179 s), 12 on no meter. A cut at 272 s puts all 11 boiler runs on the long side and 19 of the other 134 with them (11 of 30, 37 % pure); at 300 s 10 of 11 and 18 of 134. The largest log gap over all 145 lengths is 23 to 41 s (an outlier), so the gap rule alone does not find it. Clock: the boiler's 11 runs start 12:00-14:59; Hiša's element starts 10:00-21:00 (26 of 122 in 12:00-14:59). Length and hour together are expected to be much purer than either: to be measured before the bar is set.

Load Juggler's own signal is fed as an input from the same date: `sensor.server_room_hot_water_tank_tank_status` (Heating / idle states of the tank whose element is the 2.15 kW Workshop boiler), linked to `sensor.workshop_boiler_energy`. The boiler is metered, so it is not for the boiler's own detection but for telling similar unmetered 2 kW loads apart from it.

Built 2026-10-26 (`_two_modes`, `_densest_window`, `_hint_split`, `HINT_*`, `Detector.family_runs`; tests `test_a_minority_mode_of_length_...`, `test_one_hint_alone_...`). Bench: Home 15-25 Oct with inputs and the default card on all sites: no scorecard moves beyond 0.7 points (Home hidden partition capture -0.4, impurity -0.7). It does **not** fire on the boiler: the length cut (226 / 273 s, medians 135 / 628 s, ratio 4.65) puts 30 runs in the long mode, the boiler's 11 and 19 of Hiša's longer ones, spread over 07:00-18:00 UTC; the best 3-hour window holds 47 % of them against the 80 % required. A joint cut on length and hour (the long runs inside the window are mostly the boiler's) is the next refinement; the thresholds stay strict until there is truth to judge a looser one against.

