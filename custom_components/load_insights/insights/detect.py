"""Load detection on raw per-phase power. Pure, incremental, persistable.

The unit is the SESSION: one load, from the step up that started it to the
step down that matched it. Steps, not excursions - on a house main the power
never returns to its idle floor while anything else is running, and waiting
for that produced 100-hour "loads" of hundreds of kWh. Other loads may come
and go in between; the pairing is by size, most recent first. Sessions that
start and end together on several phases are one multi-phase session - a
two-phase kiln is 3 kW on A and 3 kW on C, and nothing else has that shape.

Closed sessions are matched to SIGNATURES: phase set, dominant power per
phase (within ~10 % or the noise), duration within a factor, power factor
when known. No match makes a new signature. Signatures carry counts, typical
duration and repeat interval, and an hour-of-day histogram - the material
the naming page describes them with and the forecast will later schedule.

Everything here works sample by sample with a small persisted state per
phase, so the recorder can be read in slices and the detector resumed from
where it stopped. Timestamps are epoch seconds; powers are watts.
"""
from __future__ import annotations

import bisect
import itertools
import functools
import math
import operator
import statistics
from collections import ChainMap
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .classify import MAX_CONFIDENCE as MAX_APPLIANCE, Guess, _fmt_s, _fmt_w, classify
from .phases import phase_mapping, share_split, support, voltage_votes
from .steps import Edge as StepEdge, Step

# A dataclass field that is not stored: a cache, a hook the Fleet or the runner
# sets each pass, or something fed again by the recorder. Every other field of
# PhaseState, Detector and Fleet is in its to_dict, so a restart keeps it -
# tests/test_detect.py's test_every_field_is_persisted_or_derived holds the
# line, and `RESTART=` in the bench the behaviour.
DERIVED = {"derived": True}

PHASES = ("a", "b", "c")
WEEK_SECONDS = 7 * 24 * 3600.0
# Never call a change smaller than this a transition. A FLOOR under the
# measured figure, not a replacement for it - the detector works out each
# phase's own noise from how much it wanders while idle, and this stops a
# pathological signal from setting it at nothing.
#
# It was 100 W, which on both of the owner's sites was eight times the real noise:
# their median sample-to-sample change is 3 to 5 W, so the measured figure
# would have been 12 to 20 W and the floor was the binding constraint on
# everything. A fridge compressor steps 60 to 150 W and could never clear it,
# which is why Kozolec has two fridges and detected neither (2026-09-18).
# At 10 W it is barely above the quantisation of the readings themselves, and
# it is safe to be that low because the level-dependent part below is
# MEASURED rather than assumed. 7 W since 2026-10-27 (swept 10 / 7 / 5 / 3 on
# the Home and Kozolec benches with the blip limits gone: 7 W kept every site
# within 1 point and gained Home's hidden partition +2.6 points; 5 and 3 W
# lost the remote site's fed devices 1.3 points or its hidden impurity 6.6 -> 13.1 %).
MIN_NOISE_W = 7.0
# ...and a reading wanders more when more is flowing through it, so the floor
# is not the whole story either (2026-09-18: "can the noise scale with
# total load or be adaptive in some other way?").
#
# It can, and the share is MEASURED rather than picked, because the two sites
# disagree by a factor of three about what it should be: fitting the observed
# median sample-to-sample change against level gives 0.61% at home and about
# 0.2% at Kozolec - home's signal being a template of two sensors subtracted,
# which is noisier than either. A square root was tried against the same data
# and fitted worse than a straight line (7.4 W of mean error against 5.4).
# So each phase learns its own, the same way it already learns the idle
# figure, and NOISE_REL_CAP only stops a pathological signal declaring itself
# all noise.
NOISE_REL_CAP = 0.05
# How far ABOVE the level at which one quantum equals the cap a reading has
# to be before its ratio is worth recording. One is the boundary itself, and
# the boundary is the worst admissible point rather than a safe one: a sample
# taken there contributes a ratio equal to the cap and drags the estimate to
# it. Swept on both sites' data - see the commit.
NOISE_REL_FLOOR_FACTOR = 1.5
# ...and where a ratio starts to mean something, which is NOT a number of
# watts. It was 300 W flat, carrying the comment "below this a ratio is
# mostly quantisation" - standing in for the very thing the detector now
# measures. See PhaseState.rel_floor, which derives it from the reading's own
# resolution and noise against the cap above, so no site needs telling.
# A house-consumption reading below this is not a reading. Home's template
# sensors are inverter/3 minus the meter, recomputed whenever EITHER input
# updates against the other's stale value, so a passing cloud puts one sample
# at -3000 W and the next back at 26 - and that +3000 step back is the exact
# shape of a load switching on, on all three phases at once. Rare (134 of
# 195,890 samples on one phase over ten days) but each one is a phantom
# 3 kW load (2026-09-18).
GLITCH_FLOOR_W = 200.0
# 5 since the noise is measured by how the reading moves (NOISE_FROM_MOVES):
# at 4 Home's house phase C came down to 60 W, just under a 65 W load cycling
# every 25-50 s, and the half-seen load's steps paired with the floor mat's
# (5.9 h of the mat outside its heating, against 1.8); at 3 the load was seen
# whole and crowded the groups (Home purity 71.0 %, 10.2 kWh wrongly placed).
# Ten days, 2026-10-01 - see the experiments branch's commit for the table.
NOISE_MAD_FACTOR = 3.0              # with EDGE_BY_METER, always on: the owner wants the 65 W load seen (2026-10-01)
# The noise is re-learned from how far the reading MOVES between samples, as
# the seed measures it, not from how far it sits from the level. A load
# cycling just inside the band - Hiša's phase C, 65 W every 25-50 s - sat
# 30 W from the level on average, set the floor at 4 x 30 W and so stayed
# inside it for good: 1,616 of its steps on 28.09 counted from a start on the
# 19th, none from a start on the 21st. A held level moves by nothing.
# Only a move from a reading at the level too: back at the level after a dip
# of a load nothing holds open, the move from the dip is the load's, not
# noise - Home's grid b, 10-12 19:20, a load cycling 280 <-> 660 W under a
# floor that had taken it in slid 360-390 W "idle" moves and held the noise at
# 700 W for an hour (2026-10-14, once the implied zeros moved the floor there).
# ^ NOISE_FROM_MOVES: always on, the name this note goes by elsewhere
# A new level is confirmed by TIME, SUSTAIN_CADENCES of the meter's own
# reading cadence (its shortest usual gap), never by a count of readings: the
# recorder writes only changes, so a meter that says nothing for that long has
# HELD its value, and its silence confirms the level as a reading would (the owner,
# 2026-10-01). Counting readings, the hidrofor's plug - polled every 10 s, one
# zero after the pump stopped and nothing written for 15 minutes - could not
# confirm the stop, declared -253 of its -809 and carried a phantom 590 W into
# the next start. Replaces a rule for meters slower than 20 s only.
# One rule for every meter, change-driven or polled: silence up to
# SUSTAIN_CADENCES of its cadence is the ordinary spacing of its readings,
# longer is its value HELD, unwritten. So a level is confirmed over that long
# (the hidrofor's plug, every 10 s: 20 s - the owner), a change seen first after a
# silence happened at most that long before (a span's start - see
# PhaseState.span_start), and a silence that long confirms a pending level
# (see PhaseState.silence_due). Confirmed over the PERIOD - the median gap - instead,
# Hisa's 3EM, reporting on change within 4 s but writing every 15 s when
# little moves, held a level 30 s: the floor mat merged into the kiln's start
# as one +3,516 W step with a span 28.8 s long. Three, on the moving-gap
# cadence (see CADENCE_GAPS), ten days, hard placement, energy gated at 0.3:
#               Home pur/wconc  mat over  F0.5 / wrong kWh  Koz wconc  F0.5 / wrong
#   2 cadences   73.1 / 42.3     2.5 h    21.5 % / 1.5       97.6     83.1 % / 16.2
#   3 cadences   75.3 / 48.2     4.2 h    42.5 % / 7.8       98.1     84.2 % / 17.1
# At two, Home's grid meter (cadence 1.1 s, writing every 2.0-2.3 s) read its
# ordinary gaps as silence and its energy recall fell to 5 %.
SUSTAIN_CADENCES = 3.0
# ...and for a meter that reports on change (a Shelly: its readings stop while a value holds, so a short pulse is
# two readings, and three of its cadences is longer than the pulse - Home's kiln on Hiša, 2026-10-26). A meter is one
# when more than CHANGE_DRIVEN_SHARE of its last CADENCE_GAPS gaps are longer than CHANGE_DRIVEN_TAIL of its
# shortest ones (the 10th percentile): a polled meter's gaps are all about one poll (the grid 2 %, a Victron 12 %,
# Mansarda's 3EM 3-6 %, the UPS 4 %), a meter reporting on change has long silences (Hiša 27-30 %, the 60 s
# heartbeats 70-87 %, Blaž's PC 44 %). All counts and shares. Two cadences where three confirmed a pulse the meter
# had shown as two readings late (the kiln on Hiša: 215 -> 241 two-phase runs, 17.1 -> 17.6 kWh; Home partition
# impurity 9.1 -> 7.7, circuits fed); 1.5 added nothing. Set Hiša and Mansarda to change-driven on the settings page:
# the classifier is within 2 points of its cut on Hiša, and forcing Mansarda is better than auto for its own loads.
# A Shelly that reports regularly (Kozolec's Pond EVSE, 10 s) must NOT be forced: impurity 2.9 -> 6.6.
SUSTAIN_CADENCES_CHANGE = 2.0
CHANGE_DRIVEN_SHARE = 0.18
CHANGE_DRIVEN_TAIL = 3.0
CHANGE_DRIVEN_MIN_GAPS = 100
# A meter reports its readings of one moment as separate entities, stamped
# milliseconds apart. Home's grid is the SolarEdge meter plus the inverter: the
# inverter's write 28 ms before the meter's showed the old level, and a kiln-
# sized step got a span of -0.0..0 - a 1 % overlap with Mansarda's own. A
# reading this close to another is the same update: not evidence the old
# level held, nor a gap between readings. Those pairs are all under 0.2 s
# (140,270 of Home's within 50 ms); real readings come 0.8-1.0 s apart -
# Home's grid meter 3,064 times, the Victron's refresh 607 times between 0.5
# and 1.0 s - and nothing between (the owner: some meters do update every second).
SAME_UPDATE_S = 0.5
SUSTAIN_SAMPLES = 2            # a level change must hold this many samples...
SUSTAIN_SECONDS = 5.0          # ...and this long, until the reading's own interval is known
# ...and at least this many of the reading's OWN measured sample intervals,
# which is what the guard needed all along: SUSTAIN_SECONDS is below both
# sites' 6 s interval, so any two consecutive samples cleared it and a
# transitional value - a ramp, a half-caught switch - could found a level.
# 1.5 means about three readings at 6 s. Swept on both sites and on the kiln,
# which has no sub-meter and so is invisible to the ground-truth score:
#   Kozolec  wconc 72.2 -> 87.3 %, purity 96.4 -> 98.0 %; held out 73.8 -> 84.2
#   the kiln full-size sessions 272 -> 317, spurious ladder 236 -> 145
#   Home     held-out purity 64.6 -> 68.3 %
# Home's metered loads alone peak at 2.0, but from 2.0 the kiln loses pulses
# (-> 239 -> 211): optimising the score alone would have eaten a quarter of
# it. The cost is on loads that WANDER rather than switch - Home's NASA
# station (computers) lost ground, since it rarely holds a level for three
# readings - which ALIKE_MAD_SHARE largely gives back (2026-09-23).
# SUSTAIN_INTERVALS (1.5 reading intervals) is replaced by SUSTAIN_CADENCES of the cadence
# A level HOLDS when its readings agree with each other, not merely when every
# one of them is away from the old level. The guard above only asked the
# second, so a run of readings that were each "not the old level" founded a
# level at their median even when they were a drift, a half-caught switch and
# the real new value: Home's hidrofor stopping read [1251, 1082, 436], the
# median made it a 198 W step, which closed a four-hour-old 179 W start, and
# the pump's own session ran on for 7.8 hours (2026-09-23). With this on, only
# the readings that agree with the newest one count towards the guard and the
# level; the ones before them are the transition. 0 is the old rule.
# Measured on the bench against the old rule (2026-09-23, one-call replay):
#   Home     purity 76.6 -> 78.7 %, held out 75.0 -> 78.6 %; hidrofor held out
#            208 -> 213, and the pump's runs filed cleanly 236 -> 247
#   the kiln full-size 399 -> 397, single-leg 158 -> 141, main signature x321 -> x335
#   Kozolec  boiler 499 / 337 -> 500 / 339; its Scala2 (which ramps) 140 / 75 -> 121 / 67
# ^ SUSTAIN_AGREE: always on, as the 2026-09-30 ablation found (AGENTS.md)
# ...but a load that WANDERS never agrees with itself for long, and would hold
# the level still while it wandered. After this many sample intervals of
# readings away from the level, the old rule decides.
SUSTAIN_AGREE_MAX_INTERVALS = 12.0
# How close two readings must be to AGREE, in multiples of the smallest step
# worth calling one (noise_at). That figure compares a reading with a smoothed
# level; two readings each carry the noise, and independent noise adds in
# quadrature, so the same judgement between two readings is sqrt(2) wider.
SUSTAIN_AGREE_TOL = 1.414
# ...or within this share of the step they are making. The noise figure alone
# was too strict under load: the kiln's off readings on phase A wander 19 W
# against an idle-noise allowance of 14, and whole pulses were lost. At the
# pairing tolerance, the step measured from either reading pairs the same way.
SUSTAIN_AGREE_REL = 0.15
# WHEN a step happened: 0 dates it at the first reading that left the old
# level, 1 at the first reading more than half-way to the new one. The first
# is fooled by a load that sags before it stops - Home's hidrofor then ends a
# reading early - the second treats a start and a stop alike. On top of
# SUSTAIN_AGREE (2026-09-23): the kiln single-leg 141 -> 118, main signature
# x335 -> x384; Home held out hidrofor 213 -> 220, workshop boiler 29 -> 38;
# Kozolec's boiler unchanged and its Scala2 held out 67 -> 53. The kiln's
# session LENGTHS were already right - 42.0 s against 42.0 s timed off the
# grid meter itself - so this is about which readings count, not about length.
# ^ STEP_AT_HALFWAY: always on, as the 2026-09-30 ablation found (AGENTS.md)
# ^ INTERVAL_PERCENTILE: the median of the last 60 gaps, gone (2026-10-02).
# The recorder writes only changes, so a leg whose value holds between polls
# has its median gap at two or three polls however fast its meter writes;
# the old test of it passed only because its quiet leg skipped exactly a
# third of an alternating value's readings. The interval is the moving gaps'
# tenth percentile now - see CADENCE_GAPS.
# The same relaxation, but only for a leg whose partner on another phase - a
# balanced edge that started with it - is stopping at the same moment. See
# Detector._corroborate. One reading is enough: the other leg is the evidence.
# Swept on Home (Kozolec is single-phase and so, correctly, untouched):
#   the kiln   full-size sessions 337 -> 374 of 441 real pulses,
#              single-leg 154 -> 96, ladder 92 -> 98
#   held out   workshop boiler 34 -> 39, hidrofor 216 -> 208, NASA 17 -> 23,
#              purity 77.6 -> 75.0 %
# The hidrofor's loss is small, consistent across every variant, and not yet
# explained; the kiln's gain is the point, and steady loads come first
# (2026-09-23).
# ^ CORROBORATED_STOP: one reading, no sustain time; 2 was worse (AGENTS.md)
# What counts as "the same load on another leg": started and stopping within
# this many sample intervals of each other, and drawing at least this share
# of each other's power. Far tighter than the merge test on purpose - see
# Detector._corroborate. 1.0 kept the most of the hidrofor.
CORROBORATE_INTERVALS = 1.0
CORROBORATE_BALANCE = 0.7
# A fall corroborated by another phase's in the same instant (the instant test, _corroborate) must be at least this
# many noises tall - a count; the legs of a 3 kW load are 80 noises, a coincident wobble a few.
CORROBORATE_MIN_NOISES = 20.0
# When a stop is corroborated, close the edge the other legs vouched for, not
# whichever open edge of that size is newest. Home's Kompresor leg (~840 W)
# and hidrofor (~870 W) share phase A inside one pairing tolerance, and the
# compressor's corroborated stop was closing the pump's session.
# ^ CORROBORATED_CLOSES_ITS_EDGE: always on, as the 2026-09-30 ablation found (AGENTS.md)
# A leg that drops by its partner's size while its own start was BIGGER - some
# other load switched on in the same poll and was measured into the step - is
# that leg stopping, not a step down of one load still running. Split it: the
# vouched-for part closes, and what is left stays open as the other load.
# Without this the kiln's leg ran on with the leftover and could not merge
# with the partner that had stopped (cause 3 of the single-leg entry in
# AGENTS.md). Only a partner that has actually CLOSED counts here - see
# Detector._corroborate. Swept on the 6-hour-sliced bench (2026-09-23):
#   Home     purity 77.6 -> 78.6 %, held out 79.7 -> 80.6 %; hidrofor 493 -> 500
#   the kiln main signature x385 -> x397, single-leg 116 -> 112, ladder 18 -> 23
#   Kozolec  identical, as a single-phase site must be
# ^ CORROBORATED_SPLIT: always on, as the 2026-09-30 ablation found (AGENTS.md)
# A meter's interval - its cadence - is how often it writes WHILE ITS VALUE MOVES: the gaps
# after a reading that moved by more than its noise, over the last
# CADENCE_GAPS of them, at MOVING_PERCENTILE. One rule for a polled meter and
# one reporting on change. Of ALL gaps, a low percentile took Kozolec's
# Victron - polled every 5.3 s, plus a refresh of every entity once a minute
# landing anywhere in the poll - at 2.3 s while the EVSE charged: a normal
# poll then counted as silence, a 2.9 s sustain confirmed the boiler's
# half-caught start, and its stop closed the EVSE's charge (27.09 11:37). A
# high one took the IR panel and Susilna - 60 s heartbeats that report a
# change within 5-12 s - at 60 s. After a move, ten days of history, p5 / p10 / p25:
#   Victron 2.3 / 5.1 / 5.2      grid meter (Home) 1.1 / 1.6 / 1.9
#   hidrofor 9.9 / 9.9 / 9.9     EVSE 10.0 / 10.0 / 10.1
#   IR panel 3.0 / 4.9 / 5.1     Susilna 11.3 / 28.3 / 60
#   Hisa A 4.1 / 5.0 / 6.0       Mansarda C 4.1 / 4.1 / 4.9
# The 10th is the lowest that skips the Victron's refresh (at most 9.3 % of
# its post-move gaps in any 600; past 10 % it drifts to ~4 s, not 2.3).
# One measure for everything that asks how often a meter writes - the
# sustain, a silence, a span, the event and corroboration windows, the
# crowding, the sample counts, the Fleet's tolerances and waits (2026-10-02;
# a median of all gaps and a 5th percentile before ten moves were two more).
# Taken from the first moving gap (the smallest until there are ten), so a
# cold start needs nothing else, and the gaps are stored, so a restart goes
# on where it stopped: a Shelly heartbeating once a minute keeps 18 of 20
# cycles from cold and 20 of 20 restarted (14 and 16 with the moving gaps
# neither used from the first nor stored).
CADENCE_GAPS = 600
MOVING_PERCENTILE = 0.10
BASELINE_EMA = 0.02            # idle baseline drifts slowly
BASELINE_SEED_SAMPLES = 24     # two minutes at 5 s; the seed takes a LOW percentile, not the median,
BASELINE_SEED_PERCENTILE = 0.25  # so a window that begins mid-load does not call the load the floor
SLOW_FOLLOW = 0.02
# how many idle samples the pre-step reactive median is taken over
Q_RECENT_SAMPLES = 8             # the held level follows drift this fast, so a ramp is never a step
# A cloud IS a step at the meter, and a big one. It is the sun switching,
# not a load, and it gives itself away by moving PV the opposite way at the
# same moment. The share is a RANGE because the array's reading may be this
# phase's own (share about 1) or the inverter's total (about a third of it
# on each phase), and three phases are never quite balanced either.
PV_SHARE_MIN = 0.25
PV_SHARE_MAX = 1.25
PV_MIN_SAMPLES = 30
# A reading that CONTAINS generation goes below zero whenever the site
# exports; one that carries the house alone cannot. Both thresholds are
# deliberately slack: a meter's noise sits well inside 50 W, and half a per
# cent of a day is minutes, not a spike.
EXPORT_FLOOR_W = 50.0
EXPORT_SHARE = 0.005
# ...and a share means nothing until one reading is worth no more than it:
# below this a single dip decides the verdict. Derived from EXPORT_SHARE, not
# tuned. A live pass reads one minute - some 25 readings - and judged on its
# own it switched the house's floor guard off on every pass, until one cloud
# edge left Home's phase B floor at -1483 W for good (2026-09-25).
GENERATION_MIN_SAMPLES = int(round(1.0 / EXPORT_SHARE))
# Below this a reading is carrying nothing - a generator's idle AC input port
SOURCE_IDLE_W = 25.0
# a reference circuit has to carry something this often to be one
LIVE_SHARE = 0.2
MATCH_EDGE_REL = 0.15          # a step down pairs with a step up this close in size, or the noise
# A fall closes the open run whose GROUP it ends best (PhaseState._score, run
# lifecycle 2026-10-28): within PAIR_TIGHT_NOISES of the two steps' noise of
# the run's own rise while the two groups have no partner evidence, the learned
# ratio's spread once they have; the best score at PAIR_SCORE_MIN or more
# closes; an age's distance from the learned length counts in spreads, never
# under PAIR_LENGTH_SD_MIN (log seconds). Bench dials (spec open point).
PAIR_TIGHT_NOISES = 3.0
PAIR_SCORE_MIN = 0.0
PAIR_LENGTH_SD_MIN = 0.1
# A fall that closed nothing and a rise of its size within this many of the
# phase's readings after the sustain (PhaseState.latency: no dip shorter is
# ever declared) are a dip of a running run (PhaseState._dip): two
# levels of it, no run of their own. Bench dial (spec open point).
DIP_INTERVALS = 3.0
CANNOT_KEEP = 200              # the cannot-link pairs a step group keeps (StepGroup.cannot)
# A valley between two piles of step sizes cuts them into two groups only when
# the shallower pile stands this many Poisson spreads above it (_significant),
# and no group spans more than GROUP_SPAN times its smallest step (the owner,
# 2026-10-28: 3, wide enough for any device that is not multi-speed or
# multi-step). Bench dials (spec open point): 0 keeps every valley as before
# the run lifecycle, the default until the owner has seen the sweep - at 2 the
# oven and its fan (2 kW, 900 W, one run each) are one group until overlaps
# split them (Detector.split_groups).
VALLEY_SIGMAS = 0.0
GROUP_SPAN = 3.0
# A load running on its own is followed as it SAGS, and its stop may close it
# at the size it has sagged to as well as the size it started at. A fridge's
# compressor draws ~58 W at the start and ~44 W by the end of a 28-minute run;
# the stop was 14 W short of the start, past Kozolec's ~10 W tolerance, so it
# read as the fridge stepping down to 14 W and running on - and closed at some
# later drop, a "run" of 1.8 h (2026-09-28).
# ^ SAG_CLOSE: always on, as the 2026-09-30 ablation found (AGENTS.md)
# Every step the house meter takes is also learned as an EDGE, on its own:
# its size and factor, whether it surged, and what each input was doing at
# that moment - which of its changes came nearest, how long before or after,
# and what each number read. Edges cluster into a library of their own,
# whatever they get paired with, so a load's stop teaches its stop cluster
# even when the pairing got it wrong; a device is then the start, step and stop
# clusters its runs used (2026-09-29: "separate objects that we then just
# group to devices"). How far an input's change is looked for either side:
EDGE_LAG_REACH_S = 60.0
EDGE_LAG_BIN_S = 2.0
# An input's lag to the meter is learned from where its changes pile up
# against edges - Home's thermostat reports 5 s before the meter shows the step,
# the middle half within 4-7 s - once this many have been seen; until then a
# change within EDGE_WINDOW_DEFAULT_S counts.
EDGE_LAG_MIN = 50
EDGE_WINDOW_DEFAULT_S = 10.0
EDGE_WINDOW_SPREADS = 3.0
EDGE_WINDOW_MIN_S = 3.0
EDGE_LIBRARY = 150             # clusters kept per phase and direction
# Which cluster a step joins is decided by where the steps of its kind PILE UP,
# not by a fixed +-10 % around whichever cluster happened to be made first.
# Per phase, direction and what its inputs did, the steps' sizes are kept as a
# histogram fading over EDGE_TAU_S, on a scale where one unit is one
# measurement error (EDGE_NOISE_SHARE of the phase's noise at small steps, EDGE_SCALE_REL of the step at
# large ones); smoothed by EDGE_KERNEL units, it is cut at its valleys, and a
# step joins the cluster that owns its segment. Against the 7,858 events
# Home's device meters labelled over 7-28 Sep, V-measure 0.436 ->
# 0.593 and each device's share in its biggest cluster 0.52 -> 0.82, with 78
# clusters instead of 498; scikit-learn's Gaussian mixtures, HDBSCAN and DBSCAN
# all did worse, and splitting by the V x I power factor hurt every method
# (2026-09-30). It replaced joining the nearest cluster within +-10 %.
# ^ EDGE_BATCH: the name this note goes by elsewhere
# One measurement error at small steps is EDGE_NOISE_SHARE of the phase's
# measured noise - the QUIETEST it has measured: set at a group's first step
# and re-binned whenever the phase's noise falls below it, never when it
# rises (_rescale). Fixed at the first step, it was hostage to that moment:
# on Home's 23 Sep - 2 Oct the phases' first rises came minutes after a dawn
# seed read 372 W of noise on A and 218 W on C (10 W at night all ten days),
# a 93 W unit merged A's starts of 12-370 W into one cluster, and every +14 W
# creep rise "started again" and ended the 261 W run of Mansarda's
# washer-dryer 6 s in. Down only, it settles the first quiet night and never
# rides the daily swing (following the noise both ways was ruled out,
# AGENTS). Home's card, hidden / circuits / fed devices 19.7/15.9 -> 33.9/16.7,
# 32.9/16.1 -> 44.0/12.9, 53.9/11.0 -> 67.3/7.6 % capture/impurity; Kozolec
# and the third site identical, their units already at the floor (2026-10-04).
# A fixed 15 W unit suited Home (whose phases' noise is 10, 35 and 114 W) and ran every small fall at quiet
# Kozolec (10 W) into one cluster - its fridges' runs then closed at 8 % of
# their length (75 of 248 right, against 175 at 5 W). Benched at a quarter,
# half and all of the noise: a quarter wins at both sites - Home 82.6 / 66.4 %
# against 80.9 / 61.3 and 76.3 / 50.2, the pump 1153 clean against 915 and 410,
# Kozolec's fridges 188 right against 154 and 81 (2026-09-30).
EDGE_NOISE_SHARE = 0.25
EDGE_SCALE_REL = 0.02          # ponytail: fixed; the phase's voltage spread is the upgrade
EDGE_BIN = 0.25
EDGE_KERNEL = 1.0
EDGE_TAU_S = 10 * 86400.0
# The histogram is cut at its valleys again at every step, the step in it. ^
# EDGE_RECUT: cut every 32 steps instead (or when a step fell outside every
# segment), the cuts were counted from the group's first step, so one step
# dropped or added moved every later cut, and a step near a valley went to
# another cluster for the rest of the replay: Home hidden, over five replays
# apart only by a reading in 10,000 dropped or 1 ms of jitter, read 13.2-25.2 %
# impurity over its devices, cut at every step 18.2-19.3 (2026-10-04). A card
# takes a quarter longer (504 -> 651 s).
# A start is an ALL-PHASE EVENT: rises on different phases within this many
# seconds, the smallest at least EVENT_BALANCE of the largest, are one event,
# clustered on their phase pattern and total size. Home's three-phase
# compressor (~840 W a leg) and its single-phase pump (~900 W on A) are the
# same size on A and both motors; per phase they shared one cluster and the
# compressor's legs chained the pump into its device. As events they are
# (840, 850, 830) and (900, 0, 0). The legs switch together; what spreads
# them on the meter is the reading cadence - each phase's step is stamped at
# the first reading that shows it - so the window is that many of the slowest
# phase's intervals (the owner: from the sensor's readout, not a constant). Home's
# phases tick every 2 s and the legs' offsets there run to 4.2 s at the 90th
# percentile, 0.0 s median (ten days, 2026-09-30); the pump gets a false
# companion within 5 s 1 % of the time. A real multi-phase load shows on its
# other phases at far more than 20 % (the owner). Rises only: a fall closes a run
# on its own phase at once, so it keeps its per-phase cluster.
# Three, measured (Home, 19-20 Sep 2026): at two intervals the compressor's
# three-leg event never formed (13 'abc' steps in two days, no cluster of
# five), at three it did (70, one cluster of 28 at 2,520 W), at four little
# more (88) with more coincidences. The reason two is not enough there: the
# house reading is a template that moves whenever EITHER of its inputs ticks,
# so its interval (2 s by day) is shorter than the grid meter's own per-phase
# cadence (4-6 s), and a leg's step shows only when its phase's meter reading
# arrives. A window learned from the pile-up of leg offsets, as lag_window
# does for inputs, is the upgrade if coincidences prove costly.
EVENT_WINDOW_INTERVALS = 3.0
EVENT_BALANCE = 0.2
# A second clustering dimension - the step's reactive angle, atan2(dQ, dP)
# in degrees. Within a size segment, steps are cut again at the valleys of
# their angle density: a pump (~35 deg) and a heater (0 deg) of one size are
# two kinds of edge (Hart 1992; Barsim & Yang 2014 cluster dP and dQ
# together). Benched on 2026-10-12 with ten days of Home's signed var: Home
# fed devices capture +3.7 / impurity -1.3, hidden partition +3.9 / -1.9, Hiša
# hidden +9.9 / -4.0, Kozolec hidden impurity -5.5 (its unsigned V x I var still
# separates), the third site +1.3 / -1.7; fragmentation no worse. A step with no var
# reading has no angle and is cut by size alone.
EDGE_ANGLE_BIN = 3.0           # degrees
EDGE_ANGLE_KERNEL = 6.0        # degrees, the smoothing before the valleys
# A house step that meters below it stepped with at the same moment is
# several steps: each meter's own share (less its sub-meters', see
# _meter_steps), and the rest. Kozolec's boiler pulses 1.9 kW about
# once a minute, and a car charge that started in the same reading was booked
# at 5.3 kW - then closed when the boiler's pulse ended, the 8 h charge lost
# (27.09 10:17). The meter knows its own share (2026-09-30: use the
# sub-meters to avoid it).
# ^ SPLIT_BY_METERS: always on, the name this note goes by elsewhere
# A single-phase step's place - the innermost meter that saw all of it - is
# part of what kind of edge it is: Home's 65 W load cycling inside Hisa and
# the NASA strip's small loads under Mansarda share a size on phase C and
# nothing else (2026-10-01: "detect it fully"). Only a start is placed:
# a device is its start cluster. Benched and ruled out (2026-10-01, AGENTS.md's
# experiments table): "soft" - a place keys the group only where its steps
# pile up above chance - and placing every step.
# ^ EDGE_BY_METER: the name this note goes by elsewhere
# A meter's step is the step its OWN detector declared - held until settled,
# the median of what it held - exactly as the grid's is measured, never two
# raw readings either side of a window: one measure, no second method (the owner,
# 2026-10-01). A meter's step is all of the grid's within both meters' noise
# together plus METER_CAL_SLACK of the step, once each meter's own gain
# against the grid is learned (METER_GAIN_MIN steps both saw clearly, for its
# power and for its reactive part alike). Wider windows were tried and placed
# and split wrongly: a reading interval is a meter's heartbeat when steady,
# not how soon it reports a change (kiln ladder 31 -> 108; mat 13.1 h).
METER_CAL_SLACK = 0.05
# Benched both ways 2026-10-23 (the owner: keep what wins, drop what does not matter): a
# grid piece a meter's bigger clean step explains is the meter's while it is at least
# half of the step (_step_meter) - no floor made no difference on the dishwasher scorer
# and read better for the Hidrofor on the card (impurity 12.8 against 27.4), but it is
# the floor that keeps a 263 W blip from being the EVSE's 3.6 kW fall (Kozolec 09-28
# 13:53, test_a_meters_stop_ends_its_run_only_through_a_fall_that_accounts_for_it), so
# half it is, as _meter_stop's; and a meter's step must point the grid step's way to be
# its share (_meter_totals' own): without that check the Sock Eater lost a burst, 33
# more sessions were filed and concentration fell.
METER_GAIN_MIN = 10
METER_GAIN_BOUND = 0.25       # a learned gain stays within this of one either way
# The two meters' spans of one change overlap - that it is one change, and how
# much is a timing confidence - and are compared over their UNION, grown by
# every step either declared whose span reaches into it, up to this many of
# the SLOWER meter's reach - the grid's event window, or the sub-meter's own
# sustain, as far back as its span can start: each meter's net change there
# (2026-10-01). Of the grid's window alone, Home's cap was 24 s and a
# 10 s plug's span, three cadences, 30 s.
UNION_CAP_WINDOWS = 4.0
# How sure the detector is of each step and run, 0..1 (2026-10-01): a
# step's size against the noise at its level (full at QUALITY_SNR_FULL times
# it), how closely the readings it settled on agree, and whether another step
# on its phase came just before it; a run's is the lesser of its start's and
# its stop's, times how closely the two agree in size. A run no stop was seen
# for - ended by the reading, or by its cluster rising again - takes
# QUALITY_UNSEEN_STOP for its stop.
QUALITY_SNR_FULL = 6.0
QUALITY_CROWDED = 0.6
QUALITY_UNSEEN_STOP = 0.5
EDGE_HELPED_SHARE = 0.3        # the naming page names an input once it came with this share of a load's edges
# B1 - edge PAIRS: the rise that starts a run and the fall that ends it, one
# cluster each, learned from every run that closes: how often, the stop's size
# against the start's (a fridge sags, ~0.75; a heat-pump water heater climbs),
# and how long the runs last. A pair is accepted once it has closed
# PAIR_MIN_RUNS runs and has come together far more often than chance would
# put its two clusters together: with N closes on the phase, a switch-on
# closing n_R of them and a switch-off n_F, chance gives n_R n_F / N, and the
# Chernoff bound must put the odds of its count under 1 in ABOVE_CHANCE_ODDS.
# It replaced fixed shares (30 % of both clusters' closes, and 20 % for the
# device links that have since gone), which were numbers to tune
# (2026-09-30: "can we have it tune itself?"): Home's floor mat 21.7 ->
# 11.5 h counted while its thermostat was not heating, everything else within
# a run or two. The odds do not matter - 10, 100 and 1000 bench identically:
# a real pair is hundreds of times above chance.
PAIR_MIN_RUNS = 8
ABOVE_CHANCE_ODDS = 100.0
# How often, on the readings' clock, the fleet works out again how far behind
# its meters the grid is read, from the lags learned so far (Fleet._horizon):
# on a fixed grid of the clock, so a pass's length cannot decide it.
MODEL_REFRESH_S = 3600.0
# Each meter's lag against the grid, learned from the steps the two share
# (Fleet._learn_lags, the owner 2026-10-02: "how long we need to wait before we
# have complete data from all sensors"): looked for this far either side of a
# grid step, kept over the last LAG_SAMPLES, believed from LAG_MIN_SAMPLES, at
# this percentile - the wait that rarely comes up short.
LAG_REACH_S = 300.0
LAG_SAMPLES = 200
LAG_MIN_SAMPLES = 20
LAG_PERCENTILE = 0.95
# ...and signed (2026-10-14: "they should be able to go negative"): a
# meter whose readings run AHEAD of the grid's has its lags below zero, and
# the other tail, the (1 - LAG_PERCENTILE) one, is how far ahead - its LEAD
# (PhaseState.lead). Clipped at zero, a meter's span never reached the grid's
# step that came after it: Home's grid read its step a median 4.6 s after the
# 3EM's in 63 % of the Hiša moments that did not add up, Blaž's plug 92 %
# (the first edge card, 2026-10-14).
# The lags are learned from every step the two show, not only the clear,
# one-of-a-kind ones (2026-10-14: "more pairs / longer time match
# window"): each grid step's nearest step that way of its size on the meter
# within EDGE_LAG_REACH_S, chance and all, and the percentiles are taken over
# those where the lags PILE UP (_shared, as an input's lag window is found):
# chance spreads evenly over the reach. Clear, unique pairs within the two
# meters' latencies taught Hiša 200 of its last 2,465 shared steps, and
# Mansarda none in ten days - its unmapped third channel made it "a meter on
# several channels" of every phase - so its lag and lead stayed 0. The small
# steps lie wider than the clear ones: at Hiša the clear ones' nearest pairs
# pile at grid - meter -4..+2 s, the small ones' too with a shoulder to +10
# (a ~62 W load the grid shows 3-10 s after the 3EM, 543 of the moments that
# did not add up). Of those moments, the parent's step of the children's size
# "off" the moment (the edge card's median +5.8 s) is chance in 87 %: spread
# evenly over the 120 s either side looked in, the children ~62 W (2026-10-14).
# A run is filed by its DEVICE - the component of edge clusters its start
# belongs to - into the signature most of the device's runs went to, or a new
# one for a device not seen before; signatures whose runs are one device's are
# one signature. No run is filed, and no two signatures merged, because their
# power looks alike: that was a second grouping beside the devices, and the
# one that undid what the edges knew (2026-09-30: "is this old layer even
# necessary still? ... this will clean up a lot of redundant code"). A
# sub-meter's word, the switch gate and a rare input value still decide first.
# Against filing by likeness: Home 82.6 / 64.6 -> 82.4 / 68.7 % with 98
# signatures for 199, the floor mat's hours outside its thermostat's heating
# 10.3 -> 2.7 (precision 95 %, caught 58 %), Kozolec 98.5 / 93.1 (2026-09-30).
# ^ DEVICE_FILING: the name this note goes by elsewhere
# C - pairing by them: a fall first closes an open run its cluster's accepted
# pair starts with, at the learned size, whatever plain sizes say; only a fall
# with no pair model falls back on sizes. And the old guess that a drop fitting
# nothing is the newest bigger load stepping down goes (2026-09-29: "the
# 200w step should just stay unmatched") - except a load settling just after
# its start.
# ^ PAIR_PAIRING: always on, as the 2026-09-30 ablation found (AGENTS.md)
SETTLE_SHARE = 0.3
# A pump's start reads high and decays over its first seconds (Home's Hidrofor:
# 960-1056 W declared, ~845 W held). Two parts, benched together (INS-8 a, INS-9 2;
# shipped 2026-10-20, AGENTS.md):
# - while a run is the only one on its phase and younger than SHAPE_SETTLED_S, the
#   phase's level follows the readings at SETTLED_FOLLOW per reading instead of
#   SLOW_FOLLOW, so what the run is followed to (``now``) and the size it is booked
#   at on closing (_close) are the level it settled at, not its first ~3 s;
# - a run's leading levels that only fall (its start decay, which the grid declares
#   as small falls that join the run as levels) are folded into the start, booked at
#   the level it settled at; each within SETTLED_SHARE of the start, all within
#   SHAPE_SETTLED_S of it.
SETTLED_FOLLOW = 0.25
SETTLED_SHARE = 0.35
# INS-9 (1), the Fleet's cross-meter gain (benched 2026-10-19/20 as three dials, shipped
# 2026-10-23 as plain behaviour - the owner: where the values' outcomes are alike, take the
# most unified code and drop the option): it learns from falls as from rises (a fall goes
# through _step_meter as a rise does), from EVERY meter whose one clean step faced the
# grid's one step within METER_GAIN_BOUND (not only the meter the step was placed on), and
# reads as the median of the last GAIN_RING log ratios, not a running mean. On the card no
# site moved by a point; on the edge truth card the Hidrofor 42.7 -> 47-48 %, Kozolec's
# boiler 69 -> 73 %. A library stored before the ring (generation <= 20) reads its mean.
# steps-and-claims stage 2 (2026-10-23, the owner: rises and falls handled alike): a
# fall is placed at the meter that saw it, as a rise is, so a stop is grouped under
# its owner and not plain - at every meter, one-device plugs too (the owner, 2026-10-23:
# no asymmetry between rises and falls). Benched against falls at several-device
# meters only: Home fed devices 64.7 / 11.2 -> 63.1 / 12.7, which is two plugs -
# the EVBox 79.5 -> 74.0, one charge of its ten changing shape (its band is 4.3),
# and the Hidrofor's impurity 10.3 -> 12.6 from a few runs; Kozolec identical.
# Read as neutral, not a loss (the owner).
# A grid run whose start a meter's own step placed is that meter's (the placed claim,
# no dial): it waits for the meter's own session (Fleet._ready_at) and pairs with it on
# time alone (_session_pairs), and a piece of it the grid opened again inside the
# meter's run is its too (Fleet._owned, _decide). Benched 2026-10-23 as a two-level
# dial, both levels kept: the dishwasher's runs 8 signatures -> 6, its best 3 -> 6 runs;
# Home fed 65.6 / 10.8 against 66.1 / 10.7, the plugs held (the owner: stays).
GAIN_RING = 200               # budget: log ratios the gain's median is read from
# (2) shipped 2026-10-22, no dial: a run's slow changes are part of its steps (edges.py
# Assembler._ends) - the window after a rise skips the opposite steps that follow it,
# and the window before a fall the same-way steps that lead up to it, while each is at
# most FOLD_SHARE of the step and within FOLD_S of it (a pump's start decay, its
# pre-stop drift).
FOLD_SHARE = 0.35
FOLD_S = 60.0
# (3) SAME_SPAN (one span for both sides of a comparison) lost on the bench and was
# removed 2026-10-22; see "Things already ruled out" in this folder's AGENTS.md.
# ...and a drop that fits nothing is HELD; held drops completing a run with a
# later drop close it together, with both steps (-200 W then -400 W against a
# +600 W start). A washer's 2 kW -> 400 W -> off is then one run at both levels:
# without it the run is one 1.2 kW level for its whole length, 29 % over its
# energy (Home purity 77.3 / wconc 57.5 % without against 76.1 / 55.4 with, but
# the bench has no stepped load in it; 2026-09-30).
HELD_DROPS = 8
# One device never runs twice at once: a rise of a cluster while an older run
# that started with the same cluster is still open means that older run ended
# unseen - it is closed after its pair's usual length, or at the new start if
# that comes first. Left open, the next stop of its kind closed the newer run
# and the old one ran on for hours (Home's mat: 114 h of overlapping runs).
# ^ SAME_CLUSTER_ENDS: always on, as the 2026-09-30 ablation found (AGENTS.md)
# B2 - DEVICES: a device is its START CLUSTER, and a start is an all-phase
# event (EVENT_WINDOW_INTERVALS), so the legs of one run on several phases
# (the kiln's A and C, the compressor's three) are one cluster from the
# start. Clusters were joined into devices through links (a pair's rise and
# fall, legs, one fall closing several rises) until 2026-09-30, when the
# union-find chained Home's pump into the compressor and then into everything
# that ever stopped beside either - and a run goes to the signature most runs
# of its device went to.
# Run length and how often a load runs are what a load DOES, not what it is:
# they raise or lower the confidence in a device and its guess, and play no part
# in grouping runs into devices or edges into runs (2026-09-29: "much
# less important than the plain electrical properties ... I don't think using
# it for grouping is doing us any good"). NASA's computers never switch off and
# Home's pump runs seconds or hours; nothing matches, merges or pairs on it,
# and no run is closed for having run long.
# A run that started with an input's change - the mat with its thermostat going
# on - belongs to a device that follows that input: when the input next changes
# back, the run's stop is due at that moment by the input's lag, and if no fall
# closed it by the end of the window its stop was hidden in another load's
# step: it is closed there - when the phase did step in that window; flat, the
# run is still on (a motion sensor flipping every 10 s cut Home's kettle and
# panini press to its flips, 2026-10-13). Evidence, not a clock.
# ^ INPUT_ENDS: always on, as the 2026-09-30 ablation found (AGENTS.md)
# ^ DEVICE_HOME: always on, as the 2026-09-30 ablation found (AGENTS.md)
# ...and it tells the pairing where its edges are: the thermostat going off at
# t means the mat's -635 W on C at t + 5.8 s. A step down there as big or
# bigger closes the mat's run at the mat's size and pairs what is left; a step
# up opens the mat's run - marked as the mat's - and a second for the rest; and
# a stop that never shows, the mat off and a hob pulse on in one reading,
# closes the mat's marked run at the expected moment once the window has
# passed. With the moves-with rule, against Home's 18-27 Sep and its
# thermostat: purity 73.7 -> 74.0 %, wconc 48.6 -> 50.1 %, the mat 106.4 ->
# 102.0 h counted once against 89.9 h of heating; kiln, pump and Kozolec
# within a few runs (2026-09-29).
MAX_OPEN_S = 24 * 3600.0       # a start whose stop never came is given up on after this
MAX_OPEN_EDGES = 12            # loads believed to be running at once on one phase
MERGE_TOLERANCE_S = 15.0       # sessions on different phases this close in start and end are one
# Readings of a summed house value closer together than this are one update
# arriving in pieces; only the last is kept - see combine(). At Home the
# inverter is read about 20 ms before the meter on every poll, so a third of
# the summed readings were phantoms computed against the meter's previous
# value (26,310 of 26,466 sub-second gaps under 50 ms). Swept on the
# production input path: purity 68.4 -> 75.7 %, concentration 31.7 -> 50.9 %,
# and on held-out days 68.6 -> 76.7 % and 25.9 -> 42.7 %. Flat from 0.1 to
# 0.3 s, and well under the 2.4 s Home now polls at (2026-09-23).
COMBINE_SETTLE_S = 0.3
# An inverter's phase map from its voltages (Fleet.inverter_map): at least this many
# passes' votes, counts; and the matched pairs' correlations summed must beat the
# best other permutation's by this factor (Home: 0.98 x 3 against 0.77 + 0.77 + 0.97 at
# worst, a factor of 1.17, over two days)
INVERTER_MAP_MIN_VOTES = 3
INVERTER_MAP_CLEAR = 1.05
# ...AND this close in SIZE. A real multi-phase load is balanced by design -
# a two-phase element, a three-phase motor - and over ten days at home the
# smallest-to-largest ratio inside kiln-sized groups sat at a median of 0.98.
# Grouping on timing alone married a 2025 W load on A to a 163 W blip on C
# and filed the pair as one 2.2 kW two-phase load, which both invented a
# phantom and stole the session from the real single-phase one (the owner,
# 2026-09-18). Below this they are simply two loads that started together,
# which is what they are.
PHASE_BALANCE_MIN = 0.4
# A run measured over this many samples is as well measured as it needs to be
WELL_SAMPLED = 12.0
# How many sightings a running mean is allowed to average over. Without a
# cap the update (mean*n + new) / (n+1) makes a signature OSSIFY: at ten
# sightings a new one moves the mean by 9%, at three hundred by 0.3%, so a
# load that genuinely changes - a kiln on a different programme, an element
# replaced - can never drag its own fingerprint across. Long before it does,
# the new sessions stop matching and found a sibling instead, which is the
# succession problem arriving by a different road. Capped, a mature
# signature keeps following change at a fixed rate: about 1% per sighting,
# so a real shift is tracked over a few dozen runs rather than never.
# count itself keeps counting - this bounds the WEIGHT, not the history
# (2026-09-18, who picked 100 over the 50 I proposed).
ABSORB_WINDOW = 100.0
# A motor draws several times its running current for the moment it starts,
# and a meter catches one sample of it: the owner's pressure pump reads 8886 W in
# one sample and 830 W in every sample after, four times over in a day. Held
# for the length of a sample that single reading dominates the run - a 40 s
# session came out at 2.8 kW for an 830 W pump - so a quarter of Kozolec's
# multi-level runs recorded a load that does not exist, at a power that
# depends on how long the session happened to last (2026-09-22).
#
# It is the START and it is BRIEF: over within a sample or two, where a real
# first stage - a washing machine heating before it spins - runs for minutes.
# So the window is taken from the session's own sampling rate rather than a
# constant, which is what tells 10 seconds on a slow meter from 10 minutes of
# heating on a fast one.
INRUSH_RATIO = 2.5
# How far a load's own band must span, as a share of its power, before the
# naming page says it VARIES. Below this the band is a steady load's jitter.
WANDER_SHARE = 0.2
INRUSH_SAMPLES = 2.0
SHAPE_SETTLED_S = 80.0           # how soon after its start a load has settled - see SETTLE_SHARE

MATCH_POWER_REL = 0.10
# How much of two signatures' OWN measured wander may widen the band that
# admits them to a merge. 0 was the historical behaviour - power_mad computed
# and never read here - and above 0.20 the anti-walk guarantee breaks (the
# 400 W -> 25 W ladder returns). Against the old sustain it looked useless at
# Kozolec; on top of SUSTAIN_INTERVALS it wins at both sites on days it was
# never tuned on: Home's NASA station 22 -> 35 in its dominant cluster, both
# hidrofors up, purity unchanged. A wandering load's signatures carry a big
# spread, and admitting some of it lets its fragments meet (2026-09-23).
ALIKE_MAD_SHARE = 0.10
MATCH_DURATION_FACTOR = 3.0
# The daily rewrite's length split (Signature.learn_split): runs on each side of
# the largest gap in a signature's run memory, at least. A count (RUN_MEMORY is 12).
SPLIT_MIN_RUNS = 4
# Families of one phase set whose start clusters sit within this many of the clusters' own spread (watts_mad)
# of each other, and whose signatures are alike, are one family: a load whose start size drifts - the kiln's
# 5.76 -> 6.05 kW over a firing - is cut into neighbouring clusters, each with signatures of its own
# (_join_neighbours). A count of measured spreads, not watts.
FAMILY_REACH_MADS = 3.0
# ...and only for a start at least this many noises (of its phases together) tall: below that the clusters'
# spread is the noise's, the spread reaches every look-alike, and the join would make blobs out of loads
# nobody could tell apart (Home, 2026-10-26: 14 families of 17-22 W steps made one). Also a count.
FAMILY_REACH_NOISES = 50.0
# The daily repair's hints that a family is two loads (_hint_split; spec
# docs/specs/2026-10-26-signature-families.md section 11): its runs' lengths
# fall in two modes at least MATCH_DURATION_FACTOR apart AND the rarer mode
# sits in a few hours of the day the other does not. Counts and shares, no
# watts or seconds. A family remembers its last HINT_KEEP runs' (length,
# start hour) and is read once it has HINT_MIN_RUNS of them.
HINT_KEEP = 400
HINT_MIN_RUNS = 30
HINT_HOURS = 3.0               # the window of the day the rarer mode must sit in
HINT_HOUR_SHARE = 0.8          # share of the rarer mode's runs in it
HINT_HOUR_LIFT = 3.0           # times the other mode's share of its runs in it (never under a uniform day's)
# Two METERS may disagree about a run's length far more than two sightings of
# one load may: a 66-second boiler cycle is 66 seconds on its own meter and
# often minutes on a busy main one, where the down-step pairs with a
# different edge. A wide bound is only safe because the cross-meter match
# scores every passing pair and takes the closest - with first-fit it made
# things worse (2026-09-18).
CROSS_METER_DURATION_FACTOR = 12.0
# How close a device meter's ENERGY over a main-meter session's window must be
# to that session's own energy for the two to be the same load. Measured at
# Kozolec over ten days: for boiler-sized sessions the ratio ran 0.97 at the
# tenth percentile, 1.02 at the median and 1.07 at the ninetieth, and 465 of
# 479 sessions fell inside this band - where matching the two meters' SESSIONS
# managed 38 of 381 (2026-09-19). Energy is what a coarse meter can answer.
ENERGY_MATCH_LO = 0.65
ENERGY_MATCH_HI = 1.35
# how much of a device's samples to keep for answering that question
SUB_SAMPLE_TAIL_S = 2 * 3600.0
# How long a main-meter session waits for a device meter to say what it saw.
# HELD_TAIL_S is about two PHASES of one load closing together, which happens
# within seconds; this is about a Shelly getting round to it, which does not.
# A session may not even be judgeable when it closes - the reading has to
# reach past its end before sample-and-hold stops guessing at the tail - so
# the wait has to outlast the slowest meter's silence, or the answer arrives
# after the question has been thrown away (2026-09-19).
MATCH_PATIENCE_S = 20 * 60.0
# A live pass reads the recorder up to now, and a meter slower than the grid
# reports the same change later - the hidrofor's plug, every 10 s, up to a
# cadence late and its own step confirmed three after that. Judged at once, a
# grid step could not yet be told from a meter that held its value (see
# Fleet._meter_held), nor placed under the meter that took it. So the grid is
# read on the meters' clock, (1 + SUSTAIN_CADENCES) of the slowest meter's
# cadence behind them (Fleet._horizon), and never more than this cap: a meter
# slower than it is judged with what it has said by then. Until 2026-10-02
# the wait was SUSTAIN_CADENCES of the slowest meter as each pass ended,
# capped at 45 s, so it moved with the pass. The owner (2026-10-02) accepts up to
# five minutes: the named loads' live readings lag by the horizon, their
# energy does not. Set on the detection settings page; 0 judges every step at
# once.
METER_WAIT_CAP_S = 300.0
# How many single-phase sessions a meter must have shared with the house
# before its channels are mapped onto the house's phases. Until then they are
# placed nowhere, and may carry any phase (Fleet._chans): its labels mean
# nothing - Home's attic 3EM calls the house's C "b" and its A "c", so with
# its labels trusted it never matched a session by phase at all (627 seen
# live, 92 credited, all of them by energy - 2026-09-23), and a plug's "a" is
# a placeholder (the unify audit, 2026-10-03).
PHASE_MAP_MIN_VOTES = 30
# A meter that reads ONE total of a load on several phases - Home's EVBox,
# 11 kW charges on all three, its one entity the charger's total - votes for
# the phase SET of the house session its own started with (Fleet._vote_phases),
# and its map may give that one channel several phases ("abc"). Until
# 2026-10-14 only single-phase house sessions took votes, so its charges never
# voted, a few single-phase coincidences mapped it to A, and the edge card
# read its steps against the grid's A leg alone (10-12 21:40:10: grid|a +3,590
# against Polnilnica +10,529). A channel carries every phase of its map's
# value; against one grid phase its step counts evenly spread over them
# (Fleet._leg_gain). phases.py stays the permutation Load Juggler shares: a
# multi-channel meter (a 3EM) still votes, and is mapped, channel by phase.
# ...or as much energy in its votes as PHASE_MAP_MIN_VOTES of the site's
# votes carry on average: a meter's few long runs are as much evidence as
# many short ones (2026-10-03: "score by both the no of events/runs and
# by their energy"). Home's Susilna plug, a dehumidifier running 20 hours a
# night, votes 2-4 times in ten days - 5.8 kWh, where 30 of the site's votes
# carry 650 Wh - and never learned its phase on the count. The map one
# measure picks must not keep less than PHASE_MAP_AGREE of the support the
# other measure's own best map has; where neither map passes on both, the
# channels are placed nowhere.
PHASE_MAP_AGREE = 0.5
# A one-channel meter carries a SET of phases when the votes that saw several
# phases step with it at once (Fleet._vote_phases, _vote_steps) are at least
# this share of its votes, by count or by energy; the map is then the set
# most voted for, and a single phase's votes inside it count toward it - a
# set carries every subset of itself (_same_load, _leg_gain). Home's EVBox
# also charges on L1 alone (10-08 15:50-16:08, 3.6 kW on A only): its votes
# a 52 / abc 16 / c 6 / ab 2 / b 1 had 23 % by count and 38 % by energy on
# several phases; no single-phase meter reached 2 % (Hidrofor 1.5 / 0.8,
# Susilna 0.3 / 0.4, Blaževa Soba 1.6 / 1.9; 2026-10-14), so a tenth sits
# far from both. Counted against a single phase vote for vote, the L1 charges
# outvoted the three-phase ones and the charger stayed on A. Either measure,
# not both: needing both, the EVBox's map went abc -> a -> abc -> a -> abc
# between 10-08 16:00 and 10-12 12:00, its count share hovering at a tenth as
# L1 charges came in; on either it holds abc from 10-08 08:00, and its fed
# capture went 75.7 -> 77.7 %, impurity 3.3 -> 2.7 %.
PHASE_SET_MIN = 0.1
# A detection on a sub-meter overrides the house meter's (2026-09-22:
# "a detection on a sub meter level should always override one on a higher
# level, especially if it is the less noisy one"). Not by handing detection to
# the sub-meter - they are quieter but SLOWER, and the kiln on its circuit's
# 3EM gave 236 full-size sessions against 409 on the house - but session by
# session: the house meter's session keeps its TIMING, and a sub-meter
# session that is the same load decides WHICH signature it joins (the one
# that sub-meter signature's sessions went to before, when it fits there).
# House sessions wait to be filed until every sub-meter fast enough to have
# seen them has reported past their end. 0 files at once, as before.
# Measured with production's meters fed in (bench SUBS=prod, 2026-09-23),
# against the same with this off: Kozolec's Scala2 pump 128 -> 195 sessions in
# its main signature (held out 53 -> 61) at unchanged purity; Home purity
# 78.6 / 80.6 -> 79.1 / 80.7 %; the kiln's main signature x397 -> x392, the
# small price of filing later. Almost all of it comes from SUB_METER_IDENTITY:
# a session partner is rarely there in time, and when it is, the house's own
# choice agrees with it all but 65 times in 1338.
# ^ SUB_OVERRIDE: always on, as the 2026-09-30 ablation found (AGENTS.md)
# A house session whose ENERGY a device meter accounts for - the meter too slow
# or too coarse to have a session of its own for it - joins the house
# signature most of that meter's sessions went to, when it fits there. Only
# for a meter that holds one device: one whose own library puts at least
# SUB_DEVICE_SHARE of its sightings in a single signature (Home's hidrofor
# plug 99 %, its Hiša circuit 55 %). 0 is off.
# ^ SUB_METER_IDENTITY: always on, as the 2026-09-30 ablation found (AGENTS.md)
SUB_DEVICE_SHARE = 0.5
# A meter that holds ONE device takes only loads on the phases that device has
# shown: a phase set it has been credited this many sightings of. Below that
# it is too young to say and takes anything. The Hidrofor plug on phase A was
# credited a 308 + 421 W load on A and B, and a 124 W one on B, because a
# meter the votes do not place is matched by its total and moment alone
# (2026-09-28: "the plug is single phase, no multi phase load should be
# attributed there"). A count, so a load two sightings strong never sets it.
METER_PHASES_MIN = 20
# An entity that says WHEN a load is on - a thermostat's heating, a relay, a
# switch - is a meter that knows no power. A house session starting (and, once
# known, stopping) with one of its on-periods is credited to it: SWITCH_PREFIX
# names it among the meters. Home's bathroom floor mat (Termostat Kopalnica)
# starts and stops within seconds of the thermostat's heating on 11 of 12 runs,
# and was named by hand from exactly that (2026-09-28).
SWITCH_PREFIX = "switch:"
# A load placed at a switch is powered through it - Home's floor mat through
# its thermostat's relay - so a run the switch was off for throughout cannot
# be it, however well its size fits: that run goes to the next signature that
# fits, or starts its own. Other 600 W runs on phase C were filed into the mat
# and counted: 32 of its 91 hours over 18-27 Sep fell outside the thermostat's
# heating (2026-09-29). A signature counts as switched once at least this
# share of its runs started and stopped with the switch - whichever meter wins
# its placement: the mat's credits split 1274 thermostat to 1253 Hiša. The mat
# is credited 45 % of its runs, look-alikes 2-12 %; at 0.3 it went from 91.1 to
# 87.9 h counted once against 89.9 h of heating, every other bench the same
# (at 0.5 it never counted as switched). Only for a switch on record.
SWITCH_GATE = 0.3
# A switch's stop and the session's may disagree by this share of the run as
# well as by the moment tolerance: a sagging or merged run ends late.
SWITCH_END_SHARE = 0.25
# ...and how far back its on-periods - and a number's readings - are kept:
# past the longest a session waits, counted from the oldest reading a pass brings.
SWITCH_MEMORY_S = 4 * 3600.0
# A setting a device reports - a washer's cycle phase, a fan's speed - is
# learned against each load: the value it had while the load ran, against
# what that value's share of the time would give by chance (the owner,
# 2026-09-28: inputs of one device, "the washer's cycle/sub cycle sensors").
# A load runs IN a value once it has this many runs (recency-weighted over
# INPUT_TIME_TAU_S, so a washer used daily keeps about 14), this share of
# them in it, and that share this many times what chance gives. Five of
# eight runs landing in a phase that fills 6 % of the time is a chance of
# about 4 in 100 000.
INPUT_MIN_RUNS = 8
INPUT_MIN_SHARE = 0.6
INPUT_MIN_LIFT = 3.0
# How long a value's share of the time is remembered over.
INPUT_TIME_TAU_S = 14 * 86400.0
# A setting's RARER values - a washer in its wash phase, not "Unavailable"
# most of the day - keep their own signatures: a run while one is in force
# joins only signatures born in it (or twins that took them back, below), and
# a run outside joins none of them. The washer's heater, 1.8 kW on A, shared
# its signature with every other 1.8 kW thing on A, so it could never be
# seen to run in the wash phase (2026-09-29). 0 is off.
# ^ INPUT_SPLIT: always on, as the 2026-09-30 ablation found (AGENTS.md)
# A value this rare or rarer - its share of the time - is one runs are
# filed in; and only once the shares have been counted for this long.
INPUT_RARE_SHARE = 0.25
# What an input's row says while its integration is down (Home Assistant's
# unavailable and unknown): no value is in force, so a run made then has no
# context. Dropping those rows carried the last value on - the Sock Eater's
# Rinse through the 19 hours of 27-28 Oct it was unavailable, making every
# pulse of Home's Inkubator a run "in Rinse" and 262 of them a signature of
# their own (2026-10-28).
INPUT_DOWN = "unavailable"
# A signature born in a setting's value with this many runs or fewer, never judged, is a fragment: see
# Detector.retake_fragments.
FRAGMENT_MAX_RUNS = 2
# An input's CONTINUOUS state at a step - not only a change near it - is part of
# the step's group, judged one input at a time against the state's share of the
# time (Detector._by_state): while the washer is Running, a 2 kW step is not the
# dishwasher's. Every state, not only the rare ones (the owner, 2026-10-23: the
# Sock Eater's signatures splitting by its cycle phase is expected and welcome).
# The key's name is the input's with this suffix.
STATE_SUFFIX = "@"
INPUT_SPLIT_MIN_TIME_S = 86400.0
# A born-in signature that is chance - a fridge running through a wash -
# goes back into its twin once it has INPUT_MIN_RUNS runs, unless the two
# together would run in the value this many times chance.
INPUT_SPLIT_LIFT = 3.0
# ...and one that runs in too few of the value's EPISODES - the kiln firing
# through one afternoon's washing, a burst of pulses in one spin phase - is
# ordinary again once the value has come round this many times since it was
# born, and it ran in fewer than this share of them. Its runs, clustered, had
# read as 7 times chance; its episodes read 1 in 12 (2026-09-29).
INPUT_MIN_EPISODES = 3
INPUT_MIN_COVERAGE = 0.5
# Each signature's energy by the clock hour it was used in, for as long as
# the backfill reaches: when a load is named, its meter's history is written
# into Home Assistant's statistics from this, so the Energy dashboard shows
# the ten days it was already seen rather than starting at the naming
# (2026-09-29).
HOURLY_KEEP_S = 11 * 86400.0
# ...and past that, for a year, the hours of an unnamed load confident enough
# to be named one day - seen this often, using this much in all, with the
# naming page's evidence bar (DEFAULT_MIN_EVIDENCE) - so naming it brings its
# year onto its meter, not ten days (2026-10-04). Signature.older,
# stored apart from the library; a named load's history is its meter's.
OLDER_MIN_COUNT = 10
OLDER_MIN_WH = 500.0
OLDER_MIN_EVIDENCE = 0.7
OLDER_KEEP_S = 365 * 86400.0
# A load that runs in one value of a setting is taken as a real load of
# that device: its runs being loose about time counts no more against it.
# 0 is off.
INPUT_EVIDENCE = 1
# How far back to look for what the device was drawing ANYWAY. Capped,
# because a session lasting hours would otherwise want hours of readings
# before it - further back than the tail we keep - and the longest sessions
# are exactly the ones energy matching answers best.
IDLE_WINDOW_S = 900.0
MATCH_PF_TOL = 0.15
# Once a day is over, its runs are booked again with what the whole day
# shows (exp15, AGENTS' "daily retroactive repair pass") - see Fleet._repair
# and _cap_day: on each phase no detector's runs of the day are booked over
# what the phase drew above the day's floor - the level it held
# REPAIR_FLOOR_SHARE of the day, by time, as the scorecard measures a meter's
# idle floor - and REPAIR_SLACK_NOISE of its measured noise. The repair waits
# REPAIR_WAIT_S past the day's end on the readings' clock, so the day's last
# runs are filed: a house run waits for its meters at most MATCH_PATIENCE_S,
# the grid is read at most METER_WAIT_CAP_S behind them.
REPAIR_DAY_S = 86400.0
REPAIR_FLOOR_SHARE = 0.1
# ...and booked again the other way: a regular cycler (a fridge, a freezer) whose runs the day's log shows a
# hole in - more than RECOVER_GAP_FACTOR of its own periods between two starts - is looked for in the day's
# readings (_recover_day): its own step pair, size and length, in what no run was booked on. Found cycles are
# booked on its hours (Signature.recovered) and kept out of what it learns. 0 is off. The owner, 2026-10-26:
# regular intervals only; the readings decide, nothing is booked on expectation alone.
RECOVER_ON = 1
RECOVER_MIN_RUNS = 20          # a signature's runs before it is looked for
RECOVER_MIN_GAPS = 6           # start-to-start gaps its runs of the day need to say what its period is
RECOVER_REGULAR = 0.35         # its period: the median gap; regular when the median miss from it is within this share
RECOVER_GAP_FACTOR = 2.0       # a gap between starts this many periods long has a missed cycle in it
RECOVER_MAX_GAP = 8.0          # a longer gap than this many periods is the load being off, not cycles missed
RECOVER_SLOT = 0.25            # a cycle found is a whole number of periods from a start known, within this share of one
RECOVER_WINDOW_S = 45.0        # the readings either side of a step that its size is the medians' difference of
RECOVER_POWER_REL = 0.2        # a step within this share of its power (or 1.5 noises) is its step
RECOVER_ENERGY_LO, RECOVER_ENERGY_HI = 0.5, 1.6   # the Wh found against its power over the length found
RECOVER_LOG_KEEP = 5000
REPAIR_SLACK_NOISE = 2.0
REPAIR_WAIT_S = MATCH_PATIENCE_S + METER_WAIT_CAP_S


def pf_tolerance(a_mad: float, b_mad: float) -> float:
    """How far apart two power factors may sit and still be one load.

    The flat figure alone assumed every factor was measured equally well, and
    at Kozolec they are not: a 62 W load's factor is uncertain by 0.20 against
    a tolerance of 0.15, so the factor REFUSED matches between sightings of
    the same load. Each side brings its own error bar, and two readings agree
    when they overlap - so a well-resolved pair still gets the tight 0.15 and
    a badly-resolved one simply stops constraining anything."""
    return MATCH_PF_TOL + a_mad + b_mad
MAX_SIGNATURES = 200
# Eviction tiers. ESTABLISHED: evidence at least this (three tight sightings,
# or five of any kind) and seen inside the horizon - never evicted. YOUNG:
# fewer than this many sightings and inside the grace period - protected so
# it can become established. Everything else goes weakest-first.
#
# The horizon is deliberately longer than a year. It was 30 days, which
# quietly said "a load that has not run this month is not a load" - and a
# kiln fired twice a year, or a pump that only runs in a wet spring, has a
# strong signature and deserves to be measured and tracked exactly as well
# as the kettle. Evidence is the gate; age is only the backstop for an
# appliance that has genuinely left the house (2026-09-18). One-offs
# are excluded by evidence, not by age: they never reach 0.5.
ESTABLISHED_EVIDENCE = 0.5
ESTABLISHED_HORIZON_S = 400 * 86400.0
YOUNG_COUNT = 3
# A NAMED signature is never evicted, so when its load CHANGES - a kiln put on
# a different programme, an element replaced - the name stays attached to a
# fingerprint nothing matches any more while the successor sits unnamed. The
# name should follow the load. Detecting that is guesswork, so it is never
# done silently: a named signature gone quiet this long, with an unnamed one
# on the same phases of a similar shape, records a HINT for the naming page
# to offer. Nothing is renamed without the user (2026-09-18).
SUCCESSOR_QUIET_S = 7 * 86400.0
# ...but a week means nothing to a load that runs twice a year, so "quiet"
# is really "silent for far longer than it has ever been between runs".
SUCCESSOR_QUIET_INTERVALS = 6.0
SUCCESSOR_POWER_REL = 0.5
SUCCESSOR_MIN_COUNT = 5
# How many of a reading's own quanta a quantity must span before it is
# believed. Every one of these is a COUNT, never a number of watts: the
# quantum itself is measured from each sensor's own history, so the same
# constant means 23 VA at Kozolec's 0.1 A Victron and 2.3 VA at Home's
# 0.01 A SolarEdge without either site being configured (2026-09-22:
# "the goal is to get the integration to work well across both sites
# without the need for you to manually set parameters").
#
# Ten, for a power factor, because sqrt(S^2 - P^2) is a difference of
# squares and amplifies any error in S exactly where the factor is near
# one: measured at Kozolec, half a current quantum moves the derived
# factor by 0.20 below 100 W and 0.13 below 200 W, against a matching
# tolerance of 0.15 - so the factor was SPLITTING loads that were the
# same. Ten quanta puts the swing under 0.05.
# The widest error bar a factor may carry and still be handed to the
# classifier, whose bands are ~0.05 wide at the edges - the heater band
# starts at 0.93 - so a factor uncertain by more than that cannot place a
# load in one of them, however plausible the number looks.
PF_TRUST_MAD = 0.05
# How many quanta a load's apparent power must span for its factor to cost
# less than a tenth. Nothing is gated on this any more - each factor carries
# its own error bar instead - but it is the honest single number for "below
# this, power factors stop meaning much here", and the sensor publishes it.
PF_MIN_QUANTA = 10.0
# Two, for an energy rise, because the window matcher subtracts one
# integral from another and each carries its own quantisation.
ENERGY_MIN_QUANTA = 2.0
# How many changes to see before a quantum is believed, and which
# percentile of them it is. A truly quantised reading changes by exactly
# one quantum most times it changes at all, so a low percentile IS the
# quantum; a continuous one has a tiny percentile and is unaffected.
QUANTUM_MIN_SAMPLES = 40
QUANTUM_PERCENTILE = 0.05
# ...and how strictly the candidate has to behave like a real lattice before
# it is believed: all but a tenth of the changes within a quarter-quantum of
# a whole multiple.
QUANTUM_LATTICE_TOL = 0.25
QUANTUM_LATTICE_SHARE = 0.9

PRUNE_GRACE_S = 6 * 3600.0
MAX_RECENT_SESSIONS = 200
# A named load's power is live (2026-10-13): the watts of its open runs
# from the moment their start is declared - an open run being a name's where
# LIVE_NAME_SHARE of its start cluster's filed runs went to that name, over
# at least LIVE_NAME_MIN_RUNS - plus, spread over the stretch of data, the
# energy of its runs that started and closed between two readings of it,
# which were never seen open (the kiln's 44 s pulses). A start whose stop
# never comes stops counting once it has been open LIVE_STALE_FACTOR times
# the longest run its name is known for: a step up with no partner sat high
# for a day when power was first read off open steps (2026-09-18). Energy
# is still booked from filed runs only.
LIVE_NAME_SHARE = 0.8
LIVE_NAME_MIN_RUNS = 3
LIVE_STALE_FACTOR = 3.0
# ...but of its USUAL runs, not its longest: Home's Inkubator is a ~20 s pulse,
# yet one mis-paired fragment run of 1,653 s kept a start that never closed
# counted for 55 minutes (2026-10-28). The LIVE_STALE_QUANTILE-th of the
# lengths its runs remember is what a run of the name is, and a name that
# remembers none falls back to its signatures' own lengths.
LIVE_STALE_QUANTILE = 0.95
# And a name never reads more than what one of its signatures draws, with room
# for the parts of one appliance that run together (a washer's heater and drum
# motor): a pulse load read 574 W off four stacked open starts and one lumped
# stretch of energy, against a signature group that tops out at 75 W
# (2026-10-28, the owner: "a detected load should never be significantly above
# its signature group"). The ceiling of a signature is the higher of its mean
# and the top of its level range.
LIVE_CAP_FACTOR = 1.25
# How many of its OWN run windows each signature remembers, so "these two are
# never on at once" can be asked about a load that runs every few days. It
# was asked of the rolling session list, which a busy house burns through in
# about two hours - 40 sessions spanning 2.1 h and holding 11 of Home's 199
# signatures - so the kiln, last fired 8.6 days ago, could never be compared
# with anything (2026-09-22).
RUN_MEMORY = 12
# The widest a device's settings may span before they are two devices. See
# suggest_levels: generous, because it only has to beat 36 to 1.
MATCH_LEVEL_RATIO = 10.0
HELD_TAIL_S = 60.0             # closed sessions wait this long for a partner on another phase


def _median(xs: Sequence[float]) -> float:
    return statistics.median(xs) if xs else 0.0


NOISE_WINDOW = 240     # the moves the noise is the median of at every reading (_slide), and the relative noise
# The relative noise is the median of its last NOISE_WINDOW moves, worked out
# again once every REL_REFRESH_S of the readings' clock. In blocks of 120 moves
# counted from the first, one reading dropped moved every later block for good
# (see _slide); the median at every move, as the noise is, made Home circuits'
# devices 2 points less pure (17.7 % impurity against 15.8 over five cards
# apart only by a reading in 10,000 dropped or 1 ms of jitter); on the clock,
# every 300 s 16.8 % and 33.7 % captured, every 3600 s 16.0 and 31.6 with the
# spread of the blocks back (14.1-17.3 against 16.4-17.0; 2026-10-04).
REL_REFRESH_S = 300.0
# NOISE_REL_FROM_LEVEL (2026-10-14): the relative noise is learned as the idle
# noise is. A move counts only where the reading before it sat at the level
# too: back from a dip of a load nothing declared, the move is the load's -
# 12 % of Home's grid c moves counted at a loaded level came from a reading
# off it, and the noise there sat at the 5 % cap (~108 W at 2.2 kW). And a
# seed's worth of moves (BASELINE_SEED_SAMPLES) starts it, as it starts the
# idle noise: Kozolec's car charger plug, read once a minute, gathered 185
# moves at its 2.2 kW in ten days - under the window - and never learned one.
# The moves are kept with the phase, as the idle ones: a restart began the
# count again.
# The recorder writes only a CHANGE, so a silence is the value held: each of
# the meter's intervals (its cadence, a low percentile of its gaps) in it is a
# move of nothing, and a reading after one brings them into the noise window
# (the seed's too). A gap is silence only past SILENCE_CADENCES intervals, and
# only that part counts: (gap - SILENCE_CADENCES x interval) / interval,
# whole intervals. Home's hidrofor plug reports while it runs (87 %
# of its readings over 8.6 % of the time) and idle once in 8 minutes, flat at
# 0.6 W: from its moves alone its noise was 158-298 W, seeded on a run and
# never re-learned, the window filling with 62 idle moves in ten days. A
# meter at a steady cadence has no gaps to fill (_implied_zeros).
# ponytail: a quarter of the window per gap, so one gap (a night, an outage
# the recorder did not mark) never decides the median alone; a judgement.
IMPLIED_ZEROS_CAP = NOISE_WINDOW // 4
# ponytail: k = 3, as SUSTAIN_CADENCES (the latency: a silence that long means
# the value held); Home's grid reads every ~2 s at a cadence of 1.1-1.8 s and
# 9-16 % of its readings implied a zero at k = 1. A judgement.
SILENCE_CADENCES = 3


def _slide(window: List[float], ordered: List[float], x: float, size: int) -> float:
    """``x`` into a window of the last ``size`` values and into its sorted
    copy (rebuilt where the two disagree, after a restore); returns the
    window's median. A measure that is the median of the last ``size``
    values at EVERY value forgets a reading that went missing once it has
    slid past; re-measured in blocks - every 120 values, over the last 240 -
    the blocks' edges are counted from the first value, and one reading
    more or less moved every edge for good: dropped one reading in 10,000,
    Home's phase A ran at another noise for 38 % of its steps and another
    relative noise for 87 %, from the hour of the first drop on (2026-10-04).
    The noise's; the relative noise is worked out on the clock (REL_REFRESH_S)."""
    window.append(x)
    if len(ordered) != len(window) - 1:
        ordered[:] = sorted(window[:-1])
    bisect.insort(ordered, x)
    if len(window) > size:
        del ordered[bisect.bisect_left(ordered, window.pop(0))]
    n = len(ordered)
    return ordered[n // 2] if n % 2 else 0.5 * (ordered[n // 2 - 1] + ordered[n // 2])


# ------------------------------------------------------------------ sessions
@dataclass
class Run:
    phases: str                          # "a", "ac", ...
    start: float
    end: float
    levels: Dict[str, List[Tuple[float, float]]]   # phase -> [(since_ts, watts above baseline)]
    pf: Optional[float] = None           # mean power factor during the session, if known
    pf_mad: float = 0.0                  # ...and how far the amps' resolution could put it out
    quality: float = 1.0                 # how sure the detector is of it, 0..1 - see QUALITY_SNR_FULL
    # How many meter samples the run actually spanned. A 43-second load read
    # every 5 s is eight numbers; the same load read every second is
    # forty-three, and the second measurement deserves more weight and a
    # tighter tolerance than the first. The owner asked whether the fingerprint
    # could take the sampling rate into account (2026-09-18) - this is where
    # it enters. 0 means unknown, which is treated as well-measured so that
    # nothing stored before this existed is suddenly distrusted.
    samples: int = 0
    # The lowest and highest the load itself read during the run. Kept as
    # WATTS rather than as the ratio between them, because "varies between
    # 104 and 247 W" is a thing someone recognises about their own pump and
    # "varies by 69%" is not (2026-09-18). None when two loads
    # overlapped and the wander could not be attributed to either.
    low: Optional[float] = None
    high: Optional[float] = None
    # A starting surge too short to become a LEVEL of its own. PhaseState
    # catches it while the readings are still separate - see _declare_surge.
    surge_w: float = 0.0
    # Which signature took this session. It used to be looked up in ``recent``,
    # a DISPLAY list capped at 200 - so a backfill slice that filed more than
    # that lost the answer for all but the last few, and with it every
    # location. Backfill is exactly when a load's place should be established
    # (2026-09-18: 173 signatures at Kozolec, every one of them "main",
    # on a site where the boiler and the car charger have their own meters).
    signature_id: Optional[int] = None
    # (start cluster, stop cluster) of the edges that opened and closed it
    pair: Optional[Tuple[Optional[int], Optional[int]]] = None
    # ...and a run merged across phases, every leg's
    legs: List[tuple] = field(default_factory=list)
    # (meter, its own session) where a meter holding several devices saw this
    # run of its parent's: the run is that session's signature's, and the
    # parent files none of its own - see Fleet._file_as
    parent_signature: Optional[tuple] = None
    # phase -> Wh it drew, where its meter followed it (PhaseState._close,
    # Fleet._meter_wh); a phase without is booked at its levels
    wh: Dict[str, float] = field(default_factory=dict)
    # the meter below the grid whose own step was all of this run's start
    # (_Open.meter, placed by the Fleet): the run waits for that meter's own
    # session and pairs with it on time alone - the placed claim, see the note above GAIN_RING
    placed: str = ""

    @property
    def confidence(self) -> float:
        """0.25 to 1, by how many samples the run was measured over."""
        if self.samples <= 0:
            return 1.0
        return max(0.25, min(1.0, (self.samples - 1) / (WELL_SAMPLED - 1.0)))

    @property
    def duration_s(self) -> float:
        return self.end - self.start

    def power_by_phase(self) -> Dict[str, float]:
        """Energy-weighted mean watts per phase - the dominant level, with a
        motor's starting surge left out of it. See INRUSH_RATIO."""
        out = {}
        for ph, lv in self.levels.items():
            levels, start = self._without_inrush(lv)
            e, span = 0.0, max(self.end - start, 0.0)
            for i, (since, w) in enumerate(levels):
                until = levels[i + 1][0] if i + 1 < len(levels) else self.end
                e += w * max(0.0, until - since)
            out[ph] = e / span if span > 0 else 0.0
        return out

    def _without_inrush(self, lv: List[Tuple[float, float]]):
        """The levels past the starting surge, and when the run really began.

        Only ever the first one, only when it towers over what follows, and
        only when it is over within a sample or two - which is what separates
        a motor starting from a washing machine heating before it spins."""
        if len(lv) < 2:
            return lv, self.start
        rest = max(w for _, w in lv[1:])
        if rest <= 0 or lv[0][1] < INRUSH_RATIO * rest:
            return lv, self.start
        interval = self.duration_s / max(self.samples - 1, 1) if self.samples > 1 else 0.0
        if interval <= 0 or (lv[1][0] - lv[0][0]) > INRUSH_SAMPLES * interval:
            return lv, self.start
        return lv[1:], lv[1][0]

    @property
    def inrush_w(self) -> float:
        """How far the start towered over the run, or 0 - which is evidence of
        a motor that nothing else in a house produces."""
        peak = 0.0
        for ph, lv in self.levels.items():
            levels, _ = self._without_inrush(lv)
            if levels is not lv and levels:
                peak += lv[0][1] - max(w for _, w in levels)
        # Disjoint cases, so the larger rather than the sum: a surge shorter
        # than the sustain window never became a level and is only in
        # surge_w; one longer than it became its own level and is found above.
        return max(peak, self.surge_w)

    @property
    def energy_wh(self) -> float:
        if not self.wh:
            return sum(p * self.duration_s for p in self.power_by_phase().values()) / 3600.0
        return sum(self.wh[ph] if ph in self.wh else p * self.duration_s / 3600.0
                   for ph, p in self.power_by_phase().items())

    def phase_wh(self, ph: str) -> float:
        """Its Wh on ``ph``: as booked, else its power there over its length."""
        return self.wh[ph] if ph in self.wh else self.power_by_phase().get(ph, 0.0) * self.duration_s / 3600.0

    @property
    def max_w(self) -> float:
        return sum(max((w for _, w in lv), default=0.0) for lv in self.levels.values())

    @property
    def level_count(self) -> int:
        return max((len(lv) for lv in self.levels.values()), default=0)

    def to_dict(self) -> dict:
        return {"phases": self.phases, "start": self.start, "end": self.end, "pf": self.pf,
                "pf_mad": self.pf_mad, "surge_w": self.surge_w,
                "samples": self.samples, "low": self.low, "high": self.high,
                "levels": {ph: [list(x) for x in lv] for ph, lv in self.levels.items()},
                "pair": list(self.pair) if self.pair else None, "legs": [list(x) for x in self.legs],
                "wh": dict(self.wh), "quality": self.quality, "signature_id": self.signature_id, "placed": self.placed,
                "owner": [self.parent_signature[0], self.parent_signature[1].to_dict()] if self.parent_signature and isinstance(self.parent_signature[1], Run) else None}

    @classmethod
    def from_dict(cls, d: dict) -> "Run":
        return cls(phases=d["phases"], start=d["start"], end=d["end"], pf=d.get("pf"),
                   pf_mad=d.get("pf_mad", 0.0), surge_w=d.get("surge_w", 0.0),
                   # written by to_dict all along and never read back, so a
                   # session that waited across a restart came back unsampled
                   samples=d.get("samples", 0), low=d.get("low"), high=d.get("high"),
                   levels={ph: [tuple(x) for x in lv] for ph, lv in d["levels"].items()},
                   pair=tuple(d["pair"]) if d.get("pair") else None,
                   legs=[tuple(x) for x in d.get("legs") or []], wh=dict(d.get("wh") or {}), placed=d.get("placed") or "",
                   quality=d.get("quality", 1.0), signature_id=d.get("signature_id"),
                   parent_signature=(d["owner"][0], cls.from_dict(d["owner"][1])) if d.get("owner") else None)


# ------------------------------------------------------------------ per-phase tracker
def _is_the_sun(step: float, pv_step: Optional[float]) -> bool:
    """Is this step at the meter just PV moving the other way?

    A grid meter carries the house MINUS the array, so a cloud arrives on it
    as a load switching on and clears as one switching off. A step that a
    simultaneous PV step of the opposite sign accounts for is not a load.
    Where the reading already excludes PV nothing fires here, because there
    is no step to explain."""
    if pv_step is None or step * pv_step >= 0:
        return False
    share = abs(step) / abs(pv_step)
    return PV_SHARE_MIN <= share <= PV_SHARE_MAX


def _angle(watts: float, var: Optional[float]) -> Optional[float]:
    """A step's reactive angle in degrees, None without its var."""
    return math.degrees(math.atan2(var, watts)) if var is not None and watts else None


def _quantile(ordered: Sequence[float], q: float) -> float:
    """The ``q``-th quantile of an already sorted list, nearest rank - 0.0 for none."""
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))] if ordered else 0.0


def _trim(value, places: int):
    """A stored number at the precision it actually carries.

    A watt-hour figure written as 276.1825572400394 spends fifteen digits on
    a quantity the energy meter publishes to two decimal places of a
    kilowatt-hour. Over a library of two hundred signatures that is a third
    of the stored state, rewritten every pass (2026-09-18).

    Energy is trimmed to whole watt-hours rather than integers on the way
    in: what is stored is what a restart restores, and an energy total that
    steps DOWN reads as a meter reset to Home Assistant's statistics. At one
    decimal a restart costs the biggest signature here 0.14 Wh out of
    38.7 kWh, which is three ten-thousandths of a per cent.
    """
    return round(value, places) if isinstance(value, float) and TRIM else value


# What is stored is rounded (_trim, and the histograms below): a restart costs
# the library that rounding, by design, for a third less store. The bench's
# RESTART dial switches it off (TRIM = 0) to ask the other question - does a
# restart lose any STATE - and runs it on again to measure the rounding alone.
TRIM = True
HIST_FLOOR = 0.01       # a histogram bin lighter than this is not stored


def site_topology(inverters: Sequence[dict], stored: Optional[str] = None) -> Optional[str]:
    """Where the site's battery sits, from the inverters that are set up.

    Series if ANY inverter is series, which is Load Juggler's rule and holds
    for the same reason: the series formula on the summed outputs is exact
    for a mix, because a parallel member contributes no battery term.

    Falls back to a layout stored before the inverter list existed, and
    answers None when nothing says - which means "read it off the data",
    not "assume parallel"."""
    declared = {inv.get("topology") for inv in inverters if inv.get("topology")}
    if "series" in declared:
        return "series"
    if declared:
        return "parallel"
    return stored or None


def combine(terms: Sequence[Tuple[Sequence[Tuple[float, float]], float]],
            settle_s: float = 0.0) -> List[Tuple[float, float]]:
    """Add several readings into one, each held forward onto the others' times.

    ``terms`` is (rows, sign). The house is a SUM and nothing else:

        house = grid meter + SUM over inverters of (output - input)

    which is why there is no wiring flag here. Every inverter contributes
    what it ADDS - a PV inverter has no AC input, so its input is absent and
    it contributes its whole output; a hybrid with the grid flowing through
    it contributes output minus input, so the grid term it passed on does not
    get counted twice. Whatever sits between the utility meter and an
    inverter's own AC input falls out of the same sum. Verified against
    The owner's home over 246,000 samples on three phases: identical to the
    template sensors he had built by hand, to the watt (2026-09-18).

    A sum recomputed whenever EITHER input updates, against the other's last
    value, has steps in it that no load made; ``settle_s`` keeps the last
    reading of each burst - see below.
    """
    stamps = sorted({ts for rows, _ in terms for ts, _ in rows})
    if not stamps:
        return []
    cursors = [0] * len(terms)
    out: List[Tuple[float, float]] = []
    for ts in stamps:
        total, ok = 0.0, True
        for i, (rows, sign) in enumerate(terms):
            j = _as_of(rows, ts, cursors[i])
            cursors[i] = max(j, 0)
            if j < 0:
                ok = False
                break
            total += sign * rows[j][1]
        if ok:
            out.append((ts, total))
    if settle_s > 0 and len(out) > 1:
        # Keep only the LAST reading of each burst. When two inputs update
        # within moments of each other the sum is computed twice: once against
        # the partner's stale value and once correctly. The first of the pair
        # is a phantom - a step of the whole change in whichever input moved
        # first - and a third of Home's house readings came in such pairs,
        # which also dragged the measured sample interval down to about 3 s.
        # A freshness limit (max_skew_s, since removed) was meant for this and
        # cannot do it on recorder data:
        # Home Assistant only records a CHANGE, so an inverter sitting at 0 W
        # all night looks hours stale and every night-time sample is dropped,
        # the kiln with them (347 -> 119 sessions). A burst needs no judgement
        # about freshness, only about what came after (2026-09-23).
        out = [r for r, nxt in zip(out, out[1:] + [None])
               if nxt is None or nxt[0] - r[0] > settle_s]
    return out


_TS = operator.itemgetter(0)


def _as_of(rows: Sequence[Tuple[float, float]], ts: float, i: int) -> int:
    """Index of the last row at or before ``ts``, never before ``i``; -1 when
    the series has not started yet. Two steps on are looked at directly -
    the readings beside a power sample mostly moved one row - and a longer
    way is bisected, not walked: a slice read from the start of its series
    walked the whole way there (rows are in time order)."""
    if not rows or rows[0][0] > ts:
        return -1
    i = max(i, 0)
    n = len(rows)
    if i + 1 >= n or rows[i + 1][0] > ts:
        return i
    if i + 2 >= n or rows[i + 2][0] > ts:
        return i + 1
    return bisect.bisect_right(rows, ts, i + 2, n, key=_TS) - 1


def _align(source: list, target_rows: list) -> Dict[float, float]:
    """``source`` read as of each of ``target_rows``' moments."""
    out: Dict[float, float] = {}
    i = 0
    for ts, _ in target_rows:
        i = _as_of(source, ts, i)
        if i >= 0:
            out[ts] = source[i][1]
    return out


# How far into a window the meter's own reactive power may start and still be
# taken for all of it: history reaches back past its first recorded day.
SIGNED_VAR_SLACK_S = 120.0


# A 3EM's apparent power read "as of" the power's stamp still held the reading
# BEFORE a kiln leg switched off - 34.5 W against 3505 VA, a 3.5 kvar spike at
# the very step (Home, 2026-09-30): the same update - see SAME_UPDATE_S.


def _with_update(rows: list, ts: float, i: int) -> int:
    """``i`` (as of ``ts``), or the next row when it is part of the same update."""
    if i + 1 < len(rows) and rows[i + 1][0] - ts <= SAME_UPDATE_S:
        return i + 1
    return i


def _reactive(power_rows: list, volts: Optional[list], amps: Optional[list],
              pfs: Optional[list], signed: Optional[list] = None,
              vas: Optional[list] = None) -> Dict[float, float]:
    """Reactive VAr at each power sample.

    Every entity updates at its own moment, so the other readings are taken
    as of the power sample's time - sample and hold - rather than looked up
    at the same instant, which almost never matches.

    The meter's own reactive power, where it publishes one and it covers the
    window, is taken as it is: signed, so a step's change is the load's own.
    The root of (V x I) squared minus P squared has no sign, and a load whose
    reactive power runs against the floor's reads the wrong size (the owner,
    2026-09-30). Else the meter's own apparent power, then V x I, then the
    power factor: finest first. Each paired with the power reading of the
    same update - see SAME_UPDATE_S."""
    if signed and power_rows and signed[0][0] <= power_rows[0][0] + SIGNED_VAR_SLACK_S:
        return _align(signed, power_rows)
    volts, amps, pfs, vas = volts or [], amps or [], pfs or [], vas or []
    out: Dict[float, float] = {}
    vi = ai = fi = si = 0
    for ts, p in power_rows:
        vi, ai, fi, si = _as_of(volts, ts, vi), _as_of(amps, ts, ai), _as_of(pfs, ts, fi), _as_of(vas, ts, si)
        v, a, f, s = (_with_update(volts, ts, vi), _with_update(amps, ts, ai),
                      _with_update(pfs, ts, fi), _with_update(vas, ts, si))
        apparent = None
        if s >= 0:
            apparent = vas[s][1]
        elif v >= 0 and a >= 0:
            apparent = volts[v][1] * amps[a][1]
        elif f >= 0 and pfs[f][1]:
            apparent = abs(p) / abs(pfs[f][1])
        if apparent is None:
            continue
        out[ts] = math.sqrt(max(0.0, apparent * apparent - p * p))
    return out


UNIT_SCALE = {
    # to watts
    "W": 1.0, "kW": 1000.0, "MW": 1_000_000.0, "mW": 0.001,
    # to amps
    "A": 1.0, "mA": 0.001, "kA": 1000.0,
    # to volts
    "V": 1.0, "mV": 0.001, "kV": 1000.0,
    # a power factor is a ratio; some meters publish it as a percentage
    "%": 0.01,
}


def input_rows(states: Sequence, domain: str) -> List[Tuple[float, str]]:
    """An input's recorder states as (when, value) rows: a fan's value is its speed ("33 %") or "off", Home
    Assistant's unavailable and unknown are INPUT_DOWN, and a value that repeats the one before is not a new row
    (an unknown after an unavailable is the same outage)."""
    rows: List[Tuple[float, str]] = []
    for st in states:
        if st.state in ("unknown", "unavailable"):
            value = INPUT_DOWN
        elif domain == "fan":
            value = f"{st.attributes.get('percentage')} %" if st.state == "on" else "off"
        else:
            value = st.state
        if not rows or rows[-1][1] != value:
            rows.append((st.last_updated.timestamp(), value))
    return rows


def unit_scale(unit: Optional[str]) -> float:
    """What to multiply a reading by to get watts, amps or volts.

    Home's EV charger publishes kW while every other meter in the house
    publishes W, so its 2 kW charging session arrived as the number 2 and
    could never match the 2000 W session the main meter saw. The load stayed
    unattributed and turned up in the naming list as an unexplained car -
    which is how the owner found this (2026-09-18). Fourteen entities at that site
    report kW.

    An unrecognised unit scales by 1 rather than being dropped: a reading
    that is probably watts is worth more than no reading."""
    return UNIT_SCALE.get((unit or "").strip(), 1.0)


def without_window_start(series: Dict[str, list], start_ts: float) -> Dict[str, list]:
    """Each series less the row the recorder stamps AT the window's start.

    Asked for include_start_time_state, which the arithmetic needs - a sum
    of two meters must know each one's value at the boundary - the recorder
    answers with the state as of the start, stamped the start: a copy of the
    last reading, not a reading. At one-minute ticks that put a repeat on
    every phase every minute, and replaying ten days that way cost Home 2
    points of purity (78.5 -> 76.8 %) and 26 extra signatures; without the
    copies it scored as one call did (2026-09-23). A real reading landing on
    the same microsecond is not a case worth keeping."""
    return {k: [r for r in rows if r[0] != start_ts] for k, rows in series.items()}


def _sum_series(a: list, b: list) -> list:
    """Two arrays' power added together, each held forward onto the other's
    sample times - one site has two trackers and reading only the first
    would leave half of every cloud unexplained."""
    if not a:
        return list(b)
    if not b:
        return list(a)
    stamps = sorted({ts for ts, _ in a} | {ts for ts, _ in b})
    ia = ib = 0
    out = []
    for ts in stamps:
        ia, ib = _as_of(a, ts, ia), _as_of(b, ts, ib)
        out.append((ts, (a[ia][1] if ia >= 0 else 0.0) + (b[ib][1] if ib >= 0 else 0.0)))
    return out


def energy_between(rows: Sequence[Tuple[float, float]], start: float, end: float,
                   hi: Optional[int] = None) -> Optional[float]:
    """Watt-hours a reading accounts for between two instants, sample and hold.

    This is what a coarse meter CAN answer. A Shelly reporting once a minute
    cannot describe a 66-second run - it gets three samples and its session
    power comes out at half the truth - but the energy it recorded over that
    minute is right, because energy integrates and sampling error cancels
    where power's does not (2026-09-19, on a Zigbee meter that cannot
    be made faster at all).

    None when the window is not covered by the reading. ``hi``: read only
    rows[:hi] - what was written by then.
    """
    hi = len(rows) if hi is None else hi
    if not hi or end <= start:
        return None
    # Both ends, not just the near one. Sample-and-hold carries the last
    # reading forward for as long as you let it, so a meter that fell silent
    # an hour ago will answer for a window it never saw - and the answer,
    # "it drew exactly what it was drawing before", is indistinguishable
    # from a device that really did stay put. The docstring promised this
    # check; the code only ever made half of it (2026-09-19).
    if rows[0][0] > start or rows[hi - 1][0] < end:
        return None
    total = 0.0
    # Seek, don't walk. _as_of scans forward from the index it is handed, and
    # a session waits MATCH_PATIENCE_S for an answer - so every pending
    # session asks every device meter twice, every pass, and each of those
    # questions would otherwise re-read the whole retained tail from the
    # front (2026-09-19).
    i = bisect.bisect_right(rows, (start, float("inf")), 0, hi) - 1
    if i < 0:
        return None
    at = start
    while at < end:
        nxt = rows[i + 1][0] if i + 1 < hi else end
        until = min(nxt, end)
        total += rows[i][1] * (until - at)
        at = until
        if i + 1 < hi:
            i += 1
        elif at < end:
            break
    return total / 3600.0


def ref_label(ref: Tuple[str, int]):
    """A signature's (meter, id) as the sensors show it: the grid's its id,
    as it always was, a meter's "Mansarda#11"."""
    return f"{ref[0]}#{ref[1]}" if ref[0] else ref[1]


RUNNING_ROWS = 10   # a dashboard card, not the library: ~2 KB at most (a judgement, 2026-10-12)


def running_markdown(rows: List[dict], link: str, offered=frozenset(), cap: int = RUNNING_ROWS,
                     now: float = 0.0) -> str:
    """Fleet.running's rows as a Markdown list for a dashboard card: named
    loads first, then unnamed, each by watts, at most ``cap`` lines. An
    unnamed row whose (meter, id) is in ``offered`` - the naming page lists
    it - links to ``link``; every row says its ``#id`` so the naming page's
    row of the same id can be found."""
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
        lines.append(f"- {label} · {_fmt_w(r['watts'])} {on_phases(r['phases'])} · {_fmt_s(max(0.0, now - r['since']))}"
                     + (f" · {r['tag']}" if r["tag"] else "") + (f" · {r['where']}" if r["where"] else ""))
    if not lines:
        return "Nothing detected running."
    return "\n".join(lines[:cap] + ([f"- … {len(lines) - cap} more"] if len(lines) > cap else []))


def _described(orphan: dict) -> "Signature":
    """A carried name's description as a signature to compare with (alike)."""
    return Signature(id=-1, phases=orphan.get("phases") or "", power=dict(orphan.get("power") or {}),
                     duration_s=orphan.get("duration_s") or 0.0, pf=orphan.get("pf"), count=1,
                     first_seen=0.0, last_seen=0.0)


def names_in_store(raw: dict) -> List[dict]:
    """Every named signature in a stored library, as a description.

    Deliberately hand-rolled rather than going through ``Fleet.from_dict``:
    this runs precisely when the stored shape is one the current detector has
    disowned, so anything that assumes today's schema is the wrong tool. It
    reaches for four fields, takes what is there, and lets a library it
    cannot read at all yield nothing rather than raise. A name whose
    description comes back empty is KEPT even though nothing will ever match
    it: it then shows on the sensor as still awaiting its load, which is a
    great deal better than disappearing (2026-09-22).
    """
    def named(det: dict, tag: dict) -> list:
        out: list = []
        for sig in det.get("signatures") or []:
            name = sig.get("name")
            if not name:
                continue
            power = sig.get("power")
            out.append({"name": name,
                        "phases": sig.get("phases") or "",
                        "power": dict(power) if isinstance(power, dict) else {},
                        "duration_s": sig.get("duration_s") or 0.0,
                        "pf": sig.get("pf"), **tag})
        return out + [{**o, **tag} for o in det.get("orphan_names") or [] if isinstance(o, dict) and o.get("name")]
    try:
        fleet = raw.get("fleet") or {}
        out = named(fleet.get("main") or raw.get("detector") or {}, {})
    except (AttributeError, TypeError, ValueError):
        return []
    # ...and each meter's, carried with its meter (Fleet.carry_names)
    subs = fleet.get("subs")
    for meter, det in sorted(subs.items()) if isinstance(subs, dict) else ():
        try:
            out += named(det, {"meter": meter})
        except (AttributeError, TypeError, ValueError):
            pass
    return out


def carries_generation(rows: Sequence[Tuple[float, float]]) -> Optional[bool]:
    """Does this reading contain the site's generation, or the house alone?

    It decides two things at once - whether a cloud can masquerade as a load
    in this signal, and whether the inverter's output has to be added back
    to get what the house draws - and it is answered by physics rather than
    statistics: a reading that includes generation goes BELOW ZERO whenever
    the site exports, and one that carries only the house cannot.

    I tried correlating the two series' changes first, and the owner's own data
    threw it out (2026-09-18): on a day when the grid meter exported 4.5 kW
    the correlation called the array absent from it. Sampling the two series
    at different instants is enough to destroy that signal, while "did it go
    negative" survives anything.

    None when there is too little to look at."""
    if len(rows) < max(PV_MIN_SAMPLES, GENERATION_MIN_SAMPLES):
        return None
    below = sum(1 for _, value in rows if value < -EXPORT_FLOOR_W)
    return below >= EXPORT_SHARE * len(rows)


def exports_positive(grid_rows: Sequence[Tuple[float, float]],
                     generation: Sequence[Tuple[float, float]]) -> Optional[bool]:
    """Which way round is this grid meter wired?

    "House = the meter plus the inverter" holds only in the dashboard's
    convention, where importing is positive. The owner's SolarEdge M1 is the
    other way round - verified on a real day, 2177 of 2177 samples positive
    while the array was over 12 kW - and summing it would have counted the
    array twice instead of cancelling it.

    Nobody should have to know this about their own meter, and the data
    says it outright: when generation is at its peak the site is exporting,
    so whichever sign the meter shows THEN is its export sign.

    Only meaningful for a reading that exports at all, so it answers None
    unless the reading goes both ways - a site that never exports has no
    export sign to find, and the sum is unaffected either way."""
    if carries_generation(grid_rows) is not True:
        return None
    if len(generation) < PV_MIN_SAMPLES:
        return None
    peak = max(value for _, value in generation)
    if peak <= 0:
        return None
    busy = {ts for ts, value in generation if value >= 0.8 * peak}
    if len(busy) < PV_MIN_SAMPLES:
        return None
    # the grid reading as of each of those moments, sample and hold
    seen = []
    rows = sorted(grid_rows)
    j = 0
    for ts in sorted(busy):
        j = _as_of(rows, ts, j)
        if j >= 0:
            seen.append(rows[j][1])
    if len(seen) < PV_MIN_SAMPLES:
        return None
    return statistics.median(seen) > 0


def carries_load(rows: Sequence[Tuple[float, float]]) -> bool:
    """Is this reading actually carrying power, or is it a dead port?

    Kozolec's MultiPlus AC input publishes power, current AND voltage - a
    perfectly coherent triple, and all three sit at zero because the
    generator behind them is off. Coherence is necessary and not sufficient:
    a reference for reactive power has to be a circuit something flows
    through (2026-09-18).

    However few readings a pass holds: a dead port reads zero at any count,
    and asking for thirty turned Home's phase A away on every one-minute pass
    - its meter reports every 4.3 s - so A had no power factor live
    (2026-09-30)."""
    if len(rows) < 3:
        return False
    live = sum(1 for _, value in rows if abs(value) > SOURCE_IDLE_W)
    return live >= LIVE_SHARE * len(rows)


def measure_quantum(values: Sequence[float]) -> float:
    """The smallest change a reading can actually express, from its own data.

    A meter's RESOLUTION is not its noise, and the detector measured only
    the second. Noise is the median deviation while idle, so a reading that
    sits rock steady on a coarse value measures ZERO of it and falls back to
    the global floor - and then its first quantum jump is taken for a load.
    Home's workshop boiler publishes in 46 W steps and was credited with
    4 kW of "noise" on exactly that basis (2026-09-22).

    A quantised reading changes by exactly one quantum most times it changes
    at all, so a low percentile of its non-zero changes IS the quantum. A
    continuous reading has a vanishing percentile and is left alone. The
    minimum would do the same job in theory and is far too fragile in
    practice: one sample from before a firmware change, or one interpolated
    value, and the estimate collapses to nothing.
    """
    return quantum_of_steps([abs(b - a) for a, b in zip(values, values[1:]) if b != a])


def quantum_of_steps(steps: Sequence[float]) -> float:
    """measure_quantum, for a caller that already has the changes themselves.

    The candidate is a low percentile of the changes - but that alone measures
    ACTIVITY, not resolution: a busy reading whose smallest honest change is
    large would be handed a quantum it does not have, and Home's three phases
    duly came back with 37, 38 and 58 W floors that were pure signal. So the
    candidate has to be confirmed: a resolution means every change is a WHOLE
    NUMBER of quanta, and nothing else does that. Continuous readings still
    pass with a vanishing quantum, which is harmless - it never beats the
    measured noise it is taken against."""
    kept = sorted(x for x in steps if x > 0)
    if len(kept) < QUANTUM_MIN_SAMPLES:
        return 0.0
    q = kept[int(QUANTUM_PERCENTILE * len(kept))]
    if q <= 0:
        return 0.0
    tol = QUANTUM_LATTICE_TOL * q
    on = sum(1 for x in kept if abs(x - q * round(x / q)) <= tol)
    return q if on >= QUANTUM_LATTICE_SHARE * len(kept) else 0.0


def _pf_spread(watts: float, var: Optional[float], va_quantum: float) -> float:
    """How far the derived power factor could be wrong, from the resolution of
    the amps it came from.

    PF = P/S, so a quantum of apparent power moves it by PF * dS/S - small on
    a load whose own apparent power is many quanta, and ruinous on one that is
    three. At Kozolec's 0.1 A (23 VA at 230 V) that is 0.01 for a 2.5 kW load
    and 0.20 for a 62 W one, which is the swing measured off the site itself.

    A VAr of exactly zero is the clamp in _reactive firing: quantisation put
    the apparent power BELOW the real power, which cannot happen, and the
    factor was set to 1.00 for want of anywhere else to go. That is not a
    precise measurement and not an imprecise one either - it is no measurement,
    so it gets the widest spread there is."""
    if var is None or va_quantum <= 0.0:
        return 0.0
    apparent = math.hypot(watts, var)
    if apparent <= 0.0:
        return 1.0
    if var == 0.0:
        return 1.0                      # clamped: the true factor could be anything
    return min(1.0, abs(watts) / apparent * (va_quantum / apparent))


def _pf_from(watts: float, var: Optional[float]) -> Optional[float]:
    """The LOAD's power factor, from its OWN step in real and reactive power.

    The meter's power factor is the whole site's and says nothing about the
    load that just started (2026-09-17). Watts and VAr add across loads,
    a ratio does not, so the load's factor comes from how much each of them
    moved when it switched - not from how the meter's factor read.
    """
    if var is None:
        return None
    s = math.hypot(watts, var)
    return None if s <= 0 else max(0.0, min(1.0, abs(watts) / s))


@dataclass(eq=False)                 # one run is one object: removing it from a list must not compare every field
class _Open:
    """A load believed to be running: the step that started it, what is still
    running of it, and every level it has held."""
    since: float
    watts: float
    var: Optional[float]                          # the reactive step it started with
    levels: List[Tuple[float, float]] = field(default_factory=list)
    surge: float = 0.0                            # see PhaseState._declare_surge
    # Lowest and highest the phase read while this was the ONLY load running.
    # A resistive element holds its level; anything behind a variable-speed
    # drive glides between them without ever taking a step big enough to be
    # a LEVEL. Kozolec's Grundfos Scala2 runs 104 to 247 W and reads as one
    # flat level, which is why it classified as a small heater (the owner,
    # 2026-09-18).
    lo: Optional[float] = None
    hi: Optional[float] = None
    # what it draws NOW, followed while it runs alone - see SAG_CLOSE
    now: Optional[float] = None
    # the edge cluster it started with - see PAIR_MIN_RUNS
    cluster: Optional[int] = None
    q: float = 1.0                                # its start's quality - see QUALITY_SNR_FULL
    # how far the runs already open overstated the phase's reading when this
    # one started - see _unseen_stop
    short0: float = 0.0
    # the meter below the grid whose own step was all of this start (its
    # event's cluster `where`, placed, not guessed): the run is that meter's
    # until the meter shows it stopped - see PhaseState.owned
    meter: Optional[str] = None

    def as_list(self) -> list:
        return [self.since, self.watts, self.var, [list(x) for x in self.levels], self.lo, self.hi,
                self.surge, self.cluster, self.meter, self.now, self.q, self.short0]

    @classmethod
    def of(cls, raw) -> "_Open":
        since, watts, var = raw[0], raw[1], raw[2]
        levels = [tuple(x) for x in raw[3]] or [(since, watts)]
        return cls(since=since, watts=watts, var=var, levels=levels, lo=raw[4], hi=raw[5],
                   surge=raw[6], cluster=raw[7], meter=raw[8] if len(raw) > 8 else None,
                   now=raw[9] if len(raw) > 9 else None, q=raw[10] if len(raw) > 10 else 1.0,
                   short0=raw[11] if len(raw) > 11 else 0.0)


@dataclass
class PhaseState:
    """Edges, not excursions.

    A load is a STEP: the phase rises by its power when it starts and falls
    by the same amount when it stops. Waiting instead for the meter to come
    back to its idle floor - the first design - only works on a phase that
    goes quiet between loads, and a house main never does: the kettle starts
    while the fridge is running, so the excursion that opened at breakfast
    closed at midnight. The owner's home meter proved it (2026-09-17): 76
    signatures from 160 sessions, 61 % of them seen once, the longest single
    "load" 155 hours and 778 kWh, and four with NEGATIVE power because the
    sun pushed the grid meter below its own floor.

    So each sustained step is recorded as an edge, and a step DOWN is paired
    with the open step UP that best matches its size, most recent first. That
    is what survives loads overlapping: A on, B on, B off, A off pairs
    correctly. A slow drift - sunrise on a grid meter, an element tapering -
    never becomes a step at all, because the tracked level follows it.
    """
    baseline: Optional[float] = None
    noise: float = MIN_NOISE_W
    level: Optional[float] = None                # what the phase is holding now
    q_level: Optional[float] = None              # reactive VAr at that level, when known
    # The reactive power just BEFORE a step, as a short median rather than
    # the slow EMA q_level is. Watts drift slowly while nothing switches, so
    # an EMA tracks them; the grid meter's VAr does not - at home it swings
    # from 259 var at night to 2447 at midday with the inverter's voltage
    # support, and a 0.02 EMA lags that badly enough to swamp a load's own
    # step. Measured on the kiln: 0.934 from the EMA, 0.989 from the median,
    # and the classifier's heater band starts at 0.93 (2026-09-18).
    q_recent: List[float] = field(default_factory=list)
    pv_level: Optional[float] = None             # what the array was making then
    # True where this reading is the house alone, which cannot go below
    # zero. The owner's per-phase template dips negative for 49 samples out of
    # 20764 - the moments its own inputs do not line up - and one of those
    # dragged the idle floor to -569 W for the rest of the day, so every
    # step after it was measured from nonsense (2026-09-18).
    floor_zero: bool = False
    ended_upto: Optional[float] = None    # how far a meter's falls were asked about - see _meter_ended
    # this phase's own floor, so a site can ask for more or less sensitivity
    # than the default without touching the measured part
    min_noise: float = MIN_NOISE_W
    # What this reading can RESOLVE, measured from its own changes - see
    # measure_quantum. Separate from noise because the two failure modes are
    # opposite: a jittery meter measures its noise correctly, while a coarse
    # but steady one measures none at all and is then trusted far past what
    # it can express.
    quantum: float = 0.0
    step_diffs: List[float] = field(default_factory=list)
    moving_gaps: List[float] = field(default_factory=list)   # the gaps after a move, see CADENCE_GAPS
    _moving_sorted: List[float] = field(default_factory=list, repr=False, compare=False, metadata=DERIVED)
    _moved: bool = field(default=False, repr=False, compare=False)   # the last reading moved past the noise
    last_w: Optional[float] = None
    # The apparent power one current quantum is worth, V x dI, which is what
    # limits any power factor derived here. Supplied by whoever read the
    # amps, since the detector only ever sees the VAr they produced.
    q_quantum: float = 0.0
    # Set by the Detector for a pass: asks whether another phase's leg of the
    # same load is stopping too. Never persisted - it is rebuilt every pass.
    corroborate: Optional[object] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    # (since, watts, closed_at) of edges closed in the last minute, for the
    # same question from the other side.
    recent_closed: List[Tuple[float, float, float]] = field(default_factory=list, repr=False, compare=False)
    # the open edge another leg vouched is stopping, for _pair to close
    close_hint: Optional[object] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    # the detector whose edge library and pair models this phase asks, its
    # name, and the cluster of the fall being paired. Set each pass.
    lib: Optional[object] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    name: str = field(default="", repr=False, compare=False, metadata=DERIVED)
    stop_cluster: Optional[int] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    stop_q: Optional[float] = field(default=None, repr=False, compare=False, metadata=DERIVED)   # the stop being paired's quality
    last_step_ts: Optional[float] = field(default=None, repr=False, compare=False)
    # every step this phase declared, (since, watts, var), newest last - what a
    # meter's step IS when the grid's is compared with it (METER_CAL_SLACK)
    declared: List[tuple] = field(default_factory=list, repr=False, compare=False)
    declared_t: List[float] = field(default_factory=list, repr=False, compare=False, metadata=DERIVED)   # their times, for bisect
    steady_ts: Optional[float] = field(default=None, repr=False, compare=False)   # its last reading at the held level
    steady_before: Optional[float] = field(default=None, repr=False, compare=False)   # ...and the update's before that
    pv_last: Optional[float] = field(default=None, repr=False, compare=False)   # the array at its last reading
    held_drops: List[tuple] = field(default_factory=list, repr=False, compare=False)
    # a ramp placed at a meter by its whole window (Fleet._window_size): the
    # rises declared inside it up to here are that run's, not runs of their own
    absorb_until: float = field(default=-math.inf, repr=False, compare=False)
    # the measured share of the running level that is noise, and the samples
    # it is measured from
    noise_rel: float = 0.0
    rel_diffs: List[float] = field(default_factory=list)
    seed: List[float] = field(default_factory=list)
    seed_zeros: int = 0               # the held moves its gaps imply - see IMPLIED_ZEROS_CAP
    idle_diffs: List[float] = field(default_factory=list)
    _rel_tick: Optional[int] = field(default=None, repr=False, compare=False)
    _idle_sorted: List[float] = field(default_factory=list, repr=False, compare=False, metadata=DERIVED)
    pending: List[Tuple[float, float, Optional[float], Optional[float]]] = field(default_factory=list)
    open_edges: List[_Open] = field(default_factory=list)   # believed to be running
    last_ts: Optional[float] = None
    raw_last: Optional[Tuple[float, float]] = None   # the last reading as the meter wrote it - see Detector._corroborate
    # a meter below the grid: its report lag against it, learned by the
    # Fleet (Fleet._learn_lags), 0 until it is - see latency
    lag: float = 0.0
    # every gap between this phase's readings (SAME_UPDATE_S to 120 s), the last CADENCE_GAPS: what tells a meter
    # that reports on change from a polled one - see SUSTAIN_CADENCES_CHANGE
    all_gaps: List[float] = field(default_factory=list)
    change_driven: bool = field(default=False, repr=False, compare=False, metadata=DERIVED)
    # the settings page's answer for this meter (Fleet.cadence_forced): None leaves it to _classify
    forced: Optional[bool] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    # ...and how far its readings run AHEAD of the grid's, the other tail of
    # its signed lags: its spans reach this much further on to meet the grid's
    # - see LAG_PERCENTILE and Fleet._near
    lead: float = 0.0
    _stood: Optional[float] = field(default=None, repr=False, compare=False)   # the pending reading last stood in for
    # this phase's own reading interval - its cadence, see CADENCE_GAPS - the
    # meter's rate, not a setting, so turning a poll up from 5 s to 1 s is
    # noticed rather than configured
    interval: float = 0.0

    def _learn_quantum(self, w: float) -> None:
        """Every change this reading makes, so its resolution is measured the
        same way its noise is - from the data, never configured."""
        if self.last_w is not None and w != self.last_w:
            self.step_diffs.append(abs(w - self.last_w))
            if len(self.step_diffs) >= QUANTUM_MIN_SAMPLES * 2:
                self.quantum = quantum_of_steps(self.step_diffs)
                self.step_diffs = self.step_diffs[-QUANTUM_MIN_SAMPLES:]
                # At once, not at the next recompute: noise is only remeasured
                # every 240 IDLE samples, and a reading coarse enough to need
                # this is often never idle for that long.
                self.noise = max(self.noise, self.quantum)
        self.last_w = w

    def process(self, ts: float, w: float, q: Optional[float] = None,
                pv: Optional[float] = None, held: bool = False) -> List[Run]:
        due = self._input_ended(ts) + self._meter_ended(ts) if self.lib is not None and self.open_edges else []
        silent = self.silence_due() if not held else None
        if silent is not None and silent < ts:     # on its own; a Detector has done this already (_advance)
            due += self.stand_in(silent)
        return due + self._process(ts, w, q, pv, held)

    def owned(self, o: _Open) -> bool:
        """Is this run a meter's that still shows it on? A start a meter's own
        step was all of (``_Open.meter``) is that load's, and the meter sees
        the load stop: until it shows a fall of half the run's size, no fall
        of the grid's, no held drop and no start of its kind can end the run
        - a pairing by size, a step-down, a joint stop, a multi-close, an
        unseen stop or a start of the same cluster. Home's Susilna plug (a
        dehumidifier, 270 W for 20 hours from 18:00) read 254-285 W all night
        while the grid's run of it was ended within 12-68 minutes five ways
        over five nights, once booked at its followed 3.3 kW (2026-10-02);
        the plug's own detector had the 20-hour run every time. The converse
        of Fleet._meter_stop."""
        return bool(o.meter) and self.lib is not None and self.lib.meter_on is not None and \
            self.lib.meter_on(o.meter, self.name, o.since, o.watts, self.last_ts)

    def _meter_ended(self, ts: float) -> List[Run]:
        """Close each run a meter's own step started whose stop that meter
        showed and the grid did not, at the meter's fall, once the grid has
        read past it by its own latency and the merge tolerance - see
        Fleet._meter_ended. Each of the meter's falls is asked once."""
        if self.lib is None or self.lib.meter_ended is None or not any(o.meter for o in self.open_edges):
            return []
        upto = ts - self.latency() - MERGE_TOLERANCE_S
        frm, self.ended_upto = self.ended_upto if self.ended_upto is not None else upto, upto
        out = []
        while upto > frm:
            got = self.lib.meter_ended(self.name, self.open_edges, frm, upto)
            if got is None:
                break
            o, at = got
            out.append(self._end(o, at, o.now or o.watts))
        return out

    def _input_ended(self, ts: float) -> List[Run]:
        """Close runs whose input has switched back and whose stop never came -
        see INPUT_ENDS - where the phase stepped then: a stop hidden in
        another load's step still leaves that step, and a phase flat through
        the input's window still carries the run. Home's living-area motion
        sensor flips every ~10 s; keyed to it, the panini press's 298 s at
        1.5 kW was ended after 29 s, the phase flat (2026-10-12)."""
        out = []
        for o in list(self.open_edges):
            if o.cluster is None or self.owned(o):
                continue
            got = self.lib.input_end(o.cluster, o.since, ts)
            if got is not None and self._stepped(o.since, *got):
                out.append(self._end(o, got[0], o.now or o.watts))
        return out

    def _stepped(self, since: float, at: float, half: float) -> bool:
        """Whether this phase declared a step after ``since`` within ``half``
        (and the merge tolerance) of ``at``."""
        k = bisect.bisect_right(self.declared_t, max(since + 0.01, at - half - MERGE_TOLERANCE_S))
        return k < len(self.declared_t) and self.declared_t[k] <= at + half + MERGE_TOLERANCE_S

    def _process(self, ts: float, w: float, q: Optional[float] = None,
                 pv: Optional[float] = None, held: bool = False) -> List[Run]:
        """One sample: seconds, watts, reactive VAr where the meter gives
        enough to work it out, and what the array was making at the time.
        Returns the sessions this sample closed - more than one when several
        loads stopped together. ``held`` marks a stand-in for a slow meter's
        silence - see SUSTAIN_CADENCES and stand_in - which teaches nothing about it."""
        if self.last_ts is not None and ts <= self.last_ts:
            return []
        if (not held and self.level is not None and len(self.pending) >= SUSTAIN_SAMPLES
                and ts - self.pending[0][0] >= self.latency() + SAME_UPDATE_S - 1e-6
                and len(self._held()) > SUSTAIN_SAMPLES
                and abs(w - self.pending[-1][1]) > max(SUSTAIN_AGREE_TOL * self.noise_at(w),
                                                       SUSTAIN_AGREE_REL * abs(w - self.level))):
            # this reading leaves the plateau the pending ones made, which
            # held from its first reading until now - as long as the latency,
            # it is a level, declared (a stand-in one update before this
            # reading) before this reading is judged against it - one reading
            # more in agreement than a plateau still held needs, in place of
            # the confirmation it never got (two sufficed for the charger but
            # churned Home's three noisy phases: 5,500 sessions more in ten
            # days, one of them 32 kWh that never was). Judged only with the
            # newest reading in the plateau, a 20 s pause read three times
            # (3, 7, 67 W) and left at the fourth reading was no level at
            # all: Kozolec's car charger paused twice that way and its 14:34
            # run ran on to 17:57 (09-20, 2026-10-02)
            at = ts - SAME_UPDATE_S
            before = self.stand_in(at)
            if self.pending and self.pending[-1][0] == at:
                self.pending.pop()               # not taken (a glitch guard, say): this reading as before
            else:
                return before + self.process(ts, w, q, pv)
        if not held:
            self.raw_last = (ts, w)
        if held:
            pass                          # not a reading: no cadence, no quantum
        elif self.last_ts is not None:
            gap = ts - self.last_ts
            if SAME_UPDATE_S < gap < 120.0:   # one update is not a gap; a restart's is not a sampling rate
                self.all_gaps.append(gap)
                if len(self.all_gaps) > CADENCE_GAPS:
                    del self.all_gaps[0]
                if len(self.all_gaps) % 25 == 0:
                    self._classify()
                if self._moved:          # the meter's CADENCE - see CADENCE_GAPS
                    self.moving_gaps.append(gap)
                    if len(self._moving_sorted) != len(self.moving_gaps) - 1:
                        self._moving_sorted = sorted(self.moving_gaps[:-1])   # restored from a store
                    bisect.insort(self._moving_sorted, gap)
                    if len(self.moving_gaps) > CADENCE_GAPS:
                        old = self.moving_gaps.pop(0)
                        del self._moving_sorted[bisect.bisect_left(self._moving_sorted, old)]
                    m = self._moving_sorted
                    self.interval = m[int(MOVING_PERCENTILE * (len(m) - 1))]
        prev_w = self.last_w             # the reading before this one - see NOISE_FROM_MOVES
        zeros = 0 if held else self._implied_zeros(ts)
        if not held:
            moved = prev_w is not None and abs(w - prev_w) >= self.noise_at(prev_w)
            same = self.last_ts is not None and ts - self.last_ts <= SAME_UPDATE_S
            self._moved = moved or (same and self._moved)
            self.last_ts = ts
        # The array moved under this reading by its noise or more: what the
        # reading does then is the array's timing as much as the house - the
        # grid meter and the inverter are not read at one moment (the third site's M1
        # takes two thirds of each change of the inverter's at once, the rest
        # a poll later) - so it teaches neither the idle noise nor the floor.
        # the third site's house moved 13 W a reading by day where the inverter moved,
        # 4.7 where it did not (2026-10-04, exp12).
        sun_moving = pv is not None and self.pv_last is not None and abs(pv - self.pv_last) >= self.noise
        if pv is not None and not held:
            self.pv_last = pv
        if self.floor_zero and w < -GLITCH_FLOOR_W:
            return []                 # a house cannot draw less than nothing; skip it
        if not held:
            self._learn_quantum(w)
        if self.baseline is None:
            self.seed.append(w)
            self.seed_zeros += zeros
            if len(self.seed) >= BASELINE_SEED_SAMPLES:
                ordered = sorted(self.seed)
                self.baseline = ordered[int(BASELINE_SEED_PERCENTILE * (len(ordered) - 1))]
                if self.floor_zero:
                    self.baseline = max(self.baseline, 0.0)
                # How far the reading moves BETWEEN SAMPLES, not how far it
                # sits from a percentile. Two reasons. It is the quantity the
                # step test actually asks about, and it is what was measured
                # on both real sites when the floor was set (2 to 5 W quiet,
                # 30 to 55 W under load). And it is blind to a load being on
                # through the seed: a steady 2 kW contributes no difference at
                # all, where a deviation-from-baseline measure would call the
                # whole load noise. The old window was "within twice the
                # floor", which tied this to a constant meant for something
                # else and made lowering that constant collapse the estimate.
                diffs = [abs(b - a) for a, b in zip(self.seed, self.seed[1:])] + [0.0] * self.seed_zeros or [0.0]
                self.noise = max(self.min_noise, self.quantum, NOISE_MAD_FACTOR * _median(diffs))
                self.level = self.baseline
                self.q_level = q
                self.pv_level = pv
                self.seed, self.seed_zeros = [], 0
            return []

        if self.open_edges and any(ts - e.since > MAX_OPEN_S for e in self.open_edges):
            # a start whose stop was never seen: give up rather than pair it
            # with an unrelated load hours later
            self.open_edges = [e for e in self.open_edges if ts - e.since <= MAX_OPEN_S]

        if abs(w - self.level) < self.noise_at(self.level):
            self.pending = []
            if self.steady_ts is None or ts - self.steady_ts > SAME_UPDATE_S:
                self.steady_before = self.steady_ts
            self.steady_ts = ts
            # With one load running, what the phase does IS what that load
            # does, so its wander can be attributed. With two it cannot, and
            # nothing is recorded rather than something wrong.
            if len(self.open_edges) == 1:
                o = self.open_edges[0]
                above = (w - self.baseline if self.baseline is not None else w) \
                    + (self.lib.pending_fall(self.name, o.since) if self.lib is not None else 0.0)
                o.lo = above if o.lo is None else min(o.lo, above)
                o.hi = above if o.hi is None else max(o.hi, above)
            # no step - follow the drift, so a ramp never becomes a load
            young = len(self.open_edges) == 1 and ts - self.open_edges[0].since <= SHAPE_SETTLED_S \
                and not self.owned(self.open_edges[0])
            self.level += (SETTLED_FOLLOW if young else SLOW_FOLLOW) * (w - self.level)
            if len(self.open_edges) == 1 and self.baseline is not None:
                o = self.open_edges[0]
                if self.owned(o):
                    # a meter's run is what its meter reads, never what the phase does
                    lvl = self.lib.meter_level(o.meter, self.name, ts, o.since) if self.lib.meter_level is not None else None
                    if lvl:
                        o.now = lvl
                elif ts - o.since <= SHAPE_SETTLED_S:
                    # followed only while it settles (a pump's start decays,
                    # SETTLE_SHARE), never after: a lone run followed for good
                    # took every undeclared load on its phase - Home's 39 W
                    # Inkubator pulse read 306 W (run lifecycle, 2026-10-28).
                    # A drop held since it started is not it sagging - see
                    # HELD_DROPS - nor is a fall still waiting for its event
                    o.now = self.level - self.baseline + sum(d[1] for d in self.held_drops if d[0] > o.since) \
                        + (self.lib.pending_fall(self.name, o.since) if self.lib is not None else 0.0)
            # the relative noise as the idle noise: a move only from a reading
            # at the level too, and from a seed's worth of moves on - see
            # NOISE_REL_FROM_LEVEL
            if (self.level is not None and abs(self.level) >= self.rel_floor and not sun_moving
                    and prev_w is not None and abs(prev_w - self.level) < self.noise_at(self.level)):
                self.rel_diffs.append(abs(w - prev_w) / abs(self.level))
                del self.rel_diffs[:-NOISE_WINDOW]
                tick = math.floor(ts / REL_REFRESH_S)
                if len(self.rel_diffs) >= BASELINE_SEED_SAMPLES and tick != self._rel_tick:
                    self.noise_rel = min(NOISE_REL_CAP, NOISE_MAD_FACTOR * _median(self.rel_diffs))
                    self._rel_tick = tick
            if q is not None:
                self.q_level = q if self.q_level is None else self.q_level + SLOW_FOLLOW * (q - self.q_level)
                self.q_recent.append(q)
                del self.q_recent[:-Q_RECENT_SAMPLES]
            if pv is not None:
                self.pv_level = pv if self.pv_level is None else self.pv_level + SLOW_FOLLOW * (pv - self.pv_level)
            if self._idle() and not sun_moving:
                self.baseline += BASELINE_EMA * (w - self.baseline)
                if self.floor_zero:
                    self.baseline = max(self.baseline, 0.0)
                # The floor only where the reading is. A reading still above it
                # with nothing open is a load whose run went with another's in
                # one close: snapped to the floor under it, the level made it a
                # new start the next reading - Home 09-26 05:31, a fall closing
                # 700 and 500 W while 700 W stayed on. Left unowned, it is
                # under-reported rather than booked as a load it is not.
                if abs(w - self.baseline) <= self.noise_at(self.baseline):
                    self.level = self.baseline
                if prev_w is not None and abs(prev_w - self.level) < self.noise_at(self.level):
                    for _ in range(zeros):
                        _slide(self.idle_diffs, self._idle_sorted, 0.0, NOISE_WINDOW)
                    mid = _slide(self.idle_diffs, self._idle_sorted, abs(w - prev_w), NOISE_WINDOW)
                    if len(self.idle_diffs) >= NOISE_WINDOW:
                        self.noise = max(self.min_noise, self.quantum, NOISE_MAD_FACTOR * mid)
            return []

        self.pending.append((ts, w, q, pv))
        # In the reading's OWN intervals once it has one, so the guard means the
        # same number of readings at any polling rate. max() with an absolute
        # floor did not: at 6 s the interval term won (9 s, three readings),
        # but at Home's new 2.4 s the 5 s floor bound instead and meant three
        # OR four readings on timing jitter, and at 1 s would mean six. The
        # absolute figure is only a fallback while the interval is unknown.
        sustain = self.latency()
        need = SUSTAIN_SAMPLES
        if self._corroborated_stop(ts):
            # another leg of the same load is stopping at the same moment
            need, sustain = 1, 0.0
        if len(self.pending) < need or (ts - self.pending[0][0]) < sustain - 1e-6:   # a stand-in lands on the mark
            return []
        # How long the phase has been away is timed from the FIRST reading that
        # left the old level - a half-caught switch is part of the change, and
        # the kiln's two-reading off-gaps only clear the guard with it counted.
        # What it moved TO is only the readings that agree (see SUSTAIN_AGREE).
        held = self._held()
        if len(held) < need:
            # never settled: a load that wanders is decided the old way, or
            # it would hold the level still for as long as it wandered
            if not (self.interval and
                    ts - self.pending[0][0] >= SUSTAIN_AGREE_MAX_INTERVALS * self.interval):
                return []
            held = self.pending
        new_level = _median([x for _, x, _, _ in held])
        surge = self._declare_surge(self.pending[0][1], new_level)
        known_q = [x for _, _, x, _ in held if x is not None]
        known_pv = [x for _, _, _, x in held if x is not None]
        new_q = _median(known_q) if known_q else None
        new_pv = _median(known_pv) if known_pv else None
        since = self.pending[0][0]
        first_off = since                  # the first reading that left the old level
        half = 0.5 * abs(new_level - self.level)
        k = next((i for i, p in enumerate(self.pending) if abs(p[1] - self.level) >= half), 0)
        since = self.pending[k][0]
        # the change happened after the last reading still on the old side of
        # it: one off the old level by the agreement tolerance but nowhere near
        # the new - the boiler's 1885 W after 1921, 35 s on - is that. Spanned
        # from the last reading AT the old level, the boiler's stop reached 41 s
        # back over the EVSE's start and was netted into it (Kozolec 09-20
        # 10:23, 2026-10-02)
        old_side = self.pending[k - 1][0] if k else None
        self.pending = []
        step = new_level - self.level
        # the level it stepped FROM, measured over the samples just before
        # rather than followed, so a fast-drifting reactive signal does not
        # leak its drift into the load's own step
        was_q = _median(self.q_recent) if self.q_recent else self.q_level
        step_q = None if (new_q is None or was_q is None) else new_q - was_q
        pv_step = None if (new_pv is None or self.pv_level is None) else new_pv - self.pv_level
        self.level = new_level
        if new_q is not None:
            self.q_level = new_q
            self.q_recent = [new_q]
        if new_pv is not None:
            self.pv_level = new_pv
        if not self.floor_zero and _is_the_sun(step, pv_step):
            return []                 # (a house-side reading the array is in: the sun cancels there)
        quality = self._step_quality(step, held, since, new_level - step)
        self.last_step_ts = since
        # its span: when the change can have happened - from the last moment
        # the old level is known to have held, to the first reading it settled
        # on - and the level it stepped to: the meter's own record of what it
        # read, spikes never in it (see Fleet._meter_wh)
        self.declared.append((since, step, step_q, old_side if old_side is not None else self.span_start(first_off),
                              held[0][0] if held else since, ts, new_level))
        self.declared_t.append(since)
        if len(self.declared) > 4000:
            del self.declared[:1000]
            del self.declared_t[:1000]
        # a fall waits for its event as a rise does (run lifecycle, 2026-10-28:
        # the legs of one load fall together as they rise together): it is
        # split, paired and closes when its event forms (Detector._form_fall)
        if step < 0 and self.lib is not None:
            hint, self.close_hint = self.close_hint, None
            self.lib.note_fall(self, since, step, step_q, surge, new_level, quality, hint)
            return []
        return self.fall_now(since, step, step_q, surge, new_level, quality)

    def _idle(self) -> bool:
        """Nothing believed running - or nothing but what the falls still
        waiting for their event (Detector._form_fall) will close: the phase
        is idle again from the fall, not from its event."""
        if not self.open_edges:
            return True
        waiting = self.lib.pending_fall(self.name, -math.inf) if self.lib is not None else 0.0
        return waiting > 0 and waiting >= sum(o.watts for o in self.open_edges) - self.noise_at()

    def fall_now(self, since: float, step: float, step_q: Optional[float], surge: float, new_level: float,
                 quality: float, cid: Optional[int] = None, hint: Optional[_Open] = None) -> List[Run]:
        """A declared step's parts declared (a rise whole) - for a fall, once its
        event formed: ``cid`` its event's step group when it spans several
        phases (then never cut into meters' shares, as a rise on several phases
        is not), None to classify each part here as a single-phase fall is.
        Only the runs open before the fall are its to close: a start declared
        while the fall waited for its event is hidden from it meanwhile."""
        later = [o for o in self.open_edges if o.since >= since] if step < 0 else []
        if later:
            self.open_edges = [o for o in self.open_edges if o.since < since]
        if hint is not None and hint in self.open_edges:
            self.close_hint = hint
        parts = (self.lib.metered_parts(self.name, since, step)
                 if self.lib is not None and step < 0 and cid is None else [step])
        closed: List[Run] = []
        for k, part in enumerate(parts):
            closed += self._declare(since, part, None if step_q is None else step_q * (part / step if step else 1.0),
                                    surge if k == len(parts) - 1 else 0.0, new_level, quality, cid)
        if step < 0 and not (later and self._dip(since, -step, later)):
            closed += self._floor_stop(since, new_level)
        self.close_hint = None
        self.open_edges.extend(later)
        return closed

    def _dip(self, at: float, watts: float, later: List[_Open]) -> bool:
        """A fall that closed nothing (held, see HELD_DROPS) and a rise of its
        size declared before its event formed: a dip, the rise taken out of
        ``later`` and its event dropped - see _take_dip."""
        if not self.held_drops or self.held_drops[-1][:2] != (at, watts):
            return False
        back = next((o for o in sorted(later, key=lambda x: x.since) if not self.owned(o)
                     and self._dip_fits(at, watts, o.since, o.watts)), None)
        if back is None or not self._take_dip(self.held_drops[-1], back.since):
            return False
        later.remove(back)
        if self.lib is not None:
            self.lib.drop_pending(back)
        return True

    def _dip_return(self, since: float, step: float) -> bool:
        """A rise that returns a held fall of its size (HELD_DROPS) within the
        dip window: a dip's return, which opens nothing - see _take_dip."""
        for d in reversed(self.held_drops):
            if since - d[0] > self.latency() + DIP_INTERVALS * (self.interval or 0.0):
                return False
            if self._dip_fits(d[0], d[1], since, step) and self._take_dip(d, since):
                return True
        return False

    def _dip_fits(self, at: float, watts: float, back_since: float, back_w: float) -> bool:
        """Is a rise of ``back_w`` at ``back_since`` the return of a fall of
        ``watts`` at ``at``: within the sustain (no dip shorter is ever
        declared) and DIP_INTERVALS readings, and within PAIR_TIGHT_NOISES of
        the two steps' noise."""
        window = self.latency() + DIP_INTERVALS * (self.interval or 0.0)
        tight = PAIR_TIGHT_NOISES * math.sqrt(2.0) * self.noise_at(watts)
        return 0.0 <= back_since - at <= window and abs(back_w - watts) <= tight

    def _take_dip(self, drop: tuple, back_since: float) -> bool:
        """The held fall ``drop`` and its return at ``back_since`` become two
        levels of the newest run that carried it (run lifecycle, 2026-10-28:
        Home's EVBox dipped for a reading or two 22 times in a charge, each
        return a run of its own). False when no run can carry it."""
        at, watts = drop[0], drop[1]
        carrier = next((o for o in reversed(self.open_edges) if o.since < at and not self.owned(o)
                        and o.watts >= watts - self.noise_at(watts)), None)
        if carrier is None:
            return False
        self.held_drops.remove(drop)
        level = carrier.levels[-1][1] if carrier.levels else carrier.watts
        carrier.levels += [(at, level - watts), (back_since, level)]
        return True

    def declared_at(self, since: float) -> Optional[tuple]:
        """The step declared exactly at ``since``, if one was."""
        k = bisect.bisect_left(self.declared_t, since - 0.01)
        return self.declared[k] if k < len(self.declared) and abs(self.declared[k][0] - since) <= 0.01 else None

    def _implied_zeros(self, ts: float) -> int:
        """The moves of nothing the gap before a reading at ``ts`` implies,
        one per interval of the meter's in its silence - see IMPLIED_ZEROS_CAP."""
        if self.last_ts is None or not self.interval:
            return 0
        silence = ts - self.last_ts - SILENCE_CADENCES * self.interval
        return max(0, min(IMPLIED_ZEROS_CAP, int(silence / self.interval)))

    @property
    def on_change(self) -> bool:
        """Does this meter report on change: the settings page's answer, else what its gaps say."""
        return self.change_driven if self.forced is None else self.forced

    def _classify(self) -> None:
        """Does this meter report on change - see SUSTAIN_CADENCES_CHANGE."""
        n = len(self.all_gaps)
        if n < CHANGE_DRIVEN_MIN_GAPS:
            return
        short = sorted(self.all_gaps)[int(MOVING_PERCENTILE * (n - 1))]
        self.change_driven = sum(g > CHANGE_DRIVEN_TAIL * short for g in self.all_gaps) > CHANGE_DRIVEN_SHARE * n

    def latency(self) -> float:
        """How long after a moment this reading's data about it is complete -
        how long a new level must go uncontradicted to be confirmed, a
        silence to mean the value held, and how far before its first reading
        a change can have happened: SUSTAIN_CADENCES of its shortest repeat
        interval, or its report lag against the grid where that is longer
        (``lag``, learned from the steps it shares with the grid - see
        Fleet._learn_lags; the grid itself is the reference and has none).
        The owner (2026-10-02) asked for the lag in place of the repeat interval
        everywhere; for a meter's own levels it is not enough: a level that
        one repeat interval leaves uncontradicted lets a half-caught reading
        found a load, at the grid as at a plug - Home with its meters fed,
        ten days, energy impurity 24.7 % confirmed over the lag against 12.9
        over three repeat intervals (the hidrofor's runs split over two
        signatures, Susilna's meter claiming runs not its own). What waits
        on another meter - the horizon, a house session's wait, the meters'
        tolerances - waits for the lag (Fleet._declare_lag). SUSTAIN_SECONDS
        until a repeat interval is measured."""
        if not self.interval:
            return max(SUSTAIN_SECONDS, self.lag)
        return max((SUSTAIN_CADENCES_CHANGE if self.on_change else SUSTAIN_CADENCES) * self.interval, self.lag)

    def silence_due(self) -> Optional[float]:
        """When the change pending here is confirmed by the meter's silence:
        its latency after the last reading of it with no reading since - the
        recorder writes only changes, so the value held.
        On the readings' clock (Detector._advance), whether the next reading
        or a pass's end comes first: at a pass's end it was confirmed at that
        moment, and in one call at the next reading, half a second before it,
        and only if that one moved away - so the slicing decided it."""
        if not self.pending or self.level is None or not self.interval or self.pending[-1][0] == self._stood:
            return None
        return self.pending[-1][0] + self.latency()

    def stand_in(self, at: float) -> List[Run]:
        """The silence_due moment: the change's last reading stands in for the
        reading the meter did not write. Offered once - a stand-in not taken
        (a glitch on the way) is not offered again."""
        last = self.pending[-1]
        self._stood = last[0]
        return self.process(at, last[1], last[2], last[3], held=True)

    def span_start(self, first_off: float) -> float:
        """From when a change seen first at ``first_off`` can have happened:
        its last reading at the old level - not one of the change's own update
        (see SAME_UPDATE_S) - but no earlier than its latency before: a longer
        silence is the old value held, unwritten.
        Taken from the last steady reading alone, a meter whose value had held
        for a minute had a span reaching a minute back, and chained in other
        loads' steps (the hidrofor's plug; the owner, 2026-10-01)."""
        held_until = self.steady_ts
        if held_until is not None and first_off - held_until <= SAME_UPDATE_S:
            held_until = self.steady_before         # the change's own update
        if not self.interval:
            return first_off if held_until is None else held_until
        earliest = first_off - self.latency()
        return earliest if held_until is None else max(held_until, earliest)

    def _step_quality(self, step: float, held: list, since: float, was: float) -> float:
        """0..1 - see QUALITY_SNR_FULL."""
        snr = abs(step) / max(self.noise_at(was), 1e-9)
        f_snr = min(1.0, max(0.0, (snr - 1.0) / (QUALITY_SNR_FULL - 1.0)))
        vals = [x for _, x, _, _ in held]
        spread = (max(vals) - min(vals)) if len(vals) > 1 else 0.0
        f_settle = min(1.0, max(0.0, 1.0 - spread / abs(step))) if step else 0.0
        window = EVENT_WINDOW_INTERVALS * (self.interval or SUSTAIN_SECONDS)
        f_crowd = QUALITY_CROWDED if self.last_step_ts is not None and since - self.last_step_ts < window else 1.0
        return f_snr * f_settle * f_crowd

    def _declare(self, since: float, step: float, step_q: Optional[float], surge: float,
                 new_level: float, quality: float = 1.0, cid: Optional[int] = None) -> List[Run]:
        """One declared step - or one part of it, see SPLIT_BY_METERS - as a
        start or a stop; ``cid`` the stop's group where its event already
        classified it (Detector._form_fall)."""
        if cid is None:
            cid = self.lib.classify(self.name, since, step, step_q, surge) if self.lib is not None else None
        if step > 0:
            if self.held_drops and self._dip_return(since, step):
                return []
            ended: List[Run] = []
            o = _Open(since, step, step_q, [(since, step)], surge=surge, cluster=cid, q=quality)
            if self.floor_zero:
                o.short0 = sum(x.now or x.watts for x in self.open_edges) - (new_level - step)
            self.open_edges.append(o)
            if self.lib is not None:
                self.lib.note_rise(self, o)      # its cluster comes with its event, see EVENT_WINDOW_INTERVALS
            if len(self.open_edges) > MAX_OPEN_EDGES:
                # the oldest run no meter holds on makes room, closed where it
                # stood, at its last level - popped silently, the dehumidifier's
                # 271 W run (Susilna's plug, 20 hours) was the oldest of twelve
                # when a thirteenth opened and left no session at all; eleven
                # of the eighteen runs dropped so in ten days at Home were a
                # meter's and still on (09-25 21:40, 2026-10-02). All twelve a
                # meter's, the thirteenth joins them
                old = next((x for x in self.open_edges if not self.owned(x)), None)
                if old is not None:
                    ended.append(self._end(old, since, old.now or old.watts))
            return ended
        self.stop_cluster, self.stop_q = cid, quality
        if self.lib is not None and self.lib.placed_fall is not None:
            # a fall a meter's own fall was all of: the stop is the owner's size,
            # as the start of the run it closes was booked (Detector._form_event)
            _, w = self.lib.placed_fall
            self.lib.placed_fall = None
            if w > 0:
                step = -w
        closed = self._pair(since, -step, None if step_q is None else -step_q, new_level)
        self.stop_cluster, self.stop_q = None, None
        closed += self._unseen_stop(since, new_level)
        return closed

    def _floor_stop(self, at: float, new_level: float) -> List[Run]:
        """A fall back to the idle floor: whatever was still open has stopped,
        and closes here. Held open, those starts would pair with an unrelated
        load hours later; dropped, as they were until 2026-10-04, they left no
        session at all - 1,212 runs and 17 kWh in Home's ten days, hidden, and
        the washer-dryer's 03:56 and 05:47 cycles of 24 Sep (+282 W creeping
        to ~395, stopping -394 against 282 + 21 + 14 open: no pairing, no
        multi-close). The runs the fall's unpaired part covers - largest first
        while it still covers them, as a multi-close takes them - close at
        their share of it by their size (followed, or their start); the rest
        at their own size, as the conservation stop closes a run the reading
        cannot carry (_unseen_stop); closing only the covered ones (298 of the
        1,212) left Home's hidden and fed cards where they were. Once per
        declared fall, after all its parts paired
        (SPLIT_BY_METERS): from the first part, the drop had taken runs the
        rest would have closed. A run its meter still shows on stays, and one
        the fall itself opened (a share the other way)."""
        if not self.open_edges or new_level > self.baseline + self.noise:
            return []
        gone = [o for o in self.open_edges if not self.owned(o) and o.since < at]
        unpaired = [d for d in self.held_drops if d[0] == at]
        left = fall = sum(d[1] for d in unpaired)
        size = {id(o): o.now if o.now and o.now > 0 else o.watts for o in gone}   # what it was followed to, or its start
        taken = []
        for o in sorted(gone, key=lambda o: -size[id(o)]):
            if left > 0 and size[id(o)] <= left + self._tol(size[id(o)], left):
                taken.append(o)
                left -= size[id(o)]
        self.open_edges = [o for o in self.open_edges if o not in gone]
        if taken:
            self.held_drops = [d for d in self.held_drops if d not in unpaired]
        whole = sum(size[id(o)] for o in taken)
        out = []
        for o in gone:                     # all out before any closes - see Detector.resolve_rise
            self._remember_close(o, at)
            out.append(self._close(o, at, size[id(o)] * fall / whole if o in taken else o.watts, None))
        return sorted(out, key=lambda x: x.start)

    @property
    def rel_floor(self) -> float:
        """The level above which a RELATIVE noise figure means anything.

        Two things spoil the ratio |w - level| / level low down, and both are
        now measured rather than guessed at: one quantum of the reading looks
        like real wander, and the absolute noise swamps the level it is being
        divided by. Taking the larger against NOISE_REL_CAP reads as "the
        level at which a single quantum would on its own saturate the cap" -
        exactly the point below which the measurement cannot say anything -
        and it needs no constant of its own, because the cap is already there.

        The flat 300 W it replaces was wrong in both directions at once: too
        low for Home, whose phase C measures 37 W of noise and so needs 740,
        and too high for Kozolec at 10 W, which needs 200 (2026-09-22).
        """
        return NOISE_REL_FLOOR_FACTOR * max(self.quantum, self.noise) / NOISE_REL_CAP

    def _held(self) -> list:
        """The pending readings that agree with the newest one - the plateau,
        if one is forming. Those before it are the transition. See
        SUSTAIN_AGREE."""
        if len(self.pending) < 2:
            return self.pending
        w = self.pending[-1][1]
        # Either inside the noise, or close enough that the step measured from
        # either reading would pair with the same edges - the pairing
        # tolerance, applied to the step being made.
        tol = SUSTAIN_AGREE_TOL * self.noise_at(w)
        if self.level is not None:
            tol = max(tol, SUSTAIN_AGREE_REL * abs(w - self.level))
        k = len(self.pending) - 1
        while k > 0 and abs(self.pending[k - 1][1] - w) <= tol:
            k -= 1
        return self.pending[k:] if k else self.pending

    def _corroborated_stop(self, ts: float) -> bool:
        """...and does another leg of the same load say it is stopping too?

        Every open edge the drop could be is asked, not just the newest: two
        loads of one size on one phase are exactly where the newest is the
        wrong answer. The one the other legs vouch for is remembered, so that
        it - and not a same-sized neighbour - is the edge that closes."""
        self.close_hint = None
        if self.level is None or not self.open_edges or not self.corroborate:
            return False
        drop = self.level - _median([x for _, x, _, _ in self._held()])
        if drop <= self.noise_at(self.level):
            return False
        for o in reversed(self.open_edges):
            if abs(o.watts - drop) <= self._tol(o.watts, drop) and self.corroborate(o.since, o.watts, ts):
                self.close_hint = o
                return True
        # a start bigger than the drop, whose partner legs match the DROP
        for o in reversed(self.open_edges):
            if o.watts - drop > self._tol(o.watts, drop) and self.corroborate(o.since, drop, ts, True):
                self.close_hint = o
                return True
        # a start smaller than the drop, whose partner legs match the DROP: its rise met another load's fall in
        # the same reading and read short (Home's kiln, 25 Oct 21:50: c rose 2258 for a 2850 step while a 600 W
        # load on c stopped; the a leg closed and the c leg stayed open five minutes - the a-only and c-only
        # fragments). The legs switch together, so the leg the others vouch for is the one that closes.
        for o in reversed(self.open_edges):
            if drop - o.watts > self._tol(o.watts, drop) and self.corroborate(o.since, drop, ts):
                self.close_hint = o
                return True
        # ...or an open edge the size of the drop, whatever its start: the other legs fell by the same amount in the
        # same instant. A leg left open by an earlier dip that was not held - the kiln's 14 s off gaps against three
        # cadences of 5 s - never matches a partner's START again, and every later pulse of it stayed one run
        # (Home, 25 Oct 23:26 - 26 Oct 00:43: a-only runs of 100-500 s beside c-only ones of 40 s).
        instant = getattr(self.corroborate, "instant", None)
        if instant is not None:
            for o in reversed(self.open_edges):
                if abs(o.watts - drop) <= self._tol(o.watts, drop) and instant(drop, ts):
                    self.close_hint = o
                    return True
        return False

    def _unseen_stop(self, at: float, level: float) -> List[Run]:
        """A phase cannot carry more than it reads: when the loads believed
        running add up to more than the whole reading, one of them stopped
        unseen, and the one that fits the shortfall - the oldest of those -
        is closed here. A shortfall nothing fits is left alone.

        Its stop landed in the same reading as another load's step: Home's
        floor mat against the hob pulsing 2 kW every five seconds, left open
        nine hours until an unrelated drop of its size closed it. Measured on
        the WHOLE reading, not above the idle floor (the owner: "closes a session
        once the meter's total draw is lower than the devices absolutely
        should be"): the floor of a phase that is never idle is a guess, and
        reading above it was worse. Against the mat's thermostat (89.9 h
        heating, 18-27.09) the mat went from 114.0 to 91.1 h counted once, and
        Kozolec's fridge purity from 73 to 86 %; Home's wconc 50.2 -> 48.6,
        the kiln's single-leg pulses 140 -> 160 (2026-09-29). Only on a reading
        of the house alone: one carrying solar reads low with everything on."""
        if not self.floor_zero or not self.open_edges:
            return []
        short = sum(o.now or o.watts for o in self.open_edges) - level
        if short <= self.noise_at(level):
            return []
        for o in self.open_edges:
            if self.owned(o):
                continue
            size = o.now or o.watts
            # only what fell short SINCE it started can be its stop. A
            # shortfall already standing when it began - an older run followed
            # past its own draw while alone - is not: the next run of about its
            # size was closed seconds after it was SEEN to start, at its first
            # settling drop (Home's hidrofor, 2026-10-02: ten runs lost, and
            # Kozolec's pond EVSE a fifth of its charging)
            arose = short - max(0.0, o.short0)
            if abs(size - arose) <= self._tol(size, arose):
                return [self._end(o, at, size)]
        # A shortfall nothing fits still rules out any run bigger ON ITS OWN
        # than the whole reading: it cannot be on. Kozolec's 10.5 kW run held
        # 289 min on a phase reading far less, and took 50 kWh (the owner,
        # 2026-09-30: "should be stopped as soon as the site's total power has
        # dropped under the 10.5 kW mark for a couple readings").
        # On the run's OWN size, never what it is followed to draw now: that
        # carries the drops held since it started - other loads' stops, when
        # their starts went unseen - and read Kozolec's 3,346 W charge as
        # 5,549 W; the rule then closed it at that, and a one-level run's
        # energy is the mean of its start and its close, so 410 min were
        # booked at 4,448 W (27.09, 6.8 kWh over).
        # Below HALF its size, not its size less the noise: a load sags and
        # is still on, as a meter's run is until its meter fell by half its
        # size (Fleet._meter_on) - one notion of a run stopping. On the noise,
        # every sag of a load the reading carried alone ended its run; Home's
        # fed card 71.9 -> 76.4 % partition capture, impurity 6.5 -> 5.6,
        # Hiša's runs 79.5 -> 82.2 (the unify audit, 2026-10-03).
        # And less what the OTHER runs believed running draw (run lifecycle,
        # the owner 2026-10-28: "a 1 kW run can safely be closed once the
        # reading goes under" half of it, the other running groups taken off):
        # a 1 kW run beside a 2 kW boiler on a reading of 2.4 kW is not on. The
        # oldest first, each close taken off before the next is judged; a run
        # its meter still shows on is never judged here, and its meter's
        # reading is what it draws for the others' (level_of).
        out = []
        while True:
            o = next((o for o in sorted(self.open_edges, key=lambda x: x.since) if not self.owned(o)
                      and self._not_carried(o, level)), None)
            if o is None:
                break
            out.append(self._end(o, at, o.watts))
        return out

    def level_of(self, o: _Open) -> float:
        """What run ``o`` draws now (run lifecycle, 2026-10-28: its group's
        learned level, never the phase's reading): what its meter reads of it
        while the meter shows it on (Fleet._meter_level); else its start times
        the ratio learned with its strongest partner; else what it settled at
        (SHAPE_SETTLED_S), or its start's size."""
        if o.meter and self.owned(o) and self.lib.meter_level is not None and self.last_ts is not None:
            lvl = self.lib.meter_level(o.meter, self.name, self.last_ts, o.since)
            if lvl:
                return lvl
        if self.lib is not None and o.cluster is not None:
            best = max(((self.lib.partner_evidence(o.cluster, f), f) for f in self.lib.partners_of(o.cluster)),
                       default=None)
            if best is not None:
                return o.watts * math.exp(self.lib.pair_ratio(o.cluster, best[1])[0])
        return o.now if o.now is not None and o.now > 0 else o.watts

    def _not_carried(self, o: _Open, level: float) -> bool:
        """Is the reading, less what the other open runs draw, under half of
        what ``o`` draws (and under it by more than the noise)?"""
        size = self.level_of(o)
        left = level - sum(self.level_of(x) for x in self.open_edges if x is not o)
        return left < 0.5 * size and size - left > self.noise_at(level)

    def second_rise(self, cid: int, keep: "_Open") -> List[Run]:
        """A rise of a group already running (run lifecycle, 2026-10-28): a
        second UNIT where the reading the rise stepped from, less the other
        runs, still carried the first - the two kept, a cannot-link pair for
        the group (StepGroup.cannot); else the first's fall was missed, and its
        run ends at the newest declared fall between the two of its size
        (PAIR_TIGHT_NOISES), else at its usual length, at most the new rise.
        A load the noise hides is never called a second unit. A run its meter
        holds is the meter's to end."""
        out = []
        e = self.declared_at(keep.since)
        before = e[6] - e[1] if e is not None and len(e) > 6 else None
        for o in [o for o in self.open_edges if o.cluster == cid and o is not keep
                  and o.since < keep.since and not self.owned(o)]:
            others = sum(x.watts for x in self.open_edges if x is not o and x is not keep and x.since < keep.since)
            noise = self.noise_at(o.watts)
            if (before is not None and self.baseline is not None and o.watts > 2.0 * noise
                    and before - self.baseline - others >= o.watts - noise):
                group = self.lib.cluster(cid) if self.lib is not None else None
                if group is not None:
                    group.cannot.append((o.watts, _angle(o.watts, o.var), keep.watts, _angle(keep.watts, keep.var)))
                    del group.cannot[:-CANNOT_KEEP]
                continue
            tight = PAIR_TIGHT_NOISES * math.sqrt(2.0) * noise
            fall = next((d for d in reversed(self.declared) if o.since < d[0] < keep.since
                         and d[1] < 0 and abs(-d[1] - o.watts) <= tight), None)
            usual = self.lib.usual_length(cid) if self.lib is not None else None
            at = fall[0] if fall is not None else (min(keep.since, o.since + usual) if usual else keep.since)
            out.append(self._end(o, at, o.now or o.watts))
        return out

    def _remember_close(self, o, at: float) -> None:
        self.recent_closed.append((o.since, o.watts, at))
        self.recent_closed = [c for c in self.recent_closed if at - c[2] <= 60.0]

    def _end(self, o: _Open, at: float, watts: float, var: Optional[float] = None,
             direct: bool = False) -> Run:
        """Take this run out of open_edges and close it at `at`."""
        self.open_edges.remove(o)
        self._remember_close(o, at)
        return self._close(o, at, watts, var, direct)

    def _declare_surge(self, first: float, new_level: float) -> float:
        """How far a start's FIRST reading towered over the level it settled
        at, when the tower is too short to be a level of its own.

        A motor's starting surge is one reading, and a level has to hold for
        SUSTAIN_INTERVALS - about three - so the median of the pending readings
        swallows it and the surge never becomes a level. The inrush detector
        recovered surges FROM the levels, so it went blind the moment the
        sustain guard was made to work. This is the one moment the individual
        reading is still in hand. Same test as _without_inrush: the first
        reading's step at least INRUSH_RATIO times the settled one."""
        if self.level is None:
            return 0.0
        step = new_level - self.level
        first_step = first - self.level
        if step <= 0 or first_step < INRUSH_RATIO * step:
            return 0.0
        return first_step - step

    def noise_at(self, level: Optional[float] = None) -> float:
        """The smallest change worth calling a step, at that level.

        The measured idle figure is a floor under it, not the whole answer:
        a phase carrying five kilowatts wanders by tens of watts where the
        same phase idle wanders by three."""
        base = self.noise
        if level is None:
            level = self.level if self.level is not None else self.baseline
        return max(base, self.noise_rel * abs(level)) if level else base

    def _tol(self, a: float, b: float) -> float:
        return max(self.noise_at(max(abs(a), abs(b))), MATCH_EDGE_REL * max(a, b))

    def _pair(self, at: float, watts: float, var: Optional[float],
              new_level: float) -> List[Run]:
        """What a step down means, most recent load first.

        Either a load STOPPED, in which case the step undoes its own step up
        and the session closes; or a load STEPPED DOWN to a lower level and
        is still running - a washer leaving its heater for its motor - in
        which case the level is recorded and the session stays open. Anything
        else is a stop we never saw start, and is dropped rather than pinned
        on an unrelated load."""
        # Not by size and recency any more (run lifecycle, 2026-10-28): the
        # newest open start of the fall's size closed Susilna's compressor runs
        # minutes in on an unrelated fall of its size (~50 kWh of its leak).
        # Each open run is scored as its group (_score): the fall against the
        # run's own rise, partner evidence learned from ORDER (Detector.
        # note_order) and the run's age against that pair's length; the best
        # above PAIR_SCORE_MIN closes, the newest of a tie. Before, swept
        # 2026-09-23: newest-that-passes beat best-fit at both sites (Kozolec's
        # boiler 89.2 -> 79.0 % in one signature with best-fit) - size alone,
        # with no order or age behind it.
        #
        # First what a one-device meter below says: its device stopped here,
        # so the run it started ends, whatever the sizes - see Fleet._meter_stop.
        if self.lib is not None and self.lib.meter_stop is not None and self.declared:
            e = self.declared[-1]
            o = self.lib.meter_stop(self.name, e[3], e[4], self.open_edges, watts)
            if o is not None:
                return [self._end(o, at, watts, var, direct=True)]
            if self.lib.meter_read is not None and self.lib.meter_read(self.name, e[0], watts, False, self.last_ts):
                # ...or its readings show this fall, declared or not: the
                # device's own dip or taper, its run going on - see Fleet._meter_read
                return []
        hint, self.close_hint = self.close_hint, None
        if hint is not None:
            for o in self.open_edges:
                if o is hint and abs(o.watts - watts) <= self._tol(o.watts, watts):
                    return [self._end(o, at, watts, var, direct=True)]
                if o is hint and watts - o.watts > self._tol(o.watts, watts):
                    # a start that read short (its rise met another load's fall) and the fall of the whole load
                    return [self._end(o, at, watts, var, direct=True)]
                if o is hint and o.watts - watts > self._tol(o.watts, watts):
                    # the vouched-for leg stopped; the rest is the load that
                    # started with it, and it is still running
                    part = _Open(since=o.since, watts=watts, var=None, levels=[(o.since, watts)],
                                 surge=o.surge)
                    o.watts -= watts
                    o.levels, o.var, o.surge, o.lo, o.hi = [(o.since, o.watts)], None, 0.0, None, None
                    self._remember_close(part, at)
                    return [self._close(part, at, watts, var)]
        cands = []
        for i, o in enumerate(self.open_edges):
            if self.owned(o):
                continue                      # its meter still shows it on: not this fall's
            got = self._score(o, watts, at, new_level)
            if got is not None and got >= PAIR_SCORE_MIN:
                cands.append((got, i))
        if cands:
            return [self._end(self.open_edges[max(cands)[1]], at, watts, var, direct=True)]   # the newest of a tie
        # ...or, nothing matching, the run a rise of the fall's size opened
        # seconds ago, whatever part of it a meter took: a 400 W load
        # dipped twice for 6 s; its first return (+392) was split, 107 W
        # to a plug's coincident wobble and a 286 W rest, and the second
        # dip (-402), matching nothing, joint-stopped a 515 W run 102
        # minutes old (Home 09-24 20:55). The newest run such a rise
        # opened is the one ending now (2026-10-03)
        for i in range(len(self.open_edges) - 1, -1, -1):
            o = self.open_edges[i]
            if self.owned(o) or at - o.since > MERGE_TOLERANCE_S:
                continue
            e = self.declared_at(o.since)
            rose = e[1] if e is not None else 0.0
            if rose > o.watts and abs(rose - watts) <= self._tol(rose, watts):
                return [self._end(o, at, watts, var, direct=True)]
        for i in range(len(self.open_edges) - 1, -1, -1):
            o = self.open_edges[i]
            if not self.owned(o) and o.watts - watts > self._tol(o.watts, watts) and (
                    at - o.since <= SHAPE_SETTLED_S and watts <= SETTLE_SHARE * o.watts):
                o.watts -= watts
                o.levels.append((at, o.watts))
                if o.meter is None and self.lib is not None and self.lib.meter_started is not None:
                    # settled, is it a meter's? A start that was a meter's rise
                    # and a brief coincident load - Susilna's plug and 30 W for
                    # a minute (09-24 18:00: 313 -> 283 W, the plug's 264) - is
                    # the meter's once the load has gone
                    found = self.lib.meter_started(self.name, o.since, o.watts)
                    if found is not None and o.watts - found[1] < max(self.noise_at(), MATCH_EDGE_REL * o.watts):
                        o.meter = found[0]
                return []
        joint = self._joint_stop(at, watts, var)
        if joint:
            return joint
        # several loads going together - the oven and its fan, a programme
        # ending - leave one step too big for any of them alone. Take them
        # largest first while the step still covers them, or nothing.
        order = sorted((i for i, o in enumerate(self.open_edges) if not self.owned(o)),
                       key=lambda i: -self.open_edges[i].watts)
        taken, left = [], watts
        for i in order:
            o = self.open_edges[i]
            if o.watts <= left + self._tol(o.watts, left):
                taken.append(i)
                left -= o.watts
                if left <= self.noise:
                    break
        if len(taken) >= 2 and abs(left) <= self._tol(watts, watts):
            # all out before any closes - see Detector.resolve_rise
            gone = [self.open_edges.pop(i) for i in sorted(taken, reverse=True)]
            for o in gone:                    # remembered like every other close (2026-10-12)
                self._remember_close(o, at)
            return sorted((self._close(o, at, o.watts, None) for o in gone), key=lambda x: x.start)
        if self.open_edges and HELD_DROPS:            # 0: none held (the bench's off)
            self.held_drops.append((at, watts, self.stop_cluster))
            del self.held_drops[:-HELD_DROPS]
        if not self.open_edges:
            # nothing was running: the floor itself moved
            self.baseline = max(new_level, 0.0) if self.floor_zero else new_level
        return []

    def _joint_stop(self, at: float, watts: float, var: Optional[float]) -> Optional[List[Run]]:
        """Close the running load this drop completes together with drops held
        since it started - one or two of them - newest load first. See
        HELD_DROPS."""
        for i in range(len(self.open_edges) - 1, -1, -1):
            o = self.open_edges[i]
            if self.owned(o):
                continue
            size = o.now or o.watts
            need = size - watts
            if need <= self._tol(size, watts):
                continue
            held = [d for d in self.held_drops if d[0] > o.since]
            pick = next(([d] for d in held if abs(d[1] - need) <= self._tol(size, need)), None)
            if pick is None:
                pick = next(([a, b] for k, a in enumerate(held) for b in held[k + 1:]
                             if abs(a[1] + b[1] - need) <= self._tol(size, need)), None)
            if pick is None:
                continue
            for d in pick:
                self.held_drops.remove(d)
            level = size
            for t, w, _ in sorted(pick):
                level -= w
                o.levels.append((t, level))
            return [self._end(o, at, watts, var)]
        return None

    def _score(self, o: _Open, watts: float, at: float, new_level: Optional[float] = None) -> Optional[float]:
        """How well the fall being paired (``watts``, group ``stop_cluster``)
        ends open run ``o``, or None when its size rules it out. Size against
        the run's own rise within PAIR_TIGHT_NOISES of the two steps' noise -
        or, once the run's group and the fall's have partner evidence, against
        the rise times their learned ratio, as wide as its spread; plus the
        evidence; less the run's age off the pair's learned length, the less
        the wider that spreads (a boiler's runs follow the weather)."""
        lib, fall = self.lib, self.stop_cluster
        ev = lib.partner_evidence(o.cluster, fall) if lib is not None and o.cluster is not None and fall is not None else 0.0
        sigma = math.sqrt(2.0) * self.noise_at(max(o.watts, watts))
        expect, width = o.watts, PAIR_TIGHT_NOISES * sigma
        if ev > 0:
            mean, spread = lib.pair_ratio(o.cluster, fall)
            expect = o.watts * math.exp(mean)
            width = PAIR_TIGHT_NOISES * math.hypot(sigma, expect * spread)
        gap = abs(watts - expect)
        if gap > width:
            # ...or the fall leaves the phase carrying the other runs and no
            # more: it emptied this one, whatever its size against the start -
            # a charge that tapered, a compressor that settled (Kozolec's pond
            # EVSE 93.6 -> 78.9 % captured without it, 2026-10-28). On a
            # reading of the house alone, as _unseen_stop.
            if new_level is None or self.baseline is None or not self.floor_zero or watts < 0.5 * self.level_of(o):
                return None
            gap = abs(new_level - self.baseline
                      - sum(self.level_of(x) for x in self.open_edges if x is not o and x.since < at))
            width = PAIR_TIGHT_NOISES * sigma
            if gap > width:
                return None
        score = 1.0 - gap / width + ev
        length = lib.pair_length(o.cluster, fall) if ev > 0 else None
        if length is not None:
            z = (math.log(max(at - o.since, 1.0)) - length[0]) / max(length[1], PAIR_LENGTH_SD_MIN)
            score -= 0.5 * z * z / (1.0 + length[1])
        return score

    def _span(self, since: float, until: float) -> int:
        """How many meter samples a run of that length was measured over, from
        this phase's own observed sampling interval."""
        if not self.interval or self.interval <= 0:
            return 0
        return max(1, int(round((until - since) / self.interval)) + 1)

    def _close(self, o: _Open, at: float, watts: float, var: Optional[float], direct: bool = False) -> Run:
        """``direct``: closed by the fall being paired, whose cluster is then
        this run's stop."""
        if o.cluster is None and self.lib is not None:
            self.lib.resolve_rise(o)             # its event window has not passed: form the event now
        pair = (o.cluster, self.stop_cluster if direct else None)
        levels = list(o.levels)
        if len(levels) == 1:
            # one level throughout: both steps measure the same load, so
            # average them, and its power factor with them - where they agree
            # (the start, or what it was followed to while alone, within the
            # pairing tolerance of the stop - a sag, or a growth only where the
            # run is a meter's and the growth its meter's: followed by the
            # grid's level, a 270 W plug grew to 5 kW with a charger ramping up
            # beside it, Home 09-26 20:28). Where they do not, the smaller: a
            # 514 W run closed by a 2,027 W fall was booked at 1,270 W for two
            # hours (Kozolec 09-22, 2026-10-02); under-report over over-report
            # (the owner)
            start = levels[0][1]
            if o.now is not None and o.now > 0 and not self.owned(o) \
                    and abs(o.now - start) <= self._tol(start, o.now) and at - o.since <= SHAPE_SETTLED_S + 2 * MERGE_TOLERANCE_S:
                start = o.now             # what the young run settled at, see SETTLED_FOLLOW
            agree = (abs(start - watts) <= self._tol(start, watts)
                     or (o.now is not None and o.now > 0 and abs(o.now - watts) <= self._tol(o.now, watts)
                         and (o.now <= start + self._tol(start, o.now) or self.owned(o))))
            levels = [(o.since, 0.5 * (start + watts) if agree else min(start, watts))]
            known = [abs(x) for x in (o.var, var) if x is not None]
            q = sum(known) / len(known) if known else None
        else:
            q = o.var                         # the factor of the level it started at
            # a pump's start reads high and decays over its first seconds
            # (Home's Hidrofor 960 -> 850 W in ~30 s, the grid declaring
            # each slice as a small fall that joins the run as a level):
            # the decay is part of the run's start, which is booked at the
            # level it settled at - the leading levels that only fall, each
            # inside SETTLED_SHARE of the start, within SHAPE_SETTLED_S
            i = 1
            while (i < len(levels) and levels[i][0] - o.since <= SHAPE_SETTLED_S
                   and levels[i][1] < levels[i - 1][1] and levels[0][1] - levels[i][1] <= SETTLED_SHARE * levels[0][1]):
                i += 1
            if i > 1:
                levels = [(levels[0][0], levels[i - 1][1])] + levels[i:]
        # A factor derived from amps too coarse to resolve this load is not a
        # measurement of it. Kozolec's Victron publishes current to 0.1 A -
        # 23 VA at 230 V - so a 62 W load's whole apparent power is under
        # three quanta, sqrt(S^2 - P^2) clamps to zero on 7 % of samples and
        # reads PF 1.00, and 63 % of all samples came out at unity. Believing
        # that split the library into 86 signatures where suppressing it
        # gives 24 (2026-09-22).
        stop_q = self.stop_q if self.stop_q is not None else QUALITY_UNSEEN_STOP
        agree = abs(o.watts - watts) / max(o.watts, abs(watts), 1e-9)
        f_pair = min(1.0, max(0.0, 1.0 - agree / (2.0 * MATCH_EDGE_REL)))
        # what it drew where a meter holding one device owns it, by that
        # meter's levels - its levels are its size, not its energy (Home's
        # EVBox started at 8 kW, charged at 10.6 and tapered, 2026-10-04)
        wh = (self.lib.meter_wh(o.meter, self.name, o, at)
              if o.meter and self.lib is not None and self.lib.meter_wh is not None else None)
        return Run(phases="", start=o.since, end=at, levels={"": levels}, placed=o.meter or "",
                       quality=min(o.q, stop_q) * f_pair,
                       surge_w=o.surge,
                       pf=_pf_from(levels[0][1], q),
                       pf_mad=_pf_spread(levels[0][1], q, self.q_quantum),
                       samples=self._span(o.since, at), low=o.lo, high=o.hi, pair=pair,
                       wh={"": wh} if wh is not None else {})

    def open_scale(self) -> float:
        """The share of the open starts' summed size the phase can really be
        carrying: its level above the idle baseline over that sum, at most 1.
        Open starts are summed as they were declared, and a start whose stop was
        never seen (a restart's replay, a lost fall) stays on that sum - so
        the sum can pass what the meter holds (a fridge read 226 W as 60 + 83 +
        83, a household 2797 W of a measured 1748 W, 2026-10-18). Nothing is
        known to scale by without a level and a baseline: 1."""
        total = sum(self.level_of(o) for o in self.open_edges)
        if total <= 0 or self.level is None or self.baseline is None:
            return 1.0
        return min(1.0, max(self.level - self.baseline, 0.0) / total)

    def active(self, now_ts: float) -> Optional[Tuple[float, float]]:
        """(since, watts) of everything believed to be running on this phase,
        never more than the phase carries above its baseline (``open_scale``)."""
        if not self.open_edges:
            return None
        return (min(o.since for o in self.open_edges),
                sum(self.level_of(o) for o in self.open_edges) * self.open_scale())

    def to_dict(self) -> dict:
        return {"baseline": self.baseline, "noise": self.noise, "level": self.level,
                "q_level": self.q_level, "q_recent": list(self.q_recent), "interval": self.interval,
                "min_noise": self.min_noise, "noise_rel": self.noise_rel,
                "quantum": self.quantum, "q_quantum": self.q_quantum,
                "step_diffs": list(self.step_diffs), "last_w": self.last_w, "pv_level": self.pv_level, "seed": self.seed,
                "seed_zeros": self.seed_zeros,
                "idle_diffs": list(self.idle_diffs), "rel_diffs": list(self.rel_diffs), "pending": [list(x) for x in self.pending],
                "open_edges": [o.as_list() for o in self.open_edges], "last_ts": self.last_ts,
                "raw_last": list(self.raw_last) if self.raw_last else None, "lag": self.lag, "lead": self.lead,
                "floor_zero": self.floor_zero, "moving_gaps": [_trim(x, 2) for x in self.moving_gaps], "all_gaps": [_trim(x, 2) for x in self.all_gaps],
                # what a pass was in the middle of deciding - see test_a_restart_is_only_a_pause
                "moved": self._moved, "ended_upto": self.ended_upto, "last_step_ts": self.last_step_ts,
                "steady_ts": self.steady_ts, "steady_before": self.steady_before, "pv_last": self.pv_last,
                "stood": self._stood, "rel_tick": self._rel_tick,
                "absorb_until": None if self.absorb_until == -math.inf else self.absorb_until,
                "held_drops": [list(x) for x in self.held_drops], "recent_closed": [list(x) for x in self.recent_closed],
                "declared": [[_trim(x, 3) for x in e] for e in self.declared]}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "PhaseState":
        if not d:
            return cls()
        st = cls(baseline=d.get("baseline"), noise=d.get("noise", MIN_NOISE_W), level=d.get("level"),
                   q_level=d.get("q_level"), q_recent=list(d.get("q_recent") or []),
                   interval=d.get("interval", 0.0), noise_rel=d.get("noise_rel", 0.0),
                   quantum=d.get("quantum", 0.0), q_quantum=d.get("q_quantum", 0.0),
                   step_diffs=list(d.get("step_diffs") or []), last_w=d.get("last_w"), pv_level=d.get("pv_level"), seed=list(d.get("seed") or []),
                   seed_zeros=int(d.get("seed_zeros") or 0),
                   idle_diffs=list(d.get("idle_diffs") or []), rel_diffs=list(d.get("rel_diffs") or []),
                   pending=[tuple(x) for x in d.get("pending") or []],
                   open_edges=[_Open.of(x) for x in d.get("open_edges") or []], last_ts=d.get("last_ts"),
                   raw_last=tuple(d["raw_last"]) if d.get("raw_last") else None, lag=float(d.get("lag") or 0.0),
                   lead=float(d.get("lead") or 0.0),
                   floor_zero=bool(d.get("floor_zero", False)), moving_gaps=list(d.get("moving_gaps") or []), all_gaps=list(d.get("all_gaps") or []),
                   _moved=bool(d.get("moved", False)), ended_upto=d.get("ended_upto"),
                   last_step_ts=d.get("last_step_ts"), steady_ts=d.get("steady_ts"),
                   steady_before=d.get("steady_before"), pv_last=d.get("pv_last"), _stood=d.get("stood"),
                   _rel_tick=d.get("rel_tick"),
                   absorb_until=-math.inf if d.get("absorb_until") is None else float(d["absorb_until"]),
                   held_drops=[tuple(x) for x in d.get("held_drops") or []],
                   recent_closed=[tuple(x) for x in d.get("recent_closed") or []],
                   declared=[tuple(e) for e in d.get("declared") or []],
                   declared_t=[e[0] for e in d.get("declared") or []])
        st._classify()
        return st


# ------------------------------------------------------------------ signatures
@dataclass
class Signature:
    id: int
    phases: str
    power: Dict[str, float]              # running mean watts per phase
    duration_s: float                    # running mean
    pf: Optional[float]
    count: int
    first_seen: float
    last_seen: float
    interval_s: Optional[float] = None   # running mean START-to-start spacing ("every 5 min")
    last_start: Optional[float] = None
    locations: Dict[str, int] = field(default_factory=dict)   # submeter name -> sessions also seen there
    # setting -> {"n": value -> runs seen in it, "e": value -> runs chance
    # would put there}, both recency-weighted - see INPUT_MIN_RUNS
    inputs: Dict[str, Dict[str, Dict[str, float]]] = field(default_factory=dict)
    # "surge" / "bump" / "flat" -> runs that started so (recency-weighted)
    # "setting=value" it was born in - see INPUT_SPLIT - and the ones it took
    # back from a born-in twin that turned out to be chance
    born_in: Optional[str] = None
    takes_in: List[str] = field(default_factory=list)
    # the family it belongs to - one load, as one meter sees it, seen through
    # every setting's value: the signatures of one start cluster on one set of
    # phases (Detector._join_families). Every signature has one; most have
    # it to themselves. A family is named, not a signature (the owner, 2026-10-26).
    family: int = 0
    # signatures its first run was kept out of - a switch that was off, a
    # one-device meter that held through its start - which it is never
    # merged into for sharing their device (Detector._merge_devices)
    apart: List[int] = field(default_factory=list)
    # the value's episode count when it was born, how many it has run in (the
    # last one's start), and whether it has been judged to belong - see
    # INPUT_MIN_EPISODES
    born_ep: float = 0.0
    ep_seen: float = 0.0
    last_ep: Optional[float] = None
    born_judged: bool = False
    # hour start (epoch s) -> Wh used in it - see HOURLY_KEEP_S
    hourly: Dict[int, float] = field(default_factory=dict)
    # ...of which the daily repair put there from the day's readings (RECOVER_*), not seen live - same hours as
    # ``hourly``, aged with it; for the live-versus-repaired figures only
    recovered: Dict[int, float] = field(default_factory=dict)
    # ...and the hours aged out of it that a confident unnamed load keeps -
    # see OLDER_KEEP_S. Not in to_dict: the runner stores them apart
    older: Dict[int, float] = field(default_factory=dict)
    # edge cluster id -> how many of its runs used it, as their "start", as a
    # "step" part way through, or as their "stop" - see EDGE_LAG_REACH_S
    edges: Dict[str, Dict[int, float]] = field(default_factory=dict)

    @property
    def location(self) -> str:
        """The meter that saw most of this signature's sessions, or "main"
        when none did. ``most_specific`` refines this with the hierarchy."""
        if not self.locations:
            return "main"
        name, n = max(self.locations.items(), key=lambda kv: kv[1])
        return name if n * 2 >= self.count else "main"
    # Where this load's ENERGY goes, in watt-hours, by hour of day and by
    # weekday (Monday first). Counting starts answered "how often"; what it
    # actually costs on a Saturday is its runtime times its draw, and that
    # is the thing worth looking at (2026-09-17).
    hour_wh: List[float] = field(default_factory=lambda: [0.0] * 24)
    day_wh: List[float] = field(default_factory=lambda: [0.0] * 7)
    level_count: float = 1.0
    name: Optional[str] = None
    # an unnamed signature that may be what this named one BECAME
    successor_id: Optional[int] = None
    # Energy inherited from a fingerprint whose name moved here. Kept apart
    # from hour_wh on purpose: the METER wants the appliance's whole history
    # so it never steps backwards, while the hour-of-day and weekday charts
    # want only what THIS behaviour did - mixing a retired 5.9 kW programme
    # into a 4.2 kW one would describe neither.
    carried_wh: float = 0.0
    # running means of the band its runs wandered across, in watts
    low: Optional[float] = None
    high: Optional[float] = None
    # how much each reading WANDERS between sightings, as a running mean
    # absolute deviation. A load that repeats to within a few per cent is a
    # real device; one whose power and duration are all over the place is
    # the detector pairing unrelated edges, and the evidence score says so.
    # PER PHASE, like every tolerance it is ever compared against. It used to
    # be the deviation of the TOTAL while the bands it met were one leg's, so
    # a three-phase load was judged by a single-phase yardstick: its total
    # wanders about three times what one leg does, and it was refused merges
    # an identical single-phase load was granted (the owner: "why don't we just
    # assume the noise level per phase everywhere", 2026-09-22).
    power_mad: float = 0.0
    pf_mad: float = 0.0
    # the last RUN_MEMORY windows this ran in, for "never two at once"
    runs: List[Tuple[float, float]] = field(default_factory=list)
    # The daily rewrite's length split (the owner, 2026-10-23: length decides in
    # the once-a-day rewrite, which sees the runs' lengths and spacing, never in
    # the live filing): seconds, with the side this signature keeps (+1 the longer
    # runs, -1 the shorter); a run on the other side is filed apart - see
    # takes_run and Fleet._split_by_length. None: no split found.
    split_s: Optional[float] = None
    split_side: int = 0
    # how far the start towers over the run, averaged - see INRUSH_RATIO. A
    # motor does this and nothing else in a house does, so it is evidence
    # rather than noise once it is kept out of the power (2026-09-22:
    # "that spike is a very good device signature, but it has to be taken
    # into account properly to not show as separate loads").
    inrush_w: float = 0.0
    duration_mad: float = 0.0
    interval_mad: Optional[float] = None

    def alike(self, other: "Signature", noise_w: float, inputs: bool = True) -> bool:
        """Would these two be the same signature if they arrived now?

        The same test as ``matches``, between two signatures rather than a
        signature and a session."""
        if self.phases != other.phases or self is other:
            return False
        if self.name and other.name and self.name != other.name:
            return False                       # named apart on purpose
        if not self.takes_len(other.duration_s) or not other.takes_len(self.duration_s):
            return False                       # the daily rewrite split them by length
        if inputs and self.born_in != other.born_in:
            # apart until judged; a born-in one that is chance goes back
            born = self.born_in or other.born_in
            if self.born_in and other.born_in and not (other.born_in in self.takes_in or self.born_in in other.takes_in):
                return False
            lift = self.input_lift(other, born)
            if lift is None or lift >= INPUT_SPLIT_LIFT:
                return False
        # NOT widened by power_mad, unlike matches() - and the difference is
        # the whole point. Against a SESSION the mad is what the signature
        # learned from its own sightings, and a single observation cannot
        # chain, so consulting it lets a genuinely variable load stop being
        # cut into pieces. Between two SIGNATURES the mad is partly damage
        # from earlier merges, and letting it widen admission is the walk
        # feeding itself: each merge grows the mad, a bigger mad admits a
        # longer stride, and the 400 W to 25 W ladder comes back at a slower
        # gait. test_a_pool_may_not_be_stretched_wider_than_the_tolerance_
        # that_made_it caught exactly that when it was tried (2026-09-22).
        spread = 0.0
        for ph in self.phases:
            mine, theirs = self.power.get(ph, 0.0), other.power.get(ph, 0.0)
            flat = max(MATCH_POWER_REL * max(mine, theirs), noise_w)
            # ADMISSION may consult what the pair already knows about its own
            # wander; the anti-walk bound below may not, or the walk rides the
            # widened band. ALIKE_MAD_SHARE says how much of that spread is
            # admissible - swept, see the comment at the constant.
            if abs(mine - theirs) > flat + ALIKE_MAD_SHARE * (self.power_mad + other.power_mad):
                return False
            spread = max(spread, flat)
        # Merging is TRANSITIVE, and that is the trap. Each merge re-centres
        # the band on the new mean, so A can reach B, the pair can reach C,
        # and the walk carries on as far as you let it: ten signatures from
        # 400 W down to 25 W collapsed into one that called itself 94 W and
        # described none of them. Relative tolerances only make the stride
        # proportional - they do not stop the walking. So the merged pool
        # must still be tight enough to be called one load: what it has
        # already absorbed, plus the distance it is about to travel, has to
        # stay inside the same tolerance that let the pair match
        # (the owner asked why consolidation used a fixed figure, 2026-09-21).
        a, b = max(self.count, 1), max(other.count, 1)
        mine_w, theirs_w = sum(self.power.values()), sum(other.power.values())
        mid_w = (mine_w * a + theirs_w * b) / (a + b)
        legs = max(len(self.phases), 1)
        after = ((self.power_mad + abs(mine_w - mid_w) / legs) * a
                 + (other.power_mad + abs(theirs_w - mid_w) / legs) * b) / (a + b)
        if after > spread:
            return False
        if self.pf is not None and other.pf is not None and \
                abs(self.pf - other.pf) > pf_tolerance(self.pf_mad, other.pf_mad):
            return False
        return True

    def swallow(self, other: "Signature") -> None:
        """Take another signature's sightings into this one, by weight."""
        for mine, theirs in ((self.hourly, other.hourly), (self.older, other.older), (self.recovered, other.recovered)):
            for hour, wh in theirs.items():
                mine[hour] = mine.get(hour, 0.0) + wh
        for role, used in other.edges.items():
            mine = self.edges.setdefault(role, {})
            for cid, n in used.items():
                mine[cid] = mine.get(cid, 0.0) + n
        if self.born_in and self.born_in == other.born_in:
            self.born_ep = min(self.born_ep, other.born_ep)
            self.ep_seen = max(self.ep_seen, other.ep_seen)
            self.born_judged = self.born_judged or other.born_judged
        if self.born_in != other.born_in:
            # a twin takes back its chance-born half, and runs in that value from now on
            taken = [b for b in (self.born_in, other.born_in) if b]
            self.takes_in = sorted(set(self.takes_in) | set(other.takes_in) | set(taken))
            self.born_in = None
        self.apart = sorted(set(self.apart) | set(other.apart))
        for name, row in other.inputs.items():
            mine = self.inputs.setdefault(name, {"n": {}, "e": {}, "t": dict(row.get("t") or {})})
            for key in ("n", "e"):
                for v, x in (row.get(key) or {}).items():
                    mine[key][v] = mine[key].get(v, 0.0) + x
            mine["t"] = {"at": max((mine.get("t") or {}).get("at", 0.0), (row.get("t") or {}).get("at", 0.0))}
        a, b = self.count, other.count
        n = a + b
        if n <= 0:
            return
        mine_w, theirs_w = sum(self.power.values()), sum(other.power.values())
        for ph in set(self.power) | set(other.power):
            self.power[ph] = (self.power.get(ph, 0.0) * a + other.power.get(ph, 0.0) * b) / n
        # The gap BETWEEN the two means is part of the merged spread, and
        # averaging the two deviations alone throws it away: fold two tight
        # signatures 300 W apart together and the result claimed its
        # sightings sat within a few watts of each other. That number feeds
        # tightness, which feeds evidence, which feeds the confidence the
        # user is shown - so a merge made a signature look BETTER measured
        # the further apart the things it merged (2026-09-21).
        mid_w = sum(self.power.values())
        legs = max(len(self.phases), 1)
        self.power_mad = ((self.power_mad + abs(mine_w - mid_w) / legs) * a
                          + (other.power_mad + abs(theirs_w - mid_w) / legs) * b) / n
        mid_s = (self.duration_s * a + other.duration_s * b) / n
        self.duration_mad = ((self.duration_mad + abs(self.duration_s - mid_s)) * a
                             + (other.duration_mad + abs(other.duration_s - mid_s)) * b) / n
        self.duration_s = (self.duration_s * a + other.duration_s * b) / n
        self.level_count = (self.level_count * a + other.level_count * b) / n
        self.inrush_w = (self.inrush_w * a + other.inrush_w * b) / n
        if self.pf is not None and other.pf is not None:
            self.pf_mad = min(1.0, ((self.pf_mad + abs(self.pf - other.pf)) * a
                                    + (other.pf_mad + abs(other.pf - self.pf)) * b) / n)
        elif self.pf is None:
            self.pf_mad = other.pf_mad
        if self.pf is None:
            self.pf = other.pf
        elif other.pf is not None:
            self.pf = (self.pf * a + other.pf * b) / n
        self.runs = sorted(self.runs + other.runs)[-RUN_MEMORY:]
        if other.interval_s is not None and (self.interval_s is None or b > a):
            self.interval_s, self.interval_mad = other.interval_s, other.interval_mad
        if other.low is not None and other.high is not None:
            self.low = other.low if self.low is None else (self.low * a + other.low * b) / n
            self.high = other.high if self.high is None else (self.high * a + other.high * b) / n
        self.hour_wh = [x + y for x, y in zip(self.hour_wh, other.hour_wh)]
        self.carried_wh += other.carried_wh
        self.day_wh = [x + y for x, y in zip(self.day_wh, other.day_wh)]
        for name, k in other.locations.items():
            self.locations[name] = self.locations.get(name, 0) + k
        self.first_seen = min(self.first_seen, other.first_seen)
        self.last_seen = max(self.last_seen, other.last_seen)
        if other.last_start is not None:
            self.last_start = max(self.last_start or 0.0, other.last_start)
        self.name = self.name or other.name
        self.count = n

    def absorb(self, s: Run, tz) -> None:
        # A session pulls the running means by how well it was MEASURED, not
        # one-for-one. Eight samples of a 43-second run and forty-three of the
        # same run are not equally good evidence of its power, and letting the
        # first move the mean as hard as the second is how a well-established
        # figure gets dragged about by its worst sightings (2026-09-18).
        n = min(float(self.count), ABSORB_WINDOW)
        k = s.confidence
        pw = s.power_by_phase()
        legs = max(len(self.phases), 1)
        self.power_mad = (self.power_mad * n
                          + k * abs(sum(pw.values()) - sum(self.power.values())) / legs) / (n + k)
        self.inrush_w = (self.inrush_w * n + k * s.inrush_w) / (n + k)
        self.duration_mad = (self.duration_mad * n + k * abs(s.duration_s - self.duration_s)) / (n + k)
        for ph, w in pw.items():
            self.power[ph] = (self.power.get(ph, w) * n + k * w) / (n + k)
        self.duration_s = (self.duration_s * n + k * s.duration_s) / (n + k)
        self.level_count = (self.level_count * n + k * s.level_count) / (n + k)
        self.runs.append((s.start, s.end))
        del self.runs[:-RUN_MEMORY]
        if s.pf is not None:
            # the spread owns BOTH the session's own uncertainty and how far
            # this sighting sits from the mean, the same way power_mad does
            gap = 0.0 if self.pf is None else abs(s.pf - self.pf)
            self.pf_mad = min(1.0, (self.pf_mad * n + k * (s.pf_mad + gap)) / (n + k))
            self.pf = s.pf if self.pf is None else (self.pf * n + k * s.pf) / (n + k)
        if s.low is not None and s.high is not None:
            self.low = s.low if self.low is None else (self.low * n + k * s.low) / (n + k)
            self.high = s.high if self.high is None else (self.high * n + k * s.high) / (n + k)
        if self.last_start is not None:
            gap = s.start - self.last_start
            if gap > 0:
                if self.interval_s is not None:
                    self.interval_mad = (abs(gap - self.interval_s) if self.interval_mad is None
                                         else 0.7 * self.interval_mad + 0.3 * abs(gap - self.interval_s))
                self.interval_s = gap if self.interval_s is None else 0.7 * self.interval_s + 0.3 * gap
        self.last_start = s.start
        self.last_seen = max(self.last_seen, s.end)
        self._spread(s, tz)
        self.count += 1

    def note_input(self, name: str, value: str, shares: Dict[str, float], ts: float) -> None:
        """One run, at ``ts``, seen while the setting ``name`` read ``value``;
        ``shares`` is each value's share of the time as it stands. Weighted by
        TIME, over INPUT_TIME_TAU_S like the shares: by sighting, a load that
        starts 285 times a day remembered eight hours - one wash - and read
        as running in it (Home's 49 W load, 2026-09-28)."""
        row = self.inputs.setdefault(name, {"n": {}, "e": {}, "t": {"at": ts}})
        keep = math.exp(-max(0.0, ts - row.get("t", {}).get("at", ts)) / INPUT_TIME_TAU_S)
        row["t"] = {"at": max(ts, row.get("t", {}).get("at", ts))}
        for key in ("n", "e"):
            for v in row[key]:
                row[key][v] *= keep
        row["n"][value] = row["n"].get(value, 0.0) + 1.0
        for v, share in shares.items():
            row["e"][v] = row["e"].get(v, 0.0) + share

    def input_of(self, name: str) -> Optional[Tuple[str, float, float]]:
        """(value, share of its runs, times chance) the load runs in, once
        believed - see INPUT_MIN_RUNS - or None."""
        row = self.inputs.get(name)
        runs = sum((row or {}).get("n", {}).values())
        if not row or runs < INPUT_MIN_RUNS or self.count < INPUT_MIN_RUNS:
            return None
        if self.born_in and not self.born_judged:
            return None                   # every run of it is in the value by construction
        value, n = max(row["n"].items(), key=lambda kv: kv[1])
        share, lift = n / runs, n / max(row["e"].get(value, 0.0), 1e-9)
        return (value, share, lift) if share >= INPUT_MIN_SHARE and lift >= INPUT_MIN_LIFT else None

    def input_lift(self, other: "Signature", born: str) -> Optional[float]:
        """How many times chance the two together run in ``born``, or None
        before the born-in one has INPUT_MIN_RUNS runs."""
        setting, _, value = born.partition("=")
        n = e = 0.0
        runs = 0.0
        for sig in (self, other):
            row = sig.inputs.get(setting) or {}
            n += (row.get("n") or {}).get(value, 0.0)
            e += (row.get("e") or {}).get(value, 0.0)
            if sig.born_in == born:
                runs += sum((row.get("n") or {}).values())
        if e <= 0 or (runs < INPUT_MIN_RUNS and n + e < INPUT_MIN_RUNS):
            return None                   # too few runs seen AND too few expected to say
        # A value that came once and stayed (a fan left on 33 %) lines up with a load's runs perfectly: every
        # run since is in it, none before. That is a regime change, not the value's doing - only a twin that ran
        # on, outside the value, after the fragment began shows the value matters. A twin that stopped before
        # the fragment began is the same load (Home's fridge and freezer, 791 signatures, 2026-10-25).
        for frag, twin in ((self, other), (other, self)):
            if frag.born_in == born and twin.born_in != born and twin.last_seen < frag.first_seen:
                return 1.0
        return n / e

    def files_in(self, context: Optional[str]) -> bool:
        """May a run made in ``context`` (None: in no rare value) join it?"""
        return self.born_in == context or (context is not None and context in self.takes_in)

    def takes_run(self, s: "Run") -> bool:
        """Is ``s`` on this signature's side of its length split (split_s)? Always, with none."""
        return self.takes_len(s.duration_s)

    def takes_len(self, duration_s: float) -> bool:
        if not self.split_s or not self.split_side:
            return True
        return (duration_s >= self.split_s) == (self.split_side > 0)

    def learn_split(self) -> bool:
        """The daily rewrite: the largest gap in the run memory's log-lengths,
        both sides at least SPLIT_MIN_RUNS runs and their medians at least
        MATCH_DURATION_FACTOR apart, becomes the split; the side with more
        runs is this signature's. True when a split was set or changed."""
        lens = sorted(max(b - a, 1.0) for a, b in self.runs)
        if len(lens) < 2 * SPLIT_MIN_RUNS:
            return False
        logs = [math.log(x) for x in lens]
        best = None
        for i in range(SPLIT_MIN_RUNS, len(logs) - SPLIT_MIN_RUNS + 1):
            gap = logs[i] - logs[i - 1]
            if best is None or gap > best[0]:
                best = (gap, i)
        if best is None or best[0] < math.log(MATCH_DURATION_FACTOR):
            return False                      # the two runs either side of the cut must differ by the factor
        i = best[1]
        lo, hi = lens[:i], lens[i:]
        if hi[len(hi) // 2] / lo[len(lo) // 2] < MATCH_DURATION_FACTOR:
            return False
        split = math.sqrt(lens[i - 1] * lens[i])
        side = 1 if len(hi) >= len(lo) else -1
        changed = (self.split_s, self.split_side) != (split, side)
        self.split_s, self.split_side = split, side
        return changed

    def strongest_input(self):
        """(setting, value, share, times chance) of the one it is most tied to, or None."""
        best = None
        for name in self.inputs:
            got = self.input_of(name)
            if got and (best is None or got[2] > best[3]):
                best = (name, *got)
        return best

    @property
    def watts(self) -> float:
        return sum(self.power.values())

    @property
    def energy_wh(self) -> float:
        """What this load has actually used over everything seen of it,
        including whatever a predecessor did before its name moved here."""
        return sum(self.hour_wh) + self.carried_wh

    @property
    def weekly_wh(self) -> float:
        """At the rate it has been going, what it costs in a week.

        Never extrapolated from less than a day: a load first seen an hour
        ago would otherwise claim a hundred and sixty-eight times its own
        energy."""
        span = max(self.last_seen - self.first_seen, 1.0)
        return self.energy_wh * min(WEEK_SECONDS / span, 7.0)

    @property
    def per_run_wh(self) -> float:
        return self.energy_wh / max(self.count, 1)

    @property
    def when(self) -> str:
        """When this load generally runs, in words, or "" when it keeps no
        time worth mentioning."""
        return when_phrase(self.hour_wh, self.day_wh,
                           max(self.last_seen - self.first_seen, 0.0), self.count)

    def menu_row(self, tz, now: Optional[float] = None, running: bool = False) -> Tuple[str, str]:
        """The same, as a menu row's two lines: a headline short enough never
        to be cut - what it draws, on which phases, how long a run lasts, what
        it might be - and underneath, a line that WRAPS, so it can say the
        rest without losing any of it. One line had to choose between the
        week in words and the week drawn; the second line has room for both
        (2026-09-23: the naming page "does not fit all the text")."""
        # "runs" and "starts every", in so many words: a bare "2 min" read as
        # either how long it runs or how often it comes (2026-09-23)
        head = [f"{_fmt_w(self.watts)} {on_phases(self.phases)}", f"runs {_fmt_s(self.duration_s)}"]
        tag = self.guess().tag
        if tag:
            head.append(tag)
        rest = []
        if self.per_day is not None:
            rest.append(_fmt_per_day(self.per_day))
        rest.append(f"{_fmt_wh(self.weekly_wh)} a week, {_fmt_wh(self.per_run_wh)} a run")
        # how often, in words a person can check against the appliance: a
        # load seen five times in five hours is not one seen five times a week
        span = max(self.last_seen - self.first_seen, 0.0)
        if self.count <= 1:
            rest.append("ran once")
        else:
            rest.append(f"{self.count} runs in {_fmt_s(span)}" if span > 0 else f"{self.count} runs")
        if self.level_count >= 1.5:
            rest.append(f"{round(self.level_count)} levels")
        if (self.low is not None and self.high is not None
                and self.high - self.low >= WANDER_SHARE * self.watts):
            rest.append(f"varies {_fmt_w(self.low)}-{_fmt_w(self.high)}")
        when = last_run_phrase(self.last_seen, now, running)
        if when:
            rest.append(when)
        generally = self.when
        if generally:
            rest.append(generally)
        bars = sparkline(self.day_wh)
        if bars:
            # A day with nothing is a SPACE, and a space is where a line
            # wraps: the week broke in two on the naming page. A no-break
            # space holds it, and holds "Mon-Sun" to it.
            rest.append("Mon-Sun\u00a0" + bars.replace(" ", "\u00a0"))
        return ", ".join(head), " · ".join(rest)

    @property
    def per_day(self) -> Optional[float]:
        """How many times a day it starts, over the days it has been seen.

        Counted, not the spacing's running mean: one long gap, or one run the
        detector merged, throws that out - Kozolec's fridge starts 13 times a
        day and its spacing read 5.5 h. A person counts starts, too (the owner,
        2026-09-28: "can we say how many cycles per day instead of the every
        x hours?"). None until it has been seen across a day, since five
        runs in two hours are not sixty a day."""
        span = self.last_seen - self.first_seen
        if self.count < 2 or span < 86400.0:
            return None
        return (self.count - 1) * 86400.0 / span

    @property
    def evidence(self) -> float:
        """How sure we are this is a REAL repeating load rather than a pair
        of unrelated edges: how often it has been seen, and how tightly its
        power and duration repeat. This is the score that decides whether it
        is worth putting in front of anyone."""
        seen = min(1.0, (self.count - 1) / 4.0)          # five sightings is plenty
        if self.count < 3:
            return round(0.4 * seen, 2)                  # nothing has repeated enough to measure
        legs = max(len(self.phases), 1)
        tight_w = 1.0 - min(1.0, (self.power_mad / max(abs(self.watts) / legs, 1.0)) / 0.15)
        spread = self.duration_mad
        tight_d = 1.0 - min(1.0, (spread / max(self.duration_s, 1.0)) / 0.5)
        tied = self.strongest_input() if INPUT_EVIDENCE else None
        if tied:
            tight_d = max(tight_d, tied[2])
        return round(0.5 * seen + 0.3 * tight_w + 0.2 * tight_d, 2)

    @property
    def regular(self) -> bool:
        """It comes back on a clock - a thermostat, a timer, a fridge."""
        return (self.count >= 4 and self.interval_s is not None and self.interval_mad is not None
                and self.interval_mad < 0.35 * self.interval_s)

    @property
    def recognisable(self) -> float:
        """How easily someone could look at this row and say what it is.

        Separate from evidence, which asks whether it is a real repeating
        load, and from the guess, which asks what kind. This asks whether the
        ROW carries enough for a person to recognise their own house in it,
        because a list sorted only by energy puts an anonymous 600 W
        something above a machine that runs every Saturday at noon (the owner,
        2026-09-18).

        What helps a person: a time it keeps to, a day it keeps to, a clock
        it comes back on, a size worth noticing, more than one phase, and a
        guess specific enough to be worth confirming."""
        bits = []
        total = sum(self.hour_wh)
        if total > 0:
            # concentrated in few hours is recognisable; spread over all 24 is not
            busy = sum(1 for w in self.hour_wh if w > total / 48.0)
            bits.append((1.0 - min(1.0, busy / 12.0), 1.0))
        days = sum(self.day_wh)
        if days > 0:
            busy_d = sum(1 for w in self.day_wh if w > days / 14.0)
            bits.append((1.0 - min(1.0, (busy_d - 1) / 6.0), 0.7))
        if self.regular:
            bits.append((1.0, 0.8))
        # something you would notice running: a kettle's worth per run
        bits.append((min(1.0, self.per_run_wh / 200.0), 1.0))
        if len(self.phases) > 1:
            bits.append((1.0, 0.5))
        if self.locations:
            # a meter saw some of it, even if not enough to claim it
            seen = max(self.locations.values()) / max(1, self.count)
            bits.append((min(1.0, seen * 2.0), 1.2))
        g = self.guess()
        if g.appliance:
            bits.append((min(1.0, g.appliance_confidence / MAX_APPLIANCE), 1.5))
        weight = sum(w for _, w in bits) or 1.0
        return round(sum(v * w for v, w in bits) / weight, 3)

    def guess(self) -> Guess:
        """What KIND of thing this might be. Never a claim - see classify.

        Except where it sits on a meter whose NAME says what it is, which is
        knowledge rather than inference and is treated as such."""
        where = self.location
        return classify(self.watts, self.pf if self.pf_mad <= PF_TRUST_MAD else None,
                        self.level_count, self.duration_s,
                        self.phases, self.interval_s, self.interval_mad, self.hour_wh,
                        self.low, self.high, None if where == "main" else where,
                        self.inrush_w)

    def _spread(self, s: Run, tz) -> None:
        """Put a session's energy into every hour and day it occupied.

        A run from 23:50 to 03:00 belongs to four hours and two days, not to
        the one it began in. And only the part of it this signature was not
        already on for: one device never runs twice at once, and a second
        run over the first is another device of its size. Counting both put
        1.96 kWh in one hour into Home's 635 W floor mat, more than the whole
        of Hiša used in it (2026-09-29)."""
        if s.end <= s.start:
            return
        # what it drew, where its meter followed it (Run.wh), else its levels
        watts = s.energy_wh * 3600.0 / (s.end - s.start) if s.wh else sum(s.power_by_phase().values())
        if watts <= 0:
            return
        for start, end in _uncovered(s.start, s.end, self.runs[:-1]):
            t = start
            while t < end:
                moment = datetime.fromtimestamp(t, tz)
                into = moment.minute * 60 + moment.second + moment.microsecond / 1e6
                step = min(end - t, max(3600.0 - into, 1.0))
                wh = watts * step / 3600.0
                self.hour_wh[moment.hour] += wh
                self.day_wh[moment.weekday()] += wh
                hour = int(t - into)
                self.hourly[hour] = self.hourly.get(hour, 0.0) + wh
                t += step
        self.age(s.end)

    def age(self, now: float) -> None:
        """Let the hours HOURLY_KEEP_S has passed leave ``hourly``: into
        ``older`` while this is an unnamed load confident enough to be named
        one day (OLDER_MIN_*), else gone; and ``older`` past a year gone."""
        cut = now - HOURLY_KEEP_S
        gone = [h for h in self.hourly if h < cut]
        keep = gone and not self.name and self.count >= OLDER_MIN_COUNT and \
            self.energy_wh >= OLDER_MIN_WH and self.evidence >= OLDER_MIN_EVIDENCE
        for hour in [h for h in self.recovered if h < cut]:
            del self.recovered[hour]
        for hour in gone:
            wh = self.hourly.pop(hour)
            if keep:
                self.older[hour] = self.older.get(hour, 0.0) + wh
        for hour in [h for h in self.older if h < now - OLDER_KEEP_S]:
            del self.older[hour]

    def describe(self, tz, now: Optional[float] = None, running: bool = False) -> str:
        """Words for the naming page: '6.1 kW on phases A and C, runs ~80 s,
        starts every 3 min, seen 258 times - maybe a heating element (power
        factor 1.00, one level)'."""
        dur = _fmt_s(self.duration_s)
        gap = f", {_fmt_per_day(self.per_day)}" if self.per_day is not None else ""
        lvl = f", {round(self.level_count)} levels" if self.level_count >= 1.5 else ""
        pf = (f", PF {self.pf:.2f}"
              if self.pf is not None and self.pf_mad <= PF_TRUST_MAD else "")
        span = max(self.last_seen - self.first_seen, 0.0)
        over = f" over {_fmt_s(span)}" if span > 0 else ""
        when = last_run_phrase(self.last_seen, now, running)
        generally = f", {self.when}" if self.when else ""
        line = (f"{_fmt_w(self.watts)} {on_phases(self.phases)}, runs ~{dur}{gap}{lvl}{pf}, "
                f"seen {self.count} times{over}{generally}"
                + (f", {when}" if when else ""))
        guess = self.guess()
        # the short form: the line above already carries the factor, the
        # levels and the size the guess rests on
        return f"{line} - {guess.short}" if guess.kind else line

    def detail(self, tz, parents: Optional[Dict[str, Optional[str]]] = None, meter: Optional[str] = None) -> str:
        """Markdown for the naming form, once one is picked: what it is, when
        it runs, where it is, and how sure each of those is - a ``meter``'s
        own signature inside that meter, its phases the meter's channels."""
        lines = [f"**{self.describe(tz)}**", ""]
        bars = hour_histogram(self.hour_wh)
        if bars:
            lines += [f"Where its energy goes, by hour of day - tallest bar "
                      f"{_fmt_wh(max(self.hour_wh))} of {_fmt_wh(sum(self.hour_wh))}:",
                      "", "```", *bars, "```", ""]
        week = day_histogram(self.day_wh)
        if week:
            # every chart is scaled to its own tallest bar, so without this
            # you cannot tell one sighting from twenty (2026-09-17)
            lines += [f"And by day of week - tallest bar {_fmt_wh(max(self.day_wh))} "
                      f"of {_fmt_wh(sum(self.day_wh))}:", "", "```", *week, "```", ""]
        guess = self.guess()
        if guess.kind:
            both = guess.kind if not guess.alternative else f"{guess.kind} or {guess.alternative}"
            lines.append(f"Looks like {both} - {', '.join(guess.because)} "
                         f"(confidence {guess.confidence:.2f}).")
        elif guess.because:
            lines.append(guess.because[0].capitalize() + ".")
        where = {**self.locations, meter: self.count} if meter else self.locations
        lines.append(f"Where: {describe_location(where, self.count, parents, self.phases)}.")
        clock = " It comes back on a clock." if self.regular else ""
        lines.append(f"Confidence that this is a real repeating load: {self.evidence:.2f}.{clock}")
        if tz is not None and self.last_seen > self.first_seen:
            first = datetime.fromtimestamp(self.first_seen, tz)
            last = datetime.fromtimestamp(self.last_seen, tz)
            lines.append(f"Seen {self.count} times between {first:%a %d %b %H:%M} "
                         f"and {last:%a %d %b %H:%M}.")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        # Written every pass, so it is trimmed to the precision each figure
        # actually carries - watts to a tenth, seconds to a tenth, a power
        # factor to four places, energy to a tenth of a watt-hour. Timestamps
        # keep theirs: an epoch second rounded is a second lost.
        return {"id": self.id, "phases": self.phases,
                "power": {k: _trim(v, 1) for k, v in self.power.items()},
                "duration_s": _trim(self.duration_s, 1),
                "pf": _trim(self.pf, 4), "count": self.count,
                "first_seen": self.first_seen, "last_seen": self.last_seen,
                "interval_s": _trim(self.interval_s, 1),
                "hour_wh": [_trim(x, 1) for x in self.hour_wh],
                "day_wh": [_trim(x, 1) for x in self.day_wh],
                "level_count": _trim(self.level_count, 3), "name": self.name,
                "last_start": self.last_start, "locations": self.locations,
                "power_mad": _trim(self.power_mad, 1), "pf_mad": _trim(self.pf_mad, 4),
                "runs": [[_trim(x, 1), _trim(y, 1)] for x, y in self.runs[-RUN_MEMORY:]],
                "inrush_w": _trim(self.inrush_w, 1),
                "successor_id": self.successor_id, "carried_wh": _trim(self.carried_wh, 1),
                "low": _trim(self.low, 1), "high": _trim(self.high, 1),
                "duration_mad": _trim(self.duration_mad, 1),
                "split_s": None if self.split_s is None else _trim(self.split_s, 1), "split_side": self.split_side,
                "interval_mad": _trim(self.interval_mad, 1),
                "inputs": {n: {k: {v: _trim(x, 3) for v, x in d.items()} for k, d in row.items()} for n, row in self.inputs.items()},
                "born_in": self.born_in, "takes_in": list(self.takes_in), "apart": list(self.apart),
                "family": self.family, "born_ep": self.born_ep, "ep_seen": self.ep_seen, "last_ep": self.last_ep,
                "born_judged": self.born_judged,
                "hourly": {str(h): _trim(wh, 1) for h, wh in self.hourly.items() if wh >= 0.05 * TRIM},
                "recovered": {str(h): _trim(wh, 1) for h, wh in self.recovered.items() if wh >= 0.05 * TRIM},
                "edges": {role: {str(c): _trim(n, 2) for c, n in used.items()} for role, used in self.edges.items()}}

    @classmethod
    def from_dict(cls, d: dict) -> "Signature":
        return cls(id=d["id"], phases=d["phases"], power=dict(d["power"]), duration_s=d["duration_s"], pf=d.get("pf"),
                   count=d["count"], first_seen=d["first_seen"], last_seen=d["last_seen"], interval_s=d.get("interval_s"),
                   hour_wh=list(d.get("hour_wh") or [0.0] * 24),
                   day_wh=list(d.get("day_wh") or [0.0] * 7),
                   level_count=d.get("level_count", 1.0), name=d.get("name"),
                   last_start=d.get("last_start"), locations=dict(d.get("locations") or {}),
                   power_mad=d.get("power_mad", 0.0), pf_mad=d.get("pf_mad", 0.0),
                   runs=[tuple(x) for x in (d.get("runs") or [])], inrush_w=d.get("inrush_w", 0.0),
                   duration_mad=d.get("duration_mad", 0.0),
                   split_s=d.get("split_s"), split_side=int(d.get("split_side") or 0),
                   successor_id=d.get("successor_id"), carried_wh=d.get("carried_wh", 0.0),
                   low=d.get("low"), high=d.get("high"),
                   interval_mad=d.get("interval_mad"),
                   inputs={n: {k: dict(v) for k, v in row.items()} for n, row in (d.get("inputs") or {}).items()},
                   born_in=d.get("born_in"), family=int(d.get("family") or 0),
                   takes_in=list(d.get("takes_in") or []), apart=list(d.get("apart") or []), born_ep=d.get("born_ep", 0.0),
                   ep_seen=d.get("ep_seen", 0.0), last_ep=d.get("last_ep"),
                   born_judged=d.get("born_judged", False),
                   hourly={int(h): float(wh) for h, wh in (d.get("hourly") or {}).items()},
                   recovered={int(h): float(wh) for h, wh in (d.get("recovered") or {}).items()},
                   edges={role: {int(c): float(n) for c, n in used.items()}
                          for role, used in (d.get("edges") or {}).items()})


def _uncovered(start: float, end: float, taken: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """The stretches of start..end that none of ``taken`` covers."""
    out, t = [], start
    for a, b in sorted(taken):
        if b <= t:
            continue
        if a >= end:
            break
        if a > t:
            out.append((t, a))
        t = max(t, b)
    if t < end:
        out.append((t, end))
    return out


def _fmt_wh(x: float) -> str:
    return f"{x:.0f} Wh" if x < 1000 else f"{x / 1000:.1f} kWh"


def _fmt_per_day(rate: float) -> str:
    """Starts a day in words: "12 times a day", or per week once it is rarer."""
    if rate >= 1.5:
        return f"{rate:.0f} times a day"
    if rate >= 0.75:
        return "about once a day"
    week = rate * 7.0
    if week >= 1.5:
        return f"{week:.0f} times a week"
    return "about once a week" if week >= 0.75 else "less than once a week"


# Parts of the day, by the hour they start. Deliberately coarse: a load that
# runs "in the evening" is recognised by that phrase far more readily than by
# "18:00-22:00", and the histogram in the detail view is there for anyone who
# wants the actual shape.
_DAY_PARTS = ((22, 6, "overnight"), (6, 12, "mornings"),
              (12, 18, "afternoons"), (18, 22, "evenings"))
# Tried only when no narrower part fits. A load running 08:00 to 17:00 belongs
# to neither the morning nor the afternoon and is plainly a daytime load; with
# only the narrow windows it got no phrase at all.
_DAY_HALVES = ((6, 18, "daytime"), (18, 6, "nights"))
# How much of a load's energy has to fall inside a window before it is worth
# saying anything at all. Below this the load simply does not keep to a time,
# and a phrase on every row would say nothing while costing the width that
# tells one row from another.
WHEN_SHARE = 0.65
# A week of history before the weekday split is worth reading - with less, one
# quiet weekend makes a load "weekdays only".
WHEN_MIN_DAYS = 7.0
# And two days before the hour-of-day split is worth reading at all: every run
# inside one evening falls in the same hours by construction, so a load seen
# five times over four hours would say "evenings" on the strength of what is
# really a single occasion.
WHEN_MIN_HOUR_DAYS = 2.0
# Three sightings, because two can agree by chance about anything.
WHEN_MIN_COUNT = 3


def _window_share(hour_wh: Sequence[float], start: int, end: int) -> float:
    """The share of a load's energy falling in a window of hours, which may
    wrap around midnight."""
    total = sum(hour_wh)
    if total <= 0:
        return 0.0
    hours = range(start, end) if start < end else list(range(start, 24)) + list(range(0, end))
    return sum(hour_wh[h] for h in hours) / total


def when_phrase(hour_wh: Sequence[float], day_wh: Optional[Sequence[float]] = None,
                span_s: float = 0.0, count: int = 0) -> str:
    """When a load generally runs, in words - or nothing, which is the common
    case and the right answer for it.

    This is a MEASUREMENT where the appliance guess is a prior: "runs at six in
    the evening" is a fact about this house, not a belief about houses. It is
    also what its owner recognises first - "the thing that runs overnight" is a
    better handle on a load than the size of its step (2026-09-22).

    Said only when the load actually keeps to a time. A load scattered through
    the day gets no phrase rather than a misleading one.
    """
    if not hour_wh or sum(hour_wh) <= 0 or count < WHEN_MIN_COUNT:
        return ""
    bits = []
    if span_s >= WHEN_MIN_HOUR_DAYS * 86400.0:
        for windows in (_DAY_PARTS, _DAY_HALVES):
            best = max(windows, key=lambda p: _window_share(hour_wh, p[0], p[1]))
            if _window_share(hour_wh, best[0], best[1]) >= WHEN_SHARE:
                bits.append(best[2])
                break
    if day_wh and sum(day_wh) > 0 and span_s >= WHEN_MIN_DAYS * 86400.0:
        # Per DAY, not per group. There are five weekdays and two weekend
        # days, so a load running uniformly puts 71 % of its energy on
        # weekdays and reads as a weekday load - which put "weekdays" on
        # twenty of the owner's twenty-four rows, distinguishing nothing from
        # nothing (2026-09-22).
        week, end = sum(day_wh[:5]) / 5.0, sum(day_wh[5:]) / 2.0
        total = week + end
        if total > 0:
            if end / total <= 1.0 - WHEN_SHARE:
                bits.append("weekdays")
            elif week / total <= 1.0 - WHEN_SHARE:
                bits.append("weekends")
    return ", ".join(bits)


def last_run_phrase(last_seen: float, now: Optional[float], running: bool = False) -> str:
    """"running now", or how long ago it last did.

    The single most useful thing for telling one row from another, and it was
    the one thing the page did not say. Someone naming a load has just been
    living in the house: they know the dishwasher went on after dinner and
    that nothing has run in the workshop since Tuesday. A row that says it is
    on RIGHT NOW turns naming into walking over and looking (2026-09-22:
    "is a last run or currently running something we could display on the
    naming menu pages?")."""
    if running:
        return "running now"
    if not now or not last_seen or now < last_seen:
        return ""
    ago = now - last_seen
    if ago < 120:
        return "just finished"
    return f"last ran {_fmt_s(ago)} ago"


# ------------------------------------------------------------------ the detector
def _balanced(group: List[Run], candidate: Run) -> bool:
    """Could these be legs of ONE multi-phase load, by size?

    Coinciding in time is not enough. A real multi-phase load is balanced by
    design - a two-phase element, a three-phase motor - and over ten days at
    home the smallest-to-largest ratio inside kiln-sized groups had a median
    of 0.98 and only 4% below 0.4. Timing alone married a 2025 W load on A to
    a 163 W blip on C and called the pair one 2.2 kW two-phase load: a phantom
    invented, and the session stolen from the real single-phase one it
    belonged to (2026-09-18).

    Below the threshold they are two loads that happened to start together,
    and saying so costs nothing - each is still filed on its own phase."""
    watts = [w for m in group for w in m.power_by_phase().values()]
    watts += list(candidate.power_by_phase().values())
    watts = [abs(w) for w in watts if w]
    if len(watts) < 2:
        return True
    return min(watts) >= PHASE_BALANCE_MIN * max(watts)


@dataclass
class StepGroup:
    """A kind of step the meter takes: a load starting, stopping, or changing
    level part way through - see EDGE_LAG_REACH_S. Learned from every step of
    its size on its phase, whatever that step was later paired with."""
    id: int
    phase: str
    up: bool                                  # a rise (a start, or a step up)
    watts: float                              # mean size of the step, always positive
    count: float = 0.0
    watts_mad: float = 0.0
    pf: Optional[float] = None
    pf_mad: float = 0.0
    surges: float = 0.0                       # how many of them surged first
    first_seen: float = 0.0
    last_seen: float = 0.0
    # input -> {what it did at the edge: weight}; "" when it did nothing near it
    signals: Dict[str, Dict[str, float]] = field(default_factory=dict)
    # input -> [weight, sum, sum of squares] of the lag of its change, seconds,
    # the input's time minus the meter's (negative: the input told first)
    lags: Dict[str, List[float]] = field(default_factory=dict)
    # number -> [weight, mean, sum of squared deviations] of its value at the edge
    values: Dict[str, List[float]] = field(default_factory=dict)
    # what each input whose lag is known did at its edges - part of what the
    # edge IS: the floor mat's +630 W with the thermostat going on and a
    # look-alike's +630 W with nothing are two kinds of edge. Only half of the
    # +630 W steps on Home's phase C came with the thermostat (2026-09-29).
    keys: Dict[str, str] = field(default_factory=dict)
    angle: Optional[float] = None             # mean reactive angle, degrees - see EDGE_ANGLE_BIN
    owner: str = ""                           # the meter its steps happened under - see EDGE_BY_METER
    angle_n: float = 0.0
    # Two of its rises that were two units running at once (PhaseState.
    # second_rise): (size, angle) of the first and the second - what may split
    # it in the daily pass (Detector.split_groups), the last CANNOT_KEEP
    cannot: List[Tuple[float, Optional[float], float, Optional[float]]] = field(default_factory=list)

    def same_signals(self, kinds: Dict[str, str]) -> bool:
        """``kinds``: what each input whose lag is known did at this edge. One
        learned after this cluster was made counts as having done nothing."""
        return all(k == self.keys.get(n, "") for n, k in kinds.items())

    def absorb(self, ts: float, watts: float, pf: Optional[float], surge: float,
               kinds: Dict[str, str], lags: Dict[str, Tuple[str, float]], values: Dict[str, float],
               angle: Optional[float] = None) -> None:
        n = min(self.count, ABSORB_WINDOW)
        if angle is not None:
            m = min(self.angle_n, ABSORB_WINDOW)
            self.angle = angle if self.angle is None else (self.angle * m + angle) / (m + 1)
            self.angle_n += 1.0
        self.watts_mad = (self.watts_mad * n + abs(watts - self.watts)) / (n + 1) if self.count else 0.0
        self.watts = (self.watts * n + watts) / (n + 1) if self.count else watts
        if pf is not None:
            if self.pf is None:
                self.pf = pf
            else:
                self.pf_mad = (self.pf_mad * n + abs(pf - self.pf)) / (n + 1)
                self.pf = (self.pf * n + pf) / (n + 1)
        self.count += 1.0
        self.surges += 1.0 if surge else 0.0
        self.first_seen = self.first_seen or ts
        self.last_seen = max(self.last_seen, ts)
        for name, kind in kinds.items():
            row = self.signals.setdefault(name, {})
            row[kind] = row.get(kind, 0.0) + 1.0
        for name, (kind, lag) in lags.items():
            acc = self.lags.setdefault(name + "=" + kind, [0.0, 0.0, 0.0])
            acc[0] += 1.0
            acc[1] += lag
            acc[2] += lag * lag
        for name, x in values.items():
            w, mean, m2 = self.values.get(name, [0.0, 0.0, 0.0])
            w += 1.0
            d = x - mean
            mean += d / w
            self.values[name] = [w, mean, m2 + d * (x - mean)]

    def signal(self, name: str) -> Optional[Tuple[str, float, Optional[float], Optional[float]]]:
        """(what the input most often did at this edge, share of the edges,
        mean lag, its spread), or None when it most often did nothing."""
        row = self.signals.get(name) or {}
        total = sum(row.values())
        kind = max((k for k in row if k), key=lambda k: row[k], default=None)
        if kind is None or total <= 0:
            return None
        w, sx, sxx = self.lags.get(name + "=" + kind, [0.0, 0.0, 0.0])
        mean = sx / w if w else None
        spread = math.sqrt(max(0.0, sxx / w - mean * mean)) if w else None
        return kind, row[kind] / total, mean, spread

    def value(self, name: str) -> Optional[Tuple[float, float]]:
        """(mean, spread) of a number at this edge, or None."""
        w, mean, m2 = self.values.get(name, [0.0, 0.0, 0.0])
        return (mean, math.sqrt(m2 / w)) if w >= 2 else None

    def to_dict(self) -> dict:
        return {"id": self.id, "phase": self.phase, "up": self.up, "watts": _trim(self.watts, 1),
                "count": _trim(self.count, 2), "watts_mad": _trim(self.watts_mad, 1), "pf": _trim(self.pf, 4),
                "pf_mad": _trim(self.pf_mad, 4), "surges": _trim(self.surges, 2),
                "first_seen": self.first_seen, "last_seen": self.last_seen,
                "signals": {n: {k: _trim(v, 2) for k, v in row.items()} for n, row in self.signals.items()},
                "lags": {k: [_trim(x, 2) for x in v] for k, v in self.lags.items()},
                "values": {k: [_trim(x, 3) for x in v] for k, v in self.values.items()},
                "keys": dict(self.keys), "angle": _trim(self.angle, 2), "angle_n": _trim(self.angle_n, 1),
                "where": self.owner, "cannot": [list(x) for x in self.cannot]}

    @classmethod
    def from_dict(cls, d: dict) -> "StepGroup":
        return cls(id=int(d["id"]), phase=d["phase"], up=bool(d["up"]), watts=float(d["watts"]),
                   count=float(d.get("count", 0.0)), watts_mad=float(d.get("watts_mad", 0.0)), pf=d.get("pf"),
                   pf_mad=float(d.get("pf_mad", 0.0)), surges=float(d.get("surges", 0.0)),
                   first_seen=d.get("first_seen", 0.0), last_seen=d.get("last_seen", 0.0),
                   signals={n: dict(row) for n, row in (d.get("signals") or {}).items()},
                   lags={k: list(v) for k, v in (d.get("lags") or {}).items()},
                   values={k: list(v) for k, v in (d.get("values") or {}).items()},
                   keys=dict(d.get("keys") or {}), angle=d.get("angle"), angle_n=float(d.get("angle_n") or 0.0),
                   owner=d.get("where") or "", cannot=[tuple(x) for x in d.get("cannot") or []])


def above_chance(n: float, expected: float, odds: float = ABOVE_CHANCE_ODDS) -> bool:
    """Is ``n`` so far above chance's ``expected`` that chance gives it under 1
    in ``odds`` - by the Chernoff bound on a Poisson count? See PAIR_MIN_RUNS."""
    if expected <= 0 or n <= expected:
        return False
    return n * math.log(n / expected) - n + expected >= math.log(odds)


def edge_scale(watts: float, unit_w: float) -> float:
    """A step's size in measurement errors, one of them ``unit_w`` watts at
    small steps and EDGE_SCALE_REL of the step at large ones - see EDGE_BATCH."""
    return math.asinh(EDGE_SCALE_REL * watts / unit_w) / EDGE_SCALE_REL


def _bin(watts: float, unit_w: float) -> int:
    """The size density's bin a step of ``watts`` falls in - see EDGE_BATCH."""
    return int(math.floor(edge_scale(watts, unit_w) / EDGE_BIN))


def _group(ph: str, up: bool, where: str) -> str:
    """The size density's key: phase, direction and the meter it happened under."""
    return f"{ph}|{int(up)}" + (f"|{where}" if where else "")


@functools.cache
def _kernel(sd: float) -> Tuple[int, List[float]]:
    """valley_segments' Gaussian of ``sd`` bins, normalised: (reach, weights).
    Shared between calls: never change what it returns."""
    reach = int(4 * sd) + 1
    kernel = [math.exp(-0.5 * (k / sd) ** 2) for k in range(-reach, reach + 1)]
    total = sum(kernel)
    return reach, [k / total for k in kernel]


def _exact_add(parts: List[float], x: float) -> None:
    """Add ``x`` to the exact sum held as ``parts`` (Shewchuk's partials, as
    math.fsum keeps them): math.fsum(parts) is then the correctly rounded
    total, the same whatever order the terms came in."""
    i = 0
    for y in parts:
        if abs(x) < abs(y):
            x, y = y, x
        hi = x + y
        lo = y - (hi - x)
        if lo:
            parts[i] = lo
            i += 1
        x = hi
    parts[i:] = [x]


def valley_segments(hist: Dict[int, float], sd: Optional[float] = None,
                    unit: Optional[float] = None) -> List[Tuple[int, int]]:
    """The bins of a size histogram cut into segments at the valleys of its
    smoothed density: (first bin, last bin) of each, where anything is.
    ``sd`` is the smoothing in bins - the size histogram's by default; with
    the size scale's ``unit``, only at the valleys that are significant
    (_significant), as a step group is cut."""
    if not hist:
        return []
    reach, kernel = _kernel(sd or EDGE_KERNEL / EDGE_BIN)
    lo, hi = min(hist) - reach, max(hist) + reach
    dens = [0.0] * (hi - lo + 1)
    for b, w in hist.items():
        base = b - lo - reach
        for k, kw in enumerate(kernel):
            dens[base + k] += w * kw
    segs = _valley_cuts(dens, lo, kernel[reach])
    return segs if unit is None else _significant(dens, lo, kernel[reach], segs, unit)


def _valley_cuts(dens: List[float], lo: int, peak: float) -> List[Tuple[int, int]]:
    """valley_segments' cut of a density whose first slot is bin ``lo``, a
    kernel of ``peak`` at its centre."""
    hi = lo + len(dens) - 1
    floor = 0.1 * peak                          # a tenth of one step's own peak
    segs, start = [], None
    for i, v in enumerate(dens):
        inside = v >= floor
        if inside and start is None:
            start = i
        cut = start is not None and 0 < i < len(dens) - 1 and dens[i - 1] > v <= dens[i + 1]
        if start is not None and (not inside or cut):
            segs.append((start + lo, i - 1 + lo if not inside else i + lo))
            start = None if not inside else i + 1
    if start is not None:
        segs.append((start + lo, hi))
    return segs


def _significant(dens: List[float], lo: int, peak: float, segs: List[Tuple[int, int]],
                 unit: float) -> List[Tuple[int, int]]:
    """The segments ``_valley_cuts`` cut, joined again across every valley
    the counts do not bear out (run lifecycle, 2026-10-28: steps stay together
    until there is evidence they are two devices - a freezer restarting after
    a short gap starts near where it stopped): the shallower side's peak must
    stand VALLEY_SIGMAS Poisson spreads above the valley, in steps (the
    density over one step's peak), an empty stretch as a valley of nothing. A segment
    whose largest size passes GROUP_SPAN times its smallest is cut again at its
    deepest valley until it does not, or has none."""
    if len(segs) < 2:
        return segs
    at = lambda b: dens[b - lo] if 0 <= b - lo < len(dens) else 0.0       # noqa: E731
    top = lambda a, b: max(dens[max(a - lo, 0):b - lo + 1], default=0.0)  # noqa: E731
    out, cuts = [list(segs[0])], {}
    for a, b in segs[1:]:
        prev = out[-1]
        # an empty stretch between them is a valley of nothing: as little
        # evidence of two devices as a shallow one while the piles are small
        valley = at(prev[1]) if a == prev[1] + 1 else 0.0
        shallow = min(top(prev[0], prev[1]), top(a, b))
        n_v, n_p = valley / peak, shallow / peak
        if n_p - n_v > VALLEY_SIGMAS * math.sqrt(n_p + n_v) or (VALLEY_SIGMAS <= 0 and n_p > 0):
            out.append([a, b])
        else:
            cuts.setdefault(len(out) - 1, []).append(prev[1])
            prev[1] = b
    # ...the span still cuts a joined group at its deepest valley, an empty stretch first
    size = lambda b: unit * math.sinh(b * EDGE_BIN * EDGE_SCALE_REL) / EDGE_SCALE_REL     # noqa: E731
    final: List[Tuple[int, int]] = []
    for k, (a, b) in enumerate(out):
        inner = sorted(cuts.get(k, []))
        pieces = [(a, b)]
        while True:
            wide = next(((x, y) for x, y in pieces if size(x) > 0 and size(y + 1) > GROUP_SPAN * size(x)
                         and any(x <= c < y for c in inner)), None)
            if wide is None:
                break
            c = min((c for c in inner if wide[0] <= c < wide[1]), key=at)
            inner.remove(c)
            i = pieces.index(wide)
            pieces[i:i + 1] = [(wide[0], c), (c + 1, wide[1])]
        final.extend(pieces)
    return final


def _valley_cuts_near(dens: List[float], lo: int, peak: float, prev: List[Tuple[int, int]],
                      first: int, last: int) -> List[Tuple[int, int]]:
    """_valley_cuts(dens, lo, peak) when only the slots of bins ``first`` to
    ``last`` have changed since ``prev`` was cut: the scan restarts after the
    last slot below the floor left of them (a segment cannot run through it)
    and stops at the first one right of them, and the cuts either side are kept.
    The same segments as the whole scan, which every step took a pass over the
    whole density for."""
    n = len(dens)
    floor = 0.1 * peak
    j = max(first - lo - 2, -1)
    while j >= 0 and dens[j] >= floor:
        j -= 1
    s0 = j + 1                                   # the scan starts here, with no segment running
    e = last - lo + 1
    while e < n and dens[e] >= floor:
        e += 1
    segs, start = [], None
    for i in range(s0, min(e, n - 1) + 1):
        v = dens[i]
        inside = v >= floor
        if inside and start is None:
            start = i
        cut = start is not None and 0 < i < n - 1 and dens[i - 1] > v <= dens[i + 1]
        if start is not None and (not inside or cut):
            segs.append((start + lo, i - 1 + lo if not inside else i + lo))
            start = None if not inside else i + 1
    if start is not None:
        segs.append((start + lo, lo + n - 1))
    left = [x for x in prev if x[1] - lo < s0]
    right = [x for x in prev if x[0] - lo > e] if e < n else []
    return left + segs + right


def _pile(hist: Sequence[float], least: float = EDGE_LAG_MIN) -> Optional[Tuple[int, int]]:
    """The bins of ``hist`` where its counts pile up, (first, last): chance
    puts them evenly across it, a real link in a few bins. None while fewer
    than ``least`` are seen, or when nothing stands out of the even spread."""
    total = sum(hist)
    if total < least:
        return None
    even = total / len(hist)
    peak = max(range(len(hist)), key=lambda i: hist[i])
    if hist[peak] < 4.0 * even:
        return None
    lo = hi = peak
    while lo > 0 and hist[lo - 1] > 2.0 * even:
        lo -= 1
    while hi < len(hist) - 1 and hist[hi + 1] > 2.0 * even:
        hi += 1
    return lo, hi


def lag_window(hist: Sequence[float]) -> Optional[Tuple[float, float]]:
    """Where an input's changes pile up against the meter's edges (_pile):
    (earliest, latest) lag in seconds, from its histogram of lags over
    -EDGE_LAG_REACH_S..+EDGE_LAG_REACH_S in EDGE_LAG_BIN_S bins."""
    got = _pile(hist)
    if got is None:
        return None
    lo, hi = got
    at = lambda i: -EDGE_LAG_REACH_S + (i + 0.5) * EDGE_LAG_BIN_S  # noqa: E731
    w = sum(hist[lo:hi + 1])
    mean = sum(at(i) * hist[i] for i in range(lo, hi + 1)) / w
    spread = math.sqrt(sum(hist[i] * (at(i) - mean) ** 2 for i in range(lo, hi + 1)) / w)
    half = max(EDGE_WINDOW_MIN_S, EDGE_WINDOW_SPREADS * spread, 0.5 * EDGE_LAG_BIN_S * (hi - lo + 1))
    return mean - half, mean + half


def edge_story(edges: Sequence[StepGroup], sig: "Signature") -> Dict[str, dict]:
    """What a device's starts and stops look like, from the edge clusters its
    runs used: per role, how many runs, the mean step, and for each input what
    it did at that edge in what share of them and how long before or after the
    meter (negative: before), and for each number its value there."""
    by_id = {c.id: c for c in edges}
    out = {}
    for role in ("start", "stop"):
        used = [(by_id[c], n) for c, n in (sig.edges.get(role) or {}).items() if c in by_id]
        total = sum(n for _, n in used)
        if total <= 0:
            continue
        story = {"runs": round(total), "watts": round(sum(c.watts * n for c, n in used) / total),
                 "signals": {}, "values": {}}
        names = {name for c, _ in used for name in c.keys}
        for name in names:
            kinds = {}
            for c, n in used:
                k = c.keys.get(name, "")
                if k:
                    kinds.setdefault(k, []).append((c, n))
            for kind, part in kinds.items():
                share = sum(n for _, n in part) / total
                lagged = [(c.lags[name + "=" + kind], n) for c, n in part if (name + "=" + kind) in c.lags]
                w = sum(acc[0] * n for acc, n in lagged)
                lag = sum(acc[1] * n for acc, n in lagged) / w if w else None
                if name not in story["signals"] or share > story["signals"][name]["share"]:
                    story["signals"][name] = {"kind": kind, "share": round(share, 3),
                                              "lag_s": None if lag is None else round(lag, 1)}
        for name in {name for c, _ in used for name in c.values}:
            got = [(c.value(name), n) for c, n in used if c.value(name) is not None]
            w = sum(n for _, n in got)
            if w:
                story["values"][name] = round(sum(v[0] * n for v, n in got) / w, 2)
        out[role] = story
    return out


@dataclass
class Detector:
    phases: Dict[str, PhaseState] = field(default_factory=lambda: {p: PhaseState() for p in PHASES})
    held: List[Run] = field(default_factory=list)          # closed, waiting for a partner phase
    signatures: List[Signature] = field(default_factory=list)
    recent: List[dict] = field(default_factory=list)           # last sessions with their signature id
    filed_n: int = field(default=0, metadata=DERIVED)   # sessions filed so far, each recent one's "n" - see live_runs
    # ids a merge has retired, so a session filed before it still resolves to
    # the signature that swallowed it
    _moved: Dict[int, int] = field(default_factory=dict)
    # Names whose signature is gone - after a reset, or after an upgrade that
    # could not read the old library. They hold enough of a description to be
    # recognised again, and are handed back to the first signature that looks
    # like them (see _reclaim).
    orphan_names: List[dict] = field(default_factory=list)
    next_id: int = 1
    # the next family id, and what each family has seen of each rare value it
    # ran through: family -> "setting=value" -> {"born": the value's episode
    # count when the family first ran in it, "seen": episodes it ran in, "last":
    # the last one's start} - see Detector._splits
    next_family: int = 1
    # family -> the last HINT_KEEP runs' (length s, start hour of the local day), for the daily repair's hints
    family_runs: Dict[int, List[Tuple[float, float]]] = field(default_factory=dict)
    family_eps: Dict[int, Dict[str, Dict[str, float]]] = field(default_factory=dict)
    tz_offset_s: float = 0.0
    # number -> sorted [(ts, value)], each held until the next: a step
    # records the value in force (StepGroup.values); fed by the fleet, not persisted
    numbers: Dict[str, List[Tuple[float, float]]] = field(default_factory=dict, metadata=DERIVED)
    # setting -> sorted [(ts, value)], fed by the fleet like numbers; and
    # setting -> value -> seconds in it (recency-weighted), with how far that
    # has been counted - these two persisted
    inputs: Dict[str, List[Tuple[float, str]]] = field(default_factory=dict, metadata=DERIVED)
    input_time: Dict[str, Dict[str, float]] = field(default_factory=dict)
    input_until: Dict[str, float] = field(default_factory=dict)
    # setting -> value -> how many times it has come round - see INPUT_MIN_EPISODES
    input_episodes: Dict[str, Dict[str, float]] = field(default_factory=dict)
    # the edge library - see EDGE_LAG_REACH_S - and, per input, how its
    # changes fall against the meter's edges, in EDGE_LAG_BIN_S bins
    edges: List[StepGroup] = field(default_factory=list)
    next_edge_id: int = 1
    lag_hist: Dict[str, List[float]] = field(default_factory=dict)
    # the steps this pass took, (phase, since, watts, VAr, surge), for the
    # fleet to file as edges; and phase -> [(since, cluster id, watts)] of the
    # recent ones, so a run filed later still finds its edges.
    edge_at: Dict[str, List[tuple]] = field(default_factory=dict, repr=False, compare=False)
    # rises waiting the event window for companions on other phases; never persisted
    _pending: List[dict] = field(default_factory=list, repr=False, compare=False)
    # runs ended by a stop found inside a rise when its event formed - see _split_rise
    _split_closed: List[tuple] = field(default_factory=list, repr=False, compare=False, metadata=DERIVED)
    # the pass's work on the readings' clock - see _advance: what it released,
    # whether it files, the reading or deadline being handled, and when the
    # first waiting group is due (None: work it out again)
    _released: List[Run] = field(default_factory=list, repr=False, compare=False, metadata=DERIVED)
    _file_now: bool = field(default=True, repr=False, compare=False, metadata=DERIVED)
    _clock: Optional[float] = field(default=None, repr=False, compare=False)
    _held_due: Optional[float] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    _q: Dict[str, Dict[float, float]] = field(default_factory=dict, repr=False, compare=False, metadata=DERIVED)
    # how far the inputs' changes are known on the fleet's clock (Fleet.process):
    # one a pass brought from after it is not known yet. None: all of them
    horizon: Optional[float] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    _pv: Dict[str, Dict[float, float]] = field(default_factory=dict, repr=False, compare=False, metadata=DERIVED)
    # Partner evidence by ORDER (run lifecycle, 2026-10-28): "rise>fall" group
    # ids -> [falls of the fall group while the rise group was running, sum and
    # sum of squares of log(seconds since its newest start), sum and sum of
    # squares of log(its size / that start's)], counted at every
    # fall against every group running then, whatever closed what - so a wrong
    # close teaches nothing; each rise group's seconds running (one unit or
    # more), each fall group's falls, and the seconds the counts cover - chance
    # is a fall group's rate times the share of time a rise group runs. All of
    # it fades as the step groups' histograms do (EDGE_TAU_S).
    order: Dict[str, List[float]] = field(default_factory=dict)
    run_time: Dict[int, float] = field(default_factory=dict)
    fall_n: Dict[int, float] = field(default_factory=dict)
    order_span: float = 0.0
    order_clock: Optional[float] = None
    order_faded: Optional[float] = None
    # what the fleet read of the inputs for this pass: (changes, their times,
    # numbers); and what is worked out once a pass from the library
    signals: Optional[tuple] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    _by_id: Optional[Dict[int, "StepGroup"]] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    _windows: Optional[Dict[str, tuple]] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    _kinds: Optional[Dict[tuple, List["StepGroup"]]] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    _by_sig: Optional[Dict[int, "Signature"]] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    # start cluster -> {signature id: runs filed there} - see DEVICE_FILING
    start_home: Dict[str, Dict[int, float]] = field(default_factory=dict, repr=False, compare=False)
    # what _merge_devices keeps between filings, so a pass judges only the
    # devices whose signatures changed: start_home's votes by the signature
    # they count for now (devices in start_home order), each signature's
    # device, each device's place in start_home, the devices to judge again
    # and whether all of them are. None: built again from start_home.
    _votes: Optional[Dict[int, Dict[int, float]]] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    _home: Dict[int, int] = field(default_factory=dict, repr=False, compare=False, metadata=DERIVED)
    _dev_pos: Dict[int, int] = field(default_factory=dict, repr=False, compare=False, metadata=DERIVED)
    _rejudge: Set[int] = field(default_factory=set, repr=False, compare=False, metadata=DERIVED)
    _rejudge_all: bool = field(default=True, repr=False, compare=False, metadata=DERIVED)
    # per phase|direction|inputs: bin -> recency-weighted steps, and when it
    # last faded - see EDGE_BATCH; its segments, cut at every step (EDGE_RECUT)
    edge_hist: Dict[str, Dict[int, float]] = field(default_factory=dict, repr=False, compare=False)
    edge_hist_at: Dict[str, float] = field(default_factory=dict, repr=False, compare=False)
    edge_unit: Dict[str, float] = field(default_factory=dict, repr=False, compare=False)
    edge_hist_keys: Dict[str, Dict[str, Dict[int, float]]] = field(default_factory=dict, repr=False, compare=False)
    # size bin -> angle bin -> weight, per phase pattern and direction - see EDGE_ANGLE_BIN;
    # stored as "size bin:angle bin" -> weight. Nested so that a segment's angle
    # density reads only the size bins in the segment (_same_angle)
    edge_hist_angle: Dict[str, Dict[int, Dict[int, float]]] = field(default_factory=dict, repr=False, compare=False)
    # group -> (first size bin, last) of a size segment -> angle bin -> the exact partial sums of
    # that segment's angle density, added to by each step and dropped whenever the histogram is
    # rewritten (fade, rescale) - see _same_angle
    _acol: Dict[str, Dict[Tuple[int, int], Dict[int, List[float]]]] = field(
        default_factory=dict, repr=False, compare=False, metadata=DERIVED)
    # (phase, since, window) -> [(meter, its step then)], set by the Fleet - see SPLIT_BY_METERS
    meter_steps: Optional[object] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    # (phase, since, size, up, var) -> the innermost meter that saw all of the step, set by the Fleet - see EDGE_BY_METER
    step_meter: Optional[object] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    # (meter, phase, since, up) -> it held its value through that moment, set by the Fleet - see _likely_meter
    meter_held: Optional[object] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    # (phase, span start, span end, open runs) -> the run a one-device meter's own stop there ends, set by the Fleet
    meter_stop: Optional[object] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    # (meter, phase, since, size) -> the meter whose step started that run still shows it on, set by the Fleet - see PhaseState.owned
    meter_on: Optional[object] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    # (phase, since, size) -> the one-device meter whose own rise of that size came with a start the map could not place
    meter_started: Optional[object] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    # (meter, phase) -> what the meter draws above its floor now, in the grid's terms - the size of a run that is its
    meter_level: Optional[object] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    meter_ended: Optional[object] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    # (meter, phase, run, until) -> Wh a run a meter holding one device owns drew, by its levels, or None
    meter_wh: Optional[object] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    # (phase, since, size, up, t) -> the one-device meter whose readings show that step while a run it owns there is on
    meter_read: Optional[object] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    placement_conf: Optional[float] = field(default=None, repr=False, compare=False, metadata=DERIVED)   # the last placement's timing confidence
    # (the grid's change, from, to) when the last placement matched the meter's step over both steps' window
    placement_window: Optional[tuple] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    # the step a placement built (steps.Step): the grid's edge, the owner's and
    # every meter's between, the change on every system phase - set by the Fleet
    # beside placement_window; what the group and the run are booked at is the
    # OWNER's reading in the grid's terms (steps-and-claims, stage 2)
    placement_step: Optional[Step] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    # (owner, its fall's size in the grid's terms) of the fall just classified,
    # where a meter's own fall was all of it: the stop pairs at the owner's size,
    # as the run was booked at the owner's rise (_declare) - read once
    placed_fall: Optional[tuple] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    _segs: Dict[str, list] = field(default_factory=dict, repr=False, compare=False)
    # each group's size density between steps: [the histogram, its keys' order, first bin, density] - see _valleys
    _dens: Dict[str, list] = field(default_factory=dict, repr=False, compare=False)
    _device_home: Optional[Dict[int, Dict[int, float]]] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    # the day's runs this detector filed or a meter took, and the readings it
    # stepped, for the day's repair (Fleet._repair), kept from when its fleet
    # first read (_day_from, None: kept by no one); stored, as the fleet's _repaired_to is
    _day_from: Optional[float] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    _day_log: List[Run] = field(default_factory=list, repr=False, compare=False)
    _day_rows: Dict[str, List[Tuple[float, float]]] = field(default_factory=dict, repr=False, compare=False)
    # the cycles the daily repair found again (_recover_day): the last RECOVER_LOG_KEEP, for the bench and the diagnostics
    recovered_log: List[Run] = field(default_factory=list, repr=False, compare=False, metadata=DERIVED)

    # ------------------------------------------------ ingest
    def process(self, samples: Dict[str, Sequence[Tuple[float, float]]],
                q: Optional[Dict[str, Dict[float, float]]] = None, now_ts: Optional[float] = None,
                pv: Optional[Dict[str, Dict[float, float]]] = None,
                q_quantum: Optional[Dict[str, float]] = None, file: bool = True) -> List[Run]:
        """Feed new (ts, watts) samples per phase, in time order per phase.
        With ``file`` False the sessions are closed and merged but not filed:
        the Fleet files them once their sub-meter partners have had a chance
        to report (see SUB_OVERRIDE).
        ``q`` is reactive VAr keyed by the SAME timestamps, where the meter
        gives enough to work it out, and ``q_quantum`` how much apparent power
        one quantum of the amps behind it is worth - the limit on any factor
        derived from it. Returns the sessions this batch closed.

        A pass is only a pause (2026-10-02). Everything is decided on the
        readings' clock - a change its meter's silence confirms, a start's
        event window, a run's wait for a partner leg - when its moment comes
        before the next reading (see _advance), and what is not due when the
        pass ends waits for the next one. ``now_ts`` is how far the readings
        reach: the meters wrote nothing after their last row until then. The
        same history read in one call, in six-hour slices or a minute at a
        time files the same sessions; before, the pass's end confirmed every
        pending change, formed every waiting event and filed what had waited
        out its tail, and a library filled only once a pass."""
        stream = self.begin(samples, q, pv, q_quantum, file)
        latest = max([now_ts or 0.0] + [ts for ts, _, _ in stream[-1:]])
        for ts, ph, w in stream:
            self._advance(ts)
            self.step(ts, ph, w)
        self._advance(latest)
        return self.finish(samples, latest)

    def begin(self, samples: Dict[str, Sequence[Tuple[float, float]]],
              q: Optional[Dict[str, Dict[float, float]]] = None,
              pv: Optional[Dict[str, Dict[float, float]]] = None,
              q_quantum: Optional[Dict[str, float]] = None, file: bool = True) -> List[tuple]:
        """A pass's set-up - see process, or Fleet.process, which interleaves
        its meters' readings. Returns the pass's readings as (ts, phase,
        watts): ALL phases in time order, not one phase after another, which
        lets one leg vouch for another (see _corroborate), and readings of one
        moment phase by phase, not by where each fell in its pass's rows."""
        self._released, self._file_now = [], file
        self._q, self._pv = q or {}, pv or {}
        for ph, st in self.phases.items():
            st.lib, st.name = self, ph
        stream = []
        for ph, rows in samples.items():
            if ph not in self.phases:
                continue
            st = self.phases[ph]
            if q_quantum and q_quantum.get(ph):
                st.q_quantum = q_quantum[ph]
            stream.extend((ts, ph, w) for ts, w in rows)
        for ph, st in self.phases.items():
            st.corroborate = self._corroborate(ph, samples)
        stream.sort(key=lambda r: (r[0], r[1]))
        return stream

    def step(self, ts: float, ph: str, w: float) -> None:
        """One reading, once everything due before it is done (_advance)."""
        self._clock = ts if self._clock is None else max(self._clock, ts)
        qm, pvm = self._q.get(ph) or {}, self._pv.get(ph) or {}
        if self._day_from is not None:
            self._day_rows.setdefault(ph, []).append((ts, w))
        self._closed(ph, self.phases[ph].process(ts, w, qm.get(ts), pvm.get(ts)))

    def finish(self, samples, latest: float, oldest: Optional[float] = None) -> List[Run]:
        """A pass's end: what it released, filed or for the fleet to file.
        The edges a signature's are told from (Fleet._edges_of) are kept
        SWITCH_MEMORY_S before the oldest run still to be filed - ``oldest``,
        as the fleet knows it, or this detector's own - not before the pass's
        first reading, which dropped a long run's start edge in short passes."""
        if oldest is None:
            oldest = min([self._clock or latest] + [o.since for st in self.phases.values() for o in st.open_edges]
                         + [st.pending[0][0] for st in self.phases.values() if st.pending]
                         + [s.start for s in self.held])
        cut = oldest - SWITCH_MEMORY_S
        for ph in self.edge_at:
            self.edge_at[ph] = [e for e in self.edge_at[ph] if e[0] >= cut]
        # once per pass, not once per session: it walks the whole
        # library for every named load, and it only points the naming page
        if latest:
            self._link_successors(latest)
        out, self._released = self._released, []
        return out

    def _closed(self, ph: Optional[str], sessions) -> None:
        """Runs a phase closed, and any a split rise ended (_split_rise), into
        the pool that waits for partner legs (see HELD_TAIL_S)."""
        got = [(ph, s) for s in sessions] + self._split_closed
        self._split_closed = []
        for fph, s in got:
            s.phases = fph
            s.levels = {fph: s.levels.pop("")}
            if "" in s.wh:
                s.wh = {fph: s.wh.pop("")}
            self.held.append(s)
        if got:
            self._held_due = None

    def _advance(self, until: float) -> None:
        """Everything due on the readings' clock before ``until``, earliest
        first: a pending change its meter's silence confirms (silence_due), a
        start whose event window has passed (event_wait), a run that has
        waited out its tail for a partner leg (HELD_TAIL_S). Called before
        each reading with its time, and at a pass's end with how far the
        readings reach - nothing happens between two readings but these, so
        a pass ending between them changes nothing."""
        while True:
            due = self.next_due()
            if due is None or due[0] >= until:
                return
            self.fire(due)

    def next_due(self) -> Optional[tuple]:
        """(when, what, phase) of the first thing due - see _advance."""
        cands = [(d, 0, ph) for ph, st in self.phases.items() for d in [st.silence_due()] if d is not None]
        if self._pending:
            cands.append((min(x["since"] for x in self._pending) + self.event_wait(), 1, None))
        d = self._held_next()
        if d is not None:
            cands.append((d, 2, None))
        return min(cands, key=lambda c: c[0], default=None)     # the first of a tie, as before

    def fire(self, due: tuple) -> None:
        d, kind, ph = due
        self._clock = d if self._clock is None else max(self._clock, d)
        if kind == 0:
            self._closed(ph, self.phases[ph].stand_in(d))
        elif kind == 1:
            self._form_first()
        else:
            self._release_held()

    def _groups(self) -> List[List[Run]]:
        """The waiting runs as legs of one load: start AND end within
        MERGE_TOLERANCE_S, on other phases, balanced."""
        groups: List[List[Run]] = []
        for s in sorted(self.held, key=lambda s: s.start):
            for g in groups:
                if (abs(g[0].start - s.start) <= MERGE_TOLERANCE_S and abs(g[0].end - s.end) <= MERGE_TOLERANCE_S
                        and all(s.phases not in m.phases for m in g)
                        and _balanced(g, s)):
                    g.append(s)
                    break
            else:
                groups.append([s])
        return groups

    @staticmethod
    def _group_due(g: List[Run]) -> float:
        """When a group stops waiting: at once with three legs, else
        HELD_TAIL_S after its last leg ended."""
        return -math.inf if len(g) >= 3 else max(m.end for m in g) + HELD_TAIL_S

    def _held_next(self) -> Optional[float]:
        if self._held_due is None:
            self._held_due = min((self._group_due(g) for g in self._groups()), default=math.inf)
        return None if self._held_due == math.inf else self._held_due

    def _release_held(self) -> None:
        """The group due first goes out as one session: filed, or handed to the
        fleet (``file`` False)."""
        g = min(self._groups(), key=lambda g: (self._group_due(g), g[0].start))
        self.held = [s for s in self.held if all(s is not m for m in g)]
        self._held_due = None
        s = self._combine(g)
        if self._file_now:
            self._file(s)
            self._merge_devices()
        self._released.append(s)

    def _corroborate(self, ph: str, samples):
        """Build the question one phase may ask of the others for this pass:
        is a leg of the same load stopping on another phase right now?

        "The same load" is the test multi-phase detection already trusts: an
        edge on another phase that started within MERGE_TOLERANCE_S of this
        one and is balanced with it. "Stopping" is either that it has already
        closed, within the same window, or that its own reading at this
        instant shows the same drop. A real multi-phase device switches its
        legs together, every time - the owner's point about co-occurrence - so one
        leg seeing its gap is evidence the other's short gap was real too.
        A single-phase load never has a partner and is never affected."""
        index = {}
        for oph, ost in self.phases.items():
            # with its last reading before the pass: at a pass's start the
            # other leg's reading as of now was in the last one's rows
            rows = list(samples.get(oph) or [])
            if ost.raw_last and (not rows or ost.raw_last[0] < rows[0][0]):
                rows.insert(0, ost.raw_last)
            if oph != ph and rows:
                index[oph] = ([t for t, _ in rows], rows)

        def as_of(oph: str, ts: float):
            times, rows = index.get(oph, ((), ()))
            # readings SAME_UPDATE_S apart are one instant: the other leg's, a millisecond after this one's
            # (Hiša's a and c of one pulse, 23:31:05.155 and .156), is its value now, not its next
            i = bisect.bisect_right(times, ts + SAME_UPDATE_S) - 1
            if i < 0:
                return None
            st = self.phases[oph]
            fresh = max(1.5 * (st.interval or 0.0), MERGE_TOLERANCE_S / 2)
            return rows[i][1] if ts - rows[i][0] <= fresh else None

        def check(since: float, watts: float, ts: float, closed_only: bool = False) -> bool:
            own = self.phases[ph].interval or 0.0
            for oph, ost in self.phases.items():
                if oph == ph or ost.level is None:
                    continue
                # Far tighter than the merge test on purpose. Borrowing that -
                # 15 s and a balance of 0.4 - let an unrelated load on another
                # phase vouch for a single-phase pump's one-reading dip, and
                # Home's hidrofor lost 20 of its sessions on days the kiln
                # never fired. A real multi-phase device switches its legs in
                # the same poll and draws near-equal on them: the kiln's legs
                # start at +0.0 s and are 98 % balanced.
                window = CORROBORATE_INTERVALS * max(own, ost.interval or 0.0) or MERGE_TOLERANCE_S
                for osince, owatts, closed_at in (
                        [(o.since, o.watts, None) for o in ost.open_edges] + list(ost.recent_closed)):
                    if abs(osince - since) > window or min(owatts, watts) <= 0:
                        continue
                    if min(owatts, watts) / max(owatts, watts) < CORROBORATE_BALANCE:
                        continue
                    if closed_at is not None:
                        if abs(closed_at - ts) <= window:
                            return True
                        continue
                    if closed_only:
                        # a split takes a partner that has actually CLOSED: the
                        # reading-level test below lets any noisy phase vouch
                        # for a small load, and the split then fired 164 times
                        # in ten days, eleven of them on the kiln (2026-09-23)
                        continue
                    v = as_of(oph, ts)
                    if v is not None and abs((ost.level - v) - owatts) <= ost._tol(owatts, abs(ost.level - v)):
                        return True
            return False

        def instant(drop: float, ts: float) -> bool:
            """Did another phase fall by about ``drop`` in this very instant (or close its own run then)?"""
            if drop < CORROBORATE_MIN_NOISES * max((st.noise or MIN_NOISE_W) for st in self.phases.values()):
                return False                  # a small fall: every phase has one now and then
            for oph, ost in self.phases.items():
                if oph == ph or ost.level is None:
                    continue
                window = CORROBORATE_INTERVALS * max(self.phases[ph].interval or 0.0, ost.interval or 0.0) or MERGE_TOLERANCE_S
                for _, owatts, closed_at in ost.recent_closed:
                    if closed_at is not None and abs(closed_at - ts) <= window \
                            and min(owatts, drop) / max(owatts, drop) >= CORROBORATE_BALANCE:
                        return True
                v = as_of(oph, ts)
                if v is not None:
                    # by its settled level - and by its own reading just before, for a leg whose level already
                    # took this fall (it is judged first when it comes first: Hiša's c at 00:00:56.759, 1 ms
                    # before a's, left a 79 s a-only run)
                    times, rows = index[oph]
                    j = bisect.bisect_left(times, ts - SAME_UPDATE_S) - 1
                    for gone in (ost.level - v, rows[j][1] - v if j >= 0 else 0.0):
                        if gone > 0 and min(gone, drop) / max(gone, drop) >= CORROBORATE_BALANCE:
                            return True
            return False
        check.instant = instant
        return check

    def signature_of(self, s: Run) -> Optional["Signature"]:
        """The signature a just-filed session went into.

        From the session itself. Reading it back out of ``recent`` worked only
        while a pass filed fewer sessions than that list keeps."""
        if s.signature_id is None:
            return None
        sid = self._current(s.signature_id)
        return next((x for x in self.signatures if x.id == sid), None)

    def _current(self, sid: int) -> int:
        """The signature ``sid`` is now: the one each merge since moved it
        into, followed until it stops."""
        if sid not in self._moved:
            return sid
        seen = set()
        while sid in self._moved and sid not in seen:
            seen.add(sid)
            sid = self._moved[sid]
        return sid

    @staticmethod
    def _combine(g: List[Run]) -> Run:
        if len(g) == 1:
            return g[0]
        levels, wh = {}, {}
        pfs = []
        for m in g:
            levels.update(m.levels)
            wh.update(m.wh)
            if m.pf is not None:
                pfs.append(m.pf)
        return Run(phases="".join(sorted(levels)), start=min(m.start for m in g), end=max(m.end for m in g),
                       levels=levels, pf=(sum(pfs) / len(pfs)) if pfs else None,
                       quality=min(m.quality for m in g),
                       surge_w=sum(m.surge_w for m in g),
                       pf_mad=max((m.pf_mad for m in g if m.pf is not None), default=0.0),
                       legs=[m.pair for m in g if m.pair], wh=wh, placed=next((m.placed for m in g if m.placed), ""))

    def _input_context(self, s: Run):
        """The rarest of the settings' rare values in force halfway through
        the run, as ("setting=value", when that episode of it began) - see
        INPUT_SPLIT - or None."""
        best = None
        for name, rows in self.inputs.items():
            spent = self.input_time.get(name) or {}
            total = sum(spent.values())
            i = bisect.bisect_right(rows, ((s.start + s.end) / 2.0, "\uffff")) - 1
            if i < 0 or total < INPUT_SPLIT_MIN_TIME_S:
                continue
            if rows[i][1] == INPUT_DOWN:
                continue
            share = spent.get(rows[i][1], 0.0) / total
            if share < INPUT_RARE_SHARE and (best is None or share < best[0]):
                best = (share, f"{name}={rows[i][1]}", rows[i][0])
        return (best[1], best[2]) if best else None

    def _file(self, s: Run, prefer: Optional[int] = None, avoid: Sequence[int] = ()) -> None:
        """File a closed session into the library. ``prefer`` is a signature
        a sub-meter's own detection says this load belongs to; it wins over
        the best-scoring one whenever it fits at all (see SUB_OVERRIDE)."""
        tz = timezone.utc if not self.tz_offset_s else timezone(__import__("datetime").timedelta(seconds=self.tz_offset_s))
        noise = max(self.phases[p].noise for p in s.phases) if s.phases else MIN_NOISE_W
        best = None
        device = self.device_of(s)
        if prefer is not None:
            prefer = self._current(prefer)
        found = self._input_context(s)
        context, episode = found if found else (None, None)
        if prefer is not None:
            # a sub-meter's word
            # taken whether or not the run's power looks like it: the device's
            # own meter saw it run. Asking it to look alike too sent the rest
            # elsewhere - Home 79.8 / 76.0 % that way, 86.0 / 85.4 trusted,
            # Kozolec 98.3 / 94.1 and 99.8 / 98.9 (2026-09-30)
            want = self._sig(prefer)
            if want is not None and want.id not in avoid and self._takes(want, context) and want.phases == s.phases and want.takes_run(s):
                best = want
        if best is None and device is not None:
            best = self._device_signature(device, context, avoid, s.phases, s)
        if best is None:
            best = Signature(id=self.next_id, phases=s.phases, power=s.power_by_phase(), duration_s=s.duration_s,
                             pf=s.pf, count=0, first_seen=s.start, last_seen=s.start, level_count=float(s.level_count),
                             born_in=context, apart=sorted({self._current(x) for x in avoid}))
            if context:
                setting, _, value = context.partition("=")
                best.born_ep = (self.input_episodes.get(setting) or {}).get(value, 1.0) - 1.0
            self.next_id += 1
            self.signatures.append(best)
            # one more of its device's family, when the device has one (a value the family runs in on its own
            # - Detector._splits); else a family of its own
            fam = self._device_family(device, s.phases) if device is not None else 0
            if fam and not any(self._fits(m, s) for m in self.signatures if m.family == fam):
                # a start cluster shared with another load's starts (Home's Hidrofor surge-high starts of 960-1056 W
                # and a 1 kW heater's): a run that looks like none of the family's signatures is no member of it,
                # or the family's name covered the other load's energy (Hidrofor's, by family: 20.2 % impure, 10.4
                # per signature; 12.6 with this, 2026-10-27)
                fam = 0
            best.family = fam or self._new_family()
            best.name = next((m.name for m in self.family_of(best) if m.name and m is not best), None)
            best.absorb(s, tz)
            best.count = 1
        else:
            best.absorb(s, tz)
        for name, rows in self.inputs.items():
            # the setting halfway through the run: a washer's heater starts
            # as its wash phase does, a moment either side of the change
            i = bisect.bisect_right(rows, ((s.start + s.end) / 2.0, "\uffff")) - 1
            total = sum((self.input_time.get(name) or {}).values())
            if i >= 0 and total > 0 and rows[i][1] != INPUT_DOWN:
                best.note_input(name, rows[i][1], {v: t / total for v, t in self.input_time[name].items()}, s.start)
        if best.family:
            rows = self.family_runs.setdefault(best.family, [])
            rows.append((s.duration_s, ((s.start + self.tz_offset_s) % 86400.0) / 3600.0))
            del rows[:-HINT_KEEP]
        if context and best.family:
            row = self.family_eps.setdefault(best.family, {}).get(context)
            if row is None:
                setting, _, value = context.partition("=")
                row = self.family_eps[best.family][context] = {
                    "born": (self.input_episodes.get(setting) or {}).get(value, 1.0) - 1.0, "seen": 0.0, "last": -1.0}
            if row["last"] != episode:
                row["seen"] += 1.0
                row["last"] = episode
        if context and best.born_in == context and best.last_ep != episode:
            best.ep_seen += 1.0
            best.last_ep = episode
        s.signature_id = best.id
        self._touch(best.id)
        if self._day_from is not None:
            self._day_log.append(s)
        for pair in ([s.pair] if s.pair else []) + list(s.legs):
            if pair and pair[0] is not None:
                row = self.start_home.setdefault(str(pair[0]), {})
                row[best.id] = row.get(best.id, 0.0) + 1.0
                self._vote(str(pair[0]), best.id)
                if self._device_home is not None:
                    pooled = self._device_home.setdefault(pair[0], {})
                    pooled[best.id] = pooled.get(best.id, 0.0) + 1.0
        self.filed_n += 1
        self.recent.append({"n": self.filed_n, "wh": round(s.energy_wh, 2),
                            "start": s.start, "end": s.end, "phases": s.phases, "kwh": round(s.energy_wh / 1000.0, 3),
                            "max_w": round(s.max_w), "levels": s.level_count, "signature": best.id})
        self.recent = self.recent[-MAX_RECENT_SESSIONS:]
        for sig in self._judge_born():
            self._touch(sig.id)
        if self.orphan_names:
            # a name lands on a signature as it stands after this filing
            for sig in self.signatures:
                self._reclaim(sig, noise)
        self._prune(s.end)

    def _judge_born(self, daily: bool = False) -> List["Signature"]:
        """A born-in signature that has seen its value come round
        INPUT_MIN_EPISODES times belongs to it if it ran in enough of them,
        and is ordinary again - running in that value now and then - if not.
        ``daily`` (Fleet._repair, once a day): one that ran in too few of them
        is not left waiting on a lift that cannot be measured yet. Returns the
        ones judged."""
        judged = []
        moved: Dict[int, int] = {}
        for sig in self.signatures:
            if not sig.born_in or sig.born_judged or sig.id in moved:
                continue
            setting, _, value = sig.born_in.partition("=")
            since = (self.input_episodes.get(setting) or {}).get(value, 0.0) - sig.born_ep
            if since < INPUT_MIN_EPISODES:
                continue
            # an alike ordinary twin that runs through the value at chance
            # (pair lift < INPUT_SPLIT_LIFT) takes the fragment back: a load
            # stepping all day runs in EVERY episode of every value, so
            # coverage alone judged Hiša's 60 W incubator born of Spin, Paused,
            # Dry and End Of Cycle (lifts 1.1-2.3; 20 signatures of one load
            # with the washer's states fed, 7 without, 2026-10-23). Twins are
            # found with the born-in gate of alike aside, since the lift is
            # decided here; while a twin's lift cannot be measured yet the
            # judgement waits
            noise = max((self.phases[p].noise for p in sig.phases if p in self.phases), default=MIN_NOISE_W)
            born, sig.born_in = sig.born_in, None
            twins = [o for o in self.signatures if not o.born_in and o.id not in moved and o.alike(sig, noise)]
            sig.born_in = born
            lifts = [(sig.input_lift(o, born), o) for o in twins]
            taken = [o for lift, o in lifts if lift is not None and lift < INPUT_SPLIT_LIFT]
            if taken:
                twin = max(taken, key=lambda o: o.count)
                twin.swallow(sig)
                moved[sig.id] = twin.id
                judged.append(twin)
                continue
            if any(lift is None for lift, _ in lifts):
                # a fragment's lift is None until it has INPUT_MIN_RUNS runs, and a named load is never pruned:
                # Home's library held 312 born-in signatures of two runs or fewer, never judged (2026-10-28). Once a
                # day one that ran in too few of its value's episodes - the coverage test below - goes back into
                # its twin (retake_fragments'), or is ordinary again
                if not daily or sig.ep_seen / since >= INPUT_MIN_COVERAGE:
                    continue
                twin = self._fragment_twin(sig, moved)
                if twin is not None:
                    twin.swallow(sig)
                    moved[sig.id] = twin.id
                    judged.append(twin)
                    continue
            if sig.ep_seen / since >= INPUT_MIN_COVERAGE:
                sig.born_judged = True
            else:
                sig.takes_in = sorted(set(sig.takes_in) | {sig.born_in})
                sig.born_in = None
            judged.append(sig)
        self._absorb(moved)
        return judged

    def retake_fragments(self) -> int:
        """Once, on a library stored before ``fragments_retaken`` existed: the signatures born in a setting's value
        that have run FRAGMENT_MAX_RUNS times or fewer and were never judged go into their alike ordinary twin, the
        one with the most runs and the same name (an unnamed fragment: any). ``_judge_born`` waits for ever on
        them - ``input_lift`` answers None below INPUT_MIN_RUNS - and a named load's signatures are never pruned:
        Home's Inkubator had 275 of them, 266 of one or two runs, from a washer phase carried through 19 hours of
        its integration being unavailable (2026-10-28; 453 -> 169 signatures, no run lost, on that library).
        Returns how many were taken back."""
        moved: Dict[int, int] = {}
        for frag in list(self.signatures):
            if not frag.born_in or frag.born_judged or frag.count > FRAGMENT_MAX_RUNS or frag.id in moved:
                continue
            twin = self._fragment_twin(frag, moved)
            if twin is not None:
                twin.swallow(frag)
                moved[frag.id] = twin.id
        self._absorb(moved)
        return len(moved)

    def _fragment_twin(self, frag: "Signature", moved: Dict[int, int]) -> Optional["Signature"]:
        """The alike ordinary twin a born-in fragment goes back into, the born gate of alike aside: at least
        YOUNG_COUNT runs, of its own name (an unnamed fragment: any), the one with the most runs - or None."""
        noise = max((self.phases[p].noise for p in frag.phases if p in self.phases), default=MIN_NOISE_W)
        born, frag.born_in = frag.born_in, None
        twins = [o for o in self.signatures if o is not frag and not o.born_in and o.id not in moved
                 and o.count >= YOUNG_COUNT and (not frag.name or o.name == frag.name) and o.alike(frag, noise)]
        frag.born_in = born
        return max(twins, key=lambda o: o.count, default=None)

    def _absorb(self, moved: Dict[int, int]) -> None:
        """Drop the signatures swallowed into others (other id -> keeper id)
        and repoint what referred to them."""
        if not moved:
            return
        for other_id, keep_id in moved.items():
            other, keep = self._sig(other_id), self._sig(keep_id)
            if other is not None and keep is not None and other.family and keep.family and other.family != keep.family:
                self._merge_family(other.family, keep.family)      # one signature of two families makes them one
        self.signatures = [x for x in self.signatures if x.id not in moved]
        for r in self.recent:
            if r.get("signature") in moved:
                r["signature"] = moved[r["signature"]]
        for s in self.held:
            if s.signature_id in moved:
                s.signature_id = moved[s.signature_id]
        self._moved.update(moved)
        for other_id, keep_id in moved.items():
            self._fold(other_id, keep_id)
        self._by_sig, self._device_home = None, None

    def classify(self, ph: str, since: float, watts: float, var: Optional[float], surge: float) -> Optional[int]:
        """Which cluster a fall belongs to, at once. A rise gets its cluster
        through note_rise, once the event window has shown which other
        phases rose with it - see EVENT_WINDOW_INTERVALS."""
        if watts > 0:
            return None
        self.placed_fall = None               # only THIS fall's placement reaches _declare (a share _split_rise classified does not)
        cluster = self._classify_step(ph, since, watts, var, surge)
        self.edge_at.setdefault(ph, []).append((since, cluster.id, watts))
        self.note_order(cluster.id, ph, since, abs(watts))
        return cluster.id

    def metered_parts(self, ph: str, since: float, step: float) -> List[float]:
        """``step`` cut into the shares the meters below it took with it and
        the rest, or ``[step]`` - see SPLIT_BY_METERS. A meter that took all
        of it, or more, leaves it whole: then the step is simply its load's.
        A share the other way is a change the grid netted into this step - a
        load stopping in the reading another started - and the rest is the
        larger for it."""
        return [part for _, part in self.metered_shares(ph, since, step)]

    def metered_shares(self, ph: str, since: float, step: float) -> List[Tuple[Optional[str], float]]:
        """metered_parts with the meter each share is - (meter, share), the
        rest (None, rest) last."""
        if self.meter_steps is None or ph not in self.phases:
            return [(None, step)]
        # Neither a piece nor what is left may be smaller than the step itself
        # can resolve - its noise, or the pairing's share of it. A server UPS
        # wandering 60 W inside a kiln pulse's window was carved off both of
        # its legs, and the two slivers made an A+C "load" of their own: 79
        # of them in ten days (Home, 2026-10-01).
        least = max(self.phases[ph].noise_at(), MATCH_EDGE_REL * abs(step))
        parts, rest = [], step
        # over the start-event window, no wider: fifteen seconds either side
        # reached into the kiln's previous pulse and read half a step, and
        # every pulse was booked as two
        for name, d in self.meter_steps(ph, since, self.event_window(), step > 0, step):
            if abs(d) < least or (d * step > 0 and abs(rest) - abs(d) < least):
                continue
            parts.append((name, d))
            rest -= d
        return parts + [(None, rest)] if parts else [(None, step)]

    def note_rise(self, state: "PhaseState", o: "_Open") -> None:
        """A rise just opened on ``state``'s phase: it waits for companions."""
        self._pending.append({"since": o.since, "ph": state.name, "watts": o.watts, "var": o.var,
                              "surge": o.surge, "open": o})

    def note_fall(self, state: "PhaseState", since: float, step: float, step_q: Optional[float], surge: float,
                  new_level: float, quality: float, hint: Optional["_Open"]) -> None:
        """A fall just declared on ``state``'s phase: it waits for companions as
        a rise does, and closes what it closes when its event forms (_form_fall)."""
        self._pending.append({"since": since, "ph": state.name, "watts": -step, "var": step_q, "surge": surge,
                              "fall": (step, new_level, quality), "hint": hint})

    def event_window(self) -> float:
        """How long rises on different phases may lie apart and be one event:
        EVENT_WINDOW_INTERVALS of the slowest phase's reading interval."""
        return EVENT_WINDOW_INTERVALS * max((st.interval or 0.0) for st in self.phases.values()) if self.phases else 0.0

    def event_wait(self) -> float:
        """How long after a rise its event may close: the window, plus the time
        a companion's step takes to be DECLARED after it began - its phase's
        sustain - or a leg whose reading falls at the window's edge is not
        pending yet."""
        return self.event_window() + max([SUSTAIN_SECONDS] + [st.latency() for st in self.phases.values()])

    def _form_first(self) -> None:
        """The oldest waiting rise, its event_wait past (see _advance), becomes
        an event with whatever rose beside it, gets its cluster, and ends an
        older open run of the same cluster on its phase. A pass's end used to
        form every waiting rise alone, so a leg whose companion's reading fell
        in the next pass was an event of its own."""
        pend = sorted(self._pending, key=lambda x: x["since"])
        self._order_tick(pend[0]["since"])           # the groups running until this event
        if "fall" in pend[0]:
            self._form_fall(pend[0], pend, self.event_window())
            return
        st = self.phases.get(pend[0]["ph"])
        if st is not None and pend[0]["since"] <= st.absorb_until:
            # the rest of a ramp a run already holds whole - see _form_event
            self._pending.remove(pend[0])
            if pend[0]["open"] in st.open_edges:
                st.open_edges.remove(pend[0]["open"])
            return
        cluster, members = self._form_event(pend[0], pend, self.event_window())
        for m in members:
            st = self.phases.get(m["ph"])
            if st is not None:
                self._closed(m["ph"], st.second_rise(cluster.id, m["open"]))
        self._closed(None, [])                    # and what forming it split off (_split_rise)

    def _form_event(self, first: dict, pend: List[dict], window: float):
        """``first`` and whatever rose beside it within ``window`` on other
        phases become one event and get its cluster; the members leave the
        pending list. Returns (cluster, members)."""
        members, phases = [first], {first["ph"]}
        for q in sorted(pend, key=lambda x: abs(x["since"] - first["since"])):
            if q is first or "fall" in q or q["ph"] in phases or abs(q["since"] - first["since"]) > window:
                continue
            ws = [m["watts"] for m in members] + [q["watts"]]
            if min(ws) < EVENT_BALANCE * max(ws):
                continue
            members.append(q)
            phases.add(q["ph"])
        for m in members:
            self._pending.remove(m)
        pattern = "".join(sorted(phases))
        if len(members) == 1:
            self._split_rise(members[0])
        since = min(m["since"] for m in members)
        vars_ = [m["var"] for m in members]
        self.placement_window = None
        cluster = self._classify_step(pattern, since, sum(m["watts"] for m in members),
                                      sum(vars_) if all(v is not None for v in vars_) else None,
                                      max(m["surge"] for m in members), window_ok=len(members) == 1,
                                      legs=[(m["ph"], m["since"], m["watts"]) for m in members])
        conf, self.placement_conf = self.placement_conf, None
        win, self.placement_window = self.placement_window, None
        step, self.placement_step = self.placement_step, None
        if win is None and step is not None and cluster.owner and len(members) == 1 and members[0]["ph"] in step.changes:
            # placed, no ramp window: the run is booked at the OWNER's size in the
            # grid's terms, not the grid's noisy reading of the same change
            m, o = members[0], members[0]["open"]
            w = abs(step.changes[m["ph"]])
            if w > 0:
                o.var = None if o.var is None else o.var * w / max(o.watts, 1e-9)
                o.watts, o.levels, o.now = w, [(o.since, w)], None
                m["watts"] = w
        if win is not None and cluster.owner and len(members) == 1:
            # placed by the grid's change over the meter's step and its own
            # (Fleet._window_size): the run is the ramp's whole size, and the
            # grid's later steps inside that window are the ramp still
            # climbing, not loads of their own (Home 09-24 05:41, Mansarda's
            # washer-dryer: +261 at the grid's first plateau, +313 on the
            # panel, +307 on the grid over both; 2026-10-03)
            m, st = members[0], self.phases.get(members[0]["ph"])
            o = m["open"]
            o.watts, o.levels, o.now = win[0], [(o.since, win[0])], None
            m["watts"] = win[0]
            if st is not None:
                inside = [x for x in st.open_edges if x is not o and o.since < x.since <= win[2] and not x.meter]
                st.open_edges = [x for x in st.open_edges if x not in inside]
                self._pending = [q for q in self._pending if q.get("open") not in inside]
                st.absorb_until = max(st.absorb_until, win[2])
        for m in members:
            m["open"].cluster = cluster.id
            if cluster.owner and conf is not None:
                m["open"].q *= 0.5 + 0.5 * conf     # how well the meters' timings agreed
                # placed by the meter's own step: its run - see PhaseState.owned
                # (unless a meter's rise already took it, _split_rise)
                m["open"].meter = m["open"].meter or cluster.owner
            elif len(members) > 1 and self.meter_started is not None and self.phases.get(m["ph"]) is not None:
                # a leg of an event on several phases: a meter's
                # rise of all of it at this moment makes the leg its own, as
                # _split_rise does for a start alone - Susilna's plug switching
                # on in the same reading as a rise on phase C (09-21, 09-24)
                found = self.meter_started(m["ph"], m["since"], m["watts"])
                if found is not None:
                    st = self.phases[m["ph"]]
                    if m["watts"] - found[1] < max(st.noise_at(), MATCH_EDGE_REL * m["watts"]):
                        m["open"].meter = found[0]
            self.edge_at.setdefault(m["ph"], []).append((m["since"], cluster.id, m["watts"]))
        for m in members if self.meter_read is not None else []:
            st = self.phases.get(m["ph"])
            if not m["open"].meter and st is not None and m["open"] in st.open_edges and \
                    self.meter_read(m["ph"], m["since"], m["watts"], True, st.last_ts):
                # a rise its one-device meter's readings show, declared or
                # not, while a run it owns there is on: that run's, not a run
                # beside it - Home's EVBox dipped for one 10 s reading 22
                # times in a charge, its own detector declaring nothing, and
                # every return opened a 2.15 kW run of its own, ~14 kWh booked
                # beside the charge (09-25, 2026-10-04) - see Fleet._meter_read
                st.open_edges.remove(m["open"])
        return cluster, members

    def running_groups(self, before: float = math.inf, phases: Optional[str] = None) -> Dict[int, Tuple[float, float]]:
        """Rise group -> (the start of its newest unit, that unit's size over
        the phases asked), over the runs open before ``before`` on any of
        ``phases`` (all of them for None)."""
        out: Dict[int, Tuple[float, float]] = {}
        for ph, st in self.phases.items():
            if phases is not None and ph not in phases:
                continue
            for o in st.open_edges:
                if o.cluster is None or o.since >= before:
                    continue
                was = out.get(o.cluster)
                if was is None or o.since > was[0]:
                    out[o.cluster] = (o.since, o.watts)
                elif o.since == was[0]:
                    out[o.cluster] = (o.since, was[1] + o.watts)      # another leg of the same start
        return out

    def _order_tick(self, t: float) -> None:
        """Book the seconds since the last tick to every group running, and
        fade the counts hourly as the histograms fade (EDGE_TAU_S)."""
        if self.order_clock is None or t <= self.order_clock:
            self.order_clock = t if self.order_clock is None else self.order_clock
            return
        dt = t - self.order_clock
        for cid in self.running_groups():
            self.run_time[cid] = self.run_time.get(cid, 0.0) + dt
        self.order_span += dt
        self.order_clock = t
        if self.order_faded is None:
            self.order_faded = t
        elif t - self.order_faded > 3600.0:
            fade = math.exp(-(t - self.order_faded) / EDGE_TAU_S)
            for acc in self.order.values():
                for i in range(len(acc)):
                    acc[i] *= fade
            for d in (self.run_time, self.fall_n):
                for k in list(d):
                    d[k] *= fade
            self.order_span *= fade
            self.order_faded = t

    def note_order(self, fall: int, phases: str, at: float, watts: float = 0.0) -> None:
        """A fall of group ``fall`` (``watts`` in all) at ``at`` on ``phases``:
        counted against every rise group running there before it, with the
        seconds since its newest start and its size against that start's."""
        self._order_tick(at)
        self.fall_n[fall] = self.fall_n.get(fall, 0.0) + 1.0
        for cid, (since, rose) in self.running_groups(at, phases).items():
            acc = self.order.setdefault(f"{cid}>{fall}", [0.0, 0.0, 0.0, 0.0, 0.0])
            ld = math.log(max(at - since, 1.0))
            lr = math.log(watts / rose) if watts > 0 and rose > 0 else 0.0
            acc[0] += 1.0
            acc[1] += ld
            acc[2] += ld * ld
            acc[3] += lr
            acc[4] += lr * lr

    def partner_evidence(self, rise: int, fall: int) -> float:
        """How far above chance falls of ``fall`` came while ``rise`` ran:
        log(seen / expected), 0.0 below PAIR_MIN_RUNS or within chance."""
        acc = self.order.get(f"{rise}>{fall}")
        if not acc or acc[0] < PAIR_MIN_RUNS or self.order_span <= 0:
            return 0.0
        expected = self.fall_n.get(fall, 0.0) * self.run_time.get(rise, 0.0) / self.order_span
        if expected <= 0 or not above_chance(acc[0], expected):
            return 0.0
        return math.log(acc[0] / expected)

    def pair_length(self, rise: int, fall: int) -> Optional[Tuple[float, float]]:
        """(mean, spread) of log seconds from a ``rise`` start to a ``fall``
        while it ran, or None while the pair has no evidence."""
        if self.partner_evidence(rise, fall) <= 0:
            return None
        n, s, s2 = self.order[f"{rise}>{fall}"][:3]
        mean = s / n
        return mean, math.sqrt(max(0.0, s2 / n - mean * mean))

    def pair_ratio(self, rise: int, fall: int) -> Optional[Tuple[float, float]]:
        """(mean, spread) of log(fall / rise) for a pair with evidence, or None."""
        if self.partner_evidence(rise, fall) <= 0:
            return None
        acc = self.order[f"{rise}>{fall}"]
        n, s, s2 = acc[0], acc[3], acc[4]
        mean = s / n
        return mean, math.sqrt(max(0.0, s2 / n - mean * mean))

    def partners_of(self, rise: int) -> List[int]:
        """The fall groups with evidence as ``rise``'s partner."""
        head = f"{rise}>"
        return [int(k[len(head):]) for k in self.order if k.startswith(head)
                and self.partner_evidence(rise, int(k[len(head):])) > 0]

    def usual_length(self, start: int) -> Optional[float]:
        """Seconds a run that starts with this group usually lasts: over its
        partners with evidence, weighted by their falls - None when it has none."""
        rows = [(self.pair_length(start, f), self.order[f"{start}>{f}"][0]) for f in self.partners_of(start)]
        w = sum(n for _, n in rows)
        return math.exp(sum(m[0] * n for m, n in rows) / w) if w else None

    def drop_pending(self, o: "_Open") -> None:
        """A rise that turned out to be a dip's return (PhaseState._dip): its event never forms."""
        self._pending = [m for m in self._pending if m.get("open") is not o]

    def pending_fall(self, ph: str, after: float) -> float:
        """Watts of the falls on ``ph`` since ``after`` still waiting for their event."""
        return sum(m["watts"] for m in self._pending if "fall" in m and m["ph"] == ph and m["since"] > after)

    def _form_fall(self, first: dict, pend: List[dict], window: float) -> None:
        """``first`` and the falls beside it within ``window`` on other phases,
        balanced as a rise's legs are (EVENT_BALANCE), are one fall: on one
        phase it is split and classified there as before (PhaseState.fall_now);
        on several it is one step group on the phase pattern, and every leg
        closes with it."""
        members, phases = [first], {first["ph"]}
        for q in sorted(pend, key=lambda x: abs(x["since"] - first["since"])):
            if q is first or "fall" not in q or q["ph"] in phases or abs(q["since"] - first["since"]) > window:
                continue
            ws = [m["watts"] for m in members] + [q["watts"]]
            if min(ws) < EVENT_BALANCE * max(ws):
                continue
            members.append(q)
            phases.add(q["ph"])
        for m in members:
            self._pending.remove(m)
        cid = None
        if len(members) > 1:
            qs = [m["var"] for m in members]
            self.placement_window = None
            cid = self._classify_step("".join(sorted(phases)), min(m["since"] for m in members),
                                      -sum(m["watts"] for m in members),
                                      sum(qs) if all(x is not None for x in qs) else None,
                                      max(m["surge"] for m in members)).id
            self.placed_fall = None
            for m in members:
                self.edge_at.setdefault(m["ph"], []).append((m["since"], cid, -m["watts"]))
            self.note_order(cid, "".join(sorted(phases)), min(m["since"] for m in members),
                            sum(m["watts"] for m in members))
        for m in members:
            st = self.phases.get(m["ph"])
            if st is None:
                continue
            step, new_level, quality = m["fall"]
            self._closed(m["ph"], st.fall_now(m["since"], step, m["var"], m["surge"], new_level, quality,
                                              cid, m["hint"]))
        self._closed(None, [])                    # and what a split rise ended meanwhile

    def _split_rise(self, m: dict) -> None:
        """A single-phase rise cut into the shares the meters below took with
        it - see SPLIT_BY_METERS. The first shares become rises of their own,
        each its meter's (PhaseState.owned); the last stays this one. A meter
        the map does not place is asked too (meter_started): a rise of its
        own at this moment that is all of this start makes the start its, one
        that is part of it takes that part and leaves the rest a start of its
        own - Home's Susilna plug (265 W) switching on with a 48 W fan, a +313
        W start the plug neither owned nor matched, cut minutes later with the
        fan's stop (09-21, 09-24; the owner, 2026-10-02: a separate fan in all
        likelihood). A rise on several phases at once is one load and is never
        cut phase by phase."""
        st = self.phases.get(m["ph"])
        if st is None:
            return
        o, whole = m["open"], m["watts"]
        shares = self.metered_shares(m["ph"], m["since"], whole)
        if len(shares) < 2 and self.meter_started is not None:
            found = self.meter_started(m["ph"], m["since"], whole)
            if found is not None:
                name, rise = found
                least = max(st.noise_at(), MATCH_EDGE_REL * whole)
                if rise < least:
                    pass                             # a wobble of the meter's, not a part of this start
                elif whole - rise < least:
                    o.meter = name                   # all of it, as far as the grid can tell
                else:
                    shares = [(name, rise), (None, whole - rise)]
        if len(shares) < 2:
            return
        for name, part in shares[:-1]:
            if part < 0:                       # a stop netted into this rise: it ends its run now
                self._split_closed.extend((m["ph"], x) for x in st._declare(m["since"], part, None, 0.0, st.level, o.q))
                continue
            extra = _Open(m["since"], part, None if m["var"] is None else m["var"] * part / whole,
                          [(m["since"], part)], q=o.q, meter=name)
            extra.cluster = self._classify_step(m["ph"], m["since"], part, extra.var, 0.0).id
            self.placement_conf = None          # its placement is its own, not the rest's
            self.edge_at.setdefault(m["ph"], []).append((m["since"], extra.cluster, part))
            st.open_edges.append(extra)
        name, rest = shares[-1]
        o.watts, o.levels, o.meter = rest, [(o.since, rest)], name
        # what the whole was followed to while it waited for its event is not
        # the rest's: kept, a 48 W rest carried the 313 W whole's level and a
        # 152 W fall took it in a multi-close (the fan-and-plug test)
        o.now, o.lo, o.hi = None, None, None
        o.var = None if o.var is None else o.var * rest / whole
        m["watts"], m["var"] = rest, o.var

    def resolve_rise(self, o: "_Open") -> None:
        """A run is closing before its event window passed: form its event now
        from what has risen beside it so far, so the run has its start
        cluster (a start without one is a device of nobody and a signature
        of its own - hundreds of them from short runs, 2026-09-30).

        Forming it can cut a single-phase rise into the meters' shares, and a
        share that is a stop is declared at once (_split_rise) - pairing on
        that phase while its caller is pairing or flushing there. So callers
        take every run they close out of open_edges before closing any, and
        _form_first re-reads what is pending after each event it forms."""
        first = next((x for x in self._pending if x.get("open") is o), None)
        if first is not None:
            self._form_event(first, list(self._pending), self.event_window())

    def _classify_step(self, ph: str, since: float, watts: float, var: Optional[float], surge: float,
                       window_ok: bool = False, legs: Optional[List[tuple]] = None) -> "StepGroup":
        """File a step - or an all-phase event, ``ph`` then being its phase
        pattern, ``watts`` its total and ``legs`` its (phase, since, watts)
        on each - as an edge (see EDGE_LAG_REACH_S) and return its cluster."""
        size = abs(watts)
        pf = size / math.hypot(size, var) if var is not None and size > 0 else None
        events, times, numbers = self.signals or ({}, {}, {})
        learned = [n for n in events if self.window(n)]     # as of this step, not of the pass's first
        bins = int(round(2 * EDGE_LAG_REACH_S / EDGE_LAG_BIN_S))
        kinds, lags, values = {}, {}, {}
        for name, evs in events.items():
            ts = times[name]
            i = bisect.bisect_left(ts, since - EDGE_LAG_REACH_S)
            near = [j for j in range(i, min(i + 8, len(ts))) if abs(ts[j] - since) <= EDGE_LAG_REACH_S
                    and (self.horizon is None or ts[j] <= self.horizon)]
            kinds[name] = ""
            if not near:
                continue
            j = min(near, key=lambda k: abs(ts[k] - since))
            lag = ts[j] - since
            hist = self.lag_hist.setdefault(name, [0.0] * bins)
            hist[min(bins - 1, int((lag + EDGE_LAG_REACH_S) / EDGE_LAG_BIN_S))] += 1.0
            if self._windows is not None:
                self._windows.pop(name, None)          # the histogram just changed
            lo, hi = self.window(name) or (-EDGE_WINDOW_DEFAULT_S, EDGE_WINDOW_DEFAULT_S)
            if lo <= lag <= hi:
                kinds[name] = evs[j][1]
                lags[name] = (evs[j][1], lag)
        for name, rows in numbers.items():
            i = bisect.bisect_right(rows, (since, math.inf)) - 1
            if i >= 0:
                values[name] = rows[i][1]
        keyed = {n: kinds.get(n, "") for n in learned}
        for name, rows in self.inputs.items():
            # ...and each input's state at the step, once a day of it is counted - see STATE_SUFFIX
            total = sum((self.input_time.get(name) or {}).values())
            i = bisect.bisect_right(rows, (since, "\uffff")) - 1
            if i >= 0 and total >= INPUT_SPLIT_MIN_TIME_S and rows[i][1] != INPUT_DOWN:
                keyed[name + STATE_SUFFIX] = rows[i][1]
        where = ""
        if watts > 0 and self.step_meter is not None:
            if len(ph) == 1:
                where = self.step_meter(ph, since, size, True) or ""
            elif legs and len(legs) == len(ph):
                # an event on several phases where every leg was one meter's
                # own step - the kiln's two legs each a channel of Hiša's - is
                # that meter's, as a start on one phase is (the unify audit's
                # F1, 2026-10-03); its timing as sure as its least sure leg
                got, confs = set(), []
                for lph, lsince, lwatts in legs:
                    self.placement_conf = None
                    got.add(self.step_meter(lph, lsince, abs(lwatts), True) or "")
                    confs.append(self.placement_conf)
                where = got.pop() if len(got) == 1 else ""
                self.placement_conf = min(confs) if where and None not in confs else None
                self.placement_window = None
        elif watts < 0 and self.step_meter is not None and len(ph) == 1:
            # a fall is placed at the meter that saw it, as a rise is (steps-and-claims,
            # stage 2) - and teaches the gains as one does; a fall's window never
            # resizes it (the ramp rule is a rise's, _window_size). Its stop group is
            # booked at the owner's size; the pairing still reads the grid's fall
            # (_close): the run's close is the grid's business - a judgement
            conf, win, prev = self.placement_conf, self.placement_window, self.placement_step
            where = self.step_meter(ph, since, size, False) or ""
            step = self.placement_step if where else None
            self.placement_conf, self.placement_window, self.placement_step = conf, win, prev
            if step is not None and ph in step.changes:
                size, since = abs(step.changes[ph]), step.since
                self.placed_fall = (where, size)
        if where and window_ok and self.placement_window is not None:
            size = abs(self.placement_window[0])     # placed by the ramp's whole window: its size
        elif where and watts > 0 and self.placement_step is not None and ph in self.placement_step.changes:
            # the owner's reading, in the grid's terms, and its moment: the
            # cleanest meter's figures are the step's (steps-and-claims, stage 2)
            size, since = abs(self.placement_step.changes[ph]), self.placement_step.since
        if self._kinds is None:
            self._kinds = {}
            for c in self.edges:
                self._kinds.setdefault((c.phase, c.up), []).append(c)
        kind = self._kinds.setdefault((ph, watts > 0), [])
        if len(ph) == 1 and watts > 0 and not where:
            where = self._likely_meter(ph, watts > 0, size, since)
        angle = math.degrees(math.atan2(var, size)) if var is not None and size > 0 else None
        cluster, keys, where = self._by_density(ph, watts > 0, since, size, keyed, kind, angle, where)
        if cluster is None:
            cluster = StepGroup(id=self.next_edge_id, phase=ph, up=watts > 0, watts=size, keys=keys, owner=where)
            self.next_edge_id += 1
            self.edges.append(cluster)
            kind.append(cluster)
            if len(kind) > EDGE_LIBRARY:
                # the cap kept as each cluster is born, the weakest going
                # first - at a pass's end, the library a six-hour slice kept
                # was not the one a minute's pass kept
                gone = min((c for c in kind if c is not cluster), key=lambda c: (c.count, c.last_seen))
                kind.remove(gone)
                self.edges = [c for c in self.edges if c is not gone]
                self._by_id = None
        cluster.absorb(since, size, pf, surge, kinds, lags, values, angle)
        return cluster

    def _by_density(self, ph: str, up: bool, since: float, size: float, keyed: Dict[str, str],
                    kind: List["StepGroup"], angle: Optional[float] = None, where: str = ""):
        """(the cluster whose segment of this phase and direction's size density
        the step falls in, or None for a new one; the keys a new one gets).

        What the inputs did is part of the clustering, not a split made before
        it (2026-09-30): a step that came with an input's change joins a
        cluster of its own only where such steps pile up in the segment far
        above chance - the floor mat's +630 W with its thermostat, beside a
        look-alike's +630 W with nothing - and otherwise the segment's plain
        one. See EDGE_BATCH."""
        g = _group(ph, up, where)
        # a state at the step (STATE_SUFFIX) is judged on its own, one input at a
        # time: the five states joined into one key never piled up above chance
        # (43 rises of 1.9 kW in Mansarda's plain group, 2026-10-23), while
        # "the washer is Running" alone does; the change keys stay joined
        states = {n: k for n, k in keyed.items() if k and n.endswith(STATE_SUFFIX)}
        changes = {n: k for n, k in keyed.items() if k and not n.endswith(STATE_SUFFIX)}
        key = ",".join(f"{n}={k}" for n, k in sorted(changes.items()))
        keyed = changes
        noise = sum((self.phases[p].noise or MIN_NOISE_W) if p in self.phases else MIN_NOISE_W for p in ph)   # a pattern: its phases' noise together
        if g not in self.edge_unit:
            self.edge_unit[g] = EDGE_NOISE_SHARE * noise
        elif EDGE_NOISE_SHARE * noise < self.edge_unit[g]:
            self._rescale(g, EDGE_NOISE_SHARE * noise)
        unit = self.edge_unit[g]
        b = _bin(size, unit)
        h = self.edge_hist.setdefault(g, {})
        hk = self.edge_hist_keys.setdefault(g, {})
        ha = self.edge_hist_angle.setdefault(g, {})
        at = self.edge_hist_at.get(g)
        if at is None:
            self.edge_hist_at[g] = since
        elif since - at > 3600.0:
            fade = math.exp(-(since - at) / EDGE_TAU_S)
            for hist in [h] + list(hk.values()):
                for k in list(hist):
                    hist[k] *= fade
                    if hist[k] < 1e-3:
                        del hist[k]
            for sb in list(ha):
                row = ha[sb]
                for ab in list(row):
                    row[ab] *= fade
                    if row[ab] < 1e-3:
                        del row[ab]
                if not row:
                    del ha[sb]
            self._acol.pop(g, None)                    # every weight moved: worked out afresh
            self.edge_hist_at[g] = since
            self._dens.pop(g, None)                    # every weight moved: worked out afresh
        added = b not in h
        h[b] = h.get(b, 0.0) + 1.0
        if key:
            row = hk.setdefault(key, {})
            row[b] = row.get(b, 0.0) + 1.0
        if angle is not None:
            ab = int(math.floor(angle / EDGE_ANGLE_BIN))
            row = ha.setdefault(b, {})
            row[ab] = row.get(ab, 0.0) + 1.0
            for (lo, hi), cols in self._acol.get(g, {}).items():
                if lo <= b <= hi:
                    _exact_add(cols.setdefault(ab, []), 1.0)
        segs = self._segs[g] = self._valleys(g, h, b, added)      # with this step in it - see EDGE_RECUT
        plain = {n: "" for n in keyed}
        seg = next(((lo, hi) for lo, hi in segs if lo <= b <= hi), None)
        if states:
            got = self._by_state(h, hk, unit, b, seg or (b, b), states, changes, kind, angle, where)
            if got is not None:
                return got
        if seg is None:
            return None, (keyed if key and self._keyed_above_chance(hk.get(key, {}), h, b, b, keyed) else plain), where
        members = [c for c in kind if c.owner == where
                   and seg[0] <= _bin(c.watts, unit) <= seg[1]]
        if angle is not None:
            members = self._same_angle(g, ha, seg, angle, members)
        if key and self._keyed_above_chance(hk.get(key, {}), h, seg[0], seg[1], keyed):
            mine = [c for c in members if c.same_signals(keyed)]
            return (max(mine, key=lambda c: c.count) if mine else None), keyed, where
        mine = [c for c in members if c.same_signals(plain)]
        return (max(mine, key=lambda c: c.count) if mine else None), plain, where

    def _by_state(self, h: Dict[int, float], hk: Dict[str, Dict[int, float]], unit: float, b: int,
                  seg: Tuple[int, int], states: Dict[str, str], changes: Dict[str, str],
                  kind: List["StepGroup"], angle: Optional[float], where: str):
        """_by_density's answer where one input's STATE at the step piles up in
        the size segment above chance - the input furthest above it decides,
        and the cluster is keyed by that one state (with the change keys);
        None where none does, so the step is filed by the change keys alone.
        Called with the step already in the group's histogram ``h`` and the
        segments cut; the state histograms are kept per input in ``hk``."""
        for n, k in states.items():
            row = hk.setdefault(f"{n}={k}", {})
            row[b] = row.get(b, 0.0) + 1.0
        best = None
        for n, k in states.items():
            row = hk.get(f"{n}={k}") or {}
            if not self._keyed_above_chance(row, h, seg[0], seg[1], {n: k}):
                continue
            got = sum(w for x, w in row.items() if seg[0] <= x <= seg[1])
            if best is None or got > best[0]:
                best = (got, n, k)
        if best is None:
            return None
        keyed = dict(changes)
        keyed[best[1]] = best[2]
        members = [c for c in kind if c.owner == where and seg[0] <= _bin(c.watts, unit) <= seg[1]]
        if angle is not None:
            g = _group(members[0].phase, members[0].up, where) if members else None
            ha = self.edge_hist_angle.get(g) if g else None
            if ha:
                members = self._same_angle(g, ha, seg, angle, members)
        mine = [c for c in members if c.same_signals(keyed) and c.keys.get(best[1]) == best[2]]
        return (max(mine, key=lambda c: c.count) if mine else None), keyed, where

    def _valleys(self, g: str, h: Dict[int, float], b: int, added: bool) -> List[Tuple[int, int]]:
        """valley_segments(h) just after a step was added to bin ``b`` (a new
        bin if ``added``), the density kept between steps: only its slots
        within the kernel's reach of ``b`` can have moved, and each is summed
        again over its bins in the histogram's own order - the very sums a
        full pass makes, so the segments are the same to the bit. A fade, a
        re-binned or replaced histogram is worked out afresh. A full pass over
        every bin on each of 127,000 steps was 2 1/2 min of a Home replay."""
        reach, kernel = _kernel(EDGE_KERNEL / EDGE_BIN)
        st = self._dens.get(g)
        if st is None or st[0] is not h:
            lo, hi = min(h) - reach, max(h) + reach
            dens = [0.0] * (hi - lo + 1)
            for k, w in h.items():
                base = k - lo - reach
                for j, kw in enumerate(kernel):
                    dens[base + j] += w * kw
            st = self._dens[g] = [h, {k: i for i, k in enumerate(h)}, lo, dens, None]
        else:
            _, order, lo, dens, _ = st
            if added:
                order[b] = len(order)                 # a new key comes last in the dict
            if b - reach < lo:
                dens[:0] = [0.0] * (lo - (b - reach))
                lo = st[2] = b - reach
            if b + reach > lo + len(dens) - 1:
                dens.extend([0.0] * (b + reach - (lo + len(dens) - 1)))
            near = sorted((k for k in range(b - 2 * reach, b + 2 * reach + 1) if k in h), key=order.__getitem__)
            for i in range(b - reach, b + reach + 1):
                v = 0.0
                for k in near:
                    j = i - k + reach
                    if 0 <= j < len(kernel):
                        v += h[k] * kernel[j]
                dens[i - lo] = v
        if st[4] is None:
            st[4] = _valley_cuts(st[3], st[2], kernel[reach])
        else:
            st[4] = _valley_cuts_near(st[3], st[2], kernel[reach], st[4], b - reach, b + reach)
        return _significant(st[3], st[2], kernel[reach], st[4], self.edge_unit.get(g) or MIN_NOISE_W)

    def _rescale(self, g: str, unit: float) -> None:
        """Group ``g``'s size histograms re-binned onto a smaller ``unit``, each
        old bin's weight spread evenly over the new bins it covers - see
        EDGE_NOISE_SHARE. Its segments need no cut here: they are cut at every
        step, the step in them (EDGE_RECUT)."""
        old, self.edge_unit[g] = self.edge_unit[g], unit

        def remap(hist: dict, angled: bool = False) -> dict:
            out: dict = {}
            for b, w in (hist.items() if not angled else ((sb, row) for sb, row in hist.items())):
                lo, hi = (edge_scale(math.sinh(x * EDGE_BIN * EDGE_SCALE_REL) * old / EDGE_SCALE_REL, unit) / EDGE_BIN
                          for x in (b, b + 1))
                for j in range(math.floor(lo), math.ceil(hi)):
                    share = (min(hi, j + 1) - max(lo, j)) / (hi - lo)
                    if angled:
                        row = out.setdefault(j, {})
                        for ab, wa in w.items():
                            row[ab] = row.get(ab, 0.0) + wa * share
                    else:
                        out[j] = out.get(j, 0.0) + w * share
            return out
        if g in self.edge_hist:
            self.edge_hist[g] = remap(self.edge_hist[g])
        if g in self.edge_hist_keys:
            self.edge_hist_keys[g] = {k: remap(row) for k, row in self.edge_hist_keys[g].items()}
        if g in self.edge_hist_angle:
            self.edge_hist_angle[g] = remap(self.edge_hist_angle[g], True)
        self._acol.pop(g, None)

    def _same_angle(self, g: str, ha: Dict[int, Dict[int, float]], seg: Tuple[int, int], angle: float,
                    members: List["StepGroup"]) -> List["StepGroup"]:
        """The size segment's clusters whose reactive angle lies in the step's
        own band of the segment's angle density - see EDGE_ANGLE_BIN.

        Every step changes the histogram before this reads it, so a cached
        answer never lasts - but a step only ADDS to it. The segment's angle
        density is kept per group and segment, each step in the segment added
        to it (_by_density), and worked out afresh from the size bins inside the
        segment only when there is none: after a fade, a rescale, a restart or
        when the segment's edges moved. The sums are exact (partial sums), so
        the density is the same whichever way it was reached."""
        cache = self._acol.setdefault(g, {})
        cols = cache.get(seg)
        if cols is None:
            if len(cache) >= 16:
                del cache[next(iter(cache))]              # the oldest segment
            cols = cache[seg] = {}
            if len(ha) <= seg[1] - seg[0] + 1:
                rows = (row for sb, row in ha.items() if seg[0] <= sb <= seg[1])
            else:
                rows = (row for sb in range(seg[0], seg[1] + 1) if (row := ha.get(sb)))
            for row in rows:
                for ab, w in row.items():
                    _exact_add(cols.setdefault(ab, []), w)
        col = {ab: math.fsum(parts) for ab, parts in cols.items()}
        ab = int(math.floor(angle / EDGE_ANGLE_BIN))
        band = next(((lo, hi) for lo, hi in valley_segments(col, EDGE_ANGLE_KERNEL / EDGE_ANGLE_BIN)
                     if lo <= ab <= hi), (ab, ab))
        # a cluster from before the angle was measured has none yet: it still
        # matches, and takes its angle from the steps it now gets - so a saved
        # library carries over instead of every load starting again
        return [c for c in members if c.angle is None
                or band[0] <= int(math.floor(c.angle / EDGE_ANGLE_BIN)) <= band[1]]

    def _likely_meter(self, ph: str, up: bool, size: float, since: float) -> str:
        """Where a step no meter placed most likely happened: the meter whose
        cluster at this size has seen more steps than the plain one there, or
        nowhere. A meter misses a step now and then - a late report, a reading
        at the window's edge - and each miss founded a plain cluster beside the
        located one: Home went from 241 signatures to 279 (2026-10-01). A load
        no meter watches stays plain wherever the plain cluster is the bigger.
        Never a meter that HELD its value through the step - nothing written
        but its old level, the recorder keeping only changes: an unmetered
        2.2 kW load on Home's phase B joined the workshop boiler's cluster 69
        times, 6.2 kWh, while the boiler's meter read 0 W for hours."""
        def busiest(where: str) -> float:
            g = _group(ph, up, where)
            unit, segs = self.edge_unit.get(g), self._segs.get(g)
            if not unit or not segs:
                return 0.0
            b = _bin(size, unit)
            seg = next(((lo, hi) for lo, hi in segs if lo <= b <= hi), None)
            if seg is None:
                return 0.0
            return max((c.count for c in self._kinds.get((ph, up), []) if c.owner == where
                        and seg[0] <= _bin(c.watts, unit) <= seg[1]), default=0.0)
        plain = busiest("")
        held = (lambda w: since is not None and self.meter_held is not None and self.meter_held(w, ph, since, up))
        best = max(((busiest(w), w) for w in {c.owner for c in self._kinds.get((ph, up), [])} if w and not held(w)),
                   default=(0.0, ""))
        return best[1] if best[0] > plain else ""

    def _keyed_above_chance(self, hk: Dict[int, float], h: Dict[int, float], lo: int, hi: int,
                            keyed: Dict[str, str]) -> bool:
        """Do the steps that came with these inputs' changes pile up in bins
        lo..hi far above the chance of a step landing near such a change -
        the share of all steps with one within EDGE_LAG_REACH_S, narrowed to
        the input's learned window? See PAIR_MIN_RUNS for the test."""
        n = sum(w for b, w in hk.items() if lo <= b <= hi)
        if n < PAIR_MIN_RUNS:
            return False
        seen = sum(c.count for c in self.edges) or 1.0
        p = 1.0
        for name, kind in keyed.items():
            if not kind:
                continue
            if name.endswith(STATE_SUFFIX):
                spent = self.input_time.get(name[:-len(STATE_SUFFIX)]) or {}
                p *= min(1.0, spent.get(kind, 0.0) / (sum(spent.values()) or 1.0))
                continue
            window = self.window(name)
            near = sum(self.lag_hist.get(name) or [])
            if window is None or not near:
                return False
            p *= min(1.0, near / seen) * min(1.0, (window[1] - window[0]) / (2 * EDGE_LAG_REACH_S))
        expected = p * sum(w for b, w in h.items() if lo <= b <= hi)
        return above_chance(n, expected)

    # ------------------------------------------------ families
    def family_of(self, sig: "Signature") -> List["Signature"]:
        """Every signature of ``sig``'s family (itself, when it has none yet)."""
        return [x for x in self.signatures if x.family == sig.family] if sig.family else [sig]

    def _new_family(self) -> int:
        self.next_family += 1
        return self.next_family - 1

    def _assign_families(self) -> None:
        """Give every signature a family: one of its own, joined with those of its start cluster that are
        otherwise alike (_join_families). What a library stored before families gets on load."""
        fresh = [x for x in self.signatures if not x.family]
        for x in fresh:
            x.family = self._new_family()
        if self.signatures:
            self._join_families()          # also for a library that has families: the neighbour join is new

    def _join_families(self) -> None:
        """One family per start cluster and phase set: the signatures a setting's values split one load into.
        And the several signatures one name sits on, when they are otherwise alike."""
        if self._votes is None:
            self._rebuild_votes()
        by: Dict[Tuple[int, str], List["Signature"]] = {}
        for sig in self.signatures:
            home = self._home.get(sig.id)
            if home is not None:
                by.setdefault((home, sig.phases), []).append(sig)
        for sigs in by.values():
            sigs.sort(key=lambda x: (not x.name, -x.count))
            for other in sigs[1:]:
                self._join(sigs[0], other)
        named: Dict[Tuple[str, str], List["Signature"]] = {}
        for sig in self.signatures:
            if sig.name:
                named.setdefault((sig.name, sig.phases), []).append(sig)
        for sigs in named.values():
            sigs.sort(key=lambda x: -x.count)
            noise = max((self.phases[p].noise for p in sigs[0].phases if p in self.phases), default=MIN_NOISE_W)
            for other in sigs[1:]:
                if sigs[0].alike(other, noise, inputs=False):
                    self._join(sigs[0], other)
        self._join_neighbours()

    def _join_neighbours(self) -> None:
        """One family from the signatures of start clusters that neighbour each other (FAMILY_REACH_MADS) on the
        same phase set, direction and meter, when the signatures are alike: a start size that drifts is cut into
        clusters side by side, and each cluster's runs make signatures of their own (Home's kiln, 2026-10-26:
        seven two-phase clusters from 5.76 to 6.05 kW, seven families, one of them named). Nearest pairs first;
        _join still refuses a pair kept apart on purpose, split by length or named differently."""
        by: Dict[Tuple[str, str], List[Tuple[float, float, "Signature"]]] = {}
        for sig in self.signatures:
            home = self._home.get(sig.id)
            c = self.cluster(home) if home is not None else None
            if c is not None and c.up and c.watts >= FAMILY_REACH_NOISES * sum(
                    (self.phases[p].noise or MIN_NOISE_W) if p in self.phases else MIN_NOISE_W for p in c.phase):
                by.setdefault((c.phase, c.owner), []).append((c.watts, c.watts_mad, sig))
        for rows in by.values():
            pairs = sorted(((abs(w1 - w2), a, b) for i, (w1, m1, a) in enumerate(rows) for w2, m2, b in rows[i + 1:]
                            if a.family != b.family and abs(w1 - w2) <= FAMILY_REACH_MADS * max(m1, m2)),
                           key=lambda x: x[0])
            for _, a, b in pairs:
                noise = max((self.phases[p].noise for p in a.phases if p in self.phases), default=MIN_NOISE_W)
                if a.family != b.family and a.alike(b, noise, inputs=False):
                    self._join(a, b)

    def _join(self, keep: "Signature", other: "Signature") -> bool:
        """Put ``other``'s family into ``keep``'s, unless the two were kept apart on purpose: named apart, a run
        kept out of the other, or split by length. The name goes to every member."""
        if keep.family == other.family:
            return False
        fa, fb = self.family_of(keep), self.family_of(other)
        if len({m.name for m in fa + fb if m.name}) > 1:
            return False
        for a in fa:
            for b in fb:
                if (a.id in {self._current(x) for x in b.apart} or b.id in {self._current(x) for x in a.apart}
                        or not a.takes_len(b.duration_s) or not b.takes_len(a.duration_s)):
                    return False
        self._merge_family(other.family, keep.family)
        return True

    def _merge_family(self, gone: int, keep: int) -> None:
        """Every member of family ``gone`` is ``keep``'s now, with the name and what it has seen of the values."""
        name = next((m.name for m in self.signatures if m.family in (gone, keep) and m.name), None)
        for m in self.signatures:
            if m.family == gone:
                m.family = keep
        for m in self.signatures:
            if m.family == keep and m.name != name:
                m.name = name
                self._touch(m.id)
        if gone in self.family_runs:
            rows = self.family_runs.setdefault(keep, [])
            rows.extend(self.family_runs.pop(gone))
            del rows[:-HINT_KEEP]
        eps = self.family_eps.setdefault(keep, {})
        for ctx, row in (self.family_eps.pop(gone, None) or {}).items():
            mine = eps.get(ctx)
            if mine is None:
                eps[ctx] = row
            else:
                mine["born"], mine["seen"], mine["last"] = min(mine["born"], row["born"]), max(mine["seen"], row["seen"]), max(mine["last"], row["last"])

    def _splits(self, sig: "Signature", context: str) -> bool:
        """Does the family of ``sig`` run in the value ``context`` names as it does not elsewhere - so that the
        runs made in it want a signature of their own? Judged on the family's runs together, at the time it is
        asked: the same measure as input_of and _judge_born, pooled. A family that has not run enough to say
        (INPUT_MIN_RUNS) splits, as before; one that runs in the value at chance, or in too few of its
        episodes (a kiln firing through one afternoon's washing), does not: the washer's cycle phase is
        everything to the washer's heater and nothing to the kiln."""
        setting, _, value = context.partition("=")
        n = e = runs = 0.0
        for m in self.family_of(sig):
            row = m.inputs.get(setting) or {}
            n += (row.get("n") or {}).get(value, 0.0)
            e += (row.get("e") or {}).get(value, 0.0)
            runs += sum((row.get("n") or {}).values())
        if runs < INPUT_MIN_RUNS:
            return True
        if n <= 0 or e <= 0 or n / e < INPUT_SPLIT_LIFT:
            return False
        eps = (self.family_eps.get(sig.family) or {}).get(context) if sig.family else None
        if not eps:
            return False
        since = (self.input_episodes.get(setting) or {}).get(value, 0.0) - eps["born"]
        return since >= INPUT_MIN_EPISODES and eps["seen"] / since >= INPUT_MIN_COVERAGE

    def _takes(self, sig: "Signature", context: Optional[str]) -> bool:
        """May ``sig`` take a run made in ``context`` (None: in no rare value)? An ordinary one takes it unless its
        family splits on the value; one born in the value only once the family does, or it has been judged to belong."""
        if sig.born_in is None:
            return context is None or context in sig.takes_in or not self._splits(sig, context)
        return sig.born_in == context and (sig.born_judged or self._splits(sig, context))

    def _fits(self, sig: "Signature", run: "Run") -> bool:
        """Does a run look like what ``sig`` has learned - its size within the match tolerance on every phase, its
        length within MATCH_DURATION_FACTOR? Asked of a run its family's signature takes in a rare value it has no
        signature for: the family takes only what looks like it (a washer's phase made some of Home's Hidrofor
        runs another load's)."""
        noise = max((self.phases[p].noise for p in sig.phases if p in self.phases), default=MIN_NOISE_W)
        pw = run.power_by_phase()
        for ph in sig.phases:
            mine, theirs = sig.power.get(ph, 0.0), pw.get(ph, 0.0)
            if abs(mine - theirs) > max(MATCH_POWER_REL * max(mine, theirs), noise) + ALIKE_MAD_SHARE * sig.power_mad:
                return False
        return sig.duration_s / MATCH_DURATION_FACTOR <= run.duration_s <= sig.duration_s * MATCH_DURATION_FACTOR

    def _device_family(self, device: int, phases: str) -> int:
        """The family of the signature most of this device's runs went to on ``phases``, or 0."""
        votes = self._device_home.get(device) if self._device_home is not None else None
        best = max(((n, sid) for sid, n in (votes or {}).items()
                    if (x := self._sig(self._current(sid))) is not None and x.phases == phases and x.family), default=None)
        return self._sig(self._current(best[1])).family if best else 0

    def _device_signature(self, device: int, context: Optional[str], avoid: Sequence[int],
                          phases: str, run: Optional["Run"] = None) -> Optional["Signature"]:
        """The signature most of this device's runs went to, of those on the
        run's own ``phases`` that take runs in ``context`` and are not
        ``avoid``ed, whatever its runs' powers - None for a device not seen
        yet. See DEVICE_FILING. On its own phases: a device whose legs link
        across phases filed an A run into a C signature, whose power then
        carried all three phases (Home, ten days, 2026-09-30)."""
        if self._device_home is None:
            self._device_home = {}
            for k, home in self.start_home.items():
                pooled = self._device_home.setdefault(int(k), {})
                for sid, n in home.items():
                    pooled[sid] = pooled.get(sid, 0.0) + n
        cands = []
        for sid, n in (self._device_home.get(device) or {}).items():
            sig = self._sig(self._current(sid))
            if sig is not None and sig.id not in avoid and self._takes(sig, context) and sig.phases == phases and (run is None or sig.takes_run(run)) \
                    and (run is None or sig.files_in(context) or self._fits(sig, run)):
                cands.append((n, sig.id, sig))
        return max(cands)[2] if cands else None

    def _sig(self, sid: int) -> Optional["Signature"]:
        if self._by_sig is None or sid not in self._by_sig:
            self._by_sig = {x.id: x for x in self.signatures}
        return self._by_sig.get(sid)

    def _merge_devices(self) -> int:
        """Signatures whose runs came from one device are one - see
        DEVICE_FILING. A named pair of them with different names stays apart,
        as do two filed in different values of a setting or on different
        phases, and one born of a run kept out of the other (Signature.apart).
        After every filing: once a pass, a minute's pass merged what a
        six-hour one had not yet.

        Judged again only where something changed since the last pass: a
        device whose signatures were filed into, judged born, named, merged
        or moved to it - and all of them after _prune ordered the library
        again, since who survives a tie is the earlier in it. Two signatures
        neither of which changed were judged the last time one did, by the
        same votes, and the merge order is the full walk's: devices as the
        library lists their first signature (a Home replay walked ~160
        signatures and ~600 votes 29,000 times for six merges, 2026-10-02)."""
        if self._votes is None:
            self._rebuild_votes()
        by_dev: Dict[int, List["Signature"]] = {}
        for sig in self.signatures:
            home = self._home.get(sig.id)
            if home is not None:
                by_dev.setdefault(home, []).append(sig)
        judge = None if self._rejudge_all else self._rejudge
        self._rejudge, self._rejudge_all = set(), False
        moved: Dict[int, int] = {}
        for dev, sigs in by_dev.items():
            if len(sigs) < 2 or (judge is not None and dev not in judge):
                continue
            sigs.sort(key=lambda x: (not x.name, -x.count))     # a named one survives
            keep = sigs[0]
            for other in sigs[1:]:
                if other.phases != keep.phases:
                    continue
                if other.name and keep.name and other.name != keep.name:
                    continue
                if (keep.id in {self._current(x) for x in other.apart}
                        or other.id in {self._current(x) for x in keep.apart}):
                    continue                  # one's run was kept out of the other - see Signature.apart
                if not keep.takes_len(other.duration_s) or not other.takes_len(keep.duration_s):
                    continue                  # the daily rewrite split them by length
                if other.born_in != keep.born_in:
                    # apart while they run in the value more than chance puts
                    # them there together - a fridge running through every
                    # rinse is not the washer (as in alike)
                    born = keep.born_in or other.born_in
                    if keep.born_in and other.born_in and not (
                            other.born_in in keep.takes_in or keep.born_in in other.takes_in):
                        continue
                    lift = keep.input_lift(other, born)
                    if lift is None or lift >= INPUT_SPLIT_LIFT:
                        continue
                keep.swallow(other)
                moved[other.id] = keep.id
        self._absorb(moved)
        return len(moved)

    def _rebuild_votes(self) -> None:
        """start_home's votes by the signature each counts for now - what
        _merge_devices walked every pass - and every device to judge."""
        votes: Dict[int, Dict[int, float]] = {}
        for k, home in self.start_home.items():
            d = int(k)
            for sid, n in home.items():
                row = votes.setdefault(self._current(sid), {})
                row[d] = row.get(d, 0.0) + n
        self._votes = votes
        self._home = {sid: max(row, key=row.get) for sid, row in votes.items()}
        self._dev_pos = {int(k): i for i, k in enumerate(self.start_home)}
        self._rejudge_all = True

    def _touch(self, sid: int) -> None:
        """This signature changed in what _merge_devices reads: judge its device again."""
        home = self._home.get(sid)
        if home is not None:
            self._rejudge.add(home)

    def _vote(self, key: str, sid: int) -> None:
        """One more of start cluster ``key``'s runs filed into ``sid`` - as
        start_home now says - kept in the votes _merge_devices reads."""
        if self._votes is None:
            return
        d = int(key)
        pos = self._dev_pos.setdefault(d, len(self._dev_pos))
        if len(self._dev_pos) != len(self.start_home):
            self._votes = None                    # start_home was not grown through here: build again
            return
        row = self._votes.get(sid)
        if row is None:
            row = self._votes[sid] = {d: 1.0}
        elif d in row:
            row[d] += 1.0
        else:
            row[d] = 1.0
            if any(self._dev_pos[x] > pos for x in row):
                row = self._votes[sid] = dict(sorted(row.items(), key=lambda kv: self._dev_pos[kv[0]]))
        self._rehome(sid, row)

    def _fold(self, other_id: int, keep_id: int) -> None:
        """A merged signature's votes count for its keeper now - see _current."""
        if self._votes is None:
            return
        self._touch(other_id)
        gone = self._votes.pop(other_id, None)
        self._home.pop(other_id, None)
        row = self._votes.get(keep_id)
        if gone is None or row is None:
            self._votes = None                    # not what start_home says: build again
            return
        for d, n in gone.items():
            row[d] = row.get(d, 0.0) + n
        row = self._votes[keep_id] = dict(sorted(row.items(), key=lambda kv: self._dev_pos[kv[0]]))
        self._rehome(keep_id, row)

    def _rehome(self, sid: int, row: Dict[int, float]) -> None:
        """The device most of ``sid``'s runs came from, after its votes changed: judge it and the old one again."""
        self._touch(sid)
        self._home[sid] = max(row, key=row.get)
        self._touch(sid)

    def device_of(self, s: "Run") -> Optional[int]:
        """The start cluster of the run, or of its first leg: its device."""
        return next((pair[0] for pair in ([s.pair] if s.pair else []) + list(s.legs)
                     if pair and pair[0] is not None), None)

    def window(self, name: str) -> Optional[Tuple[float, float]]:
        """This input's learned lag window (see lag_window), kept until its histogram changes."""
        if self._windows is None:
            self._windows = {}
        if name not in self._windows:
            self._windows[name] = lag_window(self.lag_hist.get(name) or [])
        return self._windows[name]

    def cluster(self, cid: int) -> Optional["StepGroup"]:
        c = self._by_id.get(cid) if self._by_id is not None else None
        if c is None:
            self._by_id = {x.id: x for x in self.edges}      # one born this pass
            c = self._by_id.get(cid)
        return c

    def input_end(self, cluster: int, since: float, now: float) -> Optional[Tuple[float, float]]:
        """(when, half its window): when a run that started with this cluster
        must have stopped, on the meter's clock, if its input has changed back
        and the window for the stop has passed by ``now`` - else None. See
        INPUT_ENDS."""
        c = self.cluster(cluster)
        if c is None or not c.keys or not any(c.keys.values()) or not self.signals:
            return None
        events, times, _ = self.signals
        for name, kind in c.keys.items():
            if not kind or name not in events:
                continue
            window = self.window(name)
            if window is None:
                continue
            lag, half = 0.5 * (window[0] + window[1]), 0.5 * (window[1] - window[0])
            ts, evs = times[name], events[name]
            i = bisect.bisect_right(ts, since - lag + half)       # changes after the one that started it
            for j in range(i, len(ts)):
                if self.horizon is not None and ts[j] > self.horizon:
                    break                                         # not known yet
                if evs[j][1] != kind:                             # it changed back
                    due = ts[j] - lag
                    return (due, half) if now > due + half else None
        return None

    def _link_successors(self, now: float) -> None:
        """Point a named signature that has gone quiet at what may have
        replaced it.

        A load that drifts is followed by the running mean, and one that
        changes in a step founds a sibling - after which the named original
        goes unseen forever, because named signatures are never evicted. The
        name should follow the load, but deciding that a 2.4 kW run IS the
        3 kW one you called "Kiln" is a judgement, not a measurement, so this
        only records the candidate and the naming page offers it.

        Deliberately narrow: same phases, a real history behind the
        candidate, and within half the power. A wrong guess here puts a
        person's name on someone else's load."""
        for named in self.signatures:
            if not named.name:
                continue
            quiet_after = SUCCESSOR_QUIET_S
            if named.interval_s:
                # a load that runs every five minutes is quiet after an hour;
                # one that runs twice a year is not quiet after a week
                quiet_after = max(quiet_after, SUCCESSOR_QUIET_INTERVALS * named.interval_s)
            if now - named.last_seen < quiet_after:
                named.successor_id = None
                continue
            best, best_gap = None, None
            for other in self.signatures:
                if (other.name or other.phases != named.phases
                        or other.count < SUCCESSOR_MIN_COUNT
                        or other.last_seen <= named.last_seen):
                    continue
                mine, theirs = abs(named.watts), abs(other.watts)
                if not mine or abs(mine - theirs) > SUCCESSOR_POWER_REL * max(mine, theirs):
                    continue
                gap = abs(mine - theirs) / mine
                if best_gap is None or gap < best_gap:
                    best, best_gap = other, gap
            named.successor_id = best.id if best is not None else None

    def _prune(self, now: Optional[float] = None) -> None:
        """Keep the library to its cap, in tiers.

        A NAMED load is never evicted, which the old ordering had exactly
        backwards: it sorted named signatures to the front and then kept the
        tail, so the ones the user had taken the trouble to name were the
        first to go.

        An ESTABLISHED load - real evidence, seen inside the horizon, which
        is over a year - is never evicted either. Its history is its claim on
        the library, and pausing does not forfeit it: a load that runs twice
        a year is rare, not stale. Replayed over ten real days at home, the kiln
        reached 299 runs at evidence 0.81 - the best-evidenced signature in
        the library - and was thrown out during a twelve-hour pause.

        A YOUNG signature - seen once or twice, inside the grace period - is
        protected so that it CAN become established. A signature seen once
        is always the weakest, so on a full library it was pruned in the same
        call that created it, and a new load could only survive if it had
        already been seen twice - which it never had. 2,705 created, 2,355
        evicted, the kiln founded and lost 296 times (2026-09-18).

        Everything else is fair game, weakest first. The first attempt at
        this protected by RECENCY instead, and 195 of 200 slots filled with
        recently-seen junk while the paused kiln was one of the five left to
        choose from. Recency is not a claim; evidence is."""
        if len(self.signatures) <= MAX_SIGNATURES:
            return
        now = now if now is not None else max(s.last_seen for s in self.signatures)
        tiers: Dict[int, List[Signature]] = {0: [], 1: [], 2: [], 3: []}
        for s in self.signatures:
            age = now - s.last_seen
            if s.name:
                tiers[0].append(s)
            elif s.evidence >= ESTABLISHED_EVIDENCE and age < ESTABLISHED_HORIZON_S:
                tiers[1].append(s)
            elif s.count < YOUNG_COUNT and age < PRUNE_GRACE_S:
                tiers[2].append(s)
            else:
                tiers[3].append(s)
        # The cap bounds only what is fair game. Named, established and young
        # are all kept outright, so the library can run over it - and must:
        # a house whose real loads fill the cap would otherwise never learn
        # another, which is the lockout again wearing a different hat. What
        # bounds it in practice is reality (a house has so many loads), the
        # thirty-day horizon, and the grace period on the young.
        keep = tiers[0] + tiers[1] + tiers[2]
        room = MAX_SIGNATURES - len(keep)
        tiers[3].sort(key=lambda x: (x.evidence, x.count, x.last_seen))
        keep += tiers[3][-room:] if room > 0 else []
        self.signatures = keep
        self._by_sig = None          # or _sig hands back one just evicted, and a run filed there is no one's
        self._rejudge_all = True     # the order decides a merge's survivor - see _merge_devices

    # ------------------------------------------------ query
    def active(self, now_ts: float) -> List[dict]:
        """What is on right now, per phase group, with the best signature guess."""
        out = []
        for ph, st in self.phases.items():
            a = st.active(now_ts)
            if a is None:
                continue
            since, w = a
            out.append({"phases": ph, "since": since, "watts": round(w), "signature": self._guess(ph, w, now_ts - since)})
        # phases that started together are one load
        merged: List[dict] = []
        for o in sorted(out, key=lambda x: x["since"]):
            for m in merged:
                if abs(m["since"] - o["since"]) <= MERGE_TOLERANCE_S:
                    m["phases"] += o["phases"]
                    m["watts"] += o["watts"]
                    m["signature"] = None
                    break
            else:
                merged.append(dict(o))
        for m in merged:
            m["phases"] = "".join(sorted(m["phases"]))
            if m["signature"] is None:
                m["signature"] = self._guess(m["phases"], m["watts"], now_ts - m["since"], per_phase=None)
            sig = next((x for x in self.signatures if x.id == m["signature"]), None) if m["signature"] else None
            m["name"] = sig.name if sig else None
        return merged

    def _guess(self, phases: str, watts: float, elapsed: float, per_phase=None) -> Optional[int]:
        best, best_d = None, None
        for sig in self.signatures:
            if sig.phases != phases:
                continue
            total = sum(sig.power.values())
            tol = max(MATCH_POWER_REL * max(total, watts), MIN_NOISE_W)
            if abs(total - watts) <= tol and elapsed <= sig.duration_s * MATCH_DURATION_FACTOR + 60:
                d = abs(total - watts)
                if best_d is None or d < best_d:
                    best, best_d = sig.id, d
        return best

    def unknown_power(self, now_ts: float) -> float:
        return float(sum(m["watts"] for m in self.active(now_ts)))

    def hourly_by_name(self, name: str) -> Dict[int, float]:
        """hour start (epoch s) -> Wh the load NAMED so used in it, as far back
        as HOURLY_KEEP_S - what backfills its meter's statistics."""
        out: Dict[int, float] = {}
        for sig in self.signatures:
            if sig.name == name:
                for hour, wh in sig.hourly.items():
                    out[hour] = out.get(hour, 0.0) + wh
        return dict(sorted(out.items()))

    def energy_by_name(self) -> Dict[str, float]:
        """Watt-hours each NAME has used over everything ever seen of it.

        Signatures sharing a name are one device, so their energy adds. It
        starts again from the ten days a reset rebuilds; the published meter
        counts only what it gains - see named.carry_reading."""
        out: Dict[str, float] = {}
        for sig in self.signatures:
            if sig.name:
                out[sig.name] = out.get(sig.name, 0.0) + sig.energy_wh
        return out

    def live_runs(self, now_ts: float) -> List[Tuple[str, float, float]]:
        """(name, since, watts) of each open run a named load owns - see
        LIVE_NAME_SHARE - leaving out one open longer than LIVE_STALE_FACTOR
        times the longest run its name is known for."""
        lengths: Dict[str, List[float]] = {}
        for sig in self.signatures:
            if sig.name:
                lengths.setdefault(sig.name, []).extend([b - a for a, b in sig.runs] or [sig.duration_s])
        longest = {name: _quantile(sorted(lens), LIVE_STALE_QUANTILE) for name, lens in lengths.items()}
        out = []
        for st in self.phases.values():
            scale = st.open_scale()
            for o in st.open_edges:
                name = self.start_name(o.cluster)
                # its own group's usual run where one is known (run lifecycle), not the name's
                usual = self.usual_length(o.cluster) if name and o.cluster is not None else None
                if name and now_ts - o.since <= LIVE_STALE_FACTOR * (usual or longest.get(name, 0.0)):
                    out.append((name, o.since, st.level_of(o) * scale))
        return out

    def start_name(self, cluster: Optional[int]) -> Optional[str]:
        """The name a start of ``cluster`` belongs to: the one LIVE_NAME_SHARE
        of its filed runs went to, over at least LIVE_NAME_MIN_RUNS - else None."""
        home = self.start_home.get(str(cluster)) if cluster is not None else None
        if not home:
            return None
        by: Dict[Optional[str], float] = {}
        for sid, n in home.items():
            sig = self._sig(self._current(int(sid)))
            key = sig.name if sig is not None else None
            by[key] = by.get(key, 0.0) + n
        total = sum(by.values())
        name, n = max(((k, v) for k, v in by.items() if k), key=lambda kv: kv[1], default=(None, 0.0))
        return name if n >= LIVE_NAME_MIN_RUNS and n >= LIVE_NAME_SHARE * total else None

    def filed_after(self, n: int) -> List[Tuple[str, float, float]]:
        """(name, start, Wh) of each named session filed after the ``n``-th."""
        out = []
        for r in self.recent:
            if int(r.get("n") or 0) > n:
                sig = self._sig(self._current(r["signature"]))
                if sig is not None and sig.name:
                    out.append((sig.name, r["start"], float(r.get("wh", r["kwh"] * 1000.0))))
        return out

    def names(self) -> Dict[str, List[int]]:
        """name -> the signature ids filed under it."""
        out: Dict[str, List[int]] = {}
        for sig in self.signatures:
            if sig.name:
                out.setdefault(sig.name, []).append(sig.id)
        return out

    def name_descriptors(self) -> List[dict]:
        """What a named load would need to be recognised again.

        Naming a load is the one thing in the library the USER put there, and
        it is the only thing worth carrying across a library that is about to
        be thrown away. The rest - the counts, the hours, the locations - is
        re-learned from history in a few minutes; a name is not."""
        # ...and the names still waiting from the last reset: one reset on
        # top of another dropped every name whose load had not run in between
        # (Home, 2026-09-29: the floor mat, the kiln and the washer)
        return [{"name": sig.name, "phases": sig.phases, "power": dict(sig.power),
                 "duration_s": sig.duration_s, "pf": sig.pf}
                for sig in self.signatures if sig.name] + [dict(o) for o in self.orphan_names]

    def carry_names(self, descriptors: List[dict]) -> None:
        """Take names into a fresh library."""
        self.orphan_names = [dict(d) for d in descriptors if d.get("name")]

    def awaited(self, s: Run) -> bool:
        """Does a name a reset carries look like this run - so that filed
        here, its signature takes the name back (_reclaim)?"""
        if not self.orphan_names:
            return False
        noise = max((self.phases[p].noise for p in s.phases if p in self.phases), default=MIN_NOISE_W)
        probe = Signature(id=-1, phases=s.phases, power=s.power_by_phase(), duration_s=s.duration_s, pf=s.pf,
                          count=1, first_seen=s.start, last_seen=s.start)
        return any(_described(o).alike(probe, noise) for o in self.orphan_names)

    def device_signature(self, s: Run) -> Optional["Signature"]:
        """The signature this run's device would take it into, filed now -
        None for a device not seen yet (see _device_signature)."""
        device = self.device_of(s)
        if device is None:
            return None
        found = self._input_context(s)
        return self._device_signature(device, found[0] if found else None, (), s.phases)

    def _reclaim(self, sig: "Signature", noise_w: float) -> None:
        """Give a rebuilt signature back the name a reset took from it.

        The same test that decides two signatures are one load decides this,
        so a name only returns to something that looks like what wore it. If
        the site really did change - the reason to reset by hand - nothing
        matches and the name simply never comes back, which is the right
        answer rather than a special case."""
        if sig.name or not self.orphan_names:
            return
        for i, orphan in enumerate(self.orphan_names):
            if _described(orphan).alike(sig, noise_w):
                for m in self.family_of(sig):
                    m.name = orphan.get("name")
                    self._touch(m.id)
                self.orphan_names.pop(i)
                return

    def rename(self, signature_id: int, name: Optional[str]) -> bool:
        signature_id = self._current(signature_id)      # a signature merged away is named as what it became
        for sig in self.signatures:
            if sig.id == signature_id:
                name = (name or "").strip() or None
                for m in self.family_of(sig):       # a family is named, not a signature
                    m.name = name
                    self._touch(m.id)
                return True
        return False

    def family_head(self, family: int) -> Optional["Signature"]:
        """The family's biggest signature - what stands for it - or None for a family that is gone."""
        return max((x for x in self.signatures if x.family == family), key=lambda x: x.count, default=None)

    def rename_family(self, family: int, name: Optional[str]) -> bool:
        """Name a family (None clears it): every signature of it wears the name."""
        head = self.family_head(family)
        return head is not None and self.rename(head.id, name)

    def predecessor_of(self, signature_id: int) -> Optional["Signature"]:
        """The named signature that thinks this one is what it became.

        The naming page asks this of whatever the user is looking at, so the
        offer appears where they are already standing rather than on a dead
        entry they have no reason to open."""
        for sig in self.signatures:
            if sig.name and sig.successor_id == signature_id:
                return sig
        return None

    def adopt(self, signature_id: int) -> Optional[str]:
        """Move a name onto the signature that replaced its load.

        The old fingerprint keeps its own history - a kiln that drew 5.9 kW
        really did draw it - but stops carrying a name nothing matches any
        more. Its ENERGY comes along, as carried_wh rather than folded into
        the hour and weekday charts: the meter must not step backwards when a
        name moves, or Home Assistant reads it as a reset, while the charts
        should still describe this behaviour rather than an average of two.
        Its hours by the clock come along too, so the name's statistics keep
        the old fingerprint's days (insights.named.plan_rewrite)."""
        old = self.predecessor_of(signature_id)
        if old is None or not old.name:
            return None
        name = old.name
        heir = next((s for s in self.signatures if s.id == signature_id), None)
        if heir is None:
            return None
        heir.carried_wh += old.energy_wh
        for mine, theirs in ((heir.hourly, old.hourly), (heir.older, old.older), (heir.recovered, old.recovered)):
            for hour, wh in theirs.items():
                mine[hour] = mine.get(hour, 0.0) + wh
            theirs.clear()
        for m in self.family_of(old):
            m.name = None
            self._touch(m.id)
        old.successor_id = None
        self.rename(signature_id, name)
        return name

    # ------------------------------------------------ storage
    def to_dict(self) -> dict:
        return {"phases": {p: st.to_dict() for p, st in self.phases.items()}, "held": [s.to_dict() for s in self.held],
                "signatures": [s.to_dict() for s in self.signatures], "recent": self.recent, "next_id": self.next_id,
                "next_family": self.next_family,
                "family_runs": {str(f): [[_trim(a, 1), _trim(h, 2)] for a, h in rows] for f, rows in self.family_runs.items()},
                "family_eps": {str(f): {c: dict(row) for c, row in eps.items()} for f, eps in self.family_eps.items()},
                "tz_offset_s": self.tz_offset_s, "orphan_names": self.orphan_names,
                "input_time": {n: {v: _trim(t, 1) for v, t in row.items()} for n, row in self.input_time.items()},
                "input_until": dict(self.input_until),
                "input_episodes": {n: dict(row) for n, row in self.input_episodes.items()},
                "edges": [e.to_dict() for e in self.edges], "next_edge_id": self.next_edge_id,
                "edge_hist": {g: {str(b): _trim(w, 3) for b, w in h.items() if w >= HIST_FLOOR * TRIM} for g, h in self.edge_hist.items()},
                "edge_hist_at": dict(self.edge_hist_at), "edge_unit": dict(self.edge_unit),
                "edge_hist_keys": {g: {k: {str(b): _trim(w, 3) for b, w in h.items() if w >= HIST_FLOOR * TRIM} for k, h in rows.items()}
                                   for g, rows in self.edge_hist_keys.items()},
                "edge_hist_angle": {g: {f"{sb}:{ab}": _trim(w, 3) for sb, row in h.items() for ab, w in row.items() if w >= HIST_FLOOR * TRIM}
                                    for g, h in self.edge_hist_angle.items()},
                "start_home": {k: {str(i): n for i, n in v.items()} for k, v in self.start_home.items()},
                "lag_hist": {n: [_trim(x, 2) for x in h] for n, h in self.lag_hist.items()},
                "order": {k: [_trim(x, 4) for x in v] for k, v in self.order.items()},
                "run_time": {str(k): _trim(v, 1) for k, v in self.run_time.items()},
                "fall_n": {str(k): _trim(v, 3) for k, v in self.fall_n.items()},
                "order_span": self.order_span, "order_clock": self.order_clock, "order_faded": self.order_faded,
                # rises still in their event window - a pass's end no longer forms them
                "pending_rises": [[m["ph"], i, m["since"], m["watts"], m["var"], m["surge"]] for m in self._pending
                                  for i, o in enumerate(self.phases[m["ph"]].open_edges if m["ph"] in self.phases else [])
                                  if o is m.get("open")],
                # ...and falls (run lifecycle): what each closes is decided when its event forms
                "pending_falls": [[m["ph"], m["since"], m["var"], m["surge"], *m["fall"],
                                   next((i for i, o in enumerate(self.phases[m["ph"]].open_edges) if o is m["hint"]), None)
                                   if m["ph"] in self.phases else None]
                                  for m in self._pending if "fall" in m],
                # the steps a run filed later still finds its edges by, and the readings' clock
                "edge_at": {ph: [list(e) for e in rows] for ph, rows in self.edge_at.items()},
                # the day's runs and readings, for the day's repair (Fleet._repair): what a restart mid-day must not lose
                "rejudge": sorted(self._rejudge), "rejudge_all": self._rejudge_all,
                "day_log": [x.to_dict() for x in self._day_log],
                "day_rows": {ph: [[_trim(t, 3), _trim(w, 1)] for t, w in rows] for ph, rows in self._day_rows.items()},
                # the size density kept between steps, as it stands: worked out afresh it can differ by a float's last
                # digit, and a valley cut on a plateau then lands elsewhere (see Detector._valleys)
                "dens": {g: {"order": [k for k, _ in sorted(st[1].items(), key=lambda kv: kv[1])], "lo": st[2], "dens": list(st[3])}
                         for g, st in self._dens.items() if st[0] is self.edge_hist.get(g)},
                # each group's segments as last cut (at its last step): _likely_meter reads those of groups it has not just stepped
                "segs": {g: [list(x) for x in segs] for g, segs in self._segs.items()},
                "clock": self._clock, "moved": {str(k): v for k, v in self._moved.items()},
                # a library without this key is retaken once on loading: Detector.retake_fragments
                "fragments_retaken": True,
                }

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Detector":
        det = cls()
        if not d:
            return det
        det.phases = {p: PhaseState.from_dict((d.get("phases") or {}).get(p)) for p in PHASES}
        det.held = [Run.from_dict(x) for x in d.get("held") or []]
        det.signatures = [Signature.from_dict(x) for x in d.get("signatures") or []]
        det.recent = list(d.get("recent") or [])
        det.filed_n = max([0] + [int(r.get("n") or 0) for r in det.recent])
        det.next_id = d.get("next_id", 1)
        det.next_family = d.get("next_family", 1)
        det.family_runs = {int(f): [(float(a), float(h)) for a, h in rows] for f, rows in (d.get("family_runs") or {}).items()}
        det.family_eps = {int(f): {c: {k: float(x) for k, x in row.items()} for c, row in eps.items()}
                          for f, eps in (d.get("family_eps") or {}).items()}
        det.tz_offset_s = d.get("tz_offset_s", 0.0)
        det.orphan_names = [x for x in (d.get("orphan_names") or []) if x.get("name")]
        det.input_time = {n: {v: float(t) for v, t in row.items()} for n, row in (d.get("input_time") or {}).items()}
        det.input_until = {n: float(t) for n, t in (d.get("input_until") or {}).items()}
        det.input_episodes = {n: {v: float(c) for v, c in row.items()} for n, row in (d.get("input_episodes") or {}).items()}
        det.edges = [StepGroup.from_dict(x) for x in d.get("edges") or []]
        det.edge_hist = {g: {int(b): float(w) for b, w in h.items()} for g, h in (d.get("edge_hist") or {}).items()}
        det.edge_hist_at = {g: float(t) for g, t in (d.get("edge_hist_at") or {}).items()}
        det.edge_unit = {g: float(u) for g, u in (d.get("edge_unit") or {}).items()}
        det.edge_hist_keys = {g: {k: {int(b): float(w) for b, w in h.items()} for k, h in rows.items()}
                              for g, rows in (d.get("edge_hist_keys") or {}).items()}
        det.edge_hist_angle = {}
        for g, h in (d.get("edge_hist_angle") or {}).items():
            nested = det.edge_hist_angle[g] = {}
            for k, w in h.items():
                sb, ab = k.split(":")
                nested.setdefault(int(sb), {})[int(ab)] = float(w)
        det.start_home = {k: {int(i): float(n) for i, n in v.items()} for k, v in (d.get("start_home") or {}).items()}
        det.next_edge_id = d.get("next_edge_id", 1)
        det.lag_hist = {n: [float(x) for x in h] for n, h in (d.get("lag_hist") or {}).items()}
        det.order = {k: [float(x) for x in v] for k, v in (d.get("order") or {}).items()}
        det.run_time = {int(k): float(v) for k, v in (d.get("run_time") or {}).items()}
        det.fall_n = {int(k): float(v) for k, v in (d.get("fall_n") or {}).items()}
        det.order_span = float(d.get("order_span") or 0.0)
        det.order_clock, det.order_faded = d.get("order_clock"), d.get("order_faded")
        for ph, i, since, watts, var, surge in d.get("pending_rises") or []:
            opens = det.phases[ph].open_edges if ph in det.phases else []
            if 0 <= i < len(opens) and opens[i].since == since:
                det._pending.append({"since": since, "ph": ph, "watts": watts, "var": var, "surge": surge,
                                     "open": opens[i]})
        for ph, since, var, surge, step, new_level, quality, hint in d.get("pending_falls") or []:
            opens = det.phases[ph].open_edges if ph in det.phases else []
            det._pending.append({"since": since, "ph": ph, "watts": -step, "var": var, "surge": surge,
                                 "fall": (step, new_level, quality),
                                 "hint": opens[hint] if hint is not None and 0 <= hint < len(opens) else None})
        det.edge_at = {ph: [tuple(e) for e in rows] for ph, rows in (d.get("edge_at") or {}).items()}
        for g, st in (d.get("dens") or {}).items():
            if g in det.edge_hist:
                det._dens[g] = [det.edge_hist[g], {int(k): i for i, k in enumerate(st["order"])}, int(st["lo"]),
                                [float(x) for x in st["dens"]], None]
        det._segs = {g: [tuple(x) for x in segs] for g, segs in (d.get("segs") or {}).items()}
        det._day_log = [Run.from_dict(x) for x in d.get("day_log") or []]
        det._day_rows = {ph: [tuple(r) for r in rows] for ph, rows in (d.get("day_rows") or {}).items()}
        det._clock = d.get("clock")
        det._moved = {int(k): int(v) for k, v in (d.get("moved") or {}).items()}
        det._assign_families()
        if "rejudge_all" in d:
            # the devices _merge_devices still has to judge again: a full walk after a restart would
            # merge what the pass-by-pass one had left apart
            det._rebuild_votes()
            det._rejudge, det._rejudge_all = {int(x) for x in d.get("rejudge") or []}, bool(d["rejudge_all"])
        if "fragments_retaken" not in d:
            det.retake_fragments()
        return det


# what a fleet's main detector asks it - each Fleet._<hook> - see Fleet._bind
_HOOKS = ("meter_steps", "step_meter", "meter_held", "meter_stop", "meter_on", "meter_started",
          "meter_level", "meter_ended", "meter_wh", "meter_read")


def _lag_of(lags, q: float = LAG_PERCENTILE) -> float:
    """The ``q`` percentile of a meter's learned lags, never below 0."""
    got = sorted(lags)
    return max(0.0, got[int(q * (len(got) - 1))])


def _lead_of(lags) -> float:
    """How far ahead of the grid a meter's learned lags put it: the other
    tail, negated, never below 0 - see LAG_PERCENTILE."""
    return _lag_of((-x for x in lags), LAG_PERCENTILE)


def _shared(rows: Sequence[Sequence[float]]) -> List[Sequence[float]]:
    """A meter's learned (report lag, declaring lag) rows whose report lags
    pile up (_pile over EDGE_LAG_BIN_S bins of the reach) - the steps it truly
    shares with the grid, not chance; none while fewer than LAG_MIN_SAMPLES
    are learned or nothing piles up - see LAG_PERCENTILE."""
    n = int(round(2 * EDGE_LAG_REACH_S / EDGE_LAG_BIN_S))
    hist = [0] * n
    for r in rows:
        i = math.floor((r[0] + EDGE_LAG_REACH_S) / EDGE_LAG_BIN_S)
        if 0 <= i < n:
            hist[i] += 1
    got = _pile(hist, LAG_MIN_SAMPLES)
    if got is None:
        return []
    lo, hi = (-EDGE_LAG_REACH_S + k * EDGE_LAG_BIN_S for k in (got[0], got[1] + 1))
    return [r for r in rows if lo <= r[0] < hi]


# ------------------------------------------------------------------ the fleet: main meter + downstream meters
@dataclass
class Fleet:
    """One detector per meter. The MAIN meter sees everything; a DOWNSTREAM
    meter sees only its own subpanel or circuit. A main-meter
    session that a downstream meter also saw - same start, same end, same
    phases, same size - is located there; one that none saw is upstream of
    them all. Locations accumulate per signature, so the answer sharpens
    with every session."""
    main: Detector = field(default_factory=Detector)
    subs: Dict[str, Detector] = field(default_factory=dict)
    # filed house sessions no meter has placed yet: (when the last try is due, session)
    pending_main: List[tuple] = field(default_factory=list)
    pending_sub: Dict[str, List[Run]] = field(default_factory=dict)
    # Each device's raw samples, channel by channel, kept long enough to
    # answer "how much energy did you record while this was running" - a
    # meter too slow to produce a session of its own can still answer that -
    # and what each channel read: a 3EM's phases are its own (the owner,
    # 2026-10-03). meter -> channel -> [(ts, value)]; the total, _sub_total.
    sub_rows: Dict[str, Dict[str, List[Tuple[float, float]]]] = field(default_factory=dict)
    # ...the first moment of the total kept (the value in force at the cut,
    # see _keep_rows), each channel's value from before its first row kept,
    # and the totals worked out from them
    _sub_from: Dict[str, float] = field(default_factory=dict, repr=False, compare=False)
    _sub_seed: Dict[str, Dict[str, float]] = field(default_factory=dict, repr=False, compare=False)
    _sub_totals: Dict[tuple, list] = field(default_factory=dict, repr=False, compare=False, metadata=DERIVED)
    # meter -> "p" (power) / "q" (reactive) -> [mean log of grid's step over the
    # meter's, steps] - see METER_GAIN_MIN
    meter_gain: Dict[str, Dict[str, List[float]]] = field(default_factory=dict)
    # (meter, kind) -> (the ring list it was worked out from, its count then, the gain): gain() is asked
    # millions of times and the ring changes only in _learn_gain, which counts every change
    _gain_memo: Dict[Tuple[str, str], tuple] = field(default_factory=dict, repr=False, compare=False, metadata=DERIVED)
    # meter -> the meter it hangs under, as the Energy dashboard nests them; set by the runner
    parents: Dict[str, Optional[str]] = field(default_factory=dict, metadata=DERIVED)
    # the readings' clock all meters are read on: the meters' own, the grid
    # wait_cap_s behind them (see process); how far every meter's readings reach
    _now: float = field(default=0.0, repr=False, compare=False)
    _aged_to: float = field(default=0.0, repr=False, compare=False)   # the hour Signature.age last swept to
    # sub-meter sessions waiting to vote on their channel's phase: (when, meter, past the patience, session) - see _vote_blocked
    _unvoted: List[tuple] = field(default_factory=list, repr=False, compare=False)
    # house sessions handed over lately, (when, session): what a meter's session votes against
    _recent_main: List[tuple] = field(default_factory=list, repr=False, compare=False)
    _dues: Dict[str, Optional[tuple]] = field(default_factory=dict, repr=False, compare=False, metadata=DERIVED)   # see _advance
    _names: List[str] = field(default_factory=list, repr=False, compare=False, metadata=DERIVED)
    _phase_maps: Dict[str, tuple] = field(default_factory=dict, repr=False, compare=False, metadata=DERIVED)   # see phase_map
    _composed: Dict[str, tuple] = field(default_factory=dict, repr=False, compare=False, metadata=DERIVED)   # see _through_parent
    # meter ("" the grid) -> its phase -> sorted [(ts, volts)], fed by process: the voltage
    # the cross-meter edges read beside each step (insights/edges.py); nothing here sizes a step by it.
    volts: Dict[str, Dict[str, List[Tuple[float, float]]]] = field(default_factory=dict, repr=False, compare=False)
    _sub_last: Dict[str, Dict[str, float]] = field(default_factory=dict)   # each meter's channels' last values - see _keep_rows
    _stops_used: Dict[tuple, float] = field(default_factory=dict, repr=False, compare=False)   # see _meter_stop
    # meter -> (phase, run, closed at) of the runs it owned the last day - see _meter_wh
    _owned_runs: Dict[str, List[tuple]] = field(default_factory=dict, repr=False, compare=False)
    wait_cap_s: float = field(default=0.0, metadata=DERIVED)   # set by the runner - see METER_WAIT_CAP_S
    # how far behind its meters the grid is read now, and the MODEL_REFRESH_S
    # step of the clock it was worked out at - see _horizon
    _wait: float = 0.0
    _wait_at: Optional[int] = None
    # meter -> [[report lag, declaring lag], ...] against the grid, the last
    # LAG_SAMPLES of them, and the clock up to which grid steps were asked - see _learn_lags
    meter_lag: Dict[str, List[List[float]]] = field(default_factory=dict)
    _lag_from: Optional[float] = None
    # meter -> the time of its last declared step that has voted - see _vote_steps
    _step_votes_from: Dict[str, float] = field(default_factory=dict)
    # meters the runner says are not to set the horizon (the settings page) - see _horizon
    horizon_skip: set = field(default_factory=set, repr=False, compare=False, metadata=DERIVED)
    # meter -> True reports on change, False is polled; a meter missing here is classified from its gaps - see PhaseState.forced
    cadence_forced: Dict[str, bool] = field(default_factory=dict, repr=False, compare=False, metadata=DERIVED)
    # the grid's readings, reactive and PV newer than the wait, for the next pass
    _carry: Dict[str, dict] = field(default_factory=dict, repr=False, compare=False)
    # switch -> {on moment: off moment, or None while on}, as the runner last
    # read them - see SWITCH_PREFIX. Never persisted: every pass re-reads its
    # window with a lookback.
    switch_on: Dict[str, Dict[float, Optional[float]]] = field(default_factory=dict, metadata=DERIVED)
    # meters that hold one device (True) or several (False), as the runner
    # works it out each pass - the user's answer, else a parent holds several,
    # else guess_one_device; a meter missing here is guessed
    single: Dict[str, bool] = field(default_factory=dict, metadata=DERIVED)
    # meter -> its own phase label -> house phase -> sessions that matched,
    # for phase_mapping
    phase_votes: Dict[str, Dict[str, Dict[str, int]]] = field(default_factory=dict)
    # ...and the energy those sessions carried, in the grid's terms, Wh
    phase_energy: Dict[str, Dict[str, Dict[str, float]]] = field(default_factory=dict)
    # inverter -> its phase -> grid phase -> summed voltage correlation (phases.voltage_votes,
    # one vote per pass), and the map read from it (phases.phase_mapping): a balanced
    # inverter's phases are learned from its VOLTAGES, not its steps - see inverter_map
    inverter_votes: Dict[str, Dict[str, Dict[str, float]]] = field(default_factory=dict)
    inverter_phases: Dict[str, Dict[str, str]] = field(default_factory=dict)

    def inverter_map(self, name: str, votes: Dict[str, Dict[str, float]]) -> Dict[str, str]:
        """Add one pass's voltage votes for inverter ``name`` and return its
        map, the stored one standing on a tie (phase_mapping's rule); empty
        while the votes are too few (INVERTER_MAP_MIN_VOTES passes) or the
        best pairs do not stand clearly above the rest."""
        acc = self.inverter_votes.setdefault(name, {})
        for c, row in votes.items():
            mine = acc.setdefault(c, {})
            for p, r in row.items():
                mine[p] = mine.get(p, 0.0) + r
        n = max((sum(1 for _ in row) for row in votes.values()), default=0)
        if not acc or sum(max(row.values()) for row in acc.values()) < INVERTER_MAP_MIN_VOTES * n:
            return self.inverter_phases.get(name, {})
        mp = phase_mapping(acc, tuple(PHASES), self.inverter_phases.get(name) or None)
        top = sum(acc[c].get(p, 0.0) for c, p in mp.items())
        rest = max((sum(acc[c].get(p, 0.0) for c, p in other.items())
                    for other in (dict(zip(mp, perm)) for perm in itertools.permutations(mp.values())) if other != mp), default=0.0)
        if top < INVERTER_MAP_CLEAR * rest:
            return self.inverter_phases.get(name, {})
        self.inverter_phases[name] = mp
        return mp
    # the grid's fleet, whose votes say what a typical vote carries - see
    # PHASE_MAP_MIN_VOTES; None for the grid's own
    _root: Optional["Fleet"] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    _vote_wh: Optional[tuple] = field(default=None, repr=False, compare=False, metadata=DERIVED)
    # every vote cast on this fleet, counted - what phase_map's and
    # _vote_wh_needed's caches are kept by
    _votes_rev: int = field(default=0, repr=False, compare=False, metadata=DERIVED)
    # house sessions not yet filed, waiting for a sub-meter partner: (when they stop waiting, session)
    unfiled: List[tuple] = field(default_factory=list)
    # (meter, its session, when) of the grid runs a meter holding several
    # devices owned lately - a re-opened piece of one is its too (the placed claim, _decide)
    _owned: List[tuple] = field(default_factory=list, repr=False, compare=False, metadata=DERIVED)
    # One fleet per meter others hang under - its main that meter's
    # detector, its subs the meters inside it - reading them as this one
    # reads every meter (_sync_views); and whether this is the grid's, the
    # reference every meter's report lag is learned against (_learn_lags)
    views: Dict[str, "Fleet"] = field(default_factory=dict, repr=False, compare=False)
    reference: bool = field(default=True, repr=False, compare=False, metadata=DERIVED)
    _view_states: Dict[str, dict] = field(default_factory=dict, repr=False, compare=False, metadata=DERIVED)
    # meter -> its signature id -> the house signature its sessions joined
    identity: Dict[str, Dict[str, int]] = field(default_factory=dict)
    # the start of the first day not yet repaired, on the readings' clock -
    # see _repair; stored with the day's runs and readings, so a restart mid-day
    # repairs the whole day
    _repaired_to: Optional[float] = field(default=None, repr=False, compare=False)

    def process(self, main_samples, sub_samples: Dict[str, Dict[str, Sequence[Tuple[float, float]]]],
                main_q=None, sub_q=None, now_ts: Optional[float] = None,
                pv: Optional[Dict[str, Dict[float, float]]] = None,
                main_q_quantum: Optional[Dict[str, float]] = None,
                single: Optional[Dict[str, bool]] = None,
                switches: Optional[Dict[str, Sequence[Tuple[float, Optional[float]]]]] = None,
                numbers: Optional[Dict[str, Sequence[Tuple[float, float]]]] = None,
                inputs: Optional[Dict[str, Sequence[Tuple[float, str]]]] = None,
                sub_q_quantum: Optional[Dict[str, Dict[str, float]]] = None,
                volts: Optional[Dict[str, Dict[str, Sequence[Tuple[float, float]]]]] = None) -> None:
        """A pass over every meter's new readings, on ONE clock (2026-10-02).

        The meters below are read as they come and the grid wait_cap_s behind
        them, so when a grid step asks what the meters did about it (the
        hooks: _meter_steps, _step_meter, _meter_held, _meter_stop) they have
        been read exactly that far past it - and a house session is filed,
        a meter's session votes on its phase, at their own moments on that
        clock (_ready_at, _vote_at). Before, the meters were read a whole
        pass ahead of the grid and the fleet filed once a pass: in one call
        the grid's steps saw ten days of the meters' future and no phase map
        at all, and every house session was filed at the end."""
        # kept from before the oldest moment still to be asked about - a run
        # still open, a session still waiting: a backfill slice is six hours
        # long, and pruning off its END lost the switch for its first two
        # (2026-09-28: 462 of 1232 floor-mat runs); counted from the pass's
        # oldest reading, a run older than the memory lost its own
        keep = self._oldest_asked() - SWITCH_MEMORY_S
        for name, spans in (switches or {}).items():
            known = self.switch_on.setdefault(SWITCH_PREFIX + name, {})
            for on, off in spans:
                if off is not None or on not in known:
                    known[on] = off
            for on in [t for t, off in known.items() if off is not None and off < keep]:
                del known[on]                  # a span still on is never forgotten
        for store, fed, cast in ((self.main.numbers, numbers, float), (self.main.inputs, inputs, str)):
            for name, rows in (fed or {}).items():
                held = dict(store.get(name) or [])
                held.update((float(t), cast(v)) for t, v in rows)
                last = max((t for t in held if t < keep), default=None)   # still in force at the cut
                store[name] = sorted((t, v) for t, v in held.items() if t >= keep or t == last)
        for name, by_ph in (volts or {}).items():
            for ph, rows in by_ph.items():
                held = dict(self.volts.setdefault(name, {}).get(ph) or [])
                held.update((float(t), float(v)) for t, v in rows)
                last = max((t for t in held if t < keep), default=None)
                self.volts[name][ph] = sorted((t, v) for t, v in held.items() if t >= keep or t == last)
        if single is not None:
            self.single = dict(single)      # the whole declaration, so a withdrawn one lapses
        sub_samples = sub_samples or {}
        latest = max([now_ts or 0.0] + [r[-1][0] for byp in [main_samples or {}] + list(sub_samples.values())
                                         for r in byp.values() if r])
        end = now_ts if now_ts is not None else latest
        for name in sorted(set(self.subs) | set(sub_samples)):
            det = self.subs.setdefault(name, Detector())
            det.tz_offset_s = self.main.tz_offset_s
            for st in det.phases.values():
                st.forced = self.cadence_forced.get(name)
        self._sync_views()
        for name, rows_by_phase in sub_samples.items():
            self._keep_rows(name, rows_by_phase, end)
        self._keep_rows("", main_samples or {}, end)        # the grid's own, as a meter's
        events, numbers = self._signal_events()
        signals = (events, {n: [t for t, _ in evs] for n, evs in events.items()}, numbers)
        units = self._units()
        for name, f in units:
            # every detector is told what the meters inside it did, and what
            # the switches and settings did
            f._bind()
            f.main.signals, f.main.numbers, f.main.inputs = signals, self.main.numbers, self.main.inputs
            if name:
                f._keep_rows("", sub_samples.get(name) or {}, end)
                for n in f.subs:
                    f._keep_rows(n, sub_samples.get(n) or {}, end)
        # a main's readings - the grid's, a meter others hang under - the last
        # pass's held ones in front: each is read once the clock is its
        # horizon past it (_run); the rest wait. Only the main meter needs the
        # array: a downstream meter sees the house side of it, never the sun
        streams, kept = {}, {}
        for name, f in units:
            rows, q, quantum = ((main_samples, main_q, main_q_quantum) if not name else
                                (sub_samples.get(name) or {}, (sub_q or {}).get(name), (sub_q_quantum or {}).get(name)))
            rows, q, arr = f._hold_back(rows, q, pv if not name else None, math.inf)
            kept[name] = (rows, q, arr)
            streams[name] = f.main.begin(rows, q, arr, quantum, file=False)
        meters = []
        for name in sorted(self.subs):
            if name not in self.views:
                det = self.subs[name]
                meters += [(ts, name, ph, w) for ts, ph, w in det.begin(sub_samples.get(name) or {}, (sub_q or {}).get(name),
                                                                        q_quantum=(sub_q_quantum or {}).get(name))]
        meters.sort(key=lambda r: r[:3])
        self._dues, self._names = {}, [""] + sorted(self.subs)
        first = min([end] + [r[0] for r in streams[""][:1] + meters[:1]])
        for _, f in units:
            if f._wait_at is None:
                f._wait_at = math.floor(first / MODEL_REFRESH_S)
        if self._repaired_to is None:
            tz = self.main.tz_offset_s
            self._repaired_to = (first + tz) // REPAIR_DAY_S * REPAIR_DAY_S - tz
        for _, det in self.detectors():
            det._day_from = self._repaired_to
        done = self._run(units, streams, meters, end)
        for name, f in units:
            left: Dict[str, list] = {}
            for ts, ph, w in streams[name][done[name]:]:
                left.setdefault(ph, []).append((ts, w))
            f._hold_back(left, kept[name][1], kept[name][2], -math.inf)   # kept, with their reactive and PV values
        self._now = max(self._now, end)
        for name, f in units:
            f._now = self._now
            f.main.finish(kept[name][0], f.main._clock or end, f._oldest_asked())
        for name, det in self.subs.items():
            if name not in self.views:
                det.finish(sub_samples.get(name) or {}, end)
        # once an hour, every signature's hours age as a run filed now would
        # age them: one that stopped running still lets its hours go - into
        # older, kept for naming it one day - rather than keeping its last
        # days in hourly, before the statistics' window, until it runs again
        if self._now // 3600 > self._aged_to // 3600:
            self._aged_to = self._now
            for _, det in self.detectors():
                for sig in det.signatures:
                    sig.age(self._now)

    def _units(self) -> List[Tuple[str, "Fleet"]]:
        """The grid's fleet under "", then each meter's others hang under, by name."""
        return [("", self)] + [(p, self.views[p]) for p in sorted(self.views)]

    def _bind(self) -> None:
        """This fleet's main detector asks it what the meters inside did."""
        for hook in _HOOKS:
            setattr(self.main, hook, getattr(self, "_" + hook))

    def _sync_views(self) -> None:
        """The fleet as a tree (2026-10-03): every meter others hang
        under reads the meters inside it, as the grid reads every meter - a
        fleet of its own (``views``) whose main is that meter's detector and
        whose subs are the meters under it at any depth, with its own say
        about them: their votes on its channels, their gains and lags against
        it, its sessions waiting for theirs, the identity their sessions give
        its own. It shares the grid's clock, declarations, switches and
        settings. A meter no read meter hangs under any longer is a meter
        again: its detector files its own sessions and asks no one."""
        tops = {p for n, p in self.parents.items() if p and n in self.subs and p in self.subs}
        for p in [p for p in self.views if p not in tops]:
            v = self.views.pop(p)
            for hook in _HOOKS:
                setattr(v.main, hook, None)
        for p in sorted(tops):
            v = self.views.get(p)
            if v is None:
                v = self.views[p] = Fleet.from_dict(self._view_states.pop(p, None))
            v.main, v.reference = self.subs[p], False
            v.subs = {n: d for n, d in self.subs.items() if self._under(n, p)}
            v.parents, v.single, v.switch_on, v.horizon_skip = self.parents, self.single, self.switch_on, self.horizon_skip
            v.wait_cap_s, v._now, v._root = self.wait_cap_s, self._now, self

    def _run(self, units: List[Tuple[str, "Fleet"]], streams: Dict[str, List[tuple]], meters: List[tuple],
             end: float) -> Dict[str, int]:
        """Every reading and everything due, in ONE order on one clock, up to
        how far this pass's readings reach (``end``): a meter's reading at its
        own time, a main's - the grid's, a meter others hang under (``units``,
        _sync_views) - once the clock is its horizon past it (_wait, see
        _horizon), and what falls due between them - each meter's own
        (Detector.next_due; a main's its horizon later), a main's session
        done waiting for the meters inside it (_decide), one more try at
        placing a filed one (_locate), a meter's session's vote
        (_vote_phases), a horizon worked out again. At one moment the
        readings first - the mains', the grid's first, then the meters' - and
        then what is due, in that order. Returns how many of each main's
        readings were read; the rest wait for the next pass."""
        at_ = {name: 0 for name, _ in units}
        isub = 0
        fleets = [f for _, f in units]
        while True:
            cands = [(max(streams[name][at_[name]][0] + f._wait, self._now), 0, 0, k)
                     for k, (name, f) in enumerate(units) if at_[name] < len(streams[name])]
            if isub < len(meters):
                cands.append((meters[isub][0], 0, 1))
            for name in self._names:
                if name not in self._dues:
                    self._dues[name] = self._det(name).next_due()
                d = self._dues[name]
                if d is not None:
                    cands.append((d[0] + self._wait, 1, 0) if not name else (d[0] + self._wait_of(name), 1, 1, name))
            for k, f in enumerate(fleets):
                cands += [c for c in ((f.unfiled[0][0], 1, 2, k) if f.unfiled else None,
                                      (f.pending_main[0][0], 1, 3, k) if f.pending_main else None,
                                      (f._unvoted[0][0], 1, 4, k) if f._unvoted else None,
                                      ((f._wait_at + 1) * MODEL_REFRESH_S, 1, 5, k)) if c is not None]
            if self._repaired_to is not None:
                cands.append((self._repaired_to + REPAIR_DAY_S + REPAIR_WAIT_S, 1, 6, 0))
            best = min(cands, default=None)               # the first of a tie, as the strict < kept
            at, cls, rank = best[:3]
            if at > end or (cls == 1 and at >= end):
                return at_
            self._now = max(self._now, at)
            for f in fleets:
                f._now = f.main.horizon = self._now
            if cls == 0 and rank == 0:
                name, f = units[best[3]]
                ts, ph, w = streams[name][at_[name]]
                at_[name] += 1
                f.main.step(ts, ph, w)
                self._dues.pop(name, None)
            elif cls == 0:
                ts, name, ph, w = meters[isub]
                isub += 1
                self.subs[name].step(ts, ph, w)
                self._dues.pop(name, None)
            elif rank <= 1:
                name = best[3] if rank else ""
                det = self._det(name)
                det.fire(self._dues.pop(name))
                got, det._released = det._released, []
                for s in got:
                    self._hand(name, s)
            else:
                f = fleets[best[3]]
                if rank == 2:
                    f._decide(f.unfiled.pop(0)[1])
                elif rank == 3:
                    f._locate(f.pending_main.pop(0)[1], final=True)
                elif rank == 4:
                    _, name, final, s = f._unvoted.pop(0)
                    if not final and f._vote_blocked(name, s):
                        # the main may still hand over a session started with it:
                        # it votes when the main's last such run is out (_on_main),
                        # or once MATCH_PATIENCE_S is over, with what is out by then
                        f._insort(f._unvoted, (s.end + MATCH_PATIENCE_S, name, True, s))
                    else:
                        f._vote(name, s)
                elif rank == 6:
                    self._repair(self._repaired_to, self._repaired_to + REPAIR_DAY_S)
                    self._repaired_to += REPAIR_DAY_S
                else:
                    f._learn_lags(at)
                    f._vote_steps(at)
                    f._wait, f._wait_at = f._horizon(), math.floor(at / MODEL_REFRESH_S)
                    self._dues = {}                   # every meter's latency may have moved

    def _repair(self, a: float, b: float) -> None:
        """The day [a, b) is over: its runs booked again with what the whole
        day shows - every detector's against its own readings, one measure
        for every meter (_cap_day, see REPAIR_FLOOR_SHARE). Bounded: one
        day's runs and readings, once a day; each detector lets go of its
        runs that started before ``b`` and its readings from before it."""
        for _, det in self.detectors():
            log = [s for s in det._day_log if a <= s.start < b]
            det._day_log = [s for s in det._day_log if s.start >= b]
            rows = {}
            for ph, rr in det._day_rows.items():
                rows[ph] = rr[max(bisect.bisect_left(rr, (a, -math.inf)) - 1, 0):]
                det._day_rows[ph] = rr[max(bisect.bisect_left(rr, (b, -math.inf)) - 1, 0):]
            if log:
                _cap_day(det, log, rows, a, b)
                _recover_day(det, log, rows, a, b)
            # ...the born-in signatures waiting for ever on a lift (Detector._judge_born)
            for sig in det._judge_born(daily=True):
                det._touch(sig.id)
            # ...and the length split each signature's run memory shows (the owner,
            # 2026-10-23: length decides here, with the runs' lengths in view, not live)
            for sig in det.signatures:
                if sig.learn_split() and det._home.get(sig.id) is not None:
                    det._rejudge.add(det._home[sig.id])      # its device's merges are judged again (_merge_devices)
            _hint_split(det)
            det._join_families()             # the families a drifting start size cut apart (_join_neighbours)

    def _wait_of(self, name: str) -> float:
        """How far behind the meters inside it a meter is read: its own
        fleet's horizon where others hang under it, none where none do. The
        views all live with the grid's fleet (_sync_views), so a view asks
        there too: a meter two levels down is waited for through the one
        between."""
        v = (self._root or self).views.get(name)
        return v._wait if v is not None else 0.0

    def _hand(self, name: str, s: Run) -> None:
        """A session a detector handed over: the grid's to the grid's fleet; a
        meter's to every fleet it is inside, as a meter's session - and, where
        others hang under it, to its own fleet as its main's, filed after
        theirs have had their say."""
        if not name:
            self._on_main(s)
            return
        if name in self.views:
            self.views[name]._on_main(s)
        for _, f in self._units():
            if name in f.subs:
                f._on_sub(name, s)

    def _horizon(self) -> float:
        """How far behind its meters the grid is read: the longest any meter
        takes to declare a step it shares with the grid (_declare_lag), so
        every meter has had its say on a grid step before the grid's is
        judged; at least EDGE_LAG_REACH_S where inputs are fed, whose changes
        are looked for that far either side of a step. Never past
        wait_cap_s (2026-10-02: up to five minutes): a meter slower
        than that is judged with what it has said by then. Only a meter that
        has shared a step with the grid sets it - its lag once learned, twice
        its latency until then: one that never shares a step (Home's Server
        UPS, a 60 s heartbeat) has nothing the grid waits for (the owner,
        2026-10-02) - until then Home sat at the cap for it. Only the learned
        ones setting it left Kozolec's first hours at no horizon at all (its
        pond EVSE 99 -> 88 % captured). A meter in horizon_skip - the settings
        page's own choice, a meter that shares steps but reports them slowly -
        is still read, and does not set it. Worked out again every
        MODEL_REFRESH_S of the clock as the lags are learned - at a pass's
        start it would move with the slicing."""
        wait = max([0.0] + [self._declare_lag(name) for name in self.subs
                            if name not in self.horizon_skip and self._shares(name)])
        if self.switch_on or self.main.inputs:
            wait = max(wait, EDGE_LAG_REACH_S)
        return min(self.wait_cap_s, wait)

    def _det(self, name: str) -> Optional["Detector"]:
        """A meter's detector - the grid's under ""."""
        return self.subs.get(name) if name else self.main

    def _interval(self, name: str) -> float:
        """A meter's cadence - its channels' longest reading interval."""
        det = self._det(name)
        return max((st.interval for st in det.phases.values()), default=0.0) if det else 0.0

    def _latency(self, name: str) -> float:
        """A meter's latency - its channels' longest (PhaseState.latency)."""
        det = self._det(name)
        return max((st.latency() for st in det.phases.values() if st.interval), default=0.0) if det else 0.0

    def _mapped(self, name: str, ph: str) -> List[str]:
        """Meter ``name``'s channels the phase map places on grid phase ``ph``."""
        det = self._det(name)
        return [c for c, h in self.phase_map(name).items() if ph in h and c in det.phases] if det else []

    def _several(self, name: str, c: str) -> bool:
        """Does the map give meter ``name``'s channel ``c`` several phases (a
        total, PHASE_MAP_MIN_VOTES)? Then its falls end no run on a grid
        phase (_meter_stop, _meter_ended): the grid's own legs pair the
        stop. Ended by the total's fall, read at a third, the third site's go-e
        (mapped "abc") fed capture fell 94.0 -> 81.3 %, and to 71.7 % with the
        fall used once per phase; with neither path ending them 95.5 %
        (2026-10-14)."""
        return len(self.phase_map(name).get(c) or "") > 1

    def _leg_gain(self, name: str, c: str, ph: Optional[str] = None, at: Optional[float] = None) -> float:
        """What meter ``name``'s step on channel ``c`` is worth on ONE grid
        phase, in the grid's terms: its gain, over a channel the map gives
        several phases (PHASE_MAP_MIN_VOTES) that phase's part of what the
        grid's phases among them moved, as a step votes (_moved), at ``at``
        on ``ph`` - a total carries any part of its set (PHASE_SET_MIN):
        Home's EVBox's L1-only charge is all on A and none on B -; a share
        evenly spread where none moved or no moment is asked about."""
        gain = self.gain(name, "p")
        h = self.phase_map(name).get(c) or ""
        if len(h) < 2:
            return gain
        if ph is not None and at is not None:
            tol = self._pair_tol(name)
            for up in (True, False):
                moved = self._moved(at, tol, h, up)
                if ph in moved:
                    return gain * moved[ph][0] / sum(x for x, _ in moved.values())
            if self._moved(at, tol, h, True) or self._moved(at, tol, h, False):
                return 0.0                    # its step was on the others
        return gain / len(h)

    def _moved(self, at: float, tol: float, phases, up: bool) -> Dict[str, Tuple[float, float]]:
        """The grid phases among ``phases`` whose declared steps within
        ``tol`` of ``at`` net a clear change ``up`` (or down) - ten noises,
        as _learn_lags takes a grid step: phase -> (its size, its noise)."""
        out = {}
        for p in phases:
            grid = self.main.phases.get(p)
            if grid is None:
                continue
            lo, hi = bisect.bisect_left(grid.declared_t, at - tol), bisect.bisect_right(grid.declared_t, at + tol)
            net = sum(g[1] for g in grid.declared[lo:hi])
            if (net > 0) == up and abs(net) >= 10.0 * grid.noise_at():
                out[p] = (abs(net), grid.noise_at())
        return out

    def _read_chans(self, name: str) -> List[str]:
        """Meter ``name``'s channels that have read."""
        det = self._det(name)
        return [c for c, st in det.phases.items() if st.last_ts is not None] if det else []

    def _chans(self, name: str, ph: str) -> List[str]:
        """Meter ``name``'s channels that may carry grid phase ``ph``: those the
        map places there, and those it places nowhere yet. What every hook
        asking what the meter's own load did reads; netting a meter's change
        out of a grid step needs it placed (_mapped)."""
        mp = self.phase_map(name)
        return self._mapped(name, ph) + [c for c in self._read_chans(name) if c not in mp]

    def _under(self, n: str, anc: str) -> bool:
        """Does meter ``n`` hang under ``anc``, at any depth?"""
        seen = set()
        while n in self.parents and n not in seen:
            seen.add(n)
            n = self.parents[n]
            if n == anc:
                return True
        return False

    def _pair_tol(self, name: str, main_iv: Optional[float] = None) -> float:
        """How far apart the grid's and meter ``name``'s views of one moment
        may lie: the merge tolerance, or the grid's cadence, the meter's
        latency and its lead together where that is longer."""
        det = self._det(name)
        lead = max((self._lead(st) for st in det.phases.values()), default=0.0) if det and name else 0.0
        return max(MERGE_TOLERANCE_S, (self._interval("") if main_iv is None else main_iv) + self._latency(name) + lead)

    def _shares(self, name: str) -> bool:
        """Has meter ``name`` shown a step the grid showed: any learned while
        too few are to tell, then those that pile up (_shared) - a meter whose
        nearest steps of the grid's size lie anywhere in the reach shares none."""
        rows = self.meter_lag.get(name) or []
        return bool(rows) and (len(rows) < LAG_MIN_SAMPLES or bool(_shared(rows)))

    def _declare_lag(self, name: str) -> float:
        """How long after a step the grid shares with it this meter has
        declared its own: the LAG_PERCENTILE of what was learned, once
        LAG_MIN_SAMPLES are; until then its latency twice over - the report,
        then the silence that confirms it - and in either case, where others
        hang under it, the horizon it is read behind them (_wait_of)."""
        rows = _shared(self.meter_lag.get(name) or [])
        # ...and a meter others hang under is read its own horizon behind
        # them, which a lag learned off its own readings' times does not hold
        if rows:
            return _lag_of(r[1] for r in rows) + self._wait_of(name)
        return 2.0 * self._latency(name) + self._wait_of(name)

    def _learn_lags(self, at: float) -> None:
        """Each meter's lag against the grid, from the steps the two share:
        for every clear grid step at least LAG_REACH_S old on the clock - so
        the meter has had its whole reach to declare its own, whatever the
        slicing - the meter's declared step that way of its size nearest it
        within EDGE_LAG_REACH_S, on its channels that may carry that phase:
        (when it happened, when it was declared) less the grid step's time.
        Where those pile up (_shared), the meter's report lag sets its latency
        (PhaseState.lag) and its lead, the declaring lag the grid's horizon
        (_horizon) - see LAG_PERCENTILE."""
        reach = LAG_REACH_S
        upto = at - reach
        # a step the meter shows 100 s after the grid's is another step of a
        # wandering load, not a late report - Home's office plug (a computer)
        # read a 95th report lag of 109 s and a declaring lag of 190 s that
        # way, and set Home's horizon (2026-10-02): looked for within
        # EDGE_LAG_REACH_S, and chance's left out by where the lags pile up.
        # ponytail: a meter whose reports lag past the reach learns none, its
        # latency its cadence's (none here does: the slowest pile, Kozolec's
        # pond EVSE, reaches 18 s)
        reach_m = EDGE_LAG_REACH_S
        start = self._lag_from if self._lag_from is not None else -math.inf
        if upto <= start:
            return
        self._lag_from = upto
        for ph, grid in self.main.phases.items():
            noise_g = grid.noise_at()
            lo, hi = bisect.bisect_right(grid.declared_t, start), bisect.bisect_right(grid.declared_t, upto)
            steps = [g for g in grid.declared[lo:hi] if abs(g[1]) >= 10.0 * noise_g]
            if not steps:
                continue
            for name, det in self.subs.items():
                chans = self._chans(name, ph)
                if not chans:
                    continue
                rows = self.meter_lag.setdefault(name, [])
                for g in steps:
                    best = None
                    for c in chans:
                        st = det.phases[c]
                        # its share on this one phase (_leg_gain): a total on
                        # several reads the part the grid's phases moved here
                        gain = self._leg_gain(name, c, ph, g[0])
                        if gain <= 0.0:
                            continue
                        tol = math.hypot(noise_g, st.noise_at()) + METER_CAL_SLACK * abs(g[1])
                        a, b = (bisect.bisect_left(st.declared_t, g[0] - reach_m),
                                bisect.bisect_right(st.declared_t, g[0] + reach_m))
                        for e in st.declared[a:b]:
                            if ((e[1] > 0) == (g[1] > 0) and abs(abs(e[1]) * gain - abs(g[1])) <= tol
                                    and (best is None or abs(e[0] - g[0]) < abs(best[0] - g[0]))
                                    and self._on_phase(name, c, e, ph)):
                                best = e
                    if best is not None:
                        rows.append([round(best[0] - g[0], 2), round((best[5] if len(best) > 5 else best[4]) - g[0], 2)])
                del rows[:-LAG_SAMPLES]
        if not self.reference:
            return                 # the grid is the reference clock: a report lag is against it
        for name, det in self.subs.items():
            rows = _shared(self.meter_lag.get(name) or [])
            lag = lead = 0.0
            if rows:
                lag, lead = _lag_of(r[0] for r in rows), _lead_of(r[0] for r in rows)
            for st in det.phases.values():
                st.lag, st.lead = lag, lead

    def _vote_steps(self, at: float) -> None:
        """Each step a one-channel meter declared votes for the SET of grid
        phases that moved its way in the same moment - the pair window
        session votes use (_pair_tol), LAG_REACH_S old so the grid has
        declared its own - where together they make the meter's step, in
        the grid's terms, within both meters' noise and METER_CAL_SLACK (as
        a step is placed, _step_meter). A phase moved where the net of its
        declared steps in the window is clear (10 noises, as _learn_lags
        takes a grid step); the net, not the first step, so a ramp the
        detector declared in pieces counts whole: Home's EVBox rose 4 ->
        10.5 kW on 10-12 at 19:18:09 as a +965, b +1434, c +964, then a and
        c ramped +1100 / +1750 and b +380 within 30 s (the grid's per-phase
        readings), and the house never held one three-phase session the
        charge's could vote with (_vote_phases). One set, all the phases
        that moved: a coincident load on one makes the sum miss, and the
        step does not vote. A rise's vote carries the energy of its layer,
        its size held until the meter reads back below half of it (as
        _meter_on frees a run) or MAX_OPEN_S - so it waits for that -; a
        fall's none. Same rule for every one-channel meter,
        a single phase being a set of one; a 3EM's channels vote by session.
        No clear test of the meter's own: the phases' is the bar, and Home's
        Hidrofor plug (noise 158 W) cast none of its 850 W runs' steps past
        ten of its own noises (2026-10-14)."""
        upto = at - LAG_REACH_S
        for name, det in self.subs.items():
            chans = self._read_chans(name)
            if len(chans) != 1:
                continue
            st = det.phases[chans[0]]
            frm = self._step_votes_from.get(name, -math.inf)
            lo, hi = bisect.bisect_right(st.declared_t, frm), bisect.bisect_right(st.declared_t, upto)
            if lo >= hi:
                continue
            gain, noise_m, tol = self.gain(name, "p"), st.noise_at(), self._pair_tol(name)
            for i in range(lo, hi):
                e = st.declared[i]
                end = e[0]
                if e[1] > 0:
                    # its layer: until the meter reads back below half of it
                    # (as _meter_on frees a run), or MAX_OPEN_S
                    was = e[6] - e[1] if len(e) > 6 else None
                    end = next((x[0] for x in st.declared[i + 1:] if was is not None and len(x) > 6
                                and x[6] <= was + 0.5 * e[1]), None)
                    if end is None:
                        if upto - e[0] < MAX_OPEN_S:
                            break                 # its layer has not ended yet
                        end = e[0] + MAX_OPEN_S
                frm = e[0]
                w = abs(e[1]) * gain
                moved = self._moved(e[0], tol, self.main.phases, e[1] > 0)
                size, noise_g = sum(x for x, _ in moved.values()), math.sqrt(sum(n * n for _, n in moved.values()))
                if not moved or abs(size - w) > math.hypot(noise_g, noise_m) + METER_CAL_SLACK * max(size, w):
                    continue
                house = "".join(sorted(moved))
                row = self.phase_votes.setdefault(name, {}).setdefault(chans[0], {})
                row[house] = row.get(house, 0) + 1
                if e[1] > 0:
                    row = self.phase_energy.setdefault(name, {}).setdefault(chans[0], {})
                    row[house] = row.get(house, 0.0) + w * (end - e[0]) / 3600.0
                self._votes_rev += 1
            self._step_votes_from[name] = frm

    def _oldest_asked(self) -> float:
        """The oldest moment a question may still be asked about on the clock:
        the grid's runs still open or pending, the sessions still waiting."""
        main = self.main
        ts = [self._now - self.wait_cap_s]
        ts += [o.since for st in main.phases.values() for o in st.open_edges]
        ts += [st.pending[0][0] for st in main.phases.values() if st.pending]
        ts += [s.start for s in main.held] + [m.start for _, m in self.unfiled] + [m.start for _, m in self.pending_main]
        return min(ts)

    def _keep_rows(self, name: str, rows_by_phase: Dict[str, Sequence[Tuple[float, float]]], end: float) -> None:
        """A meter's readings, channel by channel, for the energy answer
        (_energy_pairs, the channels summed - _sub_total) and for what a
        channel read (_reading). Each channel is held at its last value
        from the last pass until it writes again, not counted as nothing from
        the pass's start."""
        last = self._sub_last.setdefault(name, {})
        stamps = sorted({t for rows in rows_by_phase.values() for t, _ in rows})
        if not stamps:
            return
        chans = self.sub_rows.setdefault(name, {})
        seed = self._sub_seed.setdefault(name, {})
        for p, rows in rows_by_phase.items():
            if not rows:
                continue
            kept = chans.get(p) or []
            if not kept and p in last:
                seed[p] = last[p]                     # carried from before: what it held until it wrote
            new = [(t, v) for t, v in rows]
            chans[p] = sorted(kept + new) if kept and new[0] <= kept[-1] else kept + new   # a pass that overlapped
            last[p] = new[-1][1]
        # Everything this pass brought, plus a tail before it. Trimming to
        # a fixed two hours looked thrifty and silently gutted the
        # backfill, whose slices are six hours long: the sessions being
        # placed were mostly older than the readings kept to place them
        # with (2026-09-19). Kept back to the oldest run or session still
        # to be placed, its idle window before it, whatever the slicing.
        cut = min(self._oldest_asked(), stamps[0] - SUB_SAMPLE_TAIL_S) - IDLE_WINDOW_S
        frm = self._sub_from.get(name, -math.inf)
        every = sorted({t for rows in chans.values() for t, _ in rows if t >= frm})
        i = max(0, bisect.bisect_left(every, cut) - 1)                    # with the value in force at the cut
        self._sub_from[name] = every[i]
        for p, kept in chans.items():
            j = max(0, bisect.bisect_left(kept, (cut, -math.inf)) - 1)
            chans[p] = kept[j:]

    def _window_size(self, name: str, ph: str, since: float, up: bool,
                     size: Optional[float] = None) -> Optional[Tuple[float, float, float, float]]:
        """(the grid's change, from, to, the meter's change) over the union
        of the grid's step on ``ph`` at ``since`` and meter ``name``'s own
        steps that way overlapping it - each read off its own readings, just
        before the union's start and at its end, the grid's net of other
        meters' steps inside it (a meter's nested ones are in its reading
        too). None where ``size`` is a part of the grid's step (another
        meter's share split off it) or less than half the window's change,
        the grid stepped that way earlier inside the union (that part is a
        run already), the union is wider than the union cap, or a reading is
        missing. A ramp the two declared over different
        stretches: Mansarda's washer-dryer drew 157 -> 483 W over 21 s; the
        grid declared +261 at its first plateau, the panel +313 once settled,
        beyond each other's tolerance - and the grid read 543 W before both
        and ~850 after, +307 (Home 09-24 05:41, 2026-10-03)."""
        grid, det = self.main.phases.get(ph), self.subs.get(name)
        if grid is None or det is None:
            return None
        mine = grid.declared_at(since)
        chans = self._mapped(name, ph)
        if mine is None or not chans:
            return None
        if size is not None and abs(abs(mine[1]) - size) > grid._tol(abs(mine[1]), size):
            return None                              # a part of the grid's step, not all of it
        steps = [self._shifted(det.phases[c], e) for c in chans
                 for e in self._near(det.phases[c], mine[3], mine[4], det.phases[c].latency()) if (e[1] > 0) == up]
        if not steps:
            return None
        a, b = min([mine[3]] + [e[3] for e in steps]), max([mine[4]] + [e[4] for e in steps])
        cap = UNION_CAP_WINDOWS * max([self.main.event_window()] + [det.phases[c].latency() for c in chans])
        if b - a > cap or any((e[1] > 0) == up and a <= e[0] < since for e in self._near(grid, a, since, 0.0)):
            return None
        g0, g1 = self._reading("", ph, a, -1), self._reading("", ph, b, 1)
        m0 = [self._reading(name, c, a, -1) for c in chans]
        m1 = [self._reading(name, c, b, 1) for c in chans]
        if g0 is None or g1 is None or None in m0 or None in m1:
            return None
        change = g1 - g0
        for other, odet in self.subs.items():
            if other == name or self._under(other, name) or self._under(name, other):
                continue
            och = self._mapped(other, ph)
            change -= sum(e[1] * self._leg_gain(other, c, ph, since) for c in och
                          for e in self._near(odet.phases[c], a, b, 0.0) if a <= e[0] <= b)
        if size is not None and abs(change) > 2.0 * size:
            # a ramp's first plateau carries most of it (261 of 307 W); a
            # small step whose window holds a bigger one is not that ramp:
            # a 25 W blip 7 s before a 2 kW start was booked at 1,981 W and
            # ran on for two hours (Home 09-24 12:45)
            return None
        return change, a, b, sum((y - x) * self._leg_gain(name, c, ph, since) for c, x, y in zip(chans, m0, m1))

    def _sub_total(self, name: str, chans: Sequence[str]) -> List[Tuple[float, float]]:
        """The meter's channels ``chans`` summed at every moment any of them
        wrote - each at its value in force then - from the first moment kept."""
        rows_by = {p: r for p, r in (self.sub_rows.get(name) or {}).items() if p in chans}
        frm = self._sub_from.get(name, -math.inf)
        key = (frm, tuple((p, len(r), r[-1] if r else None) for p, r in rows_by.items()))
        hit = self._sub_totals.get((name, tuple(chans)))
        if hit is not None and hit[0] == key:
            return hit[1]
        val = {p: v for p, v in (self._sub_seed.get(name) or {}).items() if p in chans}
        order = [p for p in (self._sub_last.get(name) or {}) if p in chans]   # summed in the order the channels first wrote
        order += [p for p in list(val) + list(rows_by) if p not in order]
        at = {p: 0 for p in rows_by}
        out = []
        for ts in sorted({t for rows in rows_by.values() for t, _ in rows if t >= frm}):
            for p, rows in rows_by.items():
                k = at[p]
                while k < len(rows) and rows[k][0] <= ts:
                    val[p] = rows[k][1]
                    k += 1
                at[p] = k
            out.append((ts, sum(val[p] for p in order if p in val)))
        self._sub_totals[(name, tuple(chans))] = (key, out)
        return out

    def _on_main(self, m: Run) -> None:
        """A house session the grid's detector handed over: it waits for the
        meters' say (_ready_at), and is one a meter's session may vote for."""
        main_iv = self._interval("")
        self._insort(self.unfiled, (self._ready_at(m, main_iv), m))
        self._recent_main.append((self._now, m))
        while self._recent_main and self._now - self._recent_main[0][0] >= MATCH_PATIENCE_S:
            self._recent_main.pop(0)
        for row in [r for r in self._unvoted if r[2] and not self._vote_blocked(r[1], r[3])]:
            self._unvoted.remove(row)
            self._vote(row[1], row[3])

    def _vote_blocked(self, name: str, s: Run) -> bool:
        """Could the grid still hand over a session that started with the
        meter's ``s`` - a run still open, or waiting for a partner leg? A
        vote takes the start and the size, never the end: a grid run that
        stops late is still the one (09-25 11:02 at Kozolec: the IR panel's
        house session closed after the panel's and its vote went unheard)."""
        tol = self._pair_tol(name)
        main = self.main
        return (any(abs(o.since - s.start) <= tol for st in main.phases.values() for o in st.open_edges)
                or any(abs(m.start - s.start) <= tol for m in main.held)
                or any(st.pending and st.pending[0][0] <= s.start + tol for st in main.phases.values()))

    def _vote(self, name: str, s: Run) -> None:
        main_iv = self._interval("")
        self._vote_phases({name: [s]}, main_iv, [m for _, m in self._recent_main])

    def _on_sub(self, name: str, s: Run) -> None:
        """A meter's session: a partner for a house session, and a vote on the
        meter's phase once the grid has been read as far (_vote_at)."""
        self.pending_sub.setdefault(name, []).append(s)
        self._insort(self._unvoted, (self._vote_at(name), name, False, s))

    @staticmethod
    def _insort(rows: list, row: tuple) -> None:
        """``row`` into ``rows`` by when it is due, and at one moment by its
        session's start - one order the data fixes, whatever the slicing
        (2026-10-02)."""
        key = (row[0], row[-1].start)
        i = bisect.bisect_right([(r[0], r[-1].start) for r in rows], key)
        rows.insert(i, row)

    def _vote_at(self, name: str) -> float:
        """When a meter's session just handed over votes: once the grid has
        been read far enough to have handed over its own session of the same
        load - the grid's wait behind, plus the two meters' tolerance."""
        return self._now + self._wait + self._pair_tol(name)

    def _decide(self, m: Run) -> None:
        """File a house session done waiting (_ready_at), with its sub-meter
        partner's say in which signature it joins - see SUB_OVERRIDE - or
        else a one-device meter's energy's; placed where a meter saw it, if
        one did (_locate)."""
        main_iv = self._interval("")
        for name in self.pending_sub:
            self.pending_sub[name] = [s for s in self.pending_sub[name] if self._now - s.end < 2.0 * MATCH_PATIENCE_S]
        pairs = sorted(self._session_pairs([m], main_iv), key=lambda x: (x[0], x[1]))
        if pairs:
            _, _, name, si = pairs[0]
            if name == m.placed:
                # a placed pair: the meter's session stays for the grid's later pieces
                # of the same run (the covered rule below), until it ages out
                s = self.pending_sub[name][si]
                self._file_as(m, name, s)
                self._remember_owned(name, s)
                return
            self._file_as(m, name, self.pending_sub[name].pop(si))
            return
        # a grid run that starts and ends INSIDE a meter's run it already owns,
        # at about its size, is that run opened again: the grid closed its
        # first piece on another load's edge and re-opened (15 Oct 10:29:12,
        # 46 s and 10:30:07, 272 s inside Mansarda's 358 s dishwasher run)
        self._owned = [row for row in self._owned if self._now - row[2] < 2.0 * MATCH_PATIENCE_S]
        for name, s, _ in self._owned:
            # every meter alike: _file_as decides what the pair means (a plug's
            # identity signature, a 3EM's own signature)
            tol, gain, mp = self._pair_tol(name, main_iv), self.gain(name, "p"), self.phase_map(name)
            mapped = {p for c in s.phases for p in (mp.get(c) or "")}
            if mapped and not set(m.phases) <= mapped:
                continue
            if s.start - tol <= m.start and m.end <= s.end + tol:
                a, b = m.max_w, s.max_w * gain
                if abs(a - b) <= max(2.0 * MATCH_POWER_REL * max(a, b), MIN_NOISE_W):
                    self._file_as(m, name, s)
                    return
        one = [p for p in sorted(self._energy_pairs([m]), key=lambda x: (x[0], x[1])) if self.holds_one_device(p[2])]
        if one:
            self._place(m, one[0][2], self._meter_home(one[0][2], m))
            return
        self._place(m, None, None)
        if not self._locate(m):
            self._insort(self.pending_main, (m.end + MATCH_PATIENCE_S, m))   # one more try, as late as it may be

    def _count_input_time(self, name: str, now_ts: Optional[float]) -> None:
        """Add the time since it was last counted to each value's share of
        it, the older time fading over INPUT_TIME_TAU_S."""
        det, rows = self.main, self.main.inputs.get(name) or []
        if not rows:
            return
        end = now_ts or rows[-1][0]
        start = max(det.input_until.get(name, rows[0][0]), rows[0][0])
        if end <= start:
            return
        fade = math.exp(-(end - start) / INPUT_TIME_TAU_S)
        spent = {v: t * fade for v, t in (det.input_time.get(name) or {}).items()}
        episodes = det.input_episodes.setdefault(name, {})
        for (t, v), nxt in zip(rows, [r[0] for r in rows[1:]] + [end]):
            a, b = max(t, start), min(nxt, end)
            if b > a:
                spent[v] = spent.get(v, 0.0) + (b - a)
            if start <= t < end:
                episodes[v] = episodes.get(v, 0.0) + 1.0
        det.input_time[name], det.input_until[name] = spent, end

    def rename_entities(self, renames: Dict[str, str]) -> None:
        """Follow renamed entities: what a switch was credited and when it was
        on, and what each load learned in a setting, go with the entity."""
        pairs = [(a, b) for old, new in renames.items()
                 for a, b in ((old, new), (SWITCH_PREFIX + old, SWITCH_PREFIX + new))]
        for det in [self.main, *self.subs.values()]:
            det._rejudge_all = True              # born_in and takes_in are read by _merge_devices
            for sig in det.signatures:
                for a, b in pairs:
                    if a in sig.locations:
                        sig.locations[b] = sig.locations.get(b, 0) + sig.locations.pop(a)
                    if a in sig.inputs:
                        sig.inputs[b] = sig.inputs.pop(a)
                    # "setting=value" markers name the setting's entity
                    if sig.born_in and sig.born_in.startswith(a + "="):
                        sig.born_in = b + sig.born_in[len(a):]
                    sig.takes_in = [b + t[len(a):] if t.startswith(a + "=") else t for t in sig.takes_in]
            for a, b in pairs:
                for held in (det.numbers, det.inputs, det.input_time, det.input_until, det.lag_hist):
                    if a in held:
                        held[b] = held.pop(a)
                for c in det.edges:
                    for held in (c.signals, c.values):
                        if a in held:
                            held[b] = held.pop(a)
                    for key in [k for k in c.lags if k.startswith(a + "=")]:
                        c.lags[b + key[len(a):]] = c.lags.pop(key)
        for a, b in pairs:
            if a in self.switch_on:
                self.switch_on[b] = self.switch_on.pop(a)

    def _switch_for(self, m: Run, main_iv: float) -> Optional[str]:
        """The switch whose on-period this session is, if one fits: on within
        the moment tolerance, and off near the end once the off is known."""
        tol = max(MERGE_TOLERANCE_S, 2.0 * main_iv)
        best, best_cost = None, None
        phases = self.meter_phases()
        for name, spans in self.switch_on.items():
            if name in phases and not set(m.phases) <= set(phases[name]):
                continue
            for on, off in spans.items():
                if abs(on - m.start) > tol:
                    continue
                cost = abs(on - m.start) / tol
                if off is not None and off > self._now:
                    off = None                    # a pass read it ahead of the clock: not known yet
                if off is not None:
                    end_tol = max(tol, SWITCH_END_SHARE * max(m.duration_s, 1.0))
                    if abs(off - m.end) > end_tol:
                        continue
                    cost += abs(off - m.end) / end_tol
                if best_cost is None or cost < best_cost:
                    best, best_cost = name, cost
        return best

    def _signal_events(self) -> Tuple[Dict[str, List[Tuple[float, str]]], Dict[str, List[Tuple[float, float]]]]:
        """Every input's changes as (when, "from→to"), and every number's
        readings, as far back as they are kept."""
        events: Dict[str, List[Tuple[float, str]]] = {}
        for key, spans in self.switch_on.items():
            evs = []
            for on, off in spans.items():
                evs.append((on, "off→on"))
                if off is not None:
                    evs.append((off, "on→off"))
            events[key[len(SWITCH_PREFIX):]] = sorted(evs)
        for name, rows in self.main.inputs.items():
            if name in events:
                continue
            evs, prev = [], None
            for t, v in rows:
                if prev is not None and v != prev:
                    evs.append((t, f"{prev}→{v}"))
                prev = v
            events[name] = evs
        return events, self.main.numbers

    def _edges_of(self, m: Run) -> List[Tuple[str, tuple]]:
        """The edges a house session started, stepped and stopped with, as
        (role, (since, cluster id, watts)). A stop that does not fit the run's
        own size - a hidden stop, taken out of another load's step - is not
        its stop edge."""
        out = []
        pw = m.power_by_phase()
        for ph in m.phases:
            rows = self.main.edge_at.get(ph) or []
            if not rows:
                continue
            times = [r[0] for r in rows]
            levels = m.levels.get(ph) or [(m.start, pw.get(ph, 0.0))]

            def nearest(t, up):
                i = bisect.bisect_left(times, t - MERGE_TOLERANCE_S)
                best = None
                for j in range(i, len(rows)):
                    if rows[j][0] > t + MERGE_TOLERANCE_S:
                        break
                    if up is None or (rows[j][2] > 0) == up:
                        if best is None or abs(rows[j][0] - t) < abs(rows[best][0] - t):
                            best = j
                return rows[best] if best is not None else None

            out.append(("start", nearest(levels[0][0], True)))
            out += [("step", nearest(t, None)) for t, _ in levels[1:]]
            stop = nearest(m.end, False)
            size = pw.get(ph, 0.0)
            noise = self.main.phases[ph].noise if ph in self.main.phases else MIN_NOISE_W
            if stop is not None and abs(abs(stop[2]) - size) <= max(MATCH_POWER_REL * size, noise):
                out.append(("stop", stop))
        return [(role, e) for role, e in out if e is not None]

    def _file_main(self, m: Run, prefer: Optional[int] = None, avoid: Sequence[int] = ()) -> None:
        """File a house session, and note on its signature which edges it
        started, stepped and stopped with - see EDGE_LAG_REACH_S."""
        found = self._edges_of(m)
        for name in self.main.inputs:
            self._count_input_time(name, self._now)    # up to the clock, not the pass's end
        self.main._file(m, prefer=prefer, avoid=list(avoid) + self._held_homes(m))
        self.main._merge_devices()           # as each is filed, as a meter's own detector does
        sig = self.main.signature_of(m)
        if sig is None:
            return
        for role, edge in found:
            used = sig.edges.setdefault(role, {})
            used[edge[1]] = used.get(edge[1], 0.0) + 1.0

    def _held_homes(self, m: Run) -> List[int]:
        """Signatures placed at a meter that held its value through ``m``'s
        start on a channel that may carry its phase - it did not start ``m``.
        The switch gate's test (SWITCH_GATE), with a meter's silence for the
        switch being off: a 2.3 kW load's last 1,064 W step and an unmetered
        start netted with the pump's stop in one reading each landed in the
        plain cluster of 1 kW phase-A starts, whose runs go to the hidrofor,
        while its plug showed no start (2026-10-01). A circuit that held did
        not start it either, as a plug that held did not (the unify audit,
        2026-10-03). See Fleet._meter_held."""
        held = {name for name in self.subs if any(self._meter_held(name, ph, m.start, True) for ph in m.phases)}
        out = [sig.id for sig in self.main.signatures if sig.count >= YOUNG_COUNT
               and any(sig.locations.get(name, 0) >= SWITCH_GATE * sig.count for name in held)] if held else []
        # ...and a run of the size of a run a meter holds open on its phase -
        # one the meter's own step started, since before this one - while that
        # meter held through this start: the level is that run's already, this
        # run another load of its size, not of the meter's run's kind - kept
        # out of the signatures that run's device went to and those placed at
        # the meter, however few of the meter's runs they hold. Home's
        # dehumidifier (Susilna's plug, 254-313 W from 20:00 to 16:00) shared
        # its 263 W signature with 60 runs it never started - a 270 W workshop
        # load cycling, a ramping one under Mansarda, a 400 W load's return
        # from a 6 s dip - 3.6 kWh: the plug, guessed a strip by its library,
        # was never asked above; its 20-hour runs, filed at their end, carried
        # none of its locations there yet; and they start with a surge, in a
        # cluster of their own (2026-10-03).
        w = sum(lv[0][1] for lv in m.levels.values() if lv)
        for ph in m.phases:
            grid = self.main.phases.get(ph)
            for o in (grid.open_edges if grid is not None else ()):
                if not (o.meter and o.since < m.start and abs(o.watts - w) <= MATCH_EDGE_REL * max(o.watts, w)
                        and self._meter_held(o.meter, ph, m.start, True)):
                    continue
                homes = {self.main._current(int(sid)) for k, home in self.main.start_home.items()
                         if o.cluster is not None and int(k) == o.cluster for sid in home}
                homes |= {sig.id for sig in self.main.signatures if sig.locations.get(o.meter, 0)}
                out += [sid for sid in sorted(homes) if sid not in out]
        return out

    def _switched_off(self, m: Run, main_iv: float) -> List[int]:
        """Signatures placed at a switch that was off throughout ``m`` - see
        SWITCH_GATE."""
        if not self.switch_on:
            return []
        tol = max(MERGE_TOLERANCE_S, 2.0 * main_iv)
        # a switch it is fed and holds no span of: off for all of its memory
        off = {name for name, spans in self.switch_on.items()
               if not any(on < m.end + tol and (until is None or until > m.start - tol)
                          for on, until in spans.items())}
        if not off:
            return []
        return [sig.id for sig in self.main.signatures if sig.count >= YOUNG_COUNT
                and any(sig.locations.get(name, 0) >= SWITCH_GATE * sig.count for name in off)]

    def _place(self, m: Run, name: Optional[str], prefer: Optional[int]) -> Optional["Signature"]:
        """File a house session - ``prefer`` the signature a meter's word
        says it joins - and credit it to the meter ``name`` that saw it, if
        any, and to the switch whose on-period it is. The switch is read
        before filing: filing moves the locations it is read from."""
        main_iv = self._interval("")
        switch = self._switch_for(m, main_iv) if self.switch_on else None
        self._file_main(m, prefer=prefer, avoid=self._switched_off(m, main_iv))
        sig = self.main.signature_of(m)
        if sig is not None:
            for where in (name, switch):
                if where is not None:
                    sig.locations[where] = sig.locations.get(where, 0) + 1
        return sig

    def holds_one_device(self, name: str) -> bool:
        """Does this meter hold ONE device, as the user answered - or, where
        nobody has, as guess_one_device says? This is what hides a meter's
        loads from naming, ties it to its device's phases, lets its sessions
        and energy decide a run's identity (_file_as, _decide) - one notion
        everywhere (2026-10-03)."""
        if name.startswith(SWITCH_PREFIX):
            return True                   # a switch switches one load
        if name in self.single:
            return self.single[name]
        return self.guess_one_device(name)

    def guess_one_device(self, name: str) -> bool:
        """A meter other meters hang under holds several by definition (Home's
        Delavnica, Kozolec's Inverter); otherwise the library's shape, once it
        has something to say: at least METER_PHASES_MIN sightings, most of them
        one signature. A young meter - one renamed on the Energy dashboard
        starts again - holds several until it has shown otherwise, since hiding
        a meter's loads is the costly mistake (Kozolec's Inverter, three
        sightings in, looked like one). The runner asked the first part
        itself, so a bench replay with no declarations ran Home's Hiša - 52 %
        of its 10,125 sightings one signature - as one device (2026-10-03)."""
        if any(p == name for p in self.parents.values()):
            return False
        det = self.subs.get(name)
        counts = [s.count for s in det.signatures] if det else []
        return sum(counts) >= METER_PHASES_MIN and max(counts) >= SUB_DEVICE_SHARE * sum(counts)

    def meter_phases(self) -> Dict[str, str]:
        """meter -> the phases its one device runs on, for every one-device
        meter old enough to say: the phase sets it has been credited at least
        METER_PHASES_MIN sightings of, together. Read off the locations the
        house signatures already carry, so there is nothing new to keep."""
        seen: Dict[str, Dict[str, int]] = {}
        one: Dict[str, bool] = {}            # holds_one_device, asked once per meter rather than per location
        for sig in self.main.signatures:
            for name, n in sig.locations.items():
                if n and (name in self.subs or name in self.switch_on):
                    if name not in one:
                        one[name] = self.holds_one_device(name)
                    if one[name]:
                        row = seen.setdefault(name, {})
                        row[sig.phases] = row.get(sig.phases, 0) + n
        out = {}
        for name, row in seen.items():
            known = "".join(sorted({p for ph, n in row.items() if n >= METER_PHASES_MIN for p in ph}))
            if known:
                out[name] = known
        return out

    def _meter_home(self, name: str, m: Optional[Run] = None) -> Optional[int]:
        """The house signature most of this meter's sessions went to - of
        those on ``m``'s phases drawing what ``m`` drew, when asked for it:
        Home's dehumidifier plug had more of its sessions in an 18 W Hiša
        signature (its fan running alone) than in its own 263 W one, and its
        20-hour runs, one a night, were filed with the fan (2026-10-03)."""
        sigs = self.main.signatures
        if m is not None:
            w = sum(m.power_by_phase().values())
            sigs = [s for s in sigs if s.phases == m.phases
                    and abs(sum(s.power.values()) - w) <= MATCH_EDGE_REL * max(w, sum(s.power.values()))]
        best = max(sigs, key=lambda s: s.locations.get(name, 0), default=None)
        return best.id if best is not None and best.locations.get(name, 0) else None

    def _ready_at(self, m: Run, main_iv: float) -> float:
        """When a house session stops waiting for the meters below: once every
        one that COULD have a session for it has been read past its end far
        enough to have handed one over - the two meters' tolerance, the
        meter's sustain and a reading, and its own wait for a partner leg
        (HELD_TAIL_S). A meter is only waited for if it reads at least twice
        within the run - Home's workshop boiler meter reports every seven
        minutes and can partner no two-minute run - and never past
        MATCH_PATIENCE_S. On the one clock: every meter's readings reach it,
        a silent one's value held. Asked of the meter's last reading at a
        pass's end, as before, a meter that went quiet kept the session
        waiting for whatever the pass's end brought."""
        due = m.end
        for name in self.subs:
            iv = self._interval(name)
            if not iv or 2.0 * iv > m.duration_s:
                continue
            due = max(due, m.end + self._pair_tol(name, main_iv)
                      + self._declare_lag(name) + HELD_TAIL_S)
        if m.placed in self.subs:
            # the meter its start was placed at still has the run open: wait for
            # its own session (handed over at ITS end) - a grid run cut short by
            # another load's edge ended 9 min before Mansarda's 867 s one and
            # could never be owned by it (13 Oct 11:34, 2026-10-23)
            det, tol = self.subs[m.placed], self._pair_tol(m.placed, main_iv)
            if any(abs(o.since - m.start) <= tol for st in det.phases.values() for o in st.open_edges):
                due = m.end + MATCH_PATIENCE_S
        return min(due, m.end + MATCH_PATIENCE_S)

    def _file_as(self, m: Run, name: str, s: Run) -> None:
        """File the house's session ``m`` as the sub-meter session ``s`` says:
        a one-device meter's sessions pick the signature its device's went
        to (identity). A meter holding several is where the load lives (the owner,
        2026-10-03: "if a same load is detected by both meters, shouldn't the
        reading collapse into a single device anyway?"): it saw the load on
        its own circuit, so the run is its session's signature's (``owner``)
        and this library grows no copy of it - unless the user named the
        signature the run's device files into here, or a name a reset carries
        looks like the run: a name stays where it was given."""
        det = self.subs.get(name)
        sub_sig = det.signature_of(s) if det is not None else None
        if self.holds_one_device(name):
            prefer = (self.identity.get(name) or {}).get(str(sub_sig.id)) if sub_sig is not None else None
        else:
            here = self.main.device_signature(m)
            prefer = here.id if here is not None and here.name else None
            if prefer is None and not self.main.awaited(m):
                self._meter_owns(m, name, s)
                return
        sig = self._place(m, name, prefer)
        # ...and the device stays where its meter's word put it while that
        # signature stands: a run a guard kept out of it (another phase, a
        # meter that held through its start) is not where the device lives.
        # Home's hidrofor: one 998 W, eight-minute run kept out of its
        # signature went to a 1 kW phase-A one, and the pump's next 102 runs
        # followed it there (09-30 12:29, 2026-10-04)
        home = self.main._current(prefer) if prefer is not None else None
        if sig is not None and sub_sig is not None and (home is None or self.main._sig(home) is None or sig.id == home):
            self.identity.setdefault(name, {})[str(sub_sig.id)] = sig.id

    def _meter_owns(self, m: Run, name: str, s: Run) -> None:
        """``m`` is meter ``name``'s session ``s``'s load's - see _file_as."""
        m.parent_signature = (name, s)
        self._remember_owned(name, s)
        if self.main._day_from is not None:
            self.main._day_log.append(m)

    def _remember_owned(self, name: str, s: Run) -> None:
        """Keep meter ``name``'s session ``s`` for the grid's later pieces of its
        run (_decide); once."""
        if not any(x[1] is s for x in self._owned):
            self._owned.append((name, s, self._now))

    def sub_quantum(self, name: str) -> float:
        """What a device meter can RESOLVE, as its own detector measured it
        (PhaseState.quantum). The energy answer is an integral of its rows, so
        their quantisation is its error bar - and Home has a workshop boiler
        publishing in 46 W steps about every seven minutes (2026-09-22).
        Measured once a pass over the rows kept, it read ten days in one call
        and eight hours in a backfill's slice."""
        det = self.subs.get(name)
        return max((st.quantum for st in det.phases.values()), default=0.0) if det else 0.0

    def _energy_pairs(self, mains: List[Run]) -> list:
        """(cost, main index, meter, None) for every house session whose energy
        a device meter's own readings account for."""
        pairs = []
        phases = self.meter_phases()
        # A device meter too slow to produce a session of its own still knows
        # how much ENERGY it recorded while a main-meter session ran, and that
        # answer is right where its session power is not: sampling error
        # cancels in an integral. So every main session is also offered to the
        # raw readings, scored by how far the ratio sits from one.
        for mi, m in enumerate(mains):
            want = m.energy_wh
            span = m.duration_s
            if want <= 0 or span <= 0:
                continue
            for name in (n for n in self.sub_rows if n):    # the meters', not the grid's
                if name in phases and not set(m.phases) <= set(phases[name]):
                    continue
                # its channels that may carry the session's phases, each of
                # them - as a session is matched (_same_load): a 3EM's other
                # phases are other loads
                by_ph = [self._chans(name, ph) for ph in m.phases]
                if not all(by_ph):
                    continue
                rows = self._sub_total(name, sorted({c for cs in by_ph for c in cs}))
                # nothing the meter wrote after now, however far the pass reaches
                hi = bisect.bisect_right(rows, (self._now, math.inf))
                # A meter that held its value through the run's start did not
                # start it, whatever it drew later in the window: a pump
                # cycling every 20 minutes made up a 102 W, 47-minute run's
                # 60 Wh while its plug read 0.5 W from half an hour before to
                # three quarters of an hour after - and the run, and its
                # device's next ones, were filed as the hidrofor (2026-10-01).
                if any(self._meter_held(name, ph, m.start, True) for ph in m.phases):
                    continue
                got = energy_between(rows, m.start, m.end, hi)
                if got is None:
                    continue
                # What the device was drawing ANYWAY, over a window of the
                # same length just before. Without this a device that merely
                # happened to be running lands inside the band on coincidence:
                # a workshop meter already making 6 kW collects a heater's
                # 167 Wh without the heater being anywhere near it. The
                # detector matches edges everywhere else for the same reason.
                look = min(span, IDLE_WINDOW_S)
                before = energy_between(rows, m.start - look, m.start, hi)
                if before is None:
                    continue
                rose = got - before * (span / look)
                if rose <= 0:
                    continue
                # Both integrals are sample-and-hold over a reading that can
                # only express whole quanta, so each carries about one
                # quantum-hour of error and their difference carries two. A
                # rise inside that is not evidence of anything.
                floor = (ENERGY_MIN_QUANTA * self.sub_quantum(name) * span / 3600.0)
                if rose < floor:
                    continue
                ratio = rose * self.gain(name, "p") / want
                if ENERGY_MATCH_LO <= ratio <= ENERGY_MATCH_HI:
                    # slightly worse than a session match of the same quality,
                    # so a meter that CAN resolve the load still wins
                    pairs.append((0.5 + abs(ratio - 1.0), mi, name, None))
        return pairs

    def _session_pairs(self, mains: List[Run], main_iv: float) -> list:
        """(cost, main index, meter, sub index) for every house session and
        sub-meter session that could be the same load."""
        pairs = []
        phases = self.meter_phases()
        meters = []
        for name, subs in self.pending_sub.items():
            # the grid's reporting interval and the meter's latency, since a
            # step can land anywhere inside them, and never less than the
            # merge tolerance
            tol = self._pair_tol(name, main_iv)
            # its sessions in its own channels' names, read through the map
            mp, gain = self.phase_map(name), self.gain(name, "p")
            # by start, since only a session starting within tol can pair
            order = sorted(range(len(subs)), key=lambda i: subs[i].start)
            meters.append((name, subs, tol, mp, gain, order, [subs[i].start for i in order]))
        for mi, m in enumerate(mains):
            for name, subs, tol, mp, gain, order, starts in meters:
                if name in phases and not set(m.phases) <= set(phases[name]):
                    continue        # not on the phases this meter's one device uses
                near = order[bisect.bisect_left(starts, m.start - tol):bisect.bisect_right(starts, m.start + tol)]
                for si in sorted(near):
                    s = subs[si]
                    if name == m.placed:
                        # (the meter's session is NOT consumed by this pair - _decide keeps it
                        # for the grid's later pieces of the same run, see _owned: paired on
                        # time alone and popped, a short grid piece took the EVBox's whole
                        # charge and the rest could not pair - Polnilnica fed capture 83.9 ->
                        # 74.6 on the card, 2026-10-23)
                        # its start was this meter's own step: the meter's session
                        # starting with it is the run, whatever the grid's size
                        # (netted with another load: 1711 W against Mansarda's
                        # 2010, 13 Oct 12:53) or the grid's cut-short length
                        da, db = max(m.duration_s, 1.0), max(s.duration_s, 1.0)
                        if max(da, db) / min(da, db) <= CROSS_METER_DURATION_FACTOR:
                            pairs.append((abs(m.start - s.start) / max(tol, 1.0), mi, name, si))
                            continue
                    if _same_load(m, s, mp, gain, tol):
                        pairs.append((_match_cost(m, s, gain, tol), mi, name, si))
        return pairs

    def _meter_steps(self, ph: str, since: float, window: float, up: Optional[bool] = None,
                     size: Optional[float] = None) -> List[Tuple[str, float]]:
        """The pieces of a grid step on phase ``ph`` at ``since`` that the
        meters below it explain - see SPLIT_BY_METERS. Each meter measured to
        carry that phase gives its own declared step there, in the grid's
        terms (its gain), less what its own sub-meters stepped: Blaž PC's step
        counts once, and Hiša adds only what else inside it changed. The
        pieces do not overlap."""
        totals = self._meter_totals(ph, since, window, up)
        steps = {n: (m[0] if m[0] is not None else (size or 0.0)) for n, m in totals.items()}
        # served by FIT - timing confidence, then size - not by the meters' names:
        # alphabetical, the Hidrofor's 816 W (its plug reporting 9 s after the grid,
        # conf 0.10) was carved out of Mansarda's 1,879 W step first and Mansarda's
        # 1,854 (conf 0.30, the whole step) no longer fitted the rest (21 Oct
        # 13:37:01, 2026-10-23)
        return sorted(((name, d - sum(x for k, x in steps.items() if self._under(k, name)))
                       for name, d in steps.items()),
                      key=lambda nd: (-totals[nd[0]][3], -abs(nd[1])))

    def _hold_back(self, rows, q, pv, cut: float):
        """The grid's readings up to ``cut`` - wait_cap_s before how far the
        pass reaches, see process - with the last pass's held ones in front;
        the newer ones, and their reactive and PV values, are kept for the
        next pass. See METER_WAIT_CAP_S. The wait is the cap itself, not the
        slowest meter's sustain as the pass ended: the grid is read on the
        meters' clock, and a wait that moved with the pass moved it.

        The reactive and PV values go on as they came, the held ones behind
        them: the detector only looks them up at the readings it is handed,
        which are all at or before the cut. Copying both dicts every pass
        cost the replay, which hands over all ten days each time, five times
        its run on the live days."""
        held = self._carry
        out_rows, keep_rows = {}, {}
        for ph in set(rows or {}) | set(held.get("rows") or {}):
            both = sorted(list((held.get("rows") or {}).get(ph, [])) + [tuple(r) for r in (rows or {}).get(ph, [])])
            out_rows[ph] = [r for r in both if r[0] <= cut]
            keep_rows[ph] = [r for r in both if r[0] > cut]

        def split(new, old):
            if not new and not old:
                return new, {}
            out = {ph: ChainMap((new or {}).get(ph) or {}, (old or {}).get(ph) or {})
                   for ph in set(new or {}) | set(old or {})}
            keep = {ph: {t: m[t] for t, _ in keep_rows.get(ph, ()) if t in m} for ph, m in out.items()}
            return out, keep
        out_q, keep_q = split(q, held.get("q"))
        out_pv, keep_pv = split(pv, held.get("pv"))
        self._carry = {k: {ph: v for ph, v in d.items() if v} for k, d in
                       (("rows", keep_rows), ("q", keep_q), ("pv", keep_pv))}
        self._carry = {k: d for k, d in self._carry.items() if d}
        return out_rows, out_q, out_pv

    def _meter_stop(self, ph: str, a: float, b: float, opens: list, fall: Optional[float] = None):
        """The open run on grid phase ``ph`` that a meter's own fall over the
        grid fall's span [a, b] ends, on a channel that may carry the phase
        (_chans): of the runs the meter owns (_Open.meter) or rose for - its
        rise over the run's start (_steps_over) the run's size within the
        pairing tolerance, in the grid's terms, and its fall at least half of
        that - the one the fall accounts for best, once what the channel changed
        by from just before the run started to after this fall
        (_change_since) is below the run's size beyond the pairing tolerance -
        the run cannot still be running on less than it started with (the owner,
        2026-10-03) - and the fall is the run's size or leaves the channel too
        little to carry it. One rule for every meter (the unify audit,
        2026-10-03): a plug idling near nothing reads what it did before, and
        a strip's or a 3EM channel's other loads are left out - read
        absolutely, a circuit never fell below a small run's size.
        Whatever the grid's step measures: Home's pump started at +909 W,
        still settling, and stopped at -731 W while its plug fell 818 W to
        nothing; too unlike to pair by size, the fall closed a 689 W and a 118
        W run together instead, and the pump's ran on two hours (20.09 00:39).
        Each meter fall ends one run. A rise for it must be the run's, in size
        too: a boiler rise 41 s after the IR panel's start, two hours earlier,
        let a 2 kW boiler fall close the panel's 514 W run and book it at
        1,270 W (Kozolec 09-22 11:34, 2026-10-02). A ramp the meter's step and
        the grid's covered in different windows is its run too, placed by the
        grid's change over both (_window_size)."""
        if len(self._stops_used) > 1000:
            self._stops_used = {k: t for k, t in self._stops_used.items() if t > a - 86400.0}
        for name, det in self.subs.items():
            for c in self._chans(name, ph):
                if self._several(name, c):
                    continue                  # a total's fall is no one leg's stop - see _several
                st, gain = det.phases[c], self._leg_gain(name, c)
                for e in self._near(st, a, b, st.latency()):
                    if e[1] >= 0 or (name, e[0]) in self._stops_used or not self._on_phase(name, c, e, ph):
                        continue
                    if fall is not None and abs(fall) < 0.5 * -e[1] * gain:
                        # not through this fall: it does not account for half
                        # the meter's. A 263 W blip ending a second before the
                        # EVSE's 3.6 kW fall, the plug's own fall within reach
                        # of its span, took the EVSE's run and booked 23
                        # minutes at 257 W (Kozolec 09-28 13:53, 2026-10-02).
                        # Home's pump (-731 W for a plug fall of 818) passes.
                        continue
                    o = self._ended_by(name, c, ph, e, opens)
                    if o is not None:
                        return o
        return None

    def _ended_by(self, name: str, c: str, ph: str, e: tuple, opens: list, still: Optional[float] = None):
        """The open run on ``ph`` that meter ``name``'s fall ``e`` on channel
        ``c`` ends by _meter_stop's rule - and, asked ``still``, the channel
        still that far below where it stood then - marked used; None."""
        gain, grid = self._leg_gain(name, c), self.main.phases[ph]
        mine = []
        for i, o in enumerate(opens):
            if o.since >= e[0]:
                continue
            if o.meter != name:
                rises = [r[1] * gain for r in self._steps_over(name, c, ph, o.since, True)
                         if abs(r[1] * gain - o.watts) <= grid._tol(o.watts, r[1] * gain)]
                if not rises or -e[1] * gain < 0.5 * max(rises):
                    continue
            mine.append((abs(-e[1] * gain - o.watts), -i, o))
        if not mine:
            return None
        # the one its fall accounts for best, the newest of equals: newest
        # first, a plug's 19 W wobble that owned a run of its own took the
        # dehumidifier's 300 W stop; and only that one - a strip's older load
        # stopping leaves its newer one below what it read before it started
        o = min(mine, key=lambda x: x[:2])[2]
        size = o.watts                      # what it settled at, not a starting surge
        left = self._change_since(name, ph, c, o.since, e[4]) * gain
        tol = grid._tol(size, max(left, 0.0))
        # ...and the fall is of the run's size, or the channel reads too
        # little after it to carry the run at all: a circuit's other load
        # stopping took it below where it stood before the run started, the
        # run still on
        read = self._reading(name, c, e[4])
        its = (abs(-e[1] * gain - size) <= grid._tol(size, -e[1] * gain)
               or (read is not None and read * gain < size - tol))
        if left < size - tol and its and (
                still is None or self._change_since(name, ph, c, o.since, still) * gain < size - tol):
            self._stops_used[(name, e[0])] = e[0]
            return o
        return None

    def _meter_ended(self, ph: str, opens: list, frm: float, upto: float):
        """(run, when): a run a meter's own step started (_Open.meter) that
        the meter's fall in (``frm``, ``upto``] ends by _meter_stop's rule -
        the grid read past it by then, and no fall of its own took the run.
        Home's pump stopped at 11:20:08 (its plug -942 W) as a 3.2 kW load on
        its phase rose by about as much: the grid declared no fall, and the
        pump's 755 W run, freed by its plug's fall, stayed open two hours -
        1.5 kWh in the hidrofor's signature (09-22, 2026-10-03). Only if the
        meter still shows it stopped at ``upto``: Kozolec's car charger
        paused a minute at 15:02, too short for the grid to show, and its
        last 55 minutes went with the pause (09-20)."""
        for o in opens:
            name = o.meter
            det = self.subs.get(name) if name else None
            if det is None:
                continue
            for c in self._chans(name, ph):
                if self._several(name, c):
                    continue                  # see _several
                st = det.phases[c]
                lo = bisect.bisect_right(st.declared_t, max(frm, o.since + st.latency()))
                for e in st.declared[lo:bisect.bisect_right(st.declared_t, upto)]:
                    # ...a stop the meter timed: one whose span - its last
                    # reading at the old level to its first at the new - is
                    # within the merge tolerance. The workshop boiler's
                    # meter, writing every seven minutes, ended its runs a
                    # quarter of an hour early, 4 kWh of 23 (Home, 2026-10-03)
                    if (e[1] < 0 and e[4] - e[3] <= MERGE_TOLERANCE_S and (name, e[0]) not in self._stops_used
                            and self._on_phase(name, c, e, ph)):
                        got = self._ended_by(name, c, ph, e, opens, upto)
                        if got is not None:
                            return got, e[0]
        return None

    def _reading(self, name: str, ch: str, t: float, side: int = 0) -> Optional[float]:
        """What channel ``ch`` of meter ``name`` (a phase of the grid's, under
        "") read: in force at ``t`` (``side`` 0) - a silence being the value
        held, before its first row kept the value carried in (_sub_seed) -,
        just before ``t`` (-1), or first at or after it and not past the
        clock (1); None without."""
        rows = (self.sub_rows.get(name) or {}).get(ch) or []
        if side > 0:
            # ...never past the clock: the rows run to the pass's end, and a
            # reading the clock has not reached would be there in one slicing
            # and not in another (the unify audit, 2026-10-03)
            k = bisect.bisect_left(rows, (t, -math.inf))
            return rows[k][1] if k < len(rows) and rows[k][0] <= self._now else None
        k = bisect.bisect_left(rows, (t, -math.inf)) if side < 0 else bisect.bisect_right(rows, (t, math.inf))
        if k:
            return rows[k - 1][1]
        return (self._sub_seed.get(name) or {}).get(ch) if side == 0 else None

    def _grid_step(self, ph: str, since: float) -> Optional[tuple]:
        """The grid's own declared step on ``ph`` at ``since``, if it declared one there."""
        grid = self.main.phases.get(ph)
        if grid is None:
            return None
        return grid.declared_at(since)

    def _steps_over(self, name: str, c: str, ph: str, since: float, up: Optional[bool] = None) -> List[tuple]:
        """Channel ``c`` of meter ``name``'s declared steps (``up``'s way,
        either way with None) whose spans overlap the grid's own step on
        ``ph`` at ``since`` - [since, since] where the grid declared none
        there - and that were on ``ph`` (_on_phase). One window for every
        question of what a meter did at a grid step, the one _meter_totals
        asks it over (2026-10-01): a meter's span already reaches its
        latency back (PhaseState.span_start), and a moment padded by the
        latency counted it twice (the unify audit, 2026-10-03)."""
        st = self.subs[name].phases[c]
        g = self._grid_step(ph, since)
        a, b = (g[3], g[4]) if g else (since, since)
        return [e for e in self._near(st, a, b, st.latency())
                if (up is None or (e[1] > 0) == up) and self._on_phase(name, c, e, ph)]

    def _on_phase(self, name: str, c: str, e: tuple, ph: str) -> bool:
        """Was meter ``name``'s step ``e`` on channel ``c`` one on grid phase
        ``ph``? Where the map places the channel, it is where it is; where it
        places it nowhere yet, a step is one load's on one phase - the
        grid's that way over its span nearest its size, in the grid's terms.
        Asked about every phase, Home's Susilna plug - two votes in ten days,
        its 60 s cadence spanning three minutes - took starts and stops on
        all three for its own (the unify audit, 2026-10-03)."""
        if c in self.phase_map(name):
            return True
        size, best = e[1] * self.gain(name, "p"), None
        end = self._shifted(self._det(name).phases[c], e)[4]
        for p, grid in self.main.phases.items():
            for g in self._near(grid, e[3], end, grid.latency()):
                if (g[1] > 0) == (e[1] > 0) and (best is None or abs(g[1] - size) < best[0]):
                    best = (abs(g[1] - size), p)
        return best is None or best[1] == ph

    def _change_since(self, name: str, ph: str, c: str, since: float, t: float) -> float:
        """What channel ``c`` of meter ``name`` changed by, in its own terms,
        from just before a run on grid phase ``ph`` started at ``since`` -
        where the span of its rise for it begins (_steps_over), or its latency
        before the start where it showed none - to ``t``: its readings then,
        or its declared steps between where it has none."""
        st = self.subs[name].phases[c]
        rises = self._steps_over(name, c, ph, since, True)
        frm = min(e[3] for e in rises) if rises else since - st.latency()
        was, now = self._reading(name, c, frm), self._reading(name, c, t)
        if was is not None and now is not None:
            return now - was
        lo, hi = bisect.bisect_left(st.declared_t, frm), bisect.bisect_right(st.declared_t, t)
        return sum(e[1] for e in st.declared[lo:hi])

    def _meter_on(self, name: str, ph: str, since: float, size: float, t: Optional[float] = None) -> bool:
        """Does meter ``name``, whose own step started a grid run of ``size``
        on ``ph`` at ``since``, still show that load on at the grid's moment
        ``t`` - what its channels that may carry the phase (_chans) read then,
        against just before the run started (_change_since), in the grid's
        terms, at least half the run's size? Then, not on the clock: read
        ahead of the grid by the horizon, the plug of a 20-hour run already
        read its stop while the grid was at a 150 W load's fall three minutes
        before it, and that fall closed the run. A stop the grid has not yet
        reached is _meter_stop's, or _meter_ended's. Its net change, not any fall of half the
        size: a circuit's other loads coming and going, and a motor's surge
        settling (Susilna 09-24 18:00: 565 W, then 264), do not free the run
        (the unify audit's R4; Home fed devices 51.8 -> 63.3 % at SLICE=6).
        Read, not summed from its declared steps: Kozolec's water pump
        wandered +184, -17, -78, +74, +78 W, never down by half in declared
        steps, and held its run five hours while it drifted off (09-27,
        2026-10-03). See PhaseState.owned."""
        det = self.subs.get(name)
        chans = self._chans(name, ph) if det is not None else []
        if not chans:
            return False
        t = self._now if t is None else t
        return sum(self._change_since(name, ph, c, since, t) * self._leg_gain(name, c, ph, since) for c in chans) > 0.5 * size

    def _meter_started(self, ph: str, since: float, size: float) -> Optional[Tuple[str, float]]:
        """(meter, its rise in the grid's terms): the meter whose own rise -
        no bigger than the grid's start of ``size`` at ``since`` on ``ph``,
        within the pairing tolerance - came over the grid step's span
        (_steps_over), on a channel that may carry the phase (_chans); the
        largest such rise, and of rises alike the innermost meter's. Asked
        where the meters' own steps placed none (_step_meter): a meter the
        phase map does not place yet, its votes coming from runs that have to
        survive first (Home's Susilna plug, 0-10 of the 30 it needs in ten
        days), or one whose step and the grid's disagreed in size or span -
        the plug's 265 W inside a +313 W start with a fan (_split_rise).
        Every meter alike: a 3EM's channel owns a run through its rise as a
        plug does - the kiln's legs at Hiša (2026-10-03). Whether it
        holds one device does not matter - a strip's load starting shows on
        the strip and the grid alike, and the strip shows it stop. See
        PhaseState.owned."""
        grid = self.main.phases.get(ph)
        if grid is None:
            return None
        took: Dict[str, float] = {}
        for name, det in self.subs.items():
            for c in self._chans(name, ph):
                st, gain = det.phases[c], self._leg_gain(name, c, ph, since)
                for e in self._steps_over(name, c, ph, since, True):
                    rise = e[1] * gain
                    if rise > size + grid._tol(size, rise) or rise <= took.get(name, 0.0):
                        continue
                    # a rise the grid took as a step of its own - Kozolec's
                    # boiler pulsing within a breath of the EVSE's charge
                    # starting, its +1.9 kW a grid step 15 s later - is that
                    # step, not a part of this start
                    tol = math.hypot(grid.noise_at(), st.noise_at()) + METER_CAL_SLACK * rise
                    if any(abs(g[0] - since) > 0.01 and g[1] > 0 and g[1] >= rise - tol
                           for g in self._near(grid, e[3], self._shifted(st, e)[4], st.latency())):
                        continue
                    took[name] = rise
        if not took:
            return None
        top = max(took.values())
        alike = [n for n, r in took.items() if top - r <= grid._tol(top, r)]
        inner = [n for n in alike if not any(self._under(o, n) for o in alike if o != n)] or alike
        name = max(inner, key=lambda n: took[n])
        return name, took[name]

    def _meter_read(self, ph: str, since: float, size: float, up: bool, t: Optional[float] = None) -> Optional[str]:
        """The meter holding one device that owns a run open on grid phase
        ``ph`` (_Open.meter), started before the grid's step at ``since`` and
        on (_meter_on) both before the step and at the grid's moment ``t``
        after it - the run carried through it -, whose readings show that
        step - two of them, over the step's span widened by the meter's
        latency, ``size`` apart that way in the grid's terms, within the
        pairing tolerance - whether or not its own detector declared it. The
        device's dip,
        return or taper, not another load's step: a one-reading dip is under
        the meter's sustain, so its detector declares nothing, while the
        grid's faster cadence declares both edges (Home's EVBox 09-25, 3.45 ->
        1.30 -> 3.43 kW in one 10 s reading, 22 times in a charge). On before
        it too: a 65 W wobble Kozolec's EVSE rose by at 3.4 kW, still open
        after the charger stopped, took its restart for its own and booked
        it from the wobble's base, 0.4 of 3.9 kWh (10-01 15:2x). And only a
        step its runs open there can carry - together at least its size: a
        392 W piece of a ramp Home's EVBox owned on A took the 2.1 kW return
        of a 6 s dip for its own, and the charge's next 66 minutes, 2.3 kWh,
        went into no run (10-08 14:02, once its lags were learned sooner and
        the runs open there differed, 2026-10-14)."""
        grid = self.main.phases.get(ph)
        if grid is None:
            return None
        g = self._grid_step(ph, since)
        a, b = (g[3], g[4]) if g else (since, since)
        carried: Dict[str, float] = {}
        for o in grid.open_edges:
            if o.meter:
                carried[o.meter] = carried.get(o.meter, 0.0) + (o.now if o.now and o.now > 0 else o.watts)
        for o in grid.open_edges:
            name = o.meter
            if not name or size > carried[name] + grid._tol(size, carried[name]):
                continue
            if name not in self.subs or o.since >= a or not self.holds_one_device(name) \
                    or not self._meter_on(name, ph, o.since, o.watts, a) \
                    or not self._meter_on(name, ph, o.since, o.watts, t):
                continue
            lat = self._latency(name)
            for c in self._chans(name, ph):
                k = self._leg_gain(name, c, ph, since)
                rows = (self.sub_rows.get(name) or {}).get(c) or []
                lo, hi = bisect.bisect_right(rows, (a - lat, math.inf)), bisect.bisect_right(rows, (b + lat, math.inf))
                vals = [v for tv, v in rows[max(lo - 1, 0):hi] if tv <= self._now]
                for i, x in enumerate(vals):
                    for y in vals[i + 1:]:
                        d = (y - x if up else x - y) * k
                        if d > 0 and abs(d - size) <= grid._tol(size, d):
                            return name
        return None

    def _meter_level(self, name: str, ph: str, t: float, since: float) -> Optional[float]:
        """What meter ``name`` draws at ``t`` more than just before the run
        it owns started at ``since`` on ``ph`` (_change_since), in the grid's
        terms, on its channels that may carry the phase (_chans) - the size
        of that run (PhaseState.owned). Its change, not its level: a plug
        idles near its floor and the two agree, but a circuit's level is
        every load in it - a run Hiša owned was followed to all of its phase
        (the unify audit, 2026-10-03). Asked at the grid's reading, what it
        read then: its declared level is read ahead of the grid by the
        horizon and lags its own readings by its sustain. Kozolec's Scala2
        ramps 104-247 W, and its plug measures the ramp; the grid's level,
        followed while the run was alone, read a charger ramping up on the
        same phase as the Susilna plug's growth, 270 W to 5 kW (Home 09-26
        20:28, 2026-10-02). None without a channel."""
        det = self.subs.get(name)
        chans = self._chans(name, ph) if det is not None else []
        if not chans:
            return None
        return max(0.0, sum(self._change_since(name, ph, c, since, t) * self._leg_gain(name, c, ph, since) for c in chans))

    def _meter_wh(self, name: str, ph: str, o: "_Open", at: float) -> Optional[float]:
        """What run ``o`` on grid phase ``ph``, which meter ``name`` owns, drew
        until ``at``, Wh: what the meter's declared levels have changed by
        since just before its rise for the run (_steps_over), in the grid's
        terms (gain) - its whole rise from the run's start. The meter's
        levels, not its readings: a spike never became a level (raw readings
        integrated booked an 88 W, 13 s run at 1,040 W - exp2, 2026-10-03).
        Only a meter holding one device (holds_one_device): a circuit's
        change since is every load in it. None without the rise or its levels.
        Booked by its levels, a one-level run is the mean of its start and
        stop, or the smaller: Home's EVBox started a charge at 8.0 kW, drew
        10.6 for most of it and tapered at its end; Kozolec's water pump
        drifts 104-247 W.
        Every run the meter owns at a moment shares what it draws then, by
        their sizes - a leg of a three-phase charge a total-only meter reads
        its share: the meter's change since just before the lowest of them
        rose. Each booked at the meter's whole level, Home's EVBox's 09-25
        charge - a run its +1,255 W start opened beside ones its +736, +585
        and +661 W rises opened later and smaller ones down to 10 W - was
        booked 39.6 kWh for the 29.8 it drew (2026-10-04). The lowest, not the
        oldest: a 53 W wobble Kozolec's EVSE rose by at 3.5 kW was still open
        when the charger stopped and started again, and the restart, read from
        the wobble's base, was booked 65 Wh for 3.1 kWh (09-28 15:18). In the
        grid's terms by the meter's gain, not by the lowest run's start
        against its rise: a 92 W run the EVSE's 3.58 kW rise came with booked
        a 3.6 kW charge beside it at 132 Wh (09-30 14:00), and a run that
        started at the grid's first plateau of a ramp (Kozolec 09-30 09:30,
        2,545 W of the EVSE's 3,529) books what the meter drew, 5.83 kWh for
        5.8, not 4.18."""
        det = self.subs.get(name)
        if det is None or o.watts <= 0 or not self.holds_one_device(name):
            return None
        mine = self._run_levels(name, ph, o, at)
        if mine is None:
            return None
        kept = self._owned_runs.setdefault(name, [])
        kept[:] = [r for r in kept if r[2] > at - MAX_OPEN_S]
        runs = [(o, mine)]
        for p, x, end in [(p, x, math.inf) for p, st in self.main.phases.items() for x in st.open_edges] + kept:
            if x is o or x.meter != name or x.since >= at or end <= o.since:
                continue
            got = self._run_levels(name, p, x, min(end, at))
            if got is not None and set(got[2]) & set(mine[2]):
                runs.append((x, got, end))
        kept.append((ph, o, at))
        cuts = sorted({o.since, at} | {t for x, lv, *end in runs for t in (x.since, *end, *(m[0] for m in lv[1]))
                                         if o.since < t < at})

        def level(lv, t):                     # the run's meter change in force at t
            return lv[1][bisect.bisect_right(lv[1], (t, math.inf)) - 1][1]
        wh, gain = 0.0, self.gain(name, "p")
        for t0, t1 in zip(cuts, cuts[1:]):
            now = [r for r in runs if r[0].since <= t0 and (r[0] is o or r[2] > t0)]
            low = min(now, key=lambda r: (r[1][3], r[0].since))
            w = level(low[1], t0) * gain * o.watts / sum(r[0].watts for r in now)
            wh += max(0.0, w) * (t1 - t0)
        return wh / 3600.0

    def _run_levels(self, name: str, ph: str, o: "_Open", until: float):
        """(the rise of meter ``name`` for run ``o`` on grid phase ``ph``,
        [(when, the meter's change since just before that rise)] from the run's
        start - its rise, then each of its declared levels after the rise and
        before ``until`` -, the channels read, what they read just before the
        rise), in the meter's own terms, over its channels that may carry the
        phase (_chans); None without the rise or its levels."""
        det = self.subs[name]
        was, cur, marks, chans = {}, {}, [], []
        for c in self._chans(name, ph):
            st = det.phases[c]
            ups = self._steps_over(name, c, ph, o.since, True)
            if not ups or any(len(e) < 7 for e in ups):
                continue
            chans.append(c)
            first, last = min(ups, key=lambda e: e[0]), max(ups, key=lambda e: e[0])
            was[c] = first[6] - first[1]                 # the channel just before the run's rise
            cur[c] = last[6] - was[c]
            lo = bisect.bisect_right(st.declared_t, last[0])
            marks += [(e[0], c, e[6] - was[c]) for e in st.declared[lo:bisect.bisect_left(st.declared_t, until)]
                      if len(e) > 6]
        rise = sum(cur.values())
        if rise <= 0:
            return None
        out = [(o.since, rise)]
        for ts, c, change in sorted(marks):
            cur[c] = change
            out.append((max(ts, o.since), sum(cur.values())))
        return rise, out, chans, sum(was.values())

    def _meter_held(self, name: str, ph: str, since: float, up: bool) -> bool:
        """Did meter ``name`` hold its value through a step on grid phase
        ``ph`` at ``since``? As of the pass's end it has written nothing that
        moved for SUSTAIN_CADENCES of its cadence after the grid step - silence
        is its value held - has nothing pending, and declared no step that
        way over the grid step's span (_steps_over), on every channel that may
        carry the phase (_chans). Too soon to tell - a meter others hang under
        read its horizon behind them, not yet that far - or a meter measured
        on other phases: no."""
        det = self.subs.get(name)
        chans = self._chans(name, ph) if det is not None else []
        if not chans:
            return False
        g = self._grid_step(ph, since)
        end = g[4] if g else since
        read = self._now - self._wait_of(name)      # how far its own detector has read (_sync_views)
        for c in chans:
            st = det.phases[c]
            if read < end + st.latency() or st.pending:
                return False
            if self._steps_over(name, c, ph, since, up):
                return False                          # it stepped: a placement missed, not a silence
        return True

    def _step_meter(self, ph: str, since: float, size: float, up: bool) -> Optional[str]:
        """The innermost meter whose own declared step was all of a grid step
        on ``ph`` - within both meters' noise and METER_CAL_SLACK; of several,
        the one none of the others hangs under. Teaches the meter its gains."""
        main = self.main
        if ph not in main.phases:
            return None
        noise = main.phases[ph].noise_at()
        main.placement_window = None
        main.placement_step = None
        took, windows = {}, {}
        slack = lambda x: math.hypot(noise, noise_m) + METER_CAL_SLACK * x               # noqa: E731 - this meter's, x the size
        ramp = lambda win: abs(abs(win[3]) - abs(win[0])) <= slack(abs(win[0]))         # noqa: E731 - see _window_size
        totals = self._meter_totals(ph, since, main.event_window(), up)
        # every meter whose one clean step faced the grid's one step teaches its gain
        for n, (_, raw, noise_m, _) in totals.items():
            if (raw and (raw > 0) == up and size >= 10.0 * math.hypot(noise, noise_m)
                    and 1.0 / (1.0 + METER_GAIN_BOUND) <= size / abs(raw) <= 1.0 + METER_GAIN_BOUND):
                self._learn_gain(n, "p", size, raw)
        for n, (d, raw, noise_m, conf) in totals.items():
            if d is None:                 # all of the grid's step over the union of both spans
                if not self._meter_stepped(n, ph, since, 0.5 * size, up):
                    continue                # ...but its own steps are not half of it: not its
                took[n] = (size if up else -size, None, noise_m, conf)
                # ...and the ramp's whole size where the grid's first plateau
                # was only part of it - see _window_size
                win = self._window_size(n, ph, since, up, size)
                if win is not None and (win[0] > 0) == up and abs(win[0]) - size > slack(size) and ramp(win):
                    windows[n] = win
            elif d and (d > 0) == up and abs(abs(d) - size) <= slack(size):
                took[n] = (d, raw, noise_m, conf)
            elif d and (d > 0) == up and abs(d) > size and size >= 0.5 * abs(d) and raw:
                # the meter's one clean step is BIGGER than this grid piece: the grid
                # admitted only part of it (netted by another load's change, or the
                # inverter's swing between two polls - 22 Oct 13:35:32, +1,070 W on
                # Mansarda read ~720 on the grid); the piece is the meter's, booked at
                # the meter's own size (_placed_step) - as long as it is half of it, the
                # half _meter_stop demands of a fall: a 263 W blip a second before the
                # EVSE's 3.6 kW fall is not that fall (Kozolec 09-28 13:53)
                took[n] = (d, raw, noise_m, conf)
            elif d and (d > 0) == up:
                # unlike in size, but the two may have declared one ramp over
                # different stretches of it: the meter's change against the
                # grid's own over both (_window_size) - the grid's step whole,
                # not a part a meter's share was carved from (Kozolec 09-27
                # 08:17: the EVSE's 3,452 W, the boiler's 1,804 split off,
                # was the boiler's by the boiler's window), the window at
                # least the step
                win = self._window_size(n, ph, since, up, size)
                if win is not None and (win[0] > 0) == up and abs(win[0]) >= size - slack(size) and ramp(win):
                    took[n], windows[n] = (win[0], None, noise_m, conf), win
        inner = [n for n in took if not any(self._under(o, n) for o in took if o != n)]
        if not inner:
            return None
        meter = min(inner, key=lambda n: abs(abs(took[n][0]) - size))
        _, raw, noise_m, conf = took[meter]
        main.placement_conf = conf
        main.placement_window = windows.get(meter)
        main.placement_step = self._placed_step(ph, since, size, up, meter, took)
        return meter

    def _placed_step(self, ph: str, since: float, size: float, up: bool, meter: str, took: dict) -> Step:
        """The step a placement is: the grid's edge, the owner's own edges and
        those of every meter it hangs under (each saw the change), the owner's
        reading in the grid's terms as the change on ``ph`` - its one clean
        step where it faced one, else its edges' sum, else the grid's - and the
        grid's net on the other phases that moved with it (_moved)."""
        grid = self.main.phases[ph]
        edges = [StepEdge("", ph, since, size if up else -size, grid.noise_at())]
        own_since, own = since, None
        for name in took:
            if name != meter and not self._under(meter, name):
                continue
            det, gain = self.subs[name], self.gain(name, "p")
            es = [(c, e) for c in self._mapped(name, ph) for e in self._steps_over(name, c, ph, since, up)]
            for c, e in es:
                edges.append(StepEdge(name, ph, e[0], e[1] * gain, det.phases[c].noise_at()))
            if name == meter and es:
                own_since = min(e[0] for _, e in es)
                raw = took[meter][1]
                own = abs(raw) * gain if raw else abs(sum(e[1] for _, e in es)) * gain
        w = own if own else size
        changes = {ph: w if up else -w}
        others = [p for p in self.main.phases if p != ph]
        for p, (net, _) in self._moved(since, self._pair_tol(meter), others, up).items():
            changes[p] = net if up else -net
        return Step(edges, meter, own_since, changes)

    def _meter_stepped(self, name: str, ph: str, since: float, least: float, up: bool) -> bool:
        """Do the meter's own declared steps the way of a grid step at
        ``since`` - over its span (_steps_over), on its channels that may
        carry ``ph`` (_chans) - add up to ``least``, in the grid's terms?
        Asked of the "all of it" word of _meter_totals (the meter's net over
        the union of both spans is the grid's) with half the step: the
        hidrofor's plug rose 924 W as the grid rose 942, a 3EM's kiln stop was
        netted into the rise, which grew to 3980 W, and over the union the
        nets agreed - the plug owned a run its 924 W fall, less than half,
        could never release; 15 hours, 60 kWh (Home 09-24 00:54, 2026-10-02).
        Half, since a meter's stop frees its run at half the run's size
        (_meter_on), and Hiša's +2,240 is rightly all of a +2,800 start with a
        580 W load off in the same span."""
        det = self.subs.get(name)
        if det is None:
            return False
        total = sum(e[1] * self._leg_gain(name, c, ph, since) for c in self._chans(name, ph)
                    for e in self._steps_over(name, c, ph, since, up))
        return abs(total) >= least

    def _learn_gain(self, name: str, kind: str, grid: float, meter: float) -> None:
        if not grid or not meter:
            return
        acc = self.meter_gain.setdefault(name, {}).setdefault(kind, [0.0, 0.0])
        acc[1] += 1.0
        r = math.log(abs(grid) / abs(meter))
        acc[0] += (r - acc[0]) / min(acc[1], 200.0)      # the mean, read where no ring is stored
        acc.append(r)
        del acc[2:len(acc) - GAIN_RING]

    def gain(self, name: str, kind: str = "p") -> float:
        """What a meter's step is worth in the grid's terms - 1 until learned:
        the median of the ring of log ratios, the mean for a library from
        before the ring."""
        acc = (self.meter_gain.get(name) or {}).get(kind, [0.0, 0.0])
        mean, n = acc[0], acc[1]
        if n < METER_GAIN_MIN:
            return 1.0
        hit = self._gain_memo.get((name, kind))
        if hit is not None and hit[0] is acc and hit[1] == n and hit[2] == len(acc):
            return hit[3]
        if len(acc) > 2:
            ring = sorted(acc[2:])
            mean = ring[len(ring) // 2] if len(ring) % 2 else (ring[len(ring) // 2 - 1] + ring[len(ring) // 2]) / 2.0
        got = min(1.0 + METER_GAIN_BOUND, max(1.0 / (1.0 + METER_GAIN_BOUND), math.exp(mean)))
        self._gain_memo[(name, kind)] = (acc, n, len(acc), got)
        return got

    def _lead(self, st: "PhaseState") -> float:
        """How far ``st``'s readings run ahead of this fleet's main's: its lead
        less the main's (a view's main has one against the grid too), never
        below 0; none for the main's own."""
        if not st.lead or any(st is p for p in self.main.phases.values()):
            return 0.0
        return max(0.0, st.lead - max((p.lead for p in self.main.phases.values()), default=0.0))

    def _shifted(self, st: "PhaseState", e: tuple) -> tuple:
        """Meter channel ``st``'s declared step ``e`` with its span reaching
        its lead further on (_lead) - on the main's clock."""
        lead = self._lead(st)
        return e if not lead else e[:4] + (e[4] + lead,) + e[5:]

    def _near(self, st: "PhaseState", a: float, b: float, reach: float) -> List[tuple]:
        """``st``'s declared steps whose spans reach into [a, b] - a meter's
        reaching its lead further on (_lead)."""
        lead = self._lead(st)
        lo = bisect.bisect_left(st.declared_t, a - reach - lead)
        hi = bisect.bisect_right(st.declared_t, b + reach)
        return [e for e in st.declared[lo:hi] if e[3] <= b and e[4] + lead >= a]

    def _meter_totals(self, ph: str, since: float, window: float, up: Optional[bool] = None) -> Dict[str, tuple]:
        """Per meter measured to carry grid phase ``ph``: (its share of the grid
        step at ``since``, in the grid's terms - None when it is all of it -,
        the meter's own step raw where one clean step faces one, its noise,
        the timing confidence 0..1) - see UNION_CAP_WINDOWS."""
        out: Dict[str, tuple] = {}
        if ph not in self.main.phases:
            return out
        grid = self.main.phases[ph]
        mine = self._grid_step(ph, since)
        g_span = (mine[3], mine[4]) if mine else (since - window, since + window)
        noise_g, main_window = grid.noise_at(), self.main.event_window()
        for name, det in self.subs.items():
            chans = self._mapped(name, ph)
            if not chans:
                continue
            noise = max(det.phases[c].noise_at() for c in chans)
            cap = UNION_CAP_WINDOWS * max([main_window] + [det.phases[c].latency() for c in chans])
            a, b = g_span
            # each step its share on this one phase (_leg_gain): a total on
            # all three reads a third of its step here, and teaches its gain so
            share = {c: self._leg_gain(name, c, ph, since) / self.gain(name, "p") for c in chans}
            for _ in range(8):                       # the union, grown until it holds still
                ms = [self._shifted(det.phases[c], e if share[c] == 1.0 else e[:1] + (e[1] * share[c],) + e[2:])
                      for c in chans for e in self._near(det.phases[c], a, b, cap)]
                gs = self._near(grid, a, b, cap)
                na, nb = min([a] + [e[3] for e in ms + gs]), max([b] + [e[4] for e in ms + gs])
                if (na, nb) == (a, b) or nb - na > cap:
                    break
                a, b = na, nb
            gain = self.gain(name, "p")
            # its steps the grid step's way over the grid step's span - and one
            # the other way the grid did not take on its own, netted into this
            # reading: Home's pump stopping (-810 W on its plug) in the very
            # reading another 1.9 kW load started showed on the grid as one
            # +1,108 W rise, and the pump's run went on for minutes. One the
            # grid took as its own step stays out: the kiln's previous pulse
            # ending seconds before this one began (see e039b4c).
            # ...and one the other way only if the two spans truly overlap -
            # the meter's change can have happened while the grid's did. One
            # whose span merely touches the grid's plateau happened after it:
            # a boiler stop the grid had not yet shown its own fall for - read
            # ahead of the grid by the horizon - was carved out of an earlier
            # rise (Kozolec 09-22 11:33: closing its run at 26 s and opening a
            # phantom 2 kW start)
            # ...and one the grid has read to the end of: read ahead of the
            # grid by the horizon, a kiln stop whose span (a 3EM's 14 s
            # silence) covered a rise 12 s earlier was netted into the rise
            # while the grid's own fall for it was six seconds from being
            # read; the rise grew to 3980 W (Home 09-24 00:54, 2026-10-02)
            read = grid.last_ts if grid.last_ts is not None else float("inf")
            own = [e for e in ms if e[3] <= g_span[1] and e[4] >= g_span[0]
                   and (up is None or (e[1] > 0) == up
                        or (e[3] < g_span[1] and e[4] > g_span[0] and e[4] <= read
                            and not self._grid_took(grid, e, gain, noise_g, noise, cap)))]
            if not own:
                out[name] = (0.0, None, noise, 0.0)
                continue
            # timing: how much the grid step's span and the meter's overlap, of both together
            oa, ob = max(g_span[0], min(e[3] for e in own)), min(g_span[1], max(e[4] for e in own))
            ua, ub = min(g_span[0], min(e[3] for e in own)), max(g_span[1], max(e[4] for e in own))
            conf = max(0.0, ob - oa) / (ub - ua) if ub > ua else 1.0
            net_m, net_g = sum(e[1] for e in ms) * gain, sum(e[1] for e in gs)
            share = sum(e[1] for e in own) * gain
            if (net_g and (net_g > 0) == (share > 0) and abs(net_m - net_g) <= math.hypot(noise, noise_g) + METER_CAL_SLACK * abs(net_g)
                    and len(gs) > 1):
                share = None                         # over the union, the meter's net is the grid's: all of it
            clean = len(ms) == 1 and len(gs) == 1
            out[name] = (share, own[0][1] if clean else None, noise, conf)
        return out

    def _grid_took(self, grid: "PhaseState", e: tuple, gain: float, noise_g: float, noise_m: float,
                   reach: float) -> bool:
        """Did the grid declare a step of its own the way of the meter's step
        ``e``, over its span and big enough to hold it - within both meters'
        noise and METER_CAL_SLACK?"""
        size = abs(e[1]) * gain
        tol = math.hypot(noise_g, noise_m) + METER_CAL_SLACK * size
        return any((g[1] > 0) == (e[1] > 0) and abs(g[1]) >= size - tol for g in self._near(grid, e[3], e[4], reach))

    def _vote_phases(self, closed_sub: Dict[str, List[Run]], main_iv: float,
                     pool: List[Run]) -> None:
        """Each new single-channel session on a meter votes for the house phase
        whose single-phase session started with it at the same size - by size
        and moment only, never by label; the size its peak in the grid's
        terms, as sessions are matched (_same_load). A meter with one channel
        may also vote for the phase SET of a house session on several phases
        whose peaks add up to its own - see PHASE_MAP_MIN_VOTES. Every hook
        reads the map that comes of it (phase_map)."""
        for name, sessions in closed_sub.items():
            tol = self._pair_tol(name, main_iv)
            gain = self.gain(name, "p")
            votes = self.phase_votes.setdefault(name, {})
            energy = self.phase_energy.setdefault(name, {})
            total = len(self._read_chans(name)) == 1
            for s in sessions:
                if len(s.levels) != 1:
                    continue
                (own,), w = s.levels.keys(), s.max_w * gain
                best = None
                for m in pool:
                    if (len(m.levels) != 1 and not total) or abs(m.start - s.start) > tol:
                        continue
                    mw = m.max_w if len(m.levels) == 1 else sum(_peaks(m).values())
                    if abs(mw - w) > max(MATCH_POWER_REL * max(mw, w), MIN_NOISE_W):
                        continue
                    if best is None or abs(m.start - s.start) < abs(best.start - s.start):
                        best = m
                if best is not None:
                    house = "".join(sorted(best.levels))
                    row = votes.setdefault(own, {})
                    row[house] = row.get(house, 0) + 1
                    row = energy.setdefault(own, {})
                    row[house] = row.get(house, 0.0) + s.energy_wh * gain
                    self._votes_rev += 1

    def phase_map(self, name: str) -> Dict[str, str]:
        """Which house phase each of the meter's channels carries, from its
        votes - worked out again only once they have changed: every grid
        step asks every meter, and the answer moves only when a session
        votes (_vote_phases). Shared, so callers read it and never write.
        Nothing until the votes are PHASE_MAP_MIN_VOTES, or carry as much
        energy as that many of the site's do (_vote_wh_needed): a channel's
        label is what the meter calls it - a plug's "a" a placeholder,
        Mansarda's rotated - and taken for its phase, every plug not on A was
        read on A from its first vote to its thirtieth (the unify audit,
        2026-10-03). A channel not mapped yet may carry any phase (_chans).
        The map by count, unless it clearly loses on energy; else the map by
        energy, unless it clearly loses on count (PHASE_MAP_AGREE). A meter
        with one channel whose votes saw several phases at once often enough
        carries that set (PHASE_SET_MIN). A meter inside a circuit meter is
        mapped through it (_through_parent)."""
        got = self._through_parent(name)
        if got:
            return got
        votes = self.phase_votes.get(name) or {}
        energy = self.phase_energy.get(name) or {}
        # Kept while neither these votes nor the site's (_vote_wh_needed) have
        # changed: _votes_rev counts every vote cast, and the very dicts are
        # held, so tables put in place of them are read afresh. Rebuilding a
        # key from every vote to check was most of 26 million calls a Home
        # replay makes - 4 1/2 min of a 14 min profile.
        root = self._root or self
        rev = (self._votes_rev, root._votes_rev)
        hit = self._phase_maps.get(name)
        if hit is not None and hit[0] is votes and hit[1] is energy and hit[2] == rev:
            return hit[3]
        if (sum(sum(r.values()) for r in votes.values()) < PHASE_MAP_MIN_VOTES
                and sum(sum(r.values()) for r in energy.values()) < self._vote_wh_needed()):
            mp = {}
        else:
            # the phase sets one-channel meters voted for are candidates too
            # (PHASE_MAP_MIN_VOTES); a 3EM's votes hold single phases only
            sets = sorted(set(PHASES) | {p for r in list(votes.values()) + list(energy.values()) for p in r})
            by_n, by_wh = phase_mapping(votes, sets), phase_mapping(energy, sets)

            def holds(mp):
                return (support(votes, mp) >= PHASE_MAP_AGREE * support(votes, by_n)
                        and support(energy, mp) >= PHASE_MAP_AGREE * support(energy, by_wh))
            mp = by_n if holds(by_n) else by_wh if holds(by_wh) else {}
            if len(votes) == 1:
                (c, rn), = votes.items()
                re = energy.get(c) or {}
                many = {k: (rn.get(k, 0), re.get(k, 0.0)) for k in set(rn) | set(re) if len(k) > 1}
                if (sum(n for n, _ in many.values()) >= PHASE_SET_MIN * sum(rn.values())
                        or sum(wh for _, wh in many.values()) >= PHASE_SET_MIN * sum(re.values()) > 0):
                    mp = {c: max(many, key=lambda k: many[k])}
        if votes or energy:
            self._phase_maps[name] = (votes, energy, rev, mp)
        return mp

    def _through_parent(self, name: str) -> Dict[str, str]:
        """Meter ``name``'s map through the circuit meter it hangs in: its
        channel's place among the circuit's channels, by its votes in the
        circuit's own fleet (views), and theirs among the grid's; empty while
        either is not mapped. Against the whole house a small meter's
        sessions meet a coincidence of their size on every phase: Blaževa
        Soba, on Hiša's C, read a 33 / c 16 / b 14 at the grid (28 / 218 / 7
        Wh) and lost its map to a few votes either way, where against Hiša it
        read c 52 / a 7 / b 2 (792 / 7 / 1 Wh; Home, 2026-10-14)."""
        parent = self.parents.get(name)
        view = self.views.get(parent) if parent else None
        if view is None or view is self or name not in view.subs:
            return {}
        inner, outer = view.phase_map(name), self.phase_map(parent)
        hit = self._composed.get(name)
        if hit is None or hit[0] is not inner or hit[1] is not outer:
            mp = {c: "".join(sorted(set("".join(outer[x] for x in h))))
                  for c, h in inner.items() if all(x in outer for x in h)}
            hit = self._composed[name] = (inner, outer, mp)
        return hit[2]

    def _vote_wh_needed(self) -> float:
        """The energy PHASE_MAP_MIN_VOTES of the site's votes carry, on
        average - the grid's fleet's, every meter's; never while the site
        has cast fewer votes than that."""
        root = self._root or self
        key = (root.phase_votes, root.phase_energy, root._votes_rev)
        got = self._vote_wh
        if got is None or got[0] is not key[0] or got[1] is not key[1] or got[2] != key[2]:
            n = sum(sum(r.values()) for v in root.phase_votes.values() for r in v.values())
            wh = sum(sum(r.values()) for v in root.phase_energy.values() for r in v.values())
            got = self._vote_wh = key + (PHASE_MAP_MIN_VOTES * wh / n if n >= PHASE_MAP_MIN_VOTES else math.inf,)
        return got[3]

    def _locate(self, m: Run, final: bool = False) -> bool:
        """Credit a filed house session to the meter that saw it: the BEST
        fit of a meter's own session and the energy its readings account
        for, not the first that passes - first fit let a session that merely
        fitted take the meter's session a better one needed, and at Kozolec a
        boiler with its own meter and 367 sightings collected thirteen
        locations (2026-09-18). Tried when it is filed and once more
        MATCH_PATIENCE_S after its end, by when a slow meter has written past
        it."""
        sig = self.main.signature_of(m)
        if sig is None:
            return True
        main_iv = self._interval("")
        pairs = self._session_pairs([m], main_iv) + self._energy_pairs([m])
        if not pairs:
            return False
        _, _, name, si = min(pairs, key=lambda x: (x[0], x[1]))
        if si is not None:
            self.pending_sub[name].pop(si)
        sig.locations[name] = sig.locations.get(name, 0) + 1
        return True

    # ------------------------------------------------ names: any meter's signature may wear one
    # A signature is (meter, id), the grid's under "": a load inside a meter
    # holding several devices is that meter's own signature (_file_as), and
    # named there it is a named load like any other (2026-10-03).
    def detectors(self) -> List[Tuple[str, "Detector"]]:
        """Every meter's detector, the grid's under "" first, then by name."""
        return [("", self.main)] + sorted(self.subs.items(), key=lambda kv: kv[0])

    def signature(self, ref: Tuple[str, int]) -> Optional["Signature"]:
        """The signature ``ref`` is now, merges followed; None once gone."""
        det = self._det(ref[0])
        return det._sig(det._current(ref[1])) if det is not None else None

    def rename(self, ref: Tuple[str, int], name: Optional[str]) -> bool:
        det = self._det(ref[0])
        return det is not None and det.rename(ref[1], name)

    def family_head(self, ref: Tuple[str, int]) -> Optional["Signature"]:
        """The biggest signature of family ``ref`` = (meter, family id)."""
        det = self._det(ref[0])
        return det.family_head(ref[1]) if det is not None else None

    def rename_family(self, ref: Tuple[str, int], name: Optional[str]) -> bool:
        det = self._det(ref[0])
        return det is not None and det.rename_family(ref[1], name)

    def adopt(self, ref: Tuple[str, int]) -> Optional[str]:
        det = self._det(ref[0])
        return det.adopt(ref[1]) if det is not None else None

    def predecessor_of(self, ref: Tuple[str, int]) -> Optional["Signature"]:
        det = self._det(ref[0])
        return det.predecessor_of(ref[1]) if det is not None else None

    def names(self) -> Dict[str, List[Tuple[str, int]]]:
        """name -> every (meter, id) wearing it: one device, wherever it was seen."""
        out: Dict[str, List[Tuple[str, int]]] = {}
        for meter, det in self.detectors():
            for name, ids in det.names().items():
                out.setdefault(name, []).extend((meter, i) for i in ids)
        return out

    def entity_names(self) -> List[str]:
        """Every name a named load's entities exist for: the names the library wears and the names still
        waiting for their load after a rebuild (orphan_names). A rebuild that finds too few runs of a load in
        the recorder's days must not take its entities away: its meter keeps its reading, its power reads 0,
        and both go on until the load runs again and the name is back on it (2026-10-26)."""
        return sorted(set(self.names()) | {o["name"] for _, det in self.detectors() for o in det.orphan_names if o.get("name")})

    def _summed(self, per_detector) -> Dict:
        out: Dict = {}
        for _, det in self.detectors():
            for k, v in per_detector(det).items():
                out[k] = out.get(k, 0.0) + v
        return out

    def live_power(self, now_ts: float, span_s: float, marks: Dict[str, int],
                   shown: Dict[str, List[float]]) -> Dict[str, float]:
        """Watts each name draws now - see LIVE_NAME_SHARE. ``marks`` (meter ->
        the last session filed at the previous reading) and ``shown`` (name ->
        the starts of runs counted open then) are the previous reading's, and
        are updated to this one's. A load that several meters see is read off
        the one showing the most of it, not added up."""
        per: Dict[str, Dict[str, float]] = {}
        now_shown: Dict[str, List[float]] = {}
        for key, det in self.detectors():
            for name, since, w in det.live_runs(now_ts):
                row = per.setdefault(name, {})
                row[key] = row.get(key, 0.0) + w
                now_shown.setdefault(name, []).append(since)
            if span_s > 0:
                for name, start, wh in det.filed_after(marks.get(key, det.filed_n)):
                    if not any(abs(start - t) <= MERGE_TOLERANCE_S for t in shown.get(name, [])):
                        row = per.setdefault(name, {})
                        row[key] = row.get(key, 0.0) + wh * 3600.0 / span_s
            marks[key] = det.filed_n
        shown.clear()
        shown.update(now_shown)
        out = {name: 0.0 for name in self.names()}
        out.update({name: max(row.values()) for name, row in per.items()})
        ceiling = self.live_ceiling()
        return {name: min(w, ceiling.get(name, w)) for name, w in out.items()}

    def live_ceiling(self) -> Dict[str, float]:
        """name -> the most a read of it may show: LIVE_CAP_FACTOR times the
        highest of its signatures' means and level tops, over the signatures
        seen at least LIVE_NAME_MIN_RUNS times (all of them for a name that
        has none yet)."""
        top: Dict[str, List[Tuple[int, float]]] = {}
        for _, det in self.detectors():
            for sig in det.signatures:
                if sig.name:
                    top.setdefault(sig.name, []).append((sig.count, max(sig.watts, sig.high or 0.0)))
        out = {}
        for name, rows in top.items():
            seen = [w for n, w in rows if n >= LIVE_NAME_MIN_RUNS] or [w for _, w in rows]
            out[name] = LIVE_CAP_FACTOR * max(seen)
        return out

    def energy_by_name(self) -> Dict[str, float]:
        """Wh each NAME has used, each signature's from its own meter's sessions."""
        return self._summed(lambda d: d.energy_by_name())

    def hourly_by_name(self, name: str) -> Dict[int, float]:
        return dict(sorted(self._summed(lambda d: d.hourly_by_name(name)).items()))

    def take_older(self, name: str) -> Dict[int, float]:
        """hour start -> Wh the signatures wearing ``name`` kept from before
        they were named (Signature.older), now theirs no longer: their meter
        is owed it once, before its window."""
        out: Dict[int, float] = {}
        for _, det in self.detectors():
            for sig in det.signatures:
                if sig.name == name:
                    for hour, wh in sig.older.items():
                        out[hour] = out.get(hour, 0.0) + wh
                    sig.older = {}
        return out

    def older_to_dict(self) -> dict:
        """Every signature's older hours, meter -> id -> hour -> Wh: stored
        apart from the library, which a year of hours would swell."""
        out: Dict[str, dict] = {}
        for meter, det in self.detectors():
            for sig in det.signatures:
                hours = {str(h): _trim(wh, 1) for h, wh in sig.older.items() if wh >= 0.05 * TRIM}
                if hours:
                    out.setdefault(meter, {})[str(sig.id)] = hours
        return out

    def load_older(self, d: dict) -> None:
        """Hand stored older hours back, each to what its signature is now -
        merged into another since they were stored, or gone with it."""
        for meter, sigs in (d or {}).items():
            for sid, hours in sigs.items():
                sig = self.signature((meter, int(sid)))
                if sig is not None:
                    for h, wh in hours.items():
                        sig.older[int(h)] = sig.older.get(int(h), 0.0) + float(wh)

    def running(self, now: float) -> List[dict]:
        """Every load running now, ONCE: each grid run (legs started within
        MERGE_TOLERANCE_S are one, as Detector.active merges them) told
        through the meter its own step placed it on (_Open.meter) and that
        meter's run of it (_steps_over's rise -> the _Open opened at it). A
        meter's run never makes a row of its own: the grid's run of it is the
        row, so nothing is listed twice. Watts, phases and since are the
        grid run's. -> {name, ref, refs, watts, phases, since, where, tag}.
        The one answer to "is this load on" (see AGENTS.md, Naming page)."""
        legs = sorted(((ph, o, st.open_scale()) for ph, st in self.main.phases.items() for o in st.open_edges),
                      key=lambda x: x[1].since)
        runs: List[List[tuple]] = []
        for leg in legs:
            run = next((r for r in runs if abs(r[0][1].since - leg[1].since) <= MERGE_TOLERANCE_S
                        and r[0][1].meter == leg[1].meter), None)
            if run is None:
                runs.append([leg])
            else:
                run.append(leg)
        rows: Dict[str, dict] = {}
        out: List[dict] = []
        for run in runs:
            phases = "".join(sorted(ph for ph, _, _ in run))
            watts = sum(o.watts * k for _, o, k in run)
            since = min(o.since for _, o, _ in run)
            meter = run[0][1].meter
            one = bool(meter) and self.holds_one_device(meter)
            det, ref_meter, sig_ph, sig_w, where = self.main, "", phases, watts, None
            if meter and meter in self.subs and not one:
                sub = self.subs[meter]
                found: Dict[int, tuple] = {}      # id(_Open) -> (channel, _Open, scale): one meter run, however many rises point at it
                for ph, o, _ in run:
                    for c in self._chans(meter, ph):
                        if c not in sub.phases:
                            continue
                        for e in self._steps_over(meter, c, ph, o.since, True):
                            for x in sub.phases[c].open_edges:
                                if abs(x.since - e[0]) <= 0.01:
                                    found[id(x)] = (c, x, sub.phases[c].open_scale())
                if found:
                    det, ref_meter = sub, meter
                    sig_ph = "".join(sorted({c for c, _, _ in found.values()}))
                    sig_w = sum(x.watts * k for _, x, k in found.values())
                where = meter
            sid = det._guess(sig_ph, sig_w, now - since)
            sig = next((s for s in det.signatures if s.id == sid), None) if sid is not None else None
            if one:
                name = (sig.name if sig else None) or meter
            else:
                name = sig.name if sig else None
                if where is None:
                    where = most_specific(sig.locations, sig.count, self.parents) if sig else "main"
            ref = (ref_meter, sig.id) if sig else None
            row = {"name": name, "ref": ref, "refs": {ref} if ref else set(),
                   "watts": watts, "phases": phases, "since": since,
                   "where": None if one or where == "main" else where,
                   "tag": sig.guess().tag if sig and not name else ""}
            if name and name in rows:             # one device, one row
                r = rows[name]
                r["watts"] += watts
                r["since"] = min(r["since"], since)
                r["refs"] |= row["refs"]
                r["phases"] = "".join(sorted(set(r["phases"] + phases)))
                continue
            if name:
                rows[name] = row
            out.append(row)
        return out

    def active_by_name(self, now_ts: float) -> Dict[str, float]:
        """Watts on right now per NAME: the named rows of running() - the same
        answer the dashboard list gives, one load at a time, whatever else
        shares its phase."""
        return {r["name"]: float(r["watts"]) for r in self.running(now_ts) if r["name"]}

    def running_refs(self, now_ts: float) -> Set[Tuple[str, int]]:
        """(meter, id) of every signature believed on right now - running()'s."""
        return {ref for r in self.running(now_ts) for ref in r["refs"]}

    def name_descriptors(self) -> List[dict]:
        """Every name, as a reset carries it (Detector.name_descriptors) - a
        meter's with its meter, whose detector alone may take it back."""
        return self.main.name_descriptors() + [{**o, "meter": n} for n, det in sorted(self.subs.items())
                                               for o in det.name_descriptors()]

    def carry_names(self, descriptors: List[dict]) -> None:
        """Take names into a fresh fleet, each to its own meter's detector."""
        self.main.carry_names([d for d in descriptors if not d.get("meter")])
        for n in sorted({d["meter"] for d in descriptors if d.get("meter")}):
            self.subs.setdefault(n, Detector()).carry_names([d for d in descriptors if d.get("meter") == n])

    def namable(self, one_device, min_count: int, min_wh: float) -> Dict[str, List[Tuple[Tuple[str, int], "Signature"]]]:
        """What a person could name, by where it lives - ``where`` -> [((meter,
        id), signature)], biggest first by what it costs and whether its row
        says enough to recognise it (2026-09-18): not named yet, seen at
        least ``min_count`` times and used ``min_wh`` (2026-09-17: "as
        for signatures only seen once, dont show them") - or what a named one
        may have become, whatever its size.

        Each meter offers its own signatures under its name, the grid its own
        under "main": a load a meter below saw lives there - one holding one
        device (``one_device``) IS that device, one holding several offers it
        from its own library, seen alone on its circuit (2026-10-03:
        "shouldn't we display the most reliable sources"). A switch's stays
        with the switch. A meter's signature whose runs the grid files under a
        name the user gave there is that named load already (_file_as)."""
        groups: Dict[str, list] = {}
        for meter, det in self.detectors():
            if meter and one_device(meter):
                continue              # its own readings are that device (2026-09-28)
            twins = (self.identity.get(meter) or {}) if meter else {}
            for s in det.signatures:
                heir = det.predecessor_of(s.id) is not None
                if s.name or not (heir or (s.count >= min_count and s.energy_wh >= min_wh)):
                    continue
                twin = self.signature(("", twins[str(s.id)])) if str(s.id) in twins else None
                if twin is not None and twin.name:
                    continue
                where = most_specific(s.locations, s.count, self.parents)
                if where == "main":
                    where = meter or "main"
                elif where in self.subs:
                    continue          # a meter below that is read: its own library, or the device
                groups.setdefault(where, []).append(((meter, s.id), s))
        for rows in groups.values():
            rows.sort(key=lambda r: (-(r[1].energy_wh * (0.45 + 0.55 * r[1].recognisable)), -r[1].evidence))
        return groups

    def to_dict(self) -> dict:
        return {"main": self.main.to_dict(), "subs": {n: d.to_dict() for n, d in self.subs.items()},
                "volts": {n: {p: [[_trim(t, 3), _trim(v, 2)] for t, v in rows] for p, rows in byp.items()}
                          for n, byp in self.volts.items()},
                **self._state()}

    def _state(self) -> dict:
        """What a fleet knows beyond its detectors - a meter's own fleet is
        stored that way, its detectors being the grid's fleet's."""
        return {"views": {p: v._state() for p, v in self.views.items()} or None,
                "pending_main": [[t, s.to_dict()] for t, s in self.pending_main],
                "pending_sub": {n: [s.to_dict() for s in v] for n, v in self.pending_sub.items()},
                "phase_votes": self.phase_votes, "phase_energy": self.phase_energy,
                "inverter_votes": self.inverter_votes, "inverter_phases": self.inverter_phases,
                "unfiled": [[t, s.to_dict()] for t, s in self.unfiled], "identity": self.identity,
                "sub_last": self._sub_last, "wait": self._wait, "wait_at": self._wait_at,
                "meter_lag": self.meter_lag, "lag_from": self._lag_from, "step_votes_from": self._step_votes_from,
                "unvoted": [[t, n, final, s.to_dict()] for t, n, final, s in self._unvoted],
                "recent_main": [[t, s.to_dict()] for t, s in self._recent_main],
                "meter_gain": self.meter_gain,
                "sub_rows": {n: {c: [[_trim(t, 3), _trim(w, 1)] for t, w in rows] for c, rows in chans.items()}
                             for n, chans in self.sub_rows.items()},
                "sub_from": self._sub_from, "sub_seed": self._sub_seed, "aged_to": self._aged_to, "now": self._now,
                "stops_used": [[n, t, u] for (n, t), u in self._stops_used.items()],
                "owned_runs": {n: [[ph, o.as_list(), at] for ph, o, at in runs] for n, runs in self._owned_runs.items()},
                "repaired_to": self._repaired_to,
                "carry": {"rows": {p: [list(r) for r in v] for p, v in (self._carry.get("rows") or {}).items()},
                          **{k: {p: [[t, x] for t, x in v.items()] for p, v in (self._carry.get(k) or {}).items()}
                             for k in ("q", "pv")}}}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Fleet":
        f = cls()
        if not d:
            return f
        f.main = Detector.from_dict(d.get("main"))
        f.subs = {n: Detector.from_dict(v) for n, v in (d.get("subs") or {}).items()}
        timed = lambda rows: [(t, Run.from_dict(x)) for t, x in rows or []]  # noqa: E731 - (when, session)
        f.pending_main = timed(d.get("pending_main"))
        f.pending_sub = {n: [Run.from_dict(x) for x in v] for n, v in (d.get("pending_sub") or {}).items()}
        f.inverter_votes = {n: {c: dict(r) for c, r in v.items()} for n, v in (d.get("inverter_votes") or {}).items()}
        f.inverter_phases = {n: dict(m) for n, m in (d.get("inverter_phases") or {}).items()}
        f.phase_votes = {n: {p: dict(r) for p, r in v.items()}
                         for n, v in (d.get("phase_votes") or {}).items()}
        f.phase_energy = {n: {p: dict(r) for p, r in v.items()}
                          for n, v in (d.get("phase_energy") or {}).items()}
        f.unfiled = timed(d.get("unfiled"))
        f._sub_last = {n: dict(v) for n, v in (d.get("sub_last") or {}).items()}
        f._wait, f._wait_at = float(d.get("wait") or 0.0), d.get("wait_at")
        f.meter_lag = {n: [list(r) for r in v] for n, v in (d.get("meter_lag") or {}).items()}
        f._lag_from = d.get("lag_from")
        f.volts = {n: {p: [(t, v) for t, v in rows] for p, rows in byp.items()} for n, byp in (d.get("volts") or {}).items()}
        # -inf (nothing voted yet) is written as null by the store's JSON; None is "never"
        f._step_votes_from = {n: float(t) for n, t in (d.get("step_votes_from") or {}).items() if t is not None}
        f._unvoted = [(t, n, bool(final), Run.from_dict(x)) for t, n, final, x in d.get("unvoted") or []]
        f._recent_main = [(t, Run.from_dict(x)) for t, x in d.get("recent_main") or []]
        f.identity = {n: dict(v) for n, v in (d.get("identity") or {}).items()}
        f.meter_gain = {n: {k: list(x) for k, x in v.items()} for n, v in (d.get("meter_gain") or {}).items()}
        f._view_states = dict(d.get("views") or {})
        f.sub_rows = {n: {c: [tuple(r) for r in rows] for c, rows in chans.items()}
                      for n, chans in (d.get("sub_rows") or {}).items()}
        f._sub_from = {n: float(t) for n, t in (d.get("sub_from") or {}).items()}
        f._sub_seed = {n: dict(v) for n, v in (d.get("sub_seed") or {}).items()}
        f._aged_to = float(d.get("aged_to") or 0.0)
        f._now = float(d.get("now") or 0.0)
        f._stops_used = {(n, t): u for n, t, u in d.get("stops_used") or []}
        f._owned_runs = {n: [(ph, _Open.of(o), at) for ph, o, at in runs] for n, runs in (d.get("owned_runs") or {}).items()}
        f._repaired_to = d.get("repaired_to")
        carry = d.get("carry") or {}
        f._carry = {k: v for k, v in (
            ("rows", {p: [tuple(r) for r in v] for p, v in (carry.get("rows") or {}).items()}),
            *((k, {p: {t: x for t, x in v} for p, v in (carry.get(k) or {}).items()}) for k in ("q", "pv"))) if v}
        # a run's owner is the meter's own session, as its detector keeps it (_day_log, pending_sub, _unvoted):
        # stored apart it is a copy that the filing (its signature) never reaches
        mine = {(n, x.phases, x.start, x.end): x for n, det in f.subs.items() for x in det._day_log}
        mine.update({(n, x.phases, x.start, x.end): x for n, v in f.pending_sub.items() for x in v})
        mine.update({(n, x.phases, x.start, x.end): x for _, n, _, x in f._unvoted})
        mine.update({(n, x.phases, x.start, x.end): x for n, det in f.subs.items() for x in det.held})
        # ...and the house's runs the day keeps are the very ones still waiting for a meter's answer (pending_main,
        # unfiled, recent_main): what the meter's Wh does to one it does to the other
        day = {(x.phases, x.start, x.end): x for x in f.main._day_log}
        same = lambda x: day.get((x.phases, x.start, x.end), x)  # noqa: E731
        f.pending_main = [(t, same(x)) for t, x in f.pending_main]
        f.unfiled = [(t, same(x)) for t, x in f.unfiled]
        f._recent_main = [(t, same(x)) for t, x in f._recent_main]
        f.main.held = [same(x) for x in f.main.held]
        for m in f.main._day_log:
            if m.parent_signature and isinstance(m.parent_signature[1], Run):
                o = m.parent_signature[1]
                m.parent_signature = (m.parent_signature[0], mine.get((m.parent_signature[0], o.phases, o.start, o.end), o))
        return f


def _booked(s: Run, ph: str) -> List[Tuple[float, float, float]]:
    """A run's draw on ``ph`` as booked, [(from, to, W)]: its levels, scaled
    to the Wh it is booked at (Run.energy_wh's for the phase)."""
    lv = s.levels.get(ph) or []
    ends = [x[0] for x in lv[1:]] + [s.end]
    segs = [(max(t, s.start), min(e, s.end), w) for (t, w), e in zip(lv, ends) if w > 0 and min(e, s.end) > max(t, s.start)]
    drew = sum((t1 - t0) * w for t0, t1, w in segs) / 3600.0
    want = s.phase_wh(ph)
    return [(t0, t1, w * want / drew) for t0, t1, w in segs] if drew > 0 and want > 0 else []


def _hour_of(t: float, tz: float) -> int:
    """The clock hour ``t`` falls in, as Signature.hourly keys it."""
    return int(t - (t + tz) % 3600.0)


def _unbook(sig: "Signature", by_hour: Dict[int, float], tz: float) -> None:
    """Take Wh off a signature's hours, the hour-of-day and the weekday charts
    with them - never below nothing: what it spread was its runs' union."""
    for hour, wh in by_hour.items():
        if wh <= 0:
            continue
        if hour in sig.hourly:
            sig.hourly[hour] = max(0.0, sig.hourly[hour] - wh)
        loc = hour + tz
        h, d = int(loc % 86400.0 // 3600.0), int((loc // 86400.0 + 3) % 7)   # 1970-01-01 was a Thursday
        sig.hour_wh[h] = max(0.0, sig.hour_wh[h] - wh)
        sig.day_wh[d] = max(0.0, sig.day_wh[d] - wh)


def _floor_of(rows: Sequence[Tuple[float, float]], a: float, b: float, share: float) -> Optional[float]:
    """The level ``rows`` held at least ``share`` of [a, b), by time."""
    held = sorted((w, min(t2, b) - max(t1, a)) for (t1, w), (t2, _) in zip(rows, list(rows[1:]) + [(b, 0.0)])
                  if min(t2, b) > max(t1, a))
    span = sum(d for _, d in held)
    acc = 0.0
    for w, d in held:
        acc += d
        if acc >= share * span:
            return w
    return None


def _cap_day(det: "Detector", log: List[Run], rows: Dict[str, list], a: float, b: float) -> None:
    """The day's repair: on each phase, the day's runs never booked over what
    the phase drew above the day's floor (_floor_of, REPAIR_FLOOR_SHARE) and
    its noise (REPAIR_SLACK_NOISE). At each moment the excess is taken from
    the runs then on that no meter measured (Run.wh) before the measured
    ones, the one open longest first; each run's Wh on the phase, and its
    signature's hours, are what is left. A run booked at an EV charge's start
    for the whole tapering charge, or open hours past its stop until a floor
    fall (#16, #23, #26), keeps what the phase carried of it. Never adds: a
    run whose stop came too early keeps what it was booked (#37)."""
    tz = det.tz_offset_s
    for ph, rr in sorted(rows.items()):
        floor = _floor_of(rr, a, b, REPAIR_FLOOR_SHARE) if rr else None
        runs = [s for s in log if ph in s.levels]
        if floor is None or not runs:
            continue
        st = det.phases.get(ph)
        slack = REPAIR_SLACK_NOISE * max(st.noise if st is not None else 0.0, MIN_NOISE_W)
        measured = [ph in s.wh for s in runs]
        ev = sorted((t, up, k, w) for k, s in enumerate(runs) for t0, t1, w in _booked(s, ph)
                    for t, up in ((t0, 1), (t1, 0)))
        times = [t for t, _ in rr]
        pts = sorted(set(times) | {e[0] for e in ev})
        active: Dict[int, float] = {}
        off: Dict[int, Dict[int, float]] = {}
        ie = 0
        for u, v in zip(pts, pts[1:]):
            while ie < len(ev) and ev[ie][0] <= u:
                _, up, k, w = ev[ie]
                ie += 1
                active[k] = active.get(k, 0.0) + (w if up else -w)
                if active[k] <= 1e-9:
                    del active[k]
            j = bisect.bisect_right(times, u) - 1
            if not active or j < 0:
                continue
            over = sum(active.values()) - max(rr[j][1] - floor + slack, 0.0)
            hour = _hour_of(u, tz)
            # the runs no meter measured before the measured ones, and of each
            # the one open longest first: the longer a run has gone without
            # its stop, the likelier it went unseen
            for k in sorted(active, key=lambda k: (measured[k], runs[k].start, k)):
                if over <= 0:
                    break
                take = min(over, active[k])
                row = off.setdefault(k, {})
                row[hour] = row.get(hour, 0.0) + take * (v - u) / 3600.0
                over -= take
        for k, by_hour in sorted(off.items()):
            s = runs[k]
            was = s.phase_wh(ph)
            s.wh[ph] = max(0.0, was - sum(by_hour.values()))
            sig = det.signature_of(s) if s.parent_signature is None else None
            if sig is not None:
                _unbook(sig, by_hour, tz)


def _two_modes(lens: Sequence[float]) -> Optional[int]:
    """The index into the sorted lengths where they split in two modes (the cut maximising the between-class
    variance of the log-lengths), or None: each side needs SPLIT_MIN_RUNS runs and the sides' medians
    MATCH_DURATION_FACTOR apart. Over all of a family's lengths, not the last RUN_MEMORY: the gap rule of
    learn_split finds an outlier there, not a minority load's mode."""
    ordered = sorted(max(x, 1.0) for x in lens)
    n = len(ordered)
    if n < 2 * SPLIT_MIN_RUNS:
        return None
    logs = [math.log(x) for x in ordered]
    pre = [0.0]
    for x in logs:
        pre.append(pre[-1] + x)
    best = None
    for i in range(SPLIT_MIN_RUNS, n - SPLIT_MIN_RUNS + 1):
        lo, hi = pre[i] / i, (pre[n] - pre[i]) / (n - i)
        score = i * (n - i) * (hi - lo) ** 2
        if best is None or score > best[0]:
            best = (score, i)
    if best is None:
        return None
    i = best[1]
    if ordered[i + (n - i) // 2] / ordered[i // 2] < MATCH_DURATION_FACTOR:
        return None
    return i


def _densest_window(hours: Sequence[float]) -> Tuple[float, float]:
    """(start hour, share of ``hours``) of the HINT_HOURS-long stretch of the clock holding most of them."""
    best = (0.0, 0.0)
    for k in range(24):
        inside = sum(1 for h in hours if (h - k) % 24.0 < HINT_HOURS)
        if inside > best[1] * len(hours):
            best = (float(k), inside / max(len(hours), 1))
    return best


def _hint_split(det: "Detector") -> None:
    """The daily repair's hints that a family is two loads (HINT_*): the lengths of its runs are in two modes
    AND the rarer mode keeps to a few hours of the day. One hint alone never splits. The family's biggest
    signature then takes the length split learn_split would have set (split_s, split_side); runs on the other
    side are filed into a signature of their own, in the same family, from now on. Learning is untouched."""
    for fid in list(det.family_runs):
        rows = det.family_runs[fid]
        members = [x for x in det.signatures if x.family == fid]
        if not members:
            del det.family_runs[fid]
            continue
        head = max(members, key=lambda x: x.count)
        if len(rows) < HINT_MIN_RUNS or head.split_s is not None:
            continue
        ordered = sorted(rows)
        i = _two_modes([a for a, _ in ordered])
        if i is None:
            continue
        lo, hi = ordered[:i], ordered[i:]
        rare, rest = (lo, hi) if len(lo) < len(hi) else (hi, lo)
        start, share = _densest_window([h for _, h in rare])
        other = sum(1 for _, h in rest if (h - start) % 24.0 < HINT_HOURS) / max(len(rest), 1)
        if share < HINT_HOUR_SHARE or share < HINT_HOUR_LIFT * max(other, HINT_HOURS / 24.0):
            continue
        head.split_s = math.sqrt(ordered[i - 1][0] * ordered[i][0])
        head.split_side = 1 if len(hi) >= len(lo) else -1
        if det._home.get(head.id) is not None:
            det._rejudge.add(det._home[head.id])


def _period(starts: Sequence[float]) -> Optional[float]:
    """A regular cycler's period on the day: the median start-to-start gap of its runs, when they keep to it."""
    gaps = [y - x for x, y in zip(starts, starts[1:]) if y > x]
    if len(gaps) < RECOVER_MIN_GAPS:
        return None
    p = _median(gaps)
    if p <= 0 or _median([abs(g - p) for g in gaps]) > RECOVER_REGULAR * p:
        return None
    return p


def _residual(rr: Sequence[Tuple[float, float]], runs: Sequence[Run], ph: str) -> List[float]:
    """The readings on ``ph`` less what the day's runs are booked at, reading by reading."""
    ev = sorted((t, up * w) for s in runs for t0, t1, w in _booked(s, ph) for t, up in ((t0, 1), (t1, -1)))
    out, drawn, ie = [], 0.0, 0
    for t, w in rr:
        while ie < len(ev) and ev[ie][0] <= t:
            drawn += ev[ie][1]
            ie += 1
        out.append(w - max(drawn, 0.0))
    return out


def _find_cycles(times: Sequence[float], res: Sequence[float], g0: float, g1: float, watts: float, dur: float,
                 dur_tol: float, tol_w: float, win: float, quota: int) -> List[Tuple[float, float, float]]:
    """The cycles of a load (its size, its length) that start and stop inside g0..g1 of the residual ``res``:
    (start, end, Wh). A step is where the median of the readings after it stands ``watts`` above (or below) the
    median before it; a start pairs with the stop a length later that undoes it, the level between standing
    ``watts`` above the one before. Best pairs first, none over another."""
    def med(x0: float, x1: float) -> Optional[float]:
        i, j = bisect.bisect_left(times, x0), bisect.bisect_left(times, x1)
        return _median(res[i:j]) if j > i else None

    def edges(sign: int) -> List[Tuple[float, float, float]]:
        found = []
        for i in range(bisect.bisect_left(times, g0 - win), bisect.bisect_right(times, g1 + win)):
            t = times[i]
            before, after = med(t - win, t), med(t, t + win)
            if before is None or after is None:
                continue
            err = abs(sign * (after - before) - watts)
            if err <= tol_w:
                found.append((t, before, err))
        best: List[Tuple[float, float, float]] = []
        for e in found:
            if best and e[0] - best[-1][0] < win:
                if e[2] < best[-1][2]:
                    best[-1] = e
            else:
                best.append(e)
        return best

    falls = edges(-1)
    pairs = []
    for t0, base, e0 in edges(1):
        if t0 < g0:
            continue
        for t1, _, e1 in falls:
            if not (t0 + dur - dur_tol <= t1 <= t0 + dur + dur_tol) or t1 > g1:
                continue
            inside = med(t0 + win, t1 - win) if t1 - t0 > 2 * win else med(t0, t1)
            if inside is None:
                continue
            e2 = abs(inside - base - watts)
            if e2 > tol_w:
                continue
            i, j = bisect.bisect_left(times, t0), bisect.bisect_left(times, t1)
            held = [(max(times[k], t0), min(times[k + 1] if k + 1 < len(times) else t1, t1)) for k in range(i - 1 if i else 0, j)]
            wh = sum(max(res[k] - base, 0.0) * max(y - x, 0.0) for k, (x, y) in zip(range(i - 1 if i else 0, j), held)) / 3600.0
            want = watts * (t1 - t0) / 3600.0
            if want > 0 and RECOVER_ENERGY_LO * want <= wh <= RECOVER_ENERGY_HI * want:
                pairs.append((e0 + e1 + e2, t0, t1, wh))
    taken: List[Tuple[float, float, float]] = []
    for _, t0, t1, wh in sorted(pairs):
        if len(taken) >= quota:
            break
        if all(t1 <= x or t0 >= y for x, y, _ in taken):
            taken.append((t0, t1, wh))
    return sorted(taken)


def _recover_day(det: "Detector", log: List[Run], rows: Dict[str, list], a: float, b: float) -> None:
    """The day's repair, the other way from _cap_day: where a regular cycler's runs have a hole in them (RECOVER_*),
    its own steps are looked for in what the day's readings show beyond the runs booked, and the cycles found
    are booked on its hours (Signature.recovered, as well as hourly) and in det.recovered_log. Its learning -
    count, power, runs, edges - is never touched."""
    if not RECOVER_ON:
        return
    tz = det.tz_offset_s
    zone = timezone.utc if not tz else timezone(timedelta(seconds=tz))
    cache: Dict[str, Tuple[List[float], List[float]]] = {}
    for sig in det.signatures:
        ph = sig.phases
        if len(ph) != 1 or sig.count < RECOVER_MIN_RUNS or ph not in rows or not rows[ph]:
            continue
        mine = sorted((s for s in log if ph in s.levels and det.signature_of(s) is sig), key=lambda s: s.start)
        period, watts, dur = _period([s.start for s in mine]), sig.power.get(ph, 0.0), sig.duration_s
        if period is None or watts <= 0 or dur <= 0:
            continue
        starts = [a] + [s.start for s in mine] + [b]
        ends = [a] + [s.end for s in mine] + [b]
        pairs = [i for i in range(len(starts) - 1) if RECOVER_GAP_FACTOR * period < starts[i + 1] - starts[i] <= RECOVER_MAX_GAP * period]
        holes = [(ends[i], starts[i + 1]) for i in pairs]
        hole_starts = [(starts[i] if i > 0 else None, starts[i + 1] if i + 1 < len(starts) - 1 else None) for i in pairs]
        if not holes:
            continue
        if ph not in cache:
            cache[ph] = ([t for t, _ in rows[ph]], _residual(rows[ph], [s for s in log if ph in s.levels], ph))
        times, res = cache[ph]
        st = det.phases.get(ph)
        noise = st.noise_at(watts) if st is not None else MIN_NOISE_W
        tol_w = min(max(RECOVER_POWER_REL * watts, 1.5 * noise), 0.5 * watts)
        spacing = _median([y - x for x, y in zip(times, times[1:])]) or 1.0
        win = max(RECOVER_WINDOW_S, 3.0 * spacing)
        dur_tol = min(max(2.0 * sig.duration_mad, 0.25 * dur), 0.5 * dur)
        for (g0, g1), (xl, xr) in zip(holes, hole_starts):
            quota = int((g1 - g0) // period) + 1
            anchors = [x for x in (xl, xr) if x is not None]
            for t0, t1, wh in _find_cycles(times, res, g0, g1, watts, dur, dur_tol, tol_w, win, quota):
                # on its period: a whole number of them from a start known or already found, within RECOVER_SLOT
                if not any(abs(t0 - x) >= 0.75 * period and min((abs(t0 - x) % period), period - (abs(t0 - x) % period)) <= RECOVER_SLOT * period
                           for x in anchors):
                    continue
                anchors.append(t0)
                _book_recovered(sig, t0, t1, wh, zone)
                det.recovered_log.append(Run(phases=ph, start=t0, end=t1, levels={ph: [(t0, wh * 3600.0 / (t1 - t0))]},
                                             wh={ph: wh}, quality=0.5, signature_id=sig.id))
    del det.recovered_log[:-RECOVER_LOG_KEEP]


def _book_recovered(sig: "Signature", t0: float, t1: float, wh: float, zone) -> None:
    """``wh`` over t0..t1 into the hours and days it occupied, as Signature._spread does, and into ``recovered``."""
    watts = wh * 3600.0 / (t1 - t0)
    t = t0
    while t < t1:
        moment = datetime.fromtimestamp(t, zone)
        into = moment.minute * 60 + moment.second + moment.microsecond / 1e6
        step = min(t1 - t, max(3600.0 - into, 1.0))
        part = watts * step / 3600.0
        sig.hour_wh[moment.hour] += part
        sig.day_wh[moment.weekday()] += part
        hour = int(t - into)
        sig.hourly[hour] = sig.hourly.get(hour, 0.0) + part
        sig.recovered[hour] = sig.recovered.get(hour, 0.0) + part
        t += step


def _peaks(s: Run) -> Dict[str, float]:
    """The most each phase of a session drew."""
    return {ph: max((w for _, w in lv), default=0.0) for ph, lv in s.levels.items()}


def _match_cost(a: Run, b: Run, gain: float, tol_s: float) -> float:
    """How well these two sessions fit, smaller being better.

    Only ever asked of a pair that already passed ``_same_load``, which
    leaves every channel of ``b`` on a phase of ``a``; this is what decides
    which of several passing pairs is the real one."""
    ta, tb = a.max_w, b.max_w * gain
    biggest = max(abs(ta), abs(tb), 1.0)
    when = abs(a.start - b.start) / max(tol_s, 1.0)
    size = abs(ta - tb) / biggest
    da, db = max(a.duration_s, 1.0), max(b.duration_s, 1.0)
    length = abs(math.log(da / db))
    # the moment and the size are what two meters can agree on; the length is
    # what a busy main meter gets wrong, so it only breaks ties
    return when + size + 0.25 * length


def _same_load(a: Run, b: Run, mapping: Optional[Dict[str, str]] = None, gain: float = 1.0,
               tol_s: float = MERGE_TOLERANCE_S) -> bool:
    """Is the downstream session ``b`` - in its meter's own channel names,
    read through ``mapping`` (Fleet.phase_map) and ``gain`` - the same load as
    the main-meter session ``a``? Always the same moment; then the same size.

    One comparison for every meter (the unify audit, 2026-10-03; the owner: per
    phase, by the peak, 3EMs too): a channel the map places is compared on
    its phase, and the channels it places nowhere yet as one against the
    phases of ``a`` no placed channel claims - a plug's one channel, whose
    label is a placeholder, against all of them, which is how a device's
    phase gets learned for free. Every phase of ``a`` covered, and no placed
    channel on a phase ``a`` lacks."""
    # The START is the hard test: two meters seeing a load switch on at the
    # same instant, at the same size, are seeing the same load. The END is
    # not, and demanding it within the same fifteen seconds is what stopped
    # Kozolec placing anything - on a busy main meter a load's down-step can
    # pair with a different edge, so the session runs on. Measured there:
    # 443 boiler cycles started within a minute of a main-meter session and
    # their ends differed by 7 s at the median but 438 s at the third
    # quartile, so only 27 were accepted (2026-09-18).
    #
    # And ``tol_s`` is not a constant either, because two meters do not
    # report together: at Kozolec the GX publishes the inverter's output
    # every 5 s while the Shelly on a device publishes every 52, so a load
    # can be most of a minute old on one before it appears on the other. The
    # caller derives it from what each meter actually does.
    if abs(a.start - b.start) > tol_s:
        return False
    # They still have to be roughly the same LENGTH of thing, but the bound
    # is wide: dropping it entirely placed NOTHING, because a loose test with
    # first-fit let a wrong pairing eat the session the right one needed.
    # With best-fit scoring behind it a wide bound is safe, and it has to be
    # wide - see CROSS_METER_DURATION_FACTOR.
    da, db = max(a.duration_s, 1.0), max(b.duration_s, 1.0)
    if max(da, db) / min(da, db) > CROSS_METER_DURATION_FACTOR:
        return False
    # The PEAK, not the energy-weighted mean. A meter slower than the load
    # it watches dilutes that mean with the part of a sample where the
    # load was off: Kozolec's boiler runs 66 seconds and its Shelly
    # reports every 52, so its own sessions measured 953 W against the
    # 1813 W the main meter saw - a factor of two, and the size test threw
    # out 343 of 381 otherwise-good pairs on it (2026-09-18). What
    # a load PEAKS at survives coarse sampling; what it averages does not.
    pa, pb, mp = _peaks(a), _peaks(b), mapping or {}
    placed: Dict[str, float] = {}            # a phase, or the phases one channel carries together -> its peak
    for c, w in pb.items():
        if c in mp:
            placed[mp[c]] = placed.get(mp[c], 0.0) + w
    rest = [w for c, w in pb.items() if c not in mp]
    free = [ph for ph in pa if not any(ph in k for k in placed)]
    # a channel on a set carries any part of it - Home's EVBox charging on
    # L1 alone (PHASE_SET_MIN) - but on none of it is another load
    if any(not any(ph in pa for ph in k) for k in placed) or bool(free) != bool(rest):
        return False
    for x, y in [(sum(pa.get(ph, 0.0) for ph in k), w) for k, w in placed.items()] + \
            ([(sum(pa[ph] for ph in free), sum(rest))] if free else []):
        y *= gain
        if abs(x - y) > max(MATCH_POWER_REL * max(x, y), MIN_NOISE_W):
            return False
    return True


# How negative a house may idle before something is plainly wrong with the
# reading rather than with the house. A little below zero is ordinary - the
# arithmetic is a difference of meters that do not sample together, and a
# reading can dip briefly - but a house does not DRAW minus a kilowatt.
IMPLAUSIBLE_BASELINE_W = -400.0


def implausible_baseline(baselines: Dict[str, float]) -> List[str]:
    """Phases whose idle floor says the reading is not house consumption.

    A load reading is what the house DRAWS, so its quiet floor is a small
    positive number. When it settles deeply negative the reading is something
    else wearing that name - most often a grid meter that reports import as
    negative, or one with generation still in it and no inverter configured to
    take it back out. Home settled at -6318, -4554 and -4340 W on its three
    phases and detected loads in that for days without a word (2026-09-22).

    Worth saying out loud precisely because nothing breaks: sessions still
    open and close, signatures still form, and every one of them is nonsense.
    """
    return sorted(p.upper() for p, v in (baselines or {}).items()
                  if v is not None and v <= IMPLAUSIBLE_BASELINE_W)


def drop_stale_load_override(detection: dict) -> dict:
    """Remove a load reading that is really the grid meter, filed twice.

    ``power_a`` and friends mean "this reading already IS the house" and win
    outright over the grid-plus-inverters arithmetic - a dedicated CT, or a
    template someone built before any of this existed. When setup became three
    pages, the flat fields from before stayed where they were, and nothing
    offers them any more: the Grid connection page keeps every key it does not
    own, so a meter configured before the change sits in BOTH places and the
    older copy quietly wins. At the owner's house that meant the grid meter being
    read as the house - sign inverted, solar never added back, the detector
    settling on a baseline of minus six kilowatts - while the pages he had just
    filled in did nothing (2026-09-22).

    Only the unambiguous case: the same entity in both roles is a duplicate,
    not a choice. A genuinely different house reading is left alone, because
    that one is the feature working as intended.
    """
    out = dict(detection)
    for p in PHASES:
        load, grid = out.get(f"power_{p}"), out.get(f"grid_power_{p}")
        if load and grid and load == grid:
            for kind in ("power", "pf", "current", "voltage"):
                if out.get(f"{kind}_{p}") == out.get(f"grid_{kind}_{p}"):
                    out.pop(f"{kind}_{p}", None)
    return out


def offer_for_naming(worth: Sequence["Signature"], named: int, min_evidence: float,
                     min_rows: int, start_rows: int, rows_per_name: int,
                     is_heir=None) -> List["Signature"]:
    """Which of the namable signatures to put in front of someone, and how
    many.

    Two separate questions, and only the first is about the loads. WHICH is
    evidence: a house makes far more shapes than it has appliances, so a
    signature has to have repeated, and repeated tightly, before it is worth
    anyone's attention - but a bar that hides everything is worse than one set
    too low, so when too few clear it the best of the rest come along.

    HOW MANY is about the person. No threshold answers it: set high it hides a
    big house's real loads for ever, set low it opens with two hundred rows and
    is put down unread. So the page opens with a handful and lengthens each
    time one is named - the right number of rows being a property of how much
    work someone has already done rather than of their house (the owner,
    2026-09-22). Nothing is hidden for good; the library keeps every signature
    and naming one brings more.
    """
    clear = clears_for_naming(worth, min_evidence, min_rows, is_heir)
    return clear[:max(start_rows + named * rows_per_name, min_rows)]


def clears_for_naming(worth: Sequence["Signature"], min_evidence: float,
                      min_rows: int, is_heir=None) -> List["Signature"]:
    """WHICH of the namable signatures are worth someone's attention - the
    first of offer_for_naming's two questions, split out because the page has
    to say how many are waiting behind it.

    It said nothing: the caller had only the already-shortened list, so it
    subtracted that list from itself and told every user "0 more than fit
    here" - Kozolec showing 6 of 12 and Home 14 of 60 (the owner's screenshots,
    2026-09-22). A page that lengthens as you name things has to be able to
    promise there is something to lengthen INTO, and that promise was reading
    as "this is all there is"."""
    clear = [s for s in worth
             if s.evidence >= min_evidence or s.name or (is_heir and is_heir(s))]
    if len(clear) < min_rows and len(clear) != len(worth):
        rest = sorted((s for s in worth if s not in clear), key=lambda s: -s.evidence)
        clear = clear + rest[:min_rows - len(clear)]
    return clear


def suggest_levels(signatures: Sequence[Signature], recent: Sequence[dict]) -> List[List[int]]:
    """Signatures that look like different settings of ONE device.

    A hob on three settings looks like three signatures: same phases, the
    same power factor, and - because it is one appliance - never two of them
    running at once. That is the whole test; the sizes are deliberately not
    compared, since settings can be any ratio. It is only a suggestion, and
    confirming it means giving them the same name."""
    # Each signature's OWN record first, and the rolling session list on top
    # of it. The list alone was the whole evidence and it is far too short to
    # answer this at a busy house: 40 sessions over 2.1 hours holding 11 of
    # Home's 199 signatures, so the kiln - fired 8.6 days ago, and split
    # across sixteen balanced A+C signatures holding 304 sessions - could
    # never be compared with a single one of its own settings.
    times: Dict[int, List[Tuple[float, float]]] = {
        sig.id: list(sig.runs) for sig in signatures if sig.runs}
    for r in recent:
        times.setdefault(r["signature"], []).append((r["start"], r["end"]))

    def overlap(x: int, y: int) -> bool:
        for s1, e1 in times.get(x, ()):
            for s2, e2 in times.get(y, ()):
                if s1 < e2 and s2 < e1:
                    return True
        return False

    def compatible(a: Signature, b: Signature) -> bool:
        # A NAMED signature belongs in this: naming one setting of a device is
        # the moment its owner proved it real, and excluding it from the pool
        # switched off the very suggestion that would have found the other
        # fifteen settings of the same machine (the owner's kiln, 2026-09-22).
        # Two loads named DIFFERENTLY were told apart on purpose.
        if a.name and b.name and a.name != b.name:
            return False
        # Identical phase sets, deliberately. A device with two elements does
        # draw on A alone, on C alone and on both - the owner's kiln does exactly
        # that, 3031 W on A and 2680 W on C being the 5918 W on A+C it is
        # named for - so allowing one set inside another was tried. It groups
        # the kiln, and it also groups a 156 W load with it, because this
        # rule compares no sizes at all and a subset relation removes the only
        # thing that was holding it. Recognising one device across phase sets
        # wants size arithmetic this does not do (2026-09-22).
        if a.phases != b.phases:
            return False
        if (a.pf is None) != (b.pf is None):
            return False
        if a.pf is not None and abs(a.pf - b.pf) > pf_tolerance(a.pf_mad, b.pf_mad):
            return False
        # Sizes are not compared - a setting can be any fraction of another -
        # but DURATION is a different question, and leaving it out was what
        # let a 178 W thing and a 2.7 kW one be called one device. A hob on
        # three settings boils the same pan for about as long each time; what
        # differs is the power. At the owner's house this is exactly the line
        # between the kiln's elements, all firing for 23 to 51 seconds, and
        # the three other loads on the same phases and factor that run for
        # 106, 203 and 517 (2026-09-22).
        ratio = max(a.duration_s, 1.0) / max(b.duration_s, 1.0)
        if ratio > MATCH_DURATION_FACTOR or ratio < 1.0 / MATCH_DURATION_FACTOR:
            return False
        # Sizes are not compared CLOSELY - a setting really can be any
        # fraction - but they cannot be ignored either, which is what this
        # did. A 165 W load was offered as a setting of the 5.9 kW kiln, and
        # a 100 W one as a setting of a 2.9 kW load: at 36 to 1 that is not a
        # setting, it is another device that happens to run for about as long.
        # The kiln's own real span is 5918 W down to about 1045, near six to
        # one, so the bound leaves it room and still cuts both (2026-09-22).
        big, small = sorted((sum(abs(w) for w in a.power.values()),
                             sum(abs(w) for w in b.power.values())))[::-1]
        if small <= 0 or big / small > MATCH_LEVEL_RATIO:
            return False
        # "Never two of them at once" has to be OBSERVED. The session list is
        # finite - two hundred against a library several times that at a busy
        # house - so for most pairs there is nothing recorded either way, and
        # reading that silence as "they never overlap" is what let a 149 W
        # thing and a 2.7 kW one be offered as one device (the owner's house,
        # 2026-09-22). Both sides have to have been seen before their not
        # having been seen together means anything.
        if not times.get(a.id) or not times.get(b.id):
            return False
        return not overlap(a.id, b.id)

    pool = [s for s in signatures if s.count >= 2]
    groups: List[List[Signature]] = []
    for sig in sorted(pool, key=lambda x: -x.count):
        for g in groups:
            if all(compatible(sig, m) for m in g):
                g.append(sig)
                break
        else:
            groups.append([sig])
    # A group needs something to DO: at least one signature still unnamed.
    # A named one may anchor it - that is the whole point, since "these
    # fifteen belong to Peč za Glino" is the useful sentence - but once every
    # member is named the matter is settled and repeating it is noise.
    return [sorted(x.id for x in g) for g in groups
            if len(g) > 1 and any(not x.name for x in g)]


def input_groups(signatures: Sequence[Signature]) -> List[List[int]]:
    """Loads tied to values of the SAME setting - a washer's heater in its
    wash phase, its drum in its spin - as one device's parts or settings.
    Unlike suggest_levels this needs no likeness between them: a heater and
    a drum motor share nothing but the machine, which the setting names."""
    by_setting: Dict[str, List[int]] = {}
    for sig in signatures:
        got = sig.strongest_input()
        if got:
            by_setting.setdefault(got[0], []).append(sig.id)
    return [sorted(ids) for ids in by_setting.values() if len(ids) > 1]


def most_specific(locations: Dict[str, int], count: int, parents: Optional[Dict[str, Optional[str]]] = None) -> str:
    """Which meter a signature belongs to, given the Energy dashboard's
    nesting. A load seen by both the workshop's meter and the boiler's is the
    BOILER's - the deepest meter that saw it, not the widest. ``parents`` maps
    a meter to the meter it sits inside (``included_in_stat``)."""
    seen = {n for n, k in locations.items() if k * 2 >= count}
    if not seen:
        return "main"
    parents = parents or {}

    def ancestors(name):
        out, cur = [], parents.get(name)
        while cur and cur not in out:
            out.append(cur)
            cur = parents.get(cur)
        return out

    deepest = [n for n in seen if not any(n in ancestors(m) for m in seen if m != n)]
    # a switch that saw it says exactly when it runs - one device, and deeper
    # than any circuit it sits in (the floor mat is in Hiša AND on its thermostat)
    switched = sorted(n for n in (deepest or seen) if n.startswith(SWITCH_PREFIX))
    if switched:
        return switched[0]
    return sorted(deepest or seen)[0]


_BARS = " ▁▂▃▄▅▆▇█"


SYSTEM_PHASES = dict(zip(PHASES, "XYZ"))


def display_phase(label: str) -> str:
    """A grid meter's own phase as the system phase it is, with the meter's label
    beside it: "a" -> "X (L1)". The grid's phases ARE the system phases X, Y, Z
    (steps-and-claims plan, stage 1): the detector keeps the grid's own labels
    as its keys, because they are also the config fields (power_a ...) and every
    detector's channel keys; only what a person reads says X, Y, Z."""
    i = PHASES.index(label)
    return f"{SYSTEM_PHASES[label]} (L{i + 1})"


def on_phases(phases: str) -> str:
    """"on phase A", "on phases A and C", "on all three phases" - read
    rather than decoded. "(A)" was what the naming page said (the owner,
    2026-09-23)."""
    ps = [p.upper() for p in phases]
    if len(ps) == 1:
        return f"on phase {ps[0]}"
    if len(ps) == len(PHASES):
        return "on all three phases"
    return "on phases " + ", ".join(ps[:-1]) + f" and {ps[-1]}"


def same_device_phrase(others: Sequence["Signature"], limit: int = 2) -> str:
    """What the naming page says about a load that may be another setting of
    the same device: the OTHER loads, described, since the rows they are on
    are often not on the page at all. "set 4 of one device" said nothing to
    anyone looking at five rows in five different sets (2026-09-23)."""
    if not others:
        return ""
    named = [f"the {_fmt_w(o.watts)}, {_fmt_s(o.duration_s)} load" for o in others[:limit]]
    more = len(others) - len(named)
    words = " and ".join(named) if len(named) <= 2 else ", ".join(named)
    if more:
        words += f" and {more} more"
    return f"maybe the same device as {words}"


def sparkline(counts: Sequence[float]) -> str:
    """One line of bars, for a place that has only one line - a menu row.

    Each row of a flow menu is a single label, so the shape of the day has
    to fit on it or not appear at all (2026-09-18)."""
    top = max(counts) if counts else 0
    if top <= 0:
        return ""
    return "".join(_BARS[0] if not c else _BARS[min(8, max(1, round(8 * c / top)))] for c in counts)


HISTOGRAM_ROWS = 5
HISTOGRAM_COL = 2              # characters per hour, so the day is 48 wide


def _blocks(counts: Sequence[float], rows: int, width: int) -> List[str]:
    """``counts`` as columns of blocks ``rows`` tall and ``width`` wide,
    half-blocks for the halves, over an axis - nothing when all are zero."""
    top = max(counts) if counts else 0
    if top <= 0:
        return []
    out = []
    for r in range(rows, 0, -1):
        line = []
        for c in counts:
            level = (c / top) * rows
            line.append(("█" if level >= r else "▄" if level >= r - 0.5 else " ") * width)
        out.append("|" + "".join(line))
    out.append("+" + "-" * (len(counts) * width))
    return out


def hour_histogram(counts: Sequence[int], rows: int = HISTOGRAM_ROWS,
                   width: int = HISTOGRAM_COL) -> List[str]:
    """The day as a block chart, for a form that renders markdown.

    A config flow cannot draw a graph. It can print one: 24 columns two
    characters wide, five rows tall, half-blocks for the halves - which says
    a good deal more than one line of sparkline did.
    """
    out = _blocks(counts, rows, width)
    if not out:
        return out
    ruler = [" "] * (len(counts) * width)
    for h in range(0, len(counts), 3):
        for i, ch in enumerate(str(h)):
            if h * width + i < len(ruler):
                ruler[h * width + i] = ch
    out.append(" " + "".join(ruler))
    return out


DAY_NAMES = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")


def day_histogram(counts: Sequence[int], rows: int = 3, width: int = 3) -> List[str]:
    """The week as a block chart, Monday first.

    Which DAYS a load runs on separates a washing machine from a dishwasher
    far better than the hour does, and the hour histogram alone could not
    show it (2026-09-17)."""
    out = _blocks(counts, rows, width)
    if out:
        out.append(" " + "".join(name[:width].ljust(width) for name in DAY_NAMES[:len(counts)]))
    return out


def _and(names: Sequence[str]) -> str:
    names = list(names)
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


def describe_location(locations: Dict[str, int], count: int,
                      parents: Optional[Dict[str, Optional[str]]] = None,
                      phases: str = "") -> str:
    """Where the load is, said by EXCLUSION.

    "In the house" is worth little when the house meter covers everything.
    What narrows it is what did NOT see it: a load the house meter saw but
    neither the boy's room nor the office sockets did is somewhere in the
    rest of the house, and that sentence is the useful one. With no meter at
    all the phase is still a clue, being one leg of the board."""
    parents = parents or {}
    on = f"on phase {'+'.join(p.upper() for p in phases)}" if phases else ""
    where = most_specific(locations, count, parents)
    if where != "main":
        children = [c for c, parent in parents.items() if parent == where]
        missed = sorted(c for c in children if locations.get(c, 0) * 2 < count)
        return f"in {where}, outside {_and(missed)}" if missed else f"in {where}"
    partial = sorted((n for n, k in locations.items() if k), key=lambda n: -locations[n])
    if partial:
        n = partial[0]
        return f"under no meter, though {n} saw it {locations[n]} of {count} times" + (f", {on}" if on else "")
    return f"under no meter, {on}" if on else "under no meter"


def location_confidence(locations: Dict[str, int], count: int,
                        parents: Optional[Dict[str, Optional[str]]] = None) -> float:
    """How sure the location is: the share of sightings that agree with it."""
    if count <= 0:
        return 0.0
    where = most_specific(locations, count, parents)
    if where != "main":
        return round(min(1.0, locations.get(where, 0) / count), 2)
    return round(1.0 - min(1.0, (max(locations.values()) / count) if locations else 0.0), 2)


def signature_rows(signatures, tz, parents: Optional[Dict[str, Optional[str]]] = None) -> List[dict]:
    """The signature library as the Signature library sensor shows it, best
    evidence first. Nothing in a row depends on the clock - no "ran 5 min ago",
    no "running" flag, no age: a row changes only when its signature does, so
    Home Assistant writes no new state, and sends nothing to a dashboard, for a
    pass that filed nothing. Dashboards work the age out of ``last_seen_ts`` and
    the running ids from the Detected loads sensor's ``running_ids``."""
    return [
        {"id": s.id, "name": s.name, "description": s.describe(tz),
         "phases": s.phases.upper(),
         "system_phases": [display_phase(p) for p in s.phases if p in PHASES],
         "watts_by_phase": {p.upper(): round(w) for p, w in s.power.items()}, "count": s.count,
         "typical_duration_s": round(s.duration_s), "typical_interval_s": None if s.interval_s is None else round(s.interval_s),
         "pf": None if s.pf is None else round(s.pf, 2),
         "pf_mad": round(s.pf_mad, 3), "last_seen": datetime.fromtimestamp(s.last_seen, tz).isoformat(),
         "last_seen_ts": round(s.last_seen),
         # watt-hours per hour of day, and per weekday from Monday
         "hour_wh": [round(x) for x in s.hour_wh], "day_wh": [round(x) for x in s.day_wh],
         # the downstream meter that also saw it, or "main" (upstream of every submeter)
         "location": most_specific(s.locations, s.count, parents),
         "where": describe_location(s.locations, s.count, parents, s.phases),
         "seen_by": dict(s.locations),
         # three separate scores: whether it is a real repeating load,
         # what kind of thing it might be, and where it lives. They
         # answer different questions, so they are not blended.
         "evidence": s.evidence, "regular": s.regular,
         "guess": s.guess().to_dict(),
         "where_confidence": location_confidence(s.locations, s.count, parents)}
        # same order as the naming page: best evidence first
        for s in sorted(signatures, key=lambda x: (-x.evidence, -x.count))
    ]
