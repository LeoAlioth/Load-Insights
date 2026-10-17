# Roadmap and queue

Priorities, parked work and queued authorisations. Dated decisions by the owner. Check before starting anything listed here.

## Priorities


The owner, 2026-10-03: "as for the forecast, put that on the back burner for now. Detection is our focus for now."
The forecast fixes already shipped that day (stacked signals counted once, cap at the largest hour, band holds
the forecast, linked inputs scoped); the open forecast item (an input's "existence" acting as a date marker,
Attic AC regression) is parked in Load_Insights AGENTS.md "To explore or discuss".

**Why:** detection quality (one device per load across the meter tree, naming from meters' own signatures) is
what he is working on.

**How to apply:** don't start forecast work or propose forecast fixes unless he brings it up; route new forecast
findings to the AGENTS.md back-burner list.

Re-opened by the owner 2026-10-12, these two only (plans li-3 / li-4 in docs/plans/2026-10-12/):
the temperature term engages on statistical evidence instead of a fixed 336 h (lean on the correlation), and the
whole-house forecast is built from its parts (remainder + each device, respecting the meter nesting). Kozolec's
minimum SOC helper excludes the Pond EVSE and Power Strip forecasts - "loads running over target only". The rest
of forecasting stays parked.

Built 2026-10-17 (plan li-4 Part A plus the never-added-back exclusion, spec 2026-10-17-soc-planning-and-pacing.md): the house forecast is
`profile.assemble` over the meter nesting. Signed own part (clamped only in the sum) was the fix that made it match: Home's grid-derived
consumption sits 99 W under its top-level meters, and clamping that remainder at zero put +216 W into the built total. `PART_MIN_WEEKS` 6.

Also parked (2026-10-04): **ramping loads** - an inverter heat pump (the third site's Ecodan ramps 0.2 -> 2.8 kW over 10-15 min, sharp stop) or a slow EV ramp, where no single start step exists. Don't start that experiment until he asks.

## Current stage: cross-meter edges


While the cross-meter edges work (docs/specs/2026-10-14-cross-meter-edges.md) is in stage 1, keep to edge detection and grouping. Problems in how runs/steps are assigned to devices or meters (e.g. a meter step claimed twice, Kozolec 10-05) are a later stage: note them, don't propose fixing them now.

Judge stage-1 changes on the edge card only (conservation, coverage, invariance, tightness, mixed, duplicates, count, mirror); don't run or cite the energy card (device capture / impurity) as pass/fail until the edges become the detector's.

**Why:** the owner, 2026-10-14: "that is not an edge detection problem, but a device assignment problem. I said earlier to not yet look at that." Then: "why were the agents benching the device capture instead of just the edges and their grouping?" - I had made the energy card the rule and rejected a combo over a device-level figure.

**How to apply:** when an agent's report surfaces an assignment issue, file it as "later" in one line instead of offering it as a next step.

## Queued after the weekly reset (set 2026-10-15)


Weekly plan hit 90 % on 2026-10-15 (~10:00); the owner: "wait for reset" (2026-10-16 21:00 UTC). Then, after
checking usage is back under 90 %, run without asking again:

1. **Grouping tolerance sized on each edge's own errors** (the owner: "try the tolerance sized on the edges
   errors") - follows commit 82bd24b (per-property measured errors, weighted distance, unsure flag), whose
   cost was 1 kW+ steps fragmenting (Home single-edge >= 1 kW groups 680 -> 1,257). Report:
   scratchpad scores-report.md.
2. **Kettle merge allowed** (the owner: "that merging is probably ok") - the kettle's groups may merge with the
   matching unlabelled Mansarda B steps (likely kettle boils the 90-240 s truth pairing misses).
3. **Leave chosen loads out of the consumption forecast, in Load Insights itself** (the owner 2026-10-15):
   BUILT 2026-10-16 - Inputs page, "Not in the consumption forecast" (option `forecast_exclude`, dashboard
   devices): their hours come off the series the site forecast is fitted and scored on, so they stop shaping
   its history and level correction (the grid forecast, built from it, leaves them out too). Per-load
   forecast sensors are unchanged. Scored with `tests/forecast_bench.py` (Kozolec Pond EVSE + Power Strip +
   Well Pump, 15 Aug-16 Oct, hourly origins) against the HA-template route (fit all, subtract the loads'
   forecasts): hour-ahead 330.0 vs 331.0 W, day-ahead 326.4 vs 326.8, noon-tomorrow 2.52 vs 2.58 kWh; Home
   (EVBox + Workshop Charger): 771 vs 813, 782 vs 816, 8.37 vs 8.80. Abnormal days: an automatic rule (a day
   whose actual/expected ln-ratio is > z robust sigmas off the median, runs <= 3 days, left out of the fit
   and level, or of the level only; z 2-4) did NOT win - neutral on Kozolec with the loads left out, worse
   on Kozolec as is (hour-ahead 814 -> 819-838 W) and neutral-to-worse on Home - so it is not shipped;
   nor did a different LEVEL_DAMPING (0.25-0.7). After (a) the 14 Oct day is ratio 1.49, not 2.4; the level
   still read x1.33 at 15 Oct 12:00 (boiler, dryer, test loads) and is back to 0.78 by the 16th. A manual
   "ignore these dates" option is open for the owner.
4. Decided: pond = no special rule (angle score ~0 already); Shelly 5 % rule dropped.

**Also after the reset (the owner 2026-10-15): check whether these 2 Oct detector ideas still matter on current code (82bd24b+)** - only check, report, then the owner decides:
5. Settled start size: Home pump (Hidrofor) start sized from its first ~3.3 s (909 W) though it settles ~820 W; idea = follow a young run's drift while no other step is declared on its phase.
6. Split leftover opening a run on a one-device meter that did not rise: Home 27 Sep 13:01, 858 W cut from another load's ~2.15 kW rise landed in pump start cluster 184 (learned partner of 3), stayed open 31 min; ideas = tie-break by which run the meter started, keep split leftovers out of a one-device meter's start group when that meter did not rise.
(0a6a82b meter_stop - the fix for runs outlasting their device - is already in dev.)

The owner 2026-10-15: the missizing answers wait for FRIDAY NIGHT (ask him then). Still needing the owner's answers (missizing study, scratchpad missize/missize-report.md): may the Fleet gain
learning in detect.py change (learn from falls too, median, not gated); hidrofor start decay / pre-stop
drift as part of its run's steps; same-span comparison fix in edges.py.


## Later ideas


Found 2026-10-14 on Home's 3D edge page (ΔP × R × surge): each R arm shows two fine parallel strands.
They are the same loads at two voltages: ~237 V normally and ~244-245 V on sunny afternoons when the
SE17K's export lifts the local voltage (edges at >=243.5 V: 0 % before 10:00 / after 17:00, 37-41 % at
13-15 h; sunny days 20-34 %, cloudy 0-3 %; on every meter).

Physics: a resistive load keeps R and draws more W at higher V (slides along the R = V²/|ΔP| curve);
constant-power electronics keep W, so R ∝ V² - about +5 % R at +2.5 % V, a second strand. 56.6k of
Home's 73.3k sampled edges are < 300 W, mostly electronics.

To do LATER (group level, not per edge; the owner: "okay note that"): across a group's edges at different
voltages, the slope of log P on log V is ~2 for resistive and ~0 for constant power - a load-type
property, and the reason R alone splits constant-power loads into sunny/cloudy groups while ΔP alone is
looser for heaters. Don't group on R alone. Related: ,
.

## Plans in docs/plans/2026-10-12


Ten plans for Load Juggler and Load Insights live in docs/plans/2026-10-12/ with a README.md
index: run order, the owner's decisions, what is open. The owner approved them on 2026-10-12 but will say WHEN to run
them - likely toward the end of that week, depending on the weekly plan usage.

**Why:** he paces big multi-agent work by the weekly usage window.

**How to apply:** don't start any of them unasked; when he says go, follow the README's order, check usage
first (pause at 90 %), and keep installs to Home and Kozolec. Related: ,
, .
