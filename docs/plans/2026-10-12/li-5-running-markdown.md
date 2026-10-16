# LI-5: running loads as a Markdown list (2026-10-12)

Load Insights, branch `dev`. Personal project: never log it to work-context.md.
This is a plan only. Nothing has been changed yet.

Anze's ask: a sensor or attribute holding a Markdown list of the detected loads
running now, named and unnamed, with links to the page where they get named.

Anze's decisions, 2026-10-12:
- English only for now.
- A load on a meter that holds several devices shows **once**, with its name if it is named. An unnamed one there gets its link. This was a "known gap" in the first draft and is now fixed below.

## Decisions

| question | answer |
|---|---|
| where it lives | New attribute `running_markdown` on the existing detected-loads sensor. No new entity. `_unrecorded_attributes = {MATCH_ALL}` already keeps it out of the recorder, and `tests/test_sensor_attributes.py` passes as it is. The state, the active count, stays as it is. |
| entity ids | Home: `sensor.31b_consumption_forecast_detected_loads`. Kozolec: `sensor.kozolec_pv_kozolec_insights_detected_loads` (from the HA_Configs snapshots). |
| unit of a row | **One grid-meter run.** That is one `_Open` in `main.phases[ph].open_edges`; legs that started within `MERGE_TOLERANCE_S` of each other are merged, the rule `Detector.active` already uses. `Detector.active()` is per phase *total*: every load running on phase A is summed into one entry and guessed as a whole. So it cannot list loads one by one, and it is not used here. |
| order | Named loads first, then unnamed ones. Each group is sorted by watts, highest first. |
| cap / size | At most 10 rows, then `- … N more`. One row is about 80 to 190 bytes with the link, so the worst case is about 2 KB. |
| update rate | No new work is scheduled. The attribute is rebuilt with the others each time the runner writes (every 1 min once caught up). The Markdown card re-renders on that state_changed. |
| language | English. Every piece of text LI builds is English today (`describe`, `menu_row`, `Guess.tag`, `on_phases`, `overview_text`). `hass.config.language` is read nowhere, and only HA-rendered strings use en.json/sl.json. Slovene would need `on_phases`, `_fmt_*` and the guess tags translated: a separate job. |
| translations | Nothing changes. HA does not translate attribute values. |

## One load, one row: how a grid run is matched to its meter

The grid sees everything, and a meter below it sees its own circuit. So every load on a meter is also a run of the grid's. The rows are therefore built **from the grid's runs only**. A meter's run never makes a row of its own; it only fills in the grid row it belongs to. That way nothing can be listed twice.

The link from a grid run to its meter is one the detector already keeps. No new heuristic is needed:

1. **`_Open.meter`** (detect.py:1638) names the meter whose own step was all of this start. It is set when the run is placed:
   - the event's cluster `where` (detect.py:4346);
   - `_meter_started` in `_split_rise` and the leg rule (detect.py:4356, 4396, 4412);
   - the settled-start rule (detect.py:2495).

   It is persisted (`as_list`) and is what `PhaseState.owned` and `Fleet._meter_on` already trust to hold the run open.
2. **Which of that meter's runs:** `Fleet._steps_over(M, c, ph, o.since, up=True)`, over `Fleet._chans(M, ph)`, returns the meter's own declared rises inside the grid step's span. This is the same window `_meter_on` and `_change_since` read. Each rise is a `declared` tuple whose `[0]` is the `since` its `_Open` was opened with (`PhaseState._declare`). So the meter's run is the `_Open` in `subs[M].phases[c].open_edges` whose `since` is within 0.01 s of a rise's `e[0]`. The matching is by identity of the step, not by size or time. If a leg rose on several channels (a 3EM's kiln legs), the matched runs are joined: channels joined, watts summed, earliest `since`.
3. **What the row says**, given the grid run `o` (merged legs, grid phases, summed watts):

| case | row label | link / id | where |
|---|---|---|---|
| `o.meter` is None | grid's own guess `main._guess(phases, watts, elapsed)`. Named sig: **name**. Unnamed sig: `Unnamed #id`. No sig: `New load` | `#id` = grid sig id. Link if `("", id)` is in `offered` | `most_specific(sig.locations, sig.count, parents)`, hidden when "main" |
| `o.meter = M`, `fleet.holds_one_device(M)` (a plug, a switch) | the grid sig's name if named, else **M**. The meter IS that device (Anze, 2026-09-28), and `namable` never offers it | none | none (the label is the meter) |
| `o.meter = M`, M holds several devices, its run matched (step 2) | `subs[M]._guess(chans, sub_watts, elapsed)`. Named: **name**. Unnamed: `Unnamed #id`. No sig: `New load` | `#id` = **the meter's own signature id**, the one `Fleet.namable` offers under group M (it never offers the grid's twin of a sub-located load). Link if `(M, id)` is in `offered` | M |
| `o.meter = M`, several devices, no matched run (the meter's run already closed, or its rise was undeclared) | as the first row, from the grid's guess | as the first row | M |

   - Watts, phases and `since` always come from the grid run, so the card speaks one meter's numbers: the grid's.
   - Per-run guessing uses the existing `Detector._guess(phases, watts, elapsed)`, the same matcher `active()` applies to a phase total, here applied to one run.
   - Rows sharing a name are merged into one: watts summed, phases joined, earliest `since`. A device on two meters, or two legs filed apart, is one row.
4. **One source of truth for "is this load running"** (Anze, 2026-10-12): `Fleet.running(now)` is the only place that decides whether a load is on. Three things that each answered it before now read from it:
   - **The named-load on/off sensors** (`NamedLoadRunning`, the only user of `Fleet.active_by_name`). Today they sum each detector's `Detector.active_by_name`, which guesses a signature from a phase's whole total. So a named load is "on" only while it is alone on its phase(s): a 95 W fridge beside a 2 kW boiler is guessed as one 2,095 W thing, matches neither, and both sensors read off.
   - **The naming page's "on now"** (config_flow.py:569, `det.running_now` per meter).
   - **The sensor's `signatures[].running` flag** (sensor.py:412, `det.running_now`).

   What stays on phase totals, on purpose, is what is *about* phase totals:
   - the state, a count of active phase groups;
   - `unknown_load_power`;
   - the `active` / `meters.*.active` attributes;
   - the overview's "on right now".

   The per-entry `signature`/`name` in `active` stay as the phase total's best match. That is called out in the release note so nobody reads them as "is X running". No dashboard in HA_Configs reads those attributes (checked 2026-10-12).

### Line format

```
- **Pralni stroj** · 2.1 kW on phase A · 25 min
- **Hladilnik** · 95 W on phase B · 12 min · Mansarda
- **EVBox** · 10.6 kW on all three phases · 1.5 h
- [Unnamed #11](/config/integrations/integration/load_insights#config_entry=01M2…) · 270 W on phase C · 3 h · motor? · Mansarda
- [Unnamed #42](/config/…) · 1.2 kW on phases A and C · 3 min · kettle? · Hiša
- Unnamed #57 · 300 W on phase C · 40 s · motor
- New load · 640 W on phase B · 4 min
- … 3 more
```
With nothing running, the attribute is the line `Nothing detected running.`

### How the naming page identifies loads (current state)

- The page does not use `#id`. Rows are menu options `load_0..N`, and the position maps to `self._naming_rows[(meter, id)]`.
- Loads are grouped by meter (`Fleet.namable`: "main" first, then meters by energy).
- Within a group, rows are sorted by `energy_wh × (0.45 + 0.55·recognisable)`, then by evidence. The order is not by evidence alone.
- Fix: append `· #id` to each row's detail line (step 3). The card's "where" names the group, and `#id` names the row inside it. For a meter's group it is that meter's own id, which is the same id the card uses.

### Links: what HA's frontend can do today

Checked against `home-assistant/frontend` dev on 2026-10-12 (`ha-panel-my.ts`, `ha-config-integration-page.ts`, `ha-config-repairs-dashboard.ts`):

- **Options flow step: no.** There is no URL or my-link that opens an options flow at all, let alone one of its steps.
- **Integration entry page: yes.** `/config/integrations/integration/load_insights#config_entry=<entry_id>` reads `config_entry` from the hash, scrolls to that entry and highlights it.
- **Repairs: no deep link to one issue.** The page reads only `?dialog=system-health`. A fixable repair per running load would work, but it is still two clicks and noisy. Not recommended.
- **Developer tools action: not usable.** `developer_call_service` prefills the action but not its fields.
- **Discovery config flows: no.** One flow per signature would work, but it misuses discovery and is noisy. Not recommended.
- **Recommendation:** link each offered unnamed row to the entry page, and print `#id` on both the card and the naming row.

## Steps

1. **`insights/detect.py`**: add `Fleet.running(now)`, which returns rows as plain dicts, and the pure formatter `running_markdown(rows, link, offered, cap)`, next to `ref_label`.
   ```python
   RUNNING_ROWS = 10   # a dashboard card, not the library: ~2 KB at most

   # in Fleet
   def running(self, now: float) -> List[dict]:
       """Every load running now, ONCE: each grid run (merged legs, as
       Detector.active merges) told through the meter its own step placed it
       on (_Open.meter) and that meter's run of it (_steps_over's rise -> the
       _Open opened at that rise). A meter's run never makes a row of its own:
       the grid's run of it is the row. -> {name, ref, label_id, watts,
       phases, since, where, tag}"""
       legs = sorted(((ph, o) for ph, st in self.main.phases.items() for o in st.open_edges),
                     key=lambda x: x[1].since)
       runs: List[List[tuple]] = []
       for ph, o in legs:   # the merge rule of Detector.active, per run
           run = next((r for r in runs if abs(r[0][1].since - o.since) <= MERGE_TOLERANCE_S
                       and r[0][1].meter == o.meter), None)
           (run.append((ph, o)) if run is not None else runs.append([(ph, o)]))
       rows: Dict[str, dict] = {}
       out: List[dict] = []
       for run in runs:
           phases = "".join(sorted(ph for ph, _ in run))
           watts = sum(o.watts for _, o in run)
           since = min(o.since for _, o in run)
           meter = run[0][1].meter
           det, ref_meter, sig_ph, sig_w, where = self.main, "", phases, watts, None
           if meter and meter in self.subs and not self.holds_one_device(meter):
               found = {}   # id(_Open) -> (channel, _Open): one meter run, however many rises point at it
               for ph, o in run:
                   for c in self._chans(meter, ph):
                       for e in self._steps_over(meter, c, ph, o.since, True):
                           for x in self.subs[meter].phases[c].open_edges:
                               if abs(x.since - e[0]) <= 0.01:
                                   found[id(x)] = (c, x)
               mine = list(found.values())
               if mine:
                   det, ref_meter = self.subs[meter], meter
                   sig_ph = "".join(sorted({c for c, _ in mine}))
                   sig_w = sum(x.watts for _, x in mine)
               where = meter
           sid = det._guess(sig_ph, sig_w, now - since)
           sig = next((s for s in det.signatures if s.id == sid), None) if sid is not None else None
           if meter and self.holds_one_device(meter):
               name = (sig.name if sig else None) or meter
           else:
               name = sig.name if sig else None
               if where is None:
                   where = most_specific(sig.locations, sig.count, self.parents) if sig else "main"
           ref = (ref_meter, sig.id) if sig else None
           row = {"name": name, "ref": ref, "refs": {ref} if ref else set(),
                  "watts": watts, "phases": phases, "since": since,
                  "where": None if where == "main" or (meter and self.holds_one_device(meter)) else where,
                  "tag": sig.guess().tag if sig and not name else ""}
           if name and name in rows:   # one device, one row
               r = rows[name]
               r["watts"] += watts; r["since"] = min(r["since"], since); r["refs"] |= row["refs"]
               r["phases"] = "".join(sorted(set(r["phases"] + phases)))
               continue
           if name:
               rows[name] = row
           out.append(row)
       return out

   def active_by_name(self, now: float) -> Dict[str, float]:      # REPLACES the _summed version
       """Watts on right now per NAME: the named rows of running() - the same
       answer the dashboard list gives, one load at a time, whatever else
       shares its phase."""
       return {r["name"]: float(r["watts"]) for r in self.running(now) if r["name"]}

   def running_refs(self, now: float) -> set:
       """(meter, id) of every signature believed on right now - running()'s."""
       return {ref for r in self.running(now) for ref in r["refs"]}

   def running_markdown(rows, link, offered=frozenset(), cap=RUNNING_ROWS, now=None) -> str:
       named = sorted((r for r in rows if r["name"]), key=lambda r: -r["watts"])
       unnamed = sorted((r for r in rows if not r["name"]), key=lambda r: -r["watts"])
       lines = []
       for r in named + unnamed:
           if r["name"]:
               label = f"**{r['name']}**"
           elif r["ref"] is None:
               label = "New load"
           else:
               label = f"Unnamed #{r['ref'][1]}"
               if r["ref"] in offered:
                   label = f"[{label}]({link})"
           lines.append(f"- {label} · {_fmt_w(r['watts'])} {on_phases(r['phases'])} · {_fmt_s(now - r['since'])}"
                        + (f" · {r['tag']}" if r["tag"] else "") + (f" · {r['where']}" if r["where"] else ""))
       if not lines:
           return "Nothing detected running."
       return "\n".join(lines[:cap] + ([f"- … {len(lines) - cap} more"] if len(lines) > cap else []))
   ```
   This is a sketch; tidy it when writing it. Notes:
   - `_fmt_w`/`_fmt_s` are already imported in detect.py (line 33). `MERGE_TOLERANCE_S`, `most_specific` and `on_phases` live in the same module.
   - `self.parents` is the Fleet's own map, the one `namable` uses.
   - The `_guess` call reads `det.signatures`, but `_guess` filters on `sig.phases == phases`. For a meter's run the phases are the meter's channel letters, which is what that detector's signatures carry.
   - `_steps_over` and `_chans` are private helpers inside the same class. Calling them from a Fleet method is fine.

2. **`sensor.py` `DetectedLoadsSensor`**:
   - In `__init__`, set `self._link = f"/config/integrations/integration/{DOMAIN}#config_entry={entry.entry_id}"`. `DOMAIN` is already imported.
   - In `extra_state_attributes`, add the attribute:
     ```python
     "running_markdown": running_markdown(
         self._runner.fleet.running(now), self._link,
         {ref for _, rows, _ in self._runner.naming_groups() for ref, _ in rows}, now=now),
     ```
     Add `running_markdown` to the existing `from .insights.detect import (...)`.
   - Read under the runner's lock? `extra_state_attributes` already reads `det.signatures`/`fleet.subs` unlocked, and this does the same.
   - `signatures[].running` (line 412): `running = det.running_now(now)` becomes `running = {i for m, i in self._runner.fleet.running_refs(now) if m == ""}`.

3. **`config_flow.py` `async_step_naming_list`**:
   - About line 595: add one line, `rest += f" · #{ref[1]}"`.
   - About line 569: the per-meter `running |= {(meter, i) for i in det.running_now(now_ts)}` becomes one `running = runner.fleet.running_refs(now_ts)` before the loop. The loop keeps building `partners`/`by_ref`.

3b. **The named on/off sensors onto the same runs** (Anze, 2026-10-12: now, not later):
   - **`insights/detect.py`**: `Fleet.active_by_name` is replaced by the version in step 1 (the named rows of `running()`), and `Fleet.running_refs` is added. **Delete** `Detector.active_by_name` and `Detector.running_now`: their only callers were `Fleet.active_by_name`, sensor.py:412 and config_flow.py:569, all moved above. `Fleet._summed` stays, because `energy_by_name`/`hourly_by_name` use it.
   - **`binary_sensor.py` `NamedLoadRunning`**: no code change. `is_on` and the `watts` attribute already call `fleet.active_by_name`, which now answers per load. Update the class docstring: on while a running load is filed under this name, read one load at a time from the grid meter's runs (`Fleet.running`), the same answer as the dashboard list.
   - **Unchanged on purpose:**
     - `_running_uid`, the translation key, the device info and every entity id / unique_id;
     - the set of entities, still one per name except metered devices;
     - the `signatures`, `sessions_seen` and `phases` attributes.
   - **Cost:** `running()` runs twice per named sensor per update (`is_on` + `watts`), so about 2 × 15 walks of at most 12 open runs per phase a minute. Fine for now. If profiling ever shows it, cache `running(now)` on the runner per pass.

3c. **Meaning changes, for the release note** (step 6):
   - **On/off:** a named load now reads "on" while it runs, whatever else shares its phase. Before, it was "on" only while it was the only load on its phase(s), because the phase's total had to match it. Expect a lot more "on" time on busy phases. That is the fix, not a regression.
   - **`watts` attribute:** that load's own run(s), not the phase total.
   - **A load named on a meter below the house** (Mansarda's fridge, Hiša's boiler) is now read through the grid's run that the meter's own step placed (`_Open.meter`), not from the meter's phase totals alone. It is on from the moment the grid meter declares the run. The grid is judged behind its slowest meter by the wait (up to *Wait for slower meters, at most*, default 5 min), so that is the same lag the named loads' live readings already document.
   - **Edge case:** a meter's run the grid never placed (`o.meter` None) no longer turns the meter's named sensor on. It shows as the grid's own guess, and is listed once (see "Remaining limits").
   - **Naming page "on now"** and **`signatures[].running`** follow the same runs.
   - **Not changed:** the state, `unknown_load_power`, and the `active`/`meters.*.active` attributes stay phase totals. Their `signature`/`name` are the phase total's best match, not "is this load running".

4. **Tests** in `tests/test_detect.py` (no new file):
   - **Setup:** build `D.Fleet()` with `D.Detector()` main and subs. Put `D._Open(since, watts, None, [(since, watts)], meter=…)` straight into `phases[...].open_edges`. Add real `D.Signature(id, phases, power, duration_s, pf, count, first_seen, last_seen, locations=…)` with `count`/`duration_s` such that `_guess` matches. Stub the two span helpers on the instance, since the test is about the rows, not the span maths:
     - `f._chans = lambda m, ph: ["a"]`
     - `f._steps_over = lambda m, c, ph, since, up: [(sub_since, 95.0, None, since, since + 5)]`
     - `f.single = {"Mansarda": False, "EVBox": True}`
   - **Formatter tests**, on hand-made row dicts:
     - `test_running_markdown_empty`: an empty list gives `"Nothing detected running."`
     - `test_running_markdown_order`: a named 95 W row comes before an unnamed 1.2 kW one; unnamed rows are sorted by watts. An offered ref carries `](/config/...#config_entry=E)`, a ref not offered has no link, and `ref=None` prints `New load`.
     - `test_running_markdown_cap`: 15 rows give 10 lines plus `- … 5 more`, and `len(md.encode()) < 3000`.
   - **`Fleet.running` tests:**
     - `test_running_named_submeter_load_once`: Mansarda (several devices) holds a named "Hladilnik" sig #3 on channel a. The grid holds an unnamed 95 W sig #42 on phase b. The grid run has `meter="Mansarda"`, and the meter's run is opened at the stubbed rise's `e[0]`. Expect exactly one row: name "Hladilnik", where "Mansarda", watts from the grid run. No `Unnamed #42` row.
     - `test_running_unnamed_submeter_load_links_meter_id`: the same setup with Mansarda's sig #11 unnamed. The row's ref is `("Mansarda", 11)`, not `("", 42)`. With `offered={("Mansarda", 11)}` the markdown has `[Unnamed #11](link)` and `· Mansarda`.
     - `test_running_one_device_meter`: a grid run with `meter="EVBox"` (one device) gives a row named "EVBox", with no ref and no where.
     - `test_running_unmatched_meter_run`: a grid run with `meter="Mansarda"` but no meter run at the rise's `since` falls back to the grid's guess, with where "Mansarda". It is one row.
     - `test_running_same_name_merges`: two grid runs whose guesses carry the same name give one row, with watts summed and phases joined.
   - **Named on/off tests:**
     - `test_named_loads_on_one_phase_are_on_independently`. Grid phase a has two signatures, "Bojler" (2000 W) and "Hladilnik" (95 W). It also has two open runs, 2000 W at t0 and 95 W at t0 + 600 s, further apart than `MERGE_TOLERANCE_S`.
       - `f.active_by_name(t) == {"Bojler": 2000.0, "Hladilnik": 95.0}`. The old phase-total guess, 2,095 W, matched neither.
       - After removing the 2000 W run: `{"Hladilnik": 95.0}`.
       - With no runs: `{}`.
       - In each of these states, `set(f.active_by_name(t)) == {r["name"] for r in f.running(t) if r["name"]}`, and every named row's name appears bold in `running_markdown(f.running(t), …)`. The sensors and the card agree.
     - `test_running_refs_agree_with_rows`: `f.running_refs(t)` equals the union of the rows' `refs`, including a meter's `("Mansarda", 11)`.
   - **Existing tests that move onto the Fleet**, because the Detector methods they call are deleted:
     - `test_active_power_is_reported_per_name` (test_detect.py:715): `det2.active_by_name(...)` becomes `D.Fleet(main=det2).active_by_name(...)`. Same assertion.
     - `test_running_now_names_the_signatures_that_are_on` (:2225): `det.running_now(t)` becomes `{i for _, i in D.Fleet(main=det).running_refs(t)}`. Same assertions.
     - The circuit-named load test (:3765-3767) put Hiša's run open with **no** grid run and asserted `{"Bojler": 1000.0}`. Under one source of truth, a meter's run alone is not a load the house is running. The test also gets the grid's run of it: `f.main.phases["a"].open_edges = [D._Open(t - 60.0, 1000.0, None, [(t - 60.0, 1000.0)], meter="Hiša")]`, with Hiša's run opened at that rise. Use the real `_steps_over` if `_tree_fleet` declared it; otherwise stub it as above. Then assert both `{"Bojler": 1000.0}` and, with the grid run removed, `{}`. Rewrite the comment to say why: the grid's run, placed by the meter's step, is what is running.
   - Run `python3.14 tests/run_all.py`. Everything must pass, `test_sensor_attributes` included.

5. **`dashboard/README.md`**: add a "Running now" section with a ready-to-paste card:
   ```yaml
   type: markdown
   title: Running now
   content: >-
     {{ state_attr('sensor.31b_consumption_forecast_detected_loads', 'running_markdown') }}
   ```
   Add a note: at Kozolec use `sensor.kozolec_pv_kozolec_insights_detected_loads`. A link opens the integration's entry, highlighted; from there it is Configure → Name detected loads → the group named at the row's end (none means "main") → the row ending in that `#id`.

6. **`RELEASE_NOTES.md`**: add a top bullet under `## 0.4.1 → ### New Features`:
   > **What is running now, as a list for a dashboard.** The Detected loads sensor has a `running_markdown` attribute: every load on right now, once, named ones first, each with its power, phases, how long it has been on and where it is, ten at most. A load on a meter below the house is listed as that meter's, with its name if it has one. An unnamed load the naming page offers links to the integration's entry, where Configure → Name detected loads lists it under the same `#id`; Home Assistant cannot open the naming page itself from a link. A Markdown card for it is in `dashboard/README.md`.

   Add a second bullet for the meaning change (step 3c):
   > **A named load's running sensor reads that load, whatever else is on.** Each named load's *running* sensor was on only while that load was the only one on its phase, because the phase's whole draw had to look like it: a fridge beside a boiler read off, and so did the boiler. Each running load is now told apart from the others on its phase, and a load on a meter below the house is read through the run its meter's own step placed. The sensor, its `watts` attribute (now that load's own power, not the phase's), the naming page's "on now" and the dashboard list all give the same answer. Expect these sensors to be on far more often on busy phases. Entity ids are unchanged. The Detected loads state and its `active` list still count and describe what is on per phase.

7. **`AGENTS.md`**: add one paragraph at the end of "Naming page" covering:
   - `running_markdown` (`Fleet.running` → `running_markdown`) lists grid runs, each told through `_Open.meter` and the meter's run opened at its `_steps_over` rise. A meter's run never makes a row of its own.
   - A meter's load carries the meter's own signature id, the one `namable` offers.
   - The `#id` on a naming row's detail line is what the card links by.
   - HA has no URL into an options-flow step or a single repair (frontend checked 2026-10-12).
   - `Fleet.running` is the one answer to "is this load on":
     - the named running sensors (`Fleet.active_by_name`);
     - the naming page's "on now" and `signatures[].running` (`Fleet.running_refs`);
     - the card.

     The state, `unknown_load_power` and the `active` lists stay phase totals. Do not add a second way to say a load is on.

8. **Commit locally** on `dev`. The commit message says:
   - what changed;
   - that the 2 KB / 10 rows are a judgement;
   - that the meter link reuses `_Open.meter` + `_steps_over`, no new matching rule;
   - that the named running sensors, the naming page's "on now" and `signatures[].running` now read `Fleet.running`, and that `Detector.active_by_name`/`running_now` are deleted;
   - that the three moved tests changed and why (the Hiša one most of all);
   - that `run_all` passes.

   **Do not push** until Anze says so; a push to dev auto-deploys. Install on Home and Kozolec only, never Andrej's site. No generation bump or reset is needed: `_Open.meter` is already persisted.

9. **After Anze's push**: restart both sites and wait for `RUNNING`. Read `running_markdown` back through `/api/states/<entity>` and check:
   - it is under 3 KB;
   - **no load appears twice.** Compare against the meters' own `meters.<M>.active` in the same attributes (Mansarda's fridge, Susilna's dehumidifier at Home);
   - a meter's unnamed row's `#id` is on that meter's naming page;
   - **every named `binary_sensor.*` that is on has a bold row on the card, and vice versa.** Same moment, read through `/api/states`. Check it while two named loads share a phase at Home (the fridge beside the boiler or the washer);
   - each named sensor's `unique_id` and entity id are unchanged (HA_Configs `entities.json` before vs after);
   - clicking a link opens the entry highlighted (desktop and companion app).

   Adding the card to a dashboard is an HA change. Do it only on Anze's OK, with an HA_Configs snapshot before and after.

## Remaining limits (said, not hidden)

- **A meter's load the grid never placed** (`o.meter` None: the meters' timing or size disagreed, `_meter_started` found nothing) shows as the grid's row, with the grid's guess and id. It is still one row, because the meter's run makes none of its own. This is the same attribution the energy uses, so card and energy agree. If it happens, it is a placement miss to fix in detection, not on the card.
- **A meter's run with no grid run behind it** does not turn its named sensor on. Before, it did, from the meter's own phase total. This is the price of one source of truth, and it is in the release note. If day one at Home shows a named sub-meter load (Mansarda's fridge, Susilna's dehumidifier) off while `meters.<M>.active` shows it on for longer than the wait, that is a placement miss (`_Open.meter` unset) and a detection fix, not a reason to add a second source.
- **A named load running past its typical length** (`elapsed > duration_s × MATCH_DURATION_FACTOR + 60`) is not matched by `_guess` and reads off. That was true before too. It is not changed here.
