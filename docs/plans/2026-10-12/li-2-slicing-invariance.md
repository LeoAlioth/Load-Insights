# LI-2 - Slicing and restart invariance, recorder flush: verify and ship (plan, 2026-10-12)

Personal project (Load Insights). Never log to any work-context file.
Repo `~/Projects/Load_Insights`, branch `dev`, head `482aa9a` (pushed: origin/dev = dev).
Background: `~/.claude/plans/okay-while-the-other-refactored-sedgewick.md`, "Phase 2".
The owner's decisions (2026-10-12): verify and ship; yes to the RESTART dial (sections 3-4) and to the
recorder flush before each pass (section 5). This file is a plan only - nothing below has run.

## 0. Where phase 2 stands (git log, AGENTS.md, data/scorecard)

**Fixed, by cause (all on dev):**

| cause (background plan) | fix | commits |
|---|---|---|
| one fleet clock; meters read a whole pass ahead of the grid | every meter on one clock, the grid read the horizon behind (`Fleet.process` / `_run`) | 4e6ebdc, 04d017b, merge 03a0162 (10-02) |
| filing as a cost-ordered batch at pass end | filed when decidable, ties by start; merges after each filing | 04d017b, 03a0162 |
| final flush of pending events | carried and stored: `pending_rises`, `unfiled`, `pending_main`, `unvoted`, `carry` | 4e6ebdc, 3f41c6c-dirty-carry |
| cadence-based horizon | learned declaring lag (`_learn_lags`, p95, believed from 20), capped by `METER_WAIT_CAP_S`, refreshed on the `MODEL_REFRESH_S` grid of the readings' clock | 04d017b, ca029aa, 0d71f36, 5ecc781 |
| per-meter options | "Meters not to wait for" / "leave out of detection" (`horizon_skip`, `ignored` in `detection.py _run`) | 04d017b, 0d71f36 |
| eviction at a pass's end | `_prune(s.end)` on the clock; an evicted signature never handed back | 165a58d |
| `METER_WAIT_CAP_S` waits / `_hold_back` | the hold-back cut is the cap itself on the meters' clock, not a wait that moved with the pass | `_hold_back` docstring, 04d017b |
| schedules counted from the first reading | noise per reading + `REL_REFRESH_S` on the clock; histogram cut at every step | 7a5e70c, ab3b4dd |
| hidrofor 219 -> 203 at one-minute passes | AGENTS: "was this" (the pass-end decisions) - closed with the above | 03a0162 |
| cadence -> completeness | partly: latency sets the horizon; the repeat-interval estimator stays on purpose (AGENTS) | - |

DETECTOR_GENERATION 20 already means "a pass is only a pause" (shipped in 0.4.1-dev; RELEASE_NOTES 0.4.1
already has the bullet "The same history gives the same answer, however it is read").

**Invariance record** (sessions / kWh differing, all site-modes summed, from `data/scorecard/*.json`):
- 500c275, 10-02 11:45Z (baseline): 80,235 / 2,825 kWh. Home fed 6~1 9,790 of 15,813 / 379,
  6~L1 1,166 / 42, 0~6 14,292 / 521; Kozolec fed 192 / 49, 25 / 4.6, 335 / 101.
- f515afe-dirty, 10-02 12:42Z: **0** (first zero); 64da1b1-dirty 10-02 17:36Z: 0 (the AGENTS result).
- Two regressions caught and fixed within the hour: a4e1149 (10-03) 52,056 / 1,692 -> c3258bf 0;
  868de77-dirty (10-03 21:22Z) 1,588 / 20.5, all "only B" in 0~6 at home-hisa / home-mansarda -> 0.
- Untraced residual: 77c3159 NOOP cards drop:10000:2 and :3 - Home circuits, 6~1, 2 sessions,
  0.01 / 0.08 kWh, the office plug's runs filed into #97 instead of #308 / #311 (signature only,
  387-508 s from a pass boundary). Gone on later code (0005337 NOOP cards: 0), never explained.
- **Last recorded: 2026-10-04 10:25Z, `nogit-exp11p-bn3` (Load_Insights-invariance tree at
  8da9de1-dirty): 0 sessions / 0.0 kWh, all 11 site-modes x 3 pairs** (home hidden/circuits/fed,
  kozolec hidden/fed, home-hisa and home-mansarda hidden/fed, third hidden/fed). Last on a dev
  commit: `9b7c69c-baseline-fixedscore`, 10-04 07:01Z, also 0. Both on 23 Sep - 2 Oct.

**Not measured since** (card replays one slicing since 50cef4e, 10-09; history now 2-11 Oct).
18 detector commits since 9b7c69c. Suspects, in order:
1. 50cef4e - `phase_map` cache keyed by a vote counter (proven byte-identical at SLICE=6 only)
2. a5de09d - kept size density `_dens` (byte-identical at SLICE=6 only)
3. c689283 - daily repair; `Fleet._repaired_to` seeded from the first reading of the first call
4. 482aa9a - the multi-close remembers its runs for the corroboration (`recent_closed`, 60 s trim)
5. bd93df0 - step angle always on (`edge_hist_angle`)
6. 26575f8, 0089156, 21a6043, b6832db, 963f949, 5362664 (refactor, "same output" at SLICE=6)

**Still open, now in scope (2026-10-12: yes to both):**
- Restart-only state (AGENTS "A pass is only a pause"). The bench never restarts mid-stream, so
  invariance covers passes held in memory only. Fields that are in memory and not in `to_dict`:
  PhaseState `declared`, `steady_ts`, `held_drops`, `recent_closed`, `_moved` (l.1704-1732); Detector
  `_moved` (3724), `_dens` (3824), `_day_log` / `_day_rows` (3830); Fleet `sub_rows` (5345),
  `_stops_used` (5369), `_dues` (5365), `_repaired_to` (5419); also the `phase_map` vote-counter cache
  (50cef4e). Every deploy restarts both sites. An ungraceful stop also counts: the store is written at
  most hourly (`_persist`, `SAVE_MAX_INTERVAL_S`), and `last_processed` is saved in the same snapshot,
  so a crash is a restart at an earlier pass boundary. The new `RESTART` dial covers both.
- Recorder flush: `detection.py _run` reads the recorder up to `now` with no
  `async_block_till_done()` (`_check_named` and l.836 already call it). A reading committed after
  the pass read past its timestamp is never read, because the next pass starts at `end`.

## 1. Verification run (at the current dev head)

Before: check plan usage (< 90 %, CLAUDE.md); libench only if the owner is not using it, else the Mac; no
other bench on the Mac (else `BENCH_PARALLEL=4`); no `fetch_history` writing into `data/history/*`.

```bash
cd ~/Projects/Load_Insights
SCRATCH=<the running session's scratchpad dir>   # untracked outputs and scripts go here
git status --short                      # clean; note `git rev-parse --short HEAD`
python3.14 tests/run_all.py             # all pass (incl. test_a_pass_is_only_a_pause & co.)
BENCH_PARALLEL=8 python3.14 tests/energy_bench.py card SLICINGS=all LABEL=invariance \
  2>&1 | tee "$SCRATCH/card-invariance.txt"
```

One command covers every site the tool knows: home (hidden, circuits, fed), kozolec (hidden, fed),
home-hisa and home-mansarda (hidden, fed), third (hidden, fed) - 11 site-modes x SLICE=0 / 6 / 1 /
LIVE=1 = 44 replays, PYTHONHASHSEED=0 in each. Estimate on the Mac: ~20-30 min (today's one-slicing
card takes 300-450 s for 11 replays; one call and LIVE=1 cost more than SLICE=6); libench ~10 min.

Per site, to re-run only what differs (4 replays each; fed unless `SUBS=`):

```bash
B="env BENCH_PARALLEL=8 python3.14 tests/energy_bench.py invariance"
$B home data/history/home SUBS=none;      $B home data/history/home SUBS=circuits;  $B home data/history/home
$B kozolec data/history/kozolec SUBS=none; $B kozolec data/history/kozolec
$B third data/history/third SUBS=none; $B third data/history/third
$B home-hisa data/history/home SUBS=none;  $B home-hisa data/history/home
$B home-mansarda data/history/home SUBS=none; $B home-mansarda data/history/home
```

Reading it:
- Per site the card prints `invariance, sessions / kWh two slicings file differently: hidden: 6~1 0/0.0,
  6~L1 0/0.0, 0~6 0/0.0 ...`. Pass = every number 0/0.0. 0~6 is the reference (one call) against
  production's backfill; 6~L1 is production's own path (backfill, then minute passes); 6~1 a denser
  backfill. All three 0 = every slicing equals SLICE=0.
- Load figures: one number where the slicings agree, `a|b|c|d` (0|6|1|L1) where not.
- Files: `data/scorecard/<commit>-invariance-<utc>.json` (key `invariance`) and one worklist per
  site-mode, `data/scorecard/invariance-<site>-<mode>-<commit>-invariance-<utc>.txt`.
- Totals in one line:
  `python3.14 -c "import json,sys;j=json.load(open(sys.argv[1]));print({k:{p:(x['differ'],round(x['kwh'],2)) for p,x in v.items()} for k,v in j['invariance'].items()})" data/scorecard/<file>.json`

If all 0: go to section 3. The card is also the new SLICE=6 reference for li-1 (see section 8). If
anything differs, do section 2 first. Restart work goes on top of a code base that is invariant across slicings.

## 2. Whatever still differs: triage, then fix

Per differing site-mode, smallest replay first (kozolec < third < circuit sites < home hidden <
circuits < fed):
1. **Earliest first, not biggest.** The worklist is sorted by kWh; sort by start. Later differences are
   usually a library that already diverged. Fix the earliest; re-run; repeat.
2. **Distance to the pass boundary** (`pass boundary` column):
   - within the horizon + `MATCH_PATIENCE_S` (Home ~40-190 s, cap 300 s): decided at a pass's end.
     Check, in this order: `_hold_back` (rows/q/pv carried; the cut), a due that fires on the pass end
     instead of the clock (`silence_due`, `event_wait`, `_ready_at`, `_vote_blocked`), something run
     once per `process` call (a cache rebuilt or invalidated per call - suspects 1-2 above, the
     `_dues` reset, `_sync_views`), `_prune` / eviction.
   - far from any boundary: state diverged earlier. Find where with a scratch script (not tracked,
     `$SCRATCH`): replay SLICE=6 and SLICE=1 of the failing site-mode, wrap `Fleet.process` the way
     energy_bench wraps `_file`, and at every 6-h boundary (both slicings end a pass there) dump
     `json.dumps(fleet.to_dict(), sort_keys=True)` plus the restart-only fields; the first boundary and
     key that differ name the cause. For LIVE=1 compare at its live start.
3. **Known leads** - check the case against each: hidrofor counts per slicing (`bench.py pump` at
   SLICE=6 vs LIVE=1), `METER_WAIT_CAP_S` (a meter whose lag exceeds the cap), `_hold_back`, eviction
   at a pass's end, the office-plug #97 vs #308 residual (signature-only = filing / merge tie-break).
4. **Bisect if needed** (last zero on dev: 9b7c69c): export each candidate with
   `git archive <sha> | tar -x -C $SCRATCH/li-<sha>` and symlink `data/` into it (no worktree, no
   branch); run the failing `invariance` line there. Suspect order as in section 0.

**Fix rule** (background plan):
- Root cause in `detect.py`, one cause per commit, no dial left behind.
- A test in `tests/test_detect.py` replaying the case at two slicings (one call vs passes cut at the
  case's boundary; helpers `_passes` / `_sliced_history`, pattern `test_a_pass_is_only_a_pause`),
  asserting identical sessions (phases, start, end, Wh, signature). It must fail before the fix.
- `python3.14 tests/run_all.py`, then `card SLICINGS=all LABEL=<fix>`: invariance 0 at that site-mode
  and nowhere new; `energy_bench.py diff <verification>.json <fix>.json`: capture may drop only by
  what the fix explains (named sessions, kWh), impurity must not rise. The newest band
  (`noise-8da9de1-dirty-20261004T094503Z.json`) is old code and old days: if a fix moves a load by
  less than ~3 points, re-measure the band at the verification head first (`card NOOP=drop:10000:1`,
  `:2`, `:3`, then `energy_bench.py noise BASE N1 N2 N3`).
- Commit locally on dev, evidence in the message (counts before/after, card files). No push.

## 3. The RESTART dial: invariance across a restart (bench and tests only, then a run)

Build (tests/ only, one commit):
- `tests/bench.py`: `RESTART` goes in `OWN` (float, hours, default 0) and is passed as `--restart-hours`.
  Document it beside SLICE/LIVE in the docstring.
- `tests/replay.py`: add `--restart-hours H`. At the end of the first pass at or after every H hours
  of the readings' clock, the fleet round-trips the way production stores it:
  `D.Fleet.from_dict(json.loads(json.dumps(fleet.to_dict())))`, then
  `load_older(json.loads(json.dumps(old.older_to_dict())))`.
  Then re-apply what the runner sets on each pass or at start: `wait_cap_s`, `parents`,
  `horizon_skip`, `main.tz_offset_s` (production sets it at start).
  Move the two instance hooks (`main._file` -> `keep`, `_meter_owns` -> `owned`) into one small
  function, and call it at creation and after every round-trip. Class-level wraps (energy_bench's
  `PhaseState._close`) survive on their own. `run()` returns the last fleet.
- `tests/energy_bench.py`:
  - `SLICINGS` gains `"6R": ["SLICE=6", "RESTART=1"]` (a restart at every 6-hour pass) and
    `"L1R": ["LIVE=1", "RESTART=1"]` (hourly restarts in the live day).
  - `PAIRS` gains `("6", "6R")` and `("L1", "L1R")`.
  - The card's `SLICINGS=all` and `invariance` pick both up. The worklist's pass boundary already
    includes the restart points, because restarts fall on pass ends.
- `tests/test_energy_bench.py`: none needed. `compare()` is unchanged.
- `tests/test_detect.py`: `test_a_restart_is_only_a_pause`. Take `_sliced_history()` plus one plug
  through a Fleet in 1-hour passes, once held in memory and once with a JSON round-trip of
  `to_dict()` at every cut. Assert identical filed sessions (phases, start, end, Wh, signature).
  Write it now, but keep it out of the tree until section 4's first fix makes it pass (so run_all
  stays green), then commit the two together.

Run:
```bash
python3.14 tests/run_all.py
BENCH_PARALLEL=8 python3.14 tests/energy_bench.py card SLICINGS=all LABEL=restart 2>&1 | tee "$SCRATCH/card-restart.txt"
```
That is 66 replays, about 30-45 min on the Mac. A single site:
`$B kozolec data/history/kozolec SUBS=none` (now 6 replays).

Reading it: 6~6R and L1~L1R are the restart pairs. Read them in the same `invariance` line as the
slicing pairs. Every pair must be 0/0.0. Differences go to section 4.

## 4. Whatever the restart pairs find: persist or rebuild, one rule for all

Triage is the same as section 2. One addition: a restart difference sits AFTER a restart point.
Take the earliest one, and find the field with the scratch dump in section 2: diff the two fleets'
in-memory state (including the fields listed in section 0) at the restart boundary, held in memory
vs round-tripped.

Rule, applied to every field the dial flags (the general mechanism, no per-field special cases):
1. **Derivable from persisted state** (caches: `_dens`, `_dues`, the `phase_map` counter cache,
   Detector `_moved` if it is an index): empty after load means "work it out". Rebuild it lazily on
   first use, never persist it.
2. **Carries a decision across the boundary** (`declared`, `steady_ts`, `held_drops`,
   `recent_closed`, `_stops_used`, `sub_rows`, `_repaired_to`): persist it in `to_dict` /
   `from_dict` with a default. Trim it to the span it is read over (e.g. `recent_closed` 60 s,
   `_stops_used` a day, `sub_rows` the meters' reach and wait cap). A field with a default needs no
   generation bump.
3. **Too big to persist** (`_day_rows`, a day of readings): change its representation to what is
   read from it. The repair's floor is a time-weighted level, so keep a time-weighted histogram per
   phase and persist that. Agree this one with li-1 (its "restart mid-day" item): one change, made once.
4. **The guard that keeps it general**: `test_every_field_is_persisted_or_derived` in
   test_detect.py. For PhaseState, Detector and Fleet, every dataclass field must either be in its
   `to_dict()` output or carry `metadata={"derived": True}`. A new in-memory field then cannot slip
   past the restart unnoticed. This replaces today's implicit `repr=False, compare=False` convention
   for "runtime only".
5. Each fix: the two-slicing test pattern from section 2, with a restart at the case's boundary;
   run_all; `card SLICINGS=all`. All five pairs must be 0, and `diff` gets the same reading rule as
   section 2.
6. Store size: report Home's `to_dict` JSON size before and after in the commit message. It is
   written at most hourly; flag anything over +20 %.

## 5. Recorder flush before each production pass (custom_components, one commit)

- `detection.py DetectionRunner._run`: right after `began = time.monotonic()` and before `now` is
  taken, add `await asyncio.wait_for(get_instance(self.hass).async_block_till_done(), RECORDER_FLUSH_S)`.
  `now` is taken after the flush, so everything stamped before it is committed.
  If it times out, log at debug level and return without advancing `last_processed`. The next pass
  (one minute later) reads the same window again, so nothing is lost and nothing is read half-written.
  `RECORDER_FLUSH_S = 30.0`, a module constant with one comment. Not an option.
- Publish the time it took: `last_pass["flush_s"]`, so the diagnostics show its cost on the Pis.
- No bench effect (the CSVs hold the committed rows). run_all must pass. No generation bump.
- Check on site after deploy: `last_pass.flush_s` in the diagnostics stays well under a second, and
  the log shows no detection-run failures and no repeated timeouts.

## 6. What "ship" means

Production (`detection.py DetectionRunner._run`) backfills `DETECTION_BACKFILL_DAYS` (10) in
`DETECTION_SLICE_HOURS` (6) slices, one every `CATCH_UP_PASS_S` (2 s). After that it runs one pass every
`DETECTION_INTERVAL_MINUTES` (1) up to `now`, so pass lengths are irregular: about 60 s plus the pass's
own time. It restarts on every deploy, HA restart or options reload, or after a crash, resuming from
the last hourly snapshot. The bench covers that path with SLICE=6 + LIVE=1, plus 6R / L1R for the restarts.
- Pass length and backfill slicing stay as they are.
- custom_components changes:
  - section 5's flush;
  - section 4's persisted fields, if any;
  - any slicing fix from section 2.
- Generation: none of these changes what a stored field means, so no `DETECTOR_GENERATION` bump.
  New `to_dict` fields get defaults, so an old store loads with them empty, exactly as today. Upgrading
  is one last restart that still loses the in-memory state.
- Reset (`load_insights.reset_detection`, names kept): only if the fixed differences on 6~L1 or
  L1~L1R are material, meaning more than about 1 % of a site's detected kWh. The owner decides. Otherwise the
  libraries heal as days roll out of the ten.

## 7. Docs, release notes, deploy

AGENTS.md (with the last fix's commit, or alone if nothing needed fixing):
- Bench section (~l.160): the headline names the latest result:
  "invariance 0 / 0.0 kWh at every site, mode and pair - slicings and restarts (card `<file>`,
  `<commit>`, 2-11 Oct)".
- Scorecard section (~l.248-266):
  - `SLICINGS=all` now also replays 6R and L1R (`RESTART=` dial).
  - Keep "one slicing is enough".
  - Add the rule: "`card SLICINGS=all` before every push to dev that touches detect.py's stream,
    filing, fleet, a cache, per-pass bookkeeping or `to_dict`". The 10-04 -> 10-12 gap is why.
- "How to tune" (~l.403): the same rule, one line.
- "Storage and generations": the field rule from section 4: every field is persisted or marked
  `derived` and rebuilt on load, and `test_every_field_is_persisted_or_derived` enforces it.
- Known shortfalls:
  - "A pass is only a pause" moves into the scorecard section as the verified result.
  - Its "some state is still restart-only" sentence goes.
  - The recorder flush gets one line under "Deploying" (what `flush_s` shows).
- One paragraph per fixed cause: what differed, sessions / kWh, the test name.

RELEASE_NOTES.md (0.4.1 is unreleased on main), appended to the existing 0.4.1 bullet "The same history
gives the same answer, however it is read":
- "A restart no longer changes what detection finds: what it was in the middle of deciding - a step
  still settling, a run a meter was about to end - is kept with the library, so a site restarted
  mid-day files the same runs as one that ran straight through."
- "Each pass waits for Home Assistant's recorder to write what it has before reading, so a reading
  that arrived just as a pass ran is no longer skipped."
- If slicing fixes landed: "Checked again on the ten days to 11 October: (N runs, X kWh had differed:
  <one plain sentence per cause>)."

Deploy, only when the owner says:
- Push `dev`. It auto-builds a pre-release, and Home and Kozolec pull it. The third site gets it only
  when the owner says.
- Restart each site (AGENTS "Deploying"). No reset unless section 6 decided one.
- After `caught_up`, check `last_pass.flush_s` in each site's diagnostics.
- Optional: one restart a few hours later, then confirm detection resumes with no failures in the log.

## 8. Order (with li-1, the daily repair)

The repair (c689283) is already on dev and due on the readings' clock, so it is invariant by
construction, but that is not yet proven on current dev. li-1's restart item (`_repaired_to`,
`_day_log`, `_day_rows` not persisted) is a special case of section 4.
1. li-2 section 1: one `SLICINGS=all` card, about 20-30 min. While li-1 has changed no code, this is
   also li-1's slicing card: run it once, and both plans read it.
2. If it differs: section 2 before anything else, because every later delta would be read against a
   slicing-dependent baseline.
3. li-2 section 5 (the recorder flush): independent and small, so commit it any time after step 1.
4. li-2 section 3 (the `RESTART` dial + `test_a_restart_is_only_a_pause`), then the restart card.
5. li-2 section 4: fix what the restart pairs list. The `_repaired_to` / `_day_rows` part is done
   together with li-1's restart item (one representation, one test). li-1's
   `test_the_days_repair_after_a_restart` becomes an instance of the general rule.
6. li-1's own bench work and changes, read against the restart-clean card from step 5.
7. Last: one `SLICINGS=all` card on the final commit (all five pairs 0). It goes in the AGENTS headline
   (section 7), and one push covers both plans, when the owner says.

## Done when
- `card SLICINGS=all` at the head shows 0 sessions / 0.0 kWh for 11 site-modes x 5 pairs
  (6~1, 6~L1, 0~6, 6~6R, L1~L1R), and run_all passes, including `test_a_restart_is_only_a_pause` and
  `test_every_field_is_persisted_or_derived`.
- Every fix has its test (two slicings or a restart at the case's boundary) and a card + diff in its
  commit message. Home's store size before/after is stated.
- The recorder flush is committed and `flush_s` is published.
- AGENTS.md and RELEASE_NOTES.md are updated and committed locally on dev. The push waits for the owner.
