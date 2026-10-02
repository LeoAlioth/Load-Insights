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
import math
import operator
import statistics
from collections import ChainMap
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .classify import MAX_CONFIDENCE as MAX_APPLIANCE, Guess, _fmt_s, _fmt_w, classify
from .phases import phase_mapping

PHASES = ("a", "b", "c")
WEEK_SECONDS = 7 * 24 * 3600.0
# Never call a change smaller than this a transition. A FLOOR under the
# measured figure, not a replacement for it - the detector works out each
# phase's own noise from how much it wanders while idle, and this stops a
# pathological signal from setting it at nothing.
#
# It was 100 W, which on both of Anze's sites was eight times the real noise:
# their median sample-to-sample change is 3 to 5 W, so the measured figure
# would have been 12 to 20 W and the floor was the binding constraint on
# everything. A fridge compressor steps 60 to 150 W and could never clear it,
# which is why Kozolec has two fridges and detected neither (2026-09-18).
# At 10 W it is barely above the quantisation of the readings themselves, and
# it is safe to be that low because the level-dependent part below is
# MEASURED rather than assumed.
MIN_NOISE_W = 10.0
# ...and a reading wanders more when more is flowing through it, so the floor
# is not the whole story either (Anze, 2026-09-18: "can the noise scale with
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
# 3 kW load (Anze, 2026-09-18).
GLITCH_FLOOR_W = 200.0
# 5 since the noise is measured by how the reading moves (NOISE_FROM_MOVES):
# at 4 Home's house phase C came down to 60 W, just under a 65 W load cycling
# every 25-50 s, and the half-seen load's steps paired with the floor mat's
# (5.9 h of the mat outside its heating, against 1.8); at 3 the load was seen
# whole and crowded the groups (Home purity 71.0 %, 10.2 kWh wrongly placed).
# Ten days, 2026-10-01 - see the experiments branch's commit for the table.
NOISE_MAD_FACTOR = 3.0              # under test with EDGE_BY_METER: Anze wants the 65 W load seen (2026-10-01)
# The noise is re-learned from how far the reading MOVES between samples, as
# the seed measures it, not from how far it sits from the level. A load
# cycling just inside the band - Hiša's phase C, 65 W every 25-50 s - sat
# 30 W from the level on average, set the floor at 4 x 30 W and so stayed
# inside it for good: 1,616 of its steps on 28.09 counted from a start on the
# 19th, none from a start on the 21st. A held level moves by nothing.
# ^ NOISE_FROM_MOVES: always on, the name this note goes by elsewhere
# A new level is confirmed by TIME, SUSTAIN_CADENCES of the meter's own
# reading cadence (its shortest usual gap), never by a count of readings: the
# recorder writes only changes, so a meter that says nothing for that long has
# HELD its value, and its silence confirms the level as a reading would (Anze,
# 2026-10-01). Counting readings, the hidrofor's plug - polled every 10 s, one
# zero after the pump stopped and nothing written for 15 minutes - could not
# confirm the stop, declared -253 of its -809 and carried a phantom 590 W into
# the next start. Replaces a rule for meters slower than 20 s only.
# One rule for every meter, change-driven or polled: silence up to
# SUSTAIN_CADENCES of its cadence is the ordinary spacing of its readings,
# longer is its value HELD, unwritten. So a level is confirmed over that long
# (the hidrofor's plug, every 10 s: 20 s - Anze), a change seen first after a
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
# A meter reports its readings of one moment as separate entities, stamped
# milliseconds apart. Home's grid is the SolarEdge meter plus the inverter: the
# inverter's write 28 ms before the meter's showed the old level, and a kiln-
# sized step got a span of -0.0..0 - a 1 % overlap with Mansarda's own. A
# reading this close to another is the same update: not evidence the old
# level held, nor a gap between readings. Those pairs are all under 0.2 s
# (140,270 of Home's within 50 ms); real readings come 0.8-1.0 s apart -
# Home's grid meter 3,064 times, the Victron's refresh 607 times between 0.5
# and 1.0 s - and nothing between (Anze: some meters do update every second).
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
# How much better a size match must be before it overrides RECENCY when a
# step down chooses which open start it closes. 0 is pure best-fit, 1 treats
# every passing candidate as tied and takes the newest - which is what this
# did for its whole life. Swept on both sites; see the comment in _pair.
PAIR_TIE_BAND = 1.0
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
# clusters its runs used (Anze, 2026-09-29: "separate objects that we then just
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
# measured noise, fixed per group when it starts: a fixed 15 W suited Home
# (whose phases' noise is 10, 35 and 114 W) and ran every small fall at quiet
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
EDGE_RECUT = 32                # steps into a group before its segments are cut again
# A start is an ALL-PHASE EVENT: rises on different phases within this many
# seconds, the smallest at least EVENT_BALANCE of the largest, are one event,
# clustered on their phase pattern and total size. Home's three-phase
# compressor (~840 W a leg) and its single-phase pump (~900 W on A) are the
# same size on A and both motors; per phase they shared one cluster and the
# compressor's legs chained the pump into its device. As events they are
# (840, 850, 830) and (900, 0, 0). The legs switch together; what spreads
# them on the meter is the reading cadence - each phase's step is stamped at
# the first reading that shows it - so the window is that many of the slowest
# phase's intervals (Anze: from the sensor's readout, not a constant). Home's
# phases tick every 2 s and the legs' offsets there run to 4.2 s at the 90th
# percentile, 0.0 s median (ten days, 2026-09-30); the pump gets a false
# companion within 5 s 1 % of the time. A real multi-phase load shows on its
# other phases at far more than 20 % (Anze). Rises only: a fall closes a run
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
# bench: a second clustering dimension - the step's reactive angle,
# atan2(dQ, dP) in degrees, signed where the meter's VAr is. Within a size
# segment, steps are cut again at the valleys of their angle density: a pump
# (~35 deg) and a heater (0 deg) of one size are two kinds of edge (Hart 1992;
# Barsim & Yang 2014 cluster dP and dQ together). Off until it can be
# measured: Home's grid meter signs its var only from 2026-09-30 07:57, so
# ten days of it exist from about 2026-10-10; Kozolec's Victron and every
# 3EM give V x I or sqrt(S^2 - P^2), unsigned.
EDGE_ANGLE = False
EDGE_ANGLE_BIN = 3.0           # degrees
EDGE_ANGLE_KERNEL = 6.0        # degrees, the smoothing before the valleys
# A house step that meters below it stepped with at the same moment is
# several steps: each meter's own share (less its sub-meters', see
# _meter_steps), and the rest. Kozolec's boiler pulses 1.9 kW about
# once a minute, and a car charge that started in the same reading was booked
# at 5.3 kW - then closed when the boiler's pulse ended, the 8 h charge lost
# (27.09 10:17). The meter knows its own share (Anze, 2026-09-30: use the
# sub-meters to avoid it).
# ^ SPLIT_BY_METERS: always on, the name this note goes by elsewhere
# A single-phase step's place - the innermost meter that saw all of it - is
# part of what kind of edge it is: Home's 65 W load cycling inside Hisa and
# the NASA strip's small loads under Mansarda share a size on phase C and
# nothing else (Anze, 2026-10-01: "detect it fully"). Only a start is placed:
# a device is its start cluster. Benched and ruled out (2026-10-01, AGENTS.md's
# experiments table): "soft" - a place keys the group only where its steps
# pile up above chance - and placing every step.
# ^ EDGE_BY_METER: the name this note goes by elsewhere
# A meter's step is the step its OWN detector declared - held until settled,
# the median of what it held - exactly as the grid's is measured, never two
# raw readings either side of a window: one measure, no second method (Anze,
# 2026-10-01). A meter's step is all of the grid's within both meters' noise
# together plus METER_CAL_SLACK of the step, once each meter's own gain
# against the grid is learned (METER_GAIN_MIN steps both saw clearly, for its
# power and for its reactive part alike). Wider windows were tried and placed
# and split wrongly: a reading interval is a meter's heartbeat when steady,
# not how soon it reports a change (kiln ladder 31 -> 108; mat 13.1 h).
METER_CAL_SLACK = 0.05
METER_GAIN_MIN = 10
METER_GAIN_BOUND = 0.25       # a learned gain stays within this of one either way
# The two meters' spans of one change overlap - that it is one change, and how
# much is a timing confidence - and are compared over their UNION, grown by
# every step either declared whose span reaches into it, up to this many of
# the SLOWER meter's reach - the grid's event window, or the sub-meter's own
# sustain, as far back as its span can start: each meter's net change there
# (Anze, 2026-10-01). Of the grid's window alone, Home's cap was 24 s and a
# 10 s plug's span, three cadences, 30 s.
UNION_CAP_WINDOWS = 4.0
# How sure the detector is of each step and run, 0..1 (Anze, 2026-10-01): a
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
# (Anze, 2026-09-30: "can we have it tune itself?"): Home's floor mat 21.7 ->
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
# (Fleet._learn_lags, Anze 2026-10-02: "how long we need to wait before we
# have complete data from all sensors"): looked for this far either side of a
# grid step, kept over the last LAG_SAMPLES, believed from LAG_MIN_SAMPLES, at
# this percentile - the wait that rarely comes up short.
LAG_REACH_S = 300.0
LAG_SAMPLES = 200
LAG_MIN_SAMPLES = 20
LAG_PERCENTILE = 0.95
# A run is filed by its DEVICE - the component of edge clusters its start
# belongs to - into the signature most of the device's runs went to, or a new
# one for a device not seen before; signatures whose runs are one device's are
# one signature. No run is filed, and no two signatures merged, because their
# power looks alike: that was a second grouping beside the devices, and the
# one that undid what the edges knew (Anze, 2026-09-30: "is this old layer even
# necessary still? ... this will clean up a lot of redundant code"). A
# sub-meter's word, the switch gate and a rare input value still decide first.
# Against filing by likeness: Home 82.6 / 64.6 -> 82.4 / 68.7 % with 98
# signatures for 199, the floor mat's hours outside its thermostat's heating
# 10.3 -> 2.7 (precision 95 %, caught 58 %), Kozolec 98.5 / 93.1 (2026-09-30).
# ^ DEVICE_FILING: the name this note goes by elsewhere
# C - pairing by them: a fall first closes an open run its cluster's accepted
# pair starts with, at the learned size, whatever plain sizes say; only a fall
# with no pair model falls back on sizes. And the old guess that a drop fitting
# nothing is the newest bigger load stepping down goes (Anze, 2026-09-29: "the
# 200w step should just stay unmatched") - except a load settling just after
# its start.
# ^ PAIR_PAIRING: always on, as the 2026-09-30 ablation found (AGENTS.md)
SETTLE_SHARE = 0.3
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
# in grouping runs into devices or edges into runs (Anze, 2026-09-29: "much
# less important than the plain electrical properties ... I don't think using
# it for grouping is doing us any good"). NASA's computers never switch off and
# Home's pump runs seconds or hours; nothing matches, merges or pairs on it,
# and no run is closed for having run long.
# A run that started with an input's change - the mat with its thermostat going
# on - belongs to a device that follows that input: when the input next changes
# back, the run's stop is due at that moment by the input's lag, and if no fall
# closed it by the end of the window its stop was hidden in another load's
# step: it is closed there. Evidence, not a clock.
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
# ...AND this close in SIZE. A real multi-phase load is balanced by design -
# a two-phase element, a three-phase motor - and over ten days at home the
# smallest-to-largest ratio inside kiln-sized groups sat at a median of 0.98.
# Grouping on timing alone married a 2025 W load on A to a 163 W blip on C
# and filed the pair as one 2.2 kW two-phase load, which both invented a
# phantom and stole the session from the real single-phase one (Anze,
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
# (Anze, 2026-09-18, who picked 100 over the 50 I proposed).
ABSORB_WINDOW = 100.0
NOISE_SESSION_WH = 3.0         # a blip smaller than this AND shorter than NOISE_SESSION_S is dropped
NOISE_SESSION_S = 20.0
# A motor draws several times its running current for the moment it starts,
# and a meter catches one sample of it: Anze's pressure pump reads 8886 W in
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
# Two METERS may disagree about a run's length far more than two sightings of
# one load may: a 66-second boiler cycle is 66 seconds on its own meter and
# often minutes on a busy main one, where the down-step pairs with a
# different edge. A wide bound is only safe because the cross-meter match
# scores every passing pair and takes the closest - with first-fit it made
# things worse (Anze, 2026-09-18).
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
# capped at 45 s, so it moved with the pass. Anze (2026-10-02) accepts up to
# five minutes: the named loads' live readings lag by the horizon, their
# energy does not. Set on the detection settings page; 0 judges every step at
# once.
METER_WAIT_CAP_S = 300.0
# How many single-phase sessions a three-phase meter must have shared with the
# house before its channels are mapped onto the house's phases. Until then its
# own labels stand. Home's attic 3EM calls the house's C "b" and its A "c", so
# with its labels trusted it never matched a session by phase at all (627 seen
# live, 92 credited, all of them by energy - 2026-09-23).
PHASE_MAP_MIN_VOTES = 30
# A detection on a sub-meter overrides the house meter's (Anze, 2026-09-22:
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
# meter that reports only a total is matched by size and moment alone
# (Anze, 2026-09-28: "the plug is single phase, no multi phase load should be
# attributed there"). A count, so a load two sightings strong never sets it.
METER_PHASES_MIN = 20
# An entity that says WHEN a load is on - a thermostat's heating, a relay, a
# switch - is a meter that knows no power. A house session starting (and, once
# known, stopping) with one of its on-periods is credited to it: SWITCH_PREFIX
# names it among the meters. Home's bathroom floor mat (Termostat Kopalnica)
# starts and stops within seconds of the thermostat's heating on 11 of 12 runs,
# and was named by hand from exactly that (Anze, 2026-09-28).
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
# A number that may DRIVE a load - a fridge's room temperature, the weather -
# is learned against each load: how its run length and the gap between its
# starts follow the number's value when a run starts, a regression of their
# logarithms, recency-weighted over ABSORB_WINDOW. It is believed once a load
# has this many runs behind it and the number explains this share of the
# spread; then the run length's spread that the number does NOT explain is
# what counts as its tightness (Anze, 2026-09-28: "positively or negatively
# correlated to their frequency and runtime").
DRIVER_MIN_RUNS = 30
DRIVER_MIN_R2 = 0.25
# A setting a device reports - a washer's cycle phase, a fan's speed - is
# learned against each load: the value it had while the load ran, against
# what that value's share of the time would give by chance (Anze,
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
# seen to run in the wash phase (Anze, 2026-09-29). 0 is off.
# ^ INPUT_SPLIT: always on, as the 2026-09-30 ablation found (AGENTS.md)
# A value this rare or rarer - its share of the time - is one runs are
# filed in; and only once the shares have been counted for this long.
INPUT_RARE_SHARE = 0.25
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
# (Anze, 2026-09-29).
HOURLY_KEEP_S = 11 * 86400.0
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
# appliance that has genuinely left the house (Anze, 2026-09-18). One-offs
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
# to offer. Nothing is renamed without the user (Anze, 2026-09-18).
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
# 0.01 A SolarEdge without either site being configured (Anze, 2026-09-22:
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
# How many of its OWN run windows each signature remembers, so "these two are
# never on at once" can be asked about a load that runs every few days. It
# was asked of the rolling session list, which a busy house burns through in
# about two hours - 40 sessions spanning 2.1 h and holding 11 of Home's 199
# signatures - so the kiln, last fired 8.6 days ago, could never be compared
# with anything (Anze, 2026-09-22).
RUN_MEMORY = 12
# The widest a device's settings may span before they are two devices. See
# suggest_levels: generous, because it only has to beat 36 to 1.
MATCH_LEVEL_RATIO = 10.0
HELD_TAIL_S = 60.0             # closed sessions wait this long for a partner on another phase


def _median(xs: Sequence[float]) -> float:
    return statistics.median(xs) if xs else 0.0


# ------------------------------------------------------------------ sessions
@dataclass
class Session:
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
    # tighter tolerance than the first. Anze asked whether the fingerprint
    # could take the sampling rate into account (2026-09-18) - this is where
    # it enters. 0 means unknown, which is treated as well-measured so that
    # nothing stored before this existed is suddenly distrusted.
    samples: int = 0
    # The lowest and highest the load itself read during the run. Kept as
    # WATTS rather than as the ratio between them, because "varies between
    # 104 and 247 W" is a thing someone recognises about their own pump and
    # "varies by 69%" is not (Anze, 2026-09-18). None when two loads
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
    # (Anze, 2026-09-18: 173 signatures at Kozolec, every one of them "main",
    # on a site where the boiler and the car charger have their own meters).
    signature_id: Optional[int] = None
    # (start cluster, stop cluster) of the edges that opened and closed it
    pair: Optional[Tuple[Optional[int], Optional[int]]] = None
    # ...and a run merged across phases, every leg's
    legs: List[tuple] = field(default_factory=list)

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
        return sum(p * self.duration_s for p in self.power_by_phase().values()) / 3600.0

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
                "pair": list(self.pair) if self.pair else None, "legs": [list(x) for x in self.legs]}

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(phases=d["phases"], start=d["start"], end=d["end"], pf=d.get("pf"),
                   pf_mad=d.get("pf_mad", 0.0), surge_w=d.get("surge_w", 0.0),
                   # written by to_dict all along and never read back, so a
                   # session that waited across a restart came back unsampled
                   samples=d.get("samples", 0), low=d.get("low"), high=d.get("high"),
                   levels={ph: [tuple(x) for x in lv] for ph, lv in d["levels"].items()},
                   pair=tuple(d["pair"]) if d.get("pair") else None,
                   legs=[tuple(x) for x in d.get("legs") or []])


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


def mean_power(previous: Dict[str, float], current: Dict[str, float],
               span_s: float) -> Dict[str, float]:
    """Mean watts per name, from the watt-hours gained over ``span_s``.

    Reporting the energy that arrived divided by the time it covers, rather
    than the instantaneous power of a step whose matching step down has not
    arrived, is what makes this reading trustworthy: it counts only sessions
    that CLOSED. A 44-second run contributes its share whether or not anyone
    was looking at the right moment, and an up-step whose partner never came
    contributes nothing rather than sitting high for a day.

    A name with no earlier total is left out - one reading is a total, not a
    rate. A total that went DOWN means detection was reset, which is not
    negative power.
    """
    if span_s <= 0:
        return {}
    out: Dict[str, float] = {}
    for name, total in current.items():
        was = previous.get(name)
        if was is None:
            continue
        out[name] = max(0.0, total - was) * 3600.0 / span_s
    return out


def _trim(value, places: int):
    """A stored number at the precision it actually carries.

    A watt-hour figure written as 276.1825572400394 spends fifteen digits on
    a quantity the energy meter publishes to two decimal places of a
    kilowatt-hour. Over a library of two hundred signatures that is a third
    of the stored state, rewritten every pass (Anze, 2026-09-18).

    Energy is trimmed to whole watt-hours rather than integers on the way
    in: what is stored is what a restart restores, and an energy total that
    steps DOWN reads as a meter reset to Home Assistant's statistics. At one
    decimal a restart costs the biggest signature here 0.14 Wh out of
    38.7 kWh, which is three ten-thousandths of a per cent.
    """
    return round(value, places) if isinstance(value, float) else value


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
    Anze's home over 246,000 samples on three phases: identical to the
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
    reactive power runs against the floor's reads the wrong size (Anze,
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


def unit_scale(unit: Optional[str]) -> float:
    """What to multiply a reading by to get watts, amps or volts.

    Home's EV charger publishes kW while every other meter in the house
    publishes W, so its 2 kW charging session arrived as the number 2 and
    could never match the 2000 W session the main meter saw. The load stayed
    unattributed and turned up in the naming list as an unexplained car -
    which is how Anze found this (2026-09-18). Fourteen entities at that site
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
    where power's does not (Anze, 2026-09-19, on a Zigbee meter that cannot
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
    out: list = []
    try:
        fleet = raw.get("fleet") or {}
        main = fleet.get("main") or raw.get("detector") or {}
        for sig in main.get("signatures") or []:
            name = sig.get("name")
            if not name:
                continue
            power = sig.get("power")
            out.append({"name": name,
                        "phases": sig.get("phases") or "",
                        "power": dict(power) if isinstance(power, dict) else {},
                        "duration_s": sig.get("duration_s") or 0.0,
                        "pf": sig.get("pf")})
        out += [dict(o) for o in main.get("orphan_names") or [] if isinstance(o, dict) and o.get("name")]
    except (AttributeError, TypeError, ValueError):
        return []
    return out


def carries_generation(rows: Sequence[Tuple[float, float]]) -> Optional[bool]:
    """Does this reading contain the site's generation, or the house alone?

    It decides two things at once - whether a cloud can masquerade as a load
    in this signal, and whether the inverter's output has to be added back
    to get what the house draws - and it is answered by physics rather than
    statistics: a reading that includes generation goes BELOW ZERO whenever
    the site exports, and one that carries only the house cannot.

    I tried correlating the two series' changes first, and Anze's own data
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
    convention, where importing is positive. Anze's SolarEdge M1 is the
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
    through (Anze, 2026-09-18).

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
    4 kW of "noise" on exactly that basis (Anze, 2026-09-22).

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
    load that just started (Anze, 2026-09-17). Watts and VAr add across loads,
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
    # flat level, which is why it classified as a small heater (Anze,
    # 2026-09-18).
    lo: Optional[float] = None
    hi: Optional[float] = None
    # what it draws NOW, followed while it runs alone - see SAG_CLOSE. Never
    # persisted: after a restart the start's size stands in, as before.
    now: Optional[float] = None
    # the edge cluster it started with - see PAIR_MIN_RUNS
    cluster: Optional[int] = None
    q: float = 1.0                                # its start's quality - see QUALITY_SNR_FULL; not persisted
    # how far the runs already open overstated the phase's reading when this
    # one started - see _unseen_stop. Not persisted, as `now` is not.
    short0: float = 0.0
    # the meter below the grid whose own step was all of this start (its
    # event's cluster `where`, placed, not guessed): the run is that meter's
    # until the meter shows it stopped - see PhaseState.owned
    meter: Optional[str] = None

    def as_list(self) -> list:
        return [self.since, self.watts, self.var, [list(x) for x in self.levels], self.lo, self.hi,
                self.surge, self.cluster, self.meter]

    @classmethod
    def of(cls, raw) -> "_Open":
        since, watts, var = raw[0], raw[1], raw[2]
        levels = [tuple(x) for x in (raw[3] if len(raw) > 3 else [])] or [(since, watts)]
        return cls(since=since, watts=watts, var=var, levels=levels,
                   lo=raw[4] if len(raw) > 4 else None,
                   hi=raw[5] if len(raw) > 5 else None,
                   surge=raw[6] if len(raw) > 6 else 0.0,
                   cluster=raw[7] if len(raw) > 7 else None,
                   meter=raw[8] if len(raw) > 8 else None)


@dataclass
class PhaseState:
    """Edges, not excursions.

    A load is a STEP: the phase rises by its power when it starts and falls
    by the same amount when it stops. Waiting instead for the meter to come
    back to its idle floor - the first design - only works on a phase that
    goes quiet between loads, and a house main never does: the kettle starts
    while the fridge is running, so the excursion that opened at breakfast
    closed at midnight. Anze's home meter proved it (2026-09-17): 76
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
    # and the classifier's heater band starts at 0.93 (Anze, 2026-09-18).
    q_recent: List[float] = field(default_factory=list)
    pv_level: Optional[float] = None             # what the array was making then
    # True where this reading is the house alone, which cannot go below
    # zero. Anze's per-phase template dips negative for 49 samples out of
    # 20764 - the moments its own inputs do not line up - and one of those
    # dragged the idle floor to -569 W for the rest of the day, so every
    # step after it was measured from nonsense (2026-09-18).
    floor_zero: bool = False
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
    _moving_sorted: List[float] = field(default_factory=list, repr=False, compare=False)
    _moved: bool = field(default=False, repr=False, compare=False)   # the last reading moved past the noise
    last_w: Optional[float] = None
    # The apparent power one current quantum is worth, V x dI, which is what
    # limits any power factor derived here. Supplied by whoever read the
    # amps, since the detector only ever sees the VAr they produced.
    q_quantum: float = 0.0
    # Set by the Detector for a pass: asks whether another phase's leg of the
    # same load is stopping too. Never persisted - it is rebuilt every pass.
    corroborate: Optional[object] = field(default=None, repr=False, compare=False)
    # (since, watts, closed_at) of edges closed in the last minute, for the
    # same question from the other side. Transient, never persisted.
    recent_closed: List[Tuple[float, float, float]] = field(default_factory=list, repr=False, compare=False)
    # the open edge another leg vouched is stopping, for _pair to close
    close_hint: Optional[object] = field(default=None, repr=False, compare=False)
    # the steps this pass committed: (since, watts, VAr, surge) - the
    # detector files them as edges. Never persisted.
    steps: List[tuple] = field(default_factory=list, repr=False, compare=False)
    # the detector whose edge library and pair models this phase asks, its
    # name, and the cluster of the fall being paired. Set each pass.
    lib: Optional[object] = field(default=None, repr=False, compare=False)
    name: str = field(default="", repr=False, compare=False)
    stop_cluster: Optional[int] = field(default=None, repr=False, compare=False)
    stop_q: Optional[float] = field(default=None, repr=False, compare=False)    # the stop being paired's quality
    last_step_ts: Optional[float] = field(default=None, repr=False, compare=False)
    # every step this phase declared, (since, watts, var), newest last - what a
    # meter's step IS when the grid's is compared with it (METER_CAL_SLACK)
    declared: List[tuple] = field(default_factory=list, repr=False, compare=False)
    declared_t: List[float] = field(default_factory=list, repr=False, compare=False)   # their times, for bisect
    steady_ts: Optional[float] = field(default=None, repr=False, compare=False)   # its last reading at the held level
    steady_before: Optional[float] = field(default=None, repr=False, compare=False)   # ...and the update's before that
    held_drops: List[tuple] = field(default_factory=list, repr=False, compare=False)
    # the measured share of the running level that is noise, and the samples
    # it is measured from
    noise_rel: float = 0.0
    rel_diffs: List[float] = field(default_factory=list)
    seed: List[float] = field(default_factory=list)
    idle_diffs: List[float] = field(default_factory=list)
    pending: List[Tuple[float, float, Optional[float], Optional[float]]] = field(default_factory=list)
    open_edges: List[_Open] = field(default_factory=list)   # believed to be running
    last_ts: Optional[float] = None
    raw_last: Optional[Tuple[float, float]] = None   # the last reading as the meter wrote it - see Detector._corroborate
    # a meter below the grid: its report lag against it, learned by the
    # Fleet (Fleet._learn_lags), 0 until it is - see latency
    lag: float = 0.0
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
                pv: Optional[float] = None, held: bool = False) -> List[Session]:
        due = self._input_ended(ts) if self.lib is not None and self.open_edges else []
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
            self.lib.meter_on(o.meter, self.name, o.since, o.watts)

    def _input_ended(self, ts: float) -> List[Session]:
        """Close runs whose input has switched back and whose stop never came -
        see INPUT_ENDS."""
        out = []
        for o in list(self.open_edges):
            if o.cluster is None or self.owned(o):
                continue
            at = self.lib.input_end(o.cluster, o.since, ts)
            if at is not None:
                self.open_edges.remove(o)
                self._remember_close(o, at)
                out.append(self._close(o, at, o.now or o.watts, None))
        return out

    def _process(self, ts: float, w: float, q: Optional[float] = None,
                 pv: Optional[float] = None, held: bool = False) -> List[Session]:
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
        if not held:
            moved = prev_w is not None and abs(w - prev_w) >= self.noise_at(prev_w)
            same = self.last_ts is not None and ts - self.last_ts <= SAME_UPDATE_S
            self._moved = moved or (same and self._moved)
            self.last_ts = ts
        if self.floor_zero and w < -GLITCH_FLOOR_W:
            return []                 # a house cannot draw less than nothing; skip it
        if not held:
            self._learn_quantum(w)
        if self.baseline is None:
            self.seed.append(w)
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
                diffs = [abs(b - a) for a, b in zip(self.seed, self.seed[1:])] or [0.0]
                self.noise = max(self.min_noise, self.quantum, NOISE_MAD_FACTOR * _median(diffs))
                self.level = self.baseline
                self.q_level = q
                self.pv_level = pv
                self.seed = []
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
                above = w - self.baseline if self.baseline is not None else w
                o.lo = above if o.lo is None else min(o.lo, above)
                o.hi = above if o.hi is None else max(o.hi, above)
            # no step - follow the drift, so a ramp never becomes a load
            self.level += SLOW_FOLLOW * (w - self.level)
            if len(self.open_edges) == 1 and self.baseline is not None:
                o = self.open_edges[0]
                if self.owned(o):
                    # a meter's run is what its meter reads, never what the phase does
                    lvl = self.lib.meter_level(o.meter, self.name) if self.lib.meter_level is not None else None
                    if lvl:
                        o.now = lvl
                else:
                    # a drop held since it started is not it sagging - see HELD_DROPS
                    o.now = self.level - self.baseline + sum(d[1] for d in self.held_drops if d[0] > o.since)
            if self.level is not None and abs(self.level) >= self.rel_floor:
                wander = abs(w - prev_w) if prev_w is not None else abs(w - self.level)
                self.rel_diffs.append(wander / abs(self.level))
                if len(self.rel_diffs) >= 240:
                    self.noise_rel = min(NOISE_REL_CAP,
                                         NOISE_MAD_FACTOR * _median(self.rel_diffs))
                    self.rel_diffs = self.rel_diffs[-120:]
            if q is not None:
                self.q_level = q if self.q_level is None else self.q_level + SLOW_FOLLOW * (q - self.q_level)
                self.q_recent.append(q)
                del self.q_recent[:-Q_RECENT_SAMPLES]
            if pv is not None:
                self.pv_level = pv if self.pv_level is None else self.pv_level + SLOW_FOLLOW * (pv - self.pv_level)
            if not self.open_edges:
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
                self.idle_diffs.append(abs(w - prev_w) if prev_w is not None
                                       else abs(w - self.baseline))
                if len(self.idle_diffs) >= 240:
                    self.noise = max(self.min_noise, self.quantum, NOISE_MAD_FACTOR * _median(self.idle_diffs))
                    self.idle_diffs = self.idle_diffs[-120:]
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
        if _is_the_sun(step, pv_step):
            return []
        quality = self._step_quality(step, held, since, new_level - step)
        self.last_step_ts = since
        # its span: when the change can have happened - from the last moment
        # the old level is known to have held, to the first reading it settled on
        self.declared.append((since, step, step_q, old_side if old_side is not None else self.span_start(first_off),
                              held[0][0] if held else since, ts))
        self.declared_t.append(since)
        if len(self.declared) > 4000:
            del self.declared[:1000]
            del self.declared_t[:1000]
        # a fall is split at once; a rise when its event forms (see
        # Detector._form_event), when the grid's steps across a meter's span
        # are all declared
        parts = self.lib.metered_parts(self.name, since, step) if self.lib is not None and step < 0 else [step]
        closed: List[Session] = []
        for k, part in enumerate(parts):
            closed += self._declare(since, part, None if step_q is None else step_q * (part / step if step else 1.0),
                                    surge if k == len(parts) - 1 else 0.0, new_level, quality)
        return closed

    def latency(self) -> float:
        """How long after a moment this reading's data about it is complete -
        how long a new level must go uncontradicted to be confirmed, a
        silence to mean the value held, and how far before its first reading
        a change can have happened: SUSTAIN_CADENCES of its shortest repeat
        interval, or its report lag against the grid where that is longer
        (``lag``, learned from the steps it shares with the grid - see
        Fleet._learn_lags; the grid itself is the reference and has none).
        Anze (2026-10-02) asked for the lag in place of the repeat interval
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
        return max(SUSTAIN_CADENCES * self.interval, self.lag)

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

    def stand_in(self, at: float) -> List[Session]:
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
        loads' steps (the hidrofor's plug; Anze, 2026-10-01)."""
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
                 new_level: float, quality: float = 1.0) -> List[Session]:
        """One declared step - or one part of it, see SPLIT_BY_METERS - as a
        start or a stop."""
        cid = self.lib.classify(self.name, since, step, step_q, surge) if self.lib is not None else None
        if step > 0:
            ended: List[Session] = []
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
                    self.open_edges.remove(old)
                    self._remember_close(old, since)
                    ended.append(self._close(old, since, old.now or old.watts, None))
            return ended
        self.stop_cluster, self.stop_q = cid, quality
        closed = self._pair(since, -step, None if step_q is None else -step_q, new_level)
        self.stop_cluster, self.stop_q = None, None
        closed += self._unseen_stop(since, new_level)
        if self.open_edges and new_level <= self.baseline + self.noise:
            # back at the idle floor, so whatever was still open has stopped
            # without us seeing it go. Holding those starts open would have
            # them pair with an unrelated load hours later, and meanwhile
            # count as running. A run its meter still shows on stays.
            self.open_edges = [o for o in self.open_edges if self.owned(o)]
        return closed

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
        and too high for Kozolec at 10 W, which needs 200 (Anze, 2026-09-22).
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
        return False

    def _unseen_stop(self, at: float, level: float) -> List[Session]:
        """A phase cannot carry more than it reads: when the loads believed
        running add up to more than the whole reading, one of them stopped
        unseen, and the one that fits the shortfall - the oldest of those -
        is closed here. A shortfall nothing fits is left alone.

        Its stop landed in the same reading as another load's step: Home's
        floor mat against the hob pulsing 2 kW every five seconds, left open
        nine hours until an unrelated drop of its size closed it. Measured on
        the WHOLE reading, not above the idle floor (Anze: "closes a session
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
                self.open_edges.remove(o)
                self._remember_close(o, at)
                return [self._close(o, at, size, None)]
        # A shortfall nothing fits still rules out any run bigger ON ITS OWN
        # than the whole reading: it cannot be on. Kozolec's 10.5 kW run held
        # 289 min on a phase reading far less, and took 50 kWh (Anze,
        # 2026-09-30: "should be stopped as soon as the site's total power has
        # dropped under the 10.5 kW mark for a couple readings").
        # On the run's OWN size, never what it is followed to draw now: that
        # carries the drops held since it started - other loads' stops, when
        # their starts went unseen - and read Kozolec's 3,346 W charge as
        # 5,549 W; the rule then closed it at that, and a one-level run's
        # energy is the mean of its start and its close, so 410 min were
        # booked at 4,448 W (27.09, 6.8 kWh over).
        out = []
        for o in [o for o in self.open_edges if o.watts - level > self.noise_at(level) and not self.owned(o)]:
            # ...unless its meter still shows it on: then the reading is the
            # one that is wrong, for a moment (Home's house reading is a grid
            # meter and an inverter combined; Susilna 09-27 19:05)
            if o not in self.open_edges:
                continue                  # closed meanwhile - see Detector.resolve_rise
            self.open_edges.remove(o)
            self._remember_close(o, at)
            out.append(self._close(o, at, o.watts, None))
        return out

    def end_older(self, cid: int, since: float, keep: "_Open") -> List[Session]:
        """A start cluster rising again ends the older open run of that
        cluster on this phase, at its usual length if one is known."""
        out = []
        for o in [o for o in self.open_edges if o.cluster == cid and o is not keep and not self.owned(o)]:
            self.open_edges.remove(o)
            usual = self.lib.usual_length(cid) if self.lib is not None else None
            at = min(since, o.since + usual) if usual else since
            self._remember_close(o, at)
            out.append(self._close(o, at, o.now or o.watts, None))
        return out

    def _remember_close(self, o, at: float) -> None:
        self.recent_closed.append((o.since, o.watts, at))
        self.recent_closed = [c for c in self.recent_closed if at - c[2] <= 60.0]

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
              new_level: float) -> List[Session]:
        """What a step down means, most recent load first.

        Either a load STOPPED, in which case the step undoes its own step up
        and the session closes; or a load STEPPED DOWN to a lower level and
        is still running - a washer leaving its heater for its motor - in
        which case the level is recorded and the session stays open. Anything
        else is a stop we never saw start, and is dropped rather than pinned
        on an unrelated load."""
        # MOST RECENT first, and deliberately - do NOT "fix" this into the
        # best-size-fit that _locate uses. Tried, measured, worse at both
        # sites: Kozolec's boiler fell from 89.2 % of its sessions in one
        # signature to 79.0 %, and Home's purity from 67.6 % to 65.0 % with
        # the hidrofor spreading over 61 signatures instead of 49
        # (2026-09-23). Recency carries information here that it does not
        # carry there: a step DOWN most likely belongs to the load that most
        # recently started and matches, whereas _locate is choosing between
        # sessions that already exist and has no such prior. Size-matching
        # alone lets a stop close against an older edge of similar size.
        #
        # Then swept properly rather than tried once (2026-09-23): the dial
        # from pure best-fit (PAIR_TIE_BAND 0) to newest-that-passes (1) has
        # its optimum at 1 at Home and is flat 0.5-1 at Kozolec; widening the
        # size tolerance past it (MATCH_EDGE_REL) loses at both; and weighting
        # ABSOLUTE age rather than rank costs about six points wherever it is
        # added. Recency rank is the information, not how much newer.
        #
        # What best-fit DID improve is worth knowing if this is revisited:
        # the kiln's merged sessions reported a median 47.8 s against a true
        # pulse of 48, where recency gives 42.1 s. So the durations are
        # measurably wrong and the fix is not this one - probably a cost
        # combining size gap AND age rather than either alone.
        #
        # First what a one-device meter below says: its device stopped here,
        # so the run it started ends, whatever the sizes - see Fleet._meter_stop.
        if self.lib is not None and self.lib.meter_stop is not None and self.declared:
            e = self.declared[-1]
            o = self.lib.meter_stop(self.name, e[3], e[4], self.open_edges, watts)
            if o is not None:
                self.open_edges.remove(o)
                self._remember_close(o, at)
                return [self._close(o, at, watts, var, direct=True)]
        if self.lib is not None and self.stop_cluster is not None:
            got = self._pair_by_model(at, watts, var)
            if got is not None:
                return got
        hint, self.close_hint = self.close_hint, None
        if hint is not None:
            for i, o in enumerate(self.open_edges):
                if o is hint and abs(o.watts - watts) <= self._tol(o.watts, watts):
                    self.open_edges.pop(i)
                    self._remember_close(o, at)
                    return [self._close(o, at, watts, var, direct=True)]
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
            tol = self._tol(o.watts, watts)
            gap = abs(o.watts - watts)
            if o.now is not None and o.now > 0:
                # ...or at what it has sagged (or grown) to since it started
                now_tol = self._tol(o.now, watts)
                if abs(o.now - watts) <= now_tol and abs(o.now - watts) < gap:
                    tol, gap = now_tol, abs(o.now - watts)
            if gap <= tol:
                cands.append((i, gap, tol))
        if cands:
            best_gap = min(g for _, g, _ in cands)
            band = PAIR_TIE_BAND * max(t for _, _, t in cands)
            i = max(i for i, g, _ in cands if g <= best_gap + band)   # newest of the tied
            o = self.open_edges.pop(i)
            self._remember_close(o, at)
            return [self._close(o, at, watts, var, direct=True)]
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
            return sorted((self._close(o, at, o.watts, None) for o in gone), key=lambda x: x.start)
        if self.open_edges:
            self.held_drops.append((at, watts, self.stop_cluster))
            del self.held_drops[:-HELD_DROPS]
        if not self.open_edges:
            # nothing was running: the floor itself moved
            self.baseline = max(new_level, 0.0) if self.floor_zero else new_level
        return []

    def _joint_stop(self, at: float, watts: float, var: Optional[float]) -> Optional[List[Session]]:
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
            self.open_edges.pop(i)
            self._remember_close(o, at)
            return [self._close(o, at, watts, var)]
        return None

    def _pair_by_model(self, at: float, watts: float, var: Optional[float]) -> Optional[List[Session]]:
        """Close the open run this fall's cluster is the learned end of - see
        PAIR_PAIRING. Best by how near the learned stop size and run length it
        comes; None when no open run is its pair's start."""
        partners = self.lib.partners(self.stop_cluster)
        if not partners:
            return None
        best = None
        for i, o in enumerate(self.open_edges):
            model = partners.get(o.cluster)
            if model is None:
                continue
            ratio, ratio_sd = model[0], model[1]
            expect = o.watts * ratio
            tol = self._tol(expect, watts) + 2.0 * ratio_sd * o.watts
            gap = abs(watts - expect)
            if gap > tol:
                continue
            score = gap / tol
            if best is None or score <= best[0]:
                best = (score, i)
        if best is None:
            return None
        o = self.open_edges.pop(best[1])
        self._remember_close(o, at)
        return [self._close(o, at, watts, var, direct=True)]

    def _span(self, since: float, until: float) -> int:
        """How many meter samples a run of that length was measured over, from
        this phase's own observed sampling interval."""
        if not self.interval or self.interval <= 0:
            return 0
        return max(1, int(round((until - since) / self.interval)) + 1)

    def _close(self, o: _Open, at: float, watts: float, var: Optional[float], direct: bool = False) -> Session:
        """``direct``: closed by the fall being paired, whose cluster is then
        this run's stop - which teaches the pair (see PAIR_MIN_RUNS)."""
        if o.cluster is None and self.lib is not None:
            self.lib.resolve_rise(o)             # its event window has not passed: form the event now
        pair = (o.cluster, self.stop_cluster if direct else None)
        if direct and self.lib is not None:
            self.lib.note_pair(o.cluster, self.stop_cluster, o.watts, watts, at - o.since)
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
            # (Anze)
            start = levels[0][1]
            agree = (abs(start - watts) <= self._tol(start, watts)
                     or (o.now is not None and o.now > 0 and abs(o.now - watts) <= self._tol(o.now, watts)
                         and (o.now <= start + self._tol(start, o.now) or self.owned(o))))
            levels = [(o.since, 0.5 * (start + watts) if agree else min(start, watts))]
            known = [abs(x) for x in (o.var, var) if x is not None]
            q = sum(known) / len(known) if known else None
        else:
            q = o.var                         # the factor of the level it started at
        # A factor derived from amps too coarse to resolve this load is not a
        # measurement of it. Kozolec's Victron publishes current to 0.1 A -
        # 23 VA at 230 V - so a 62 W load's whole apparent power is under
        # three quanta, sqrt(S^2 - P^2) clamps to zero on 7 % of samples and
        # reads PF 1.00, and 63 % of all samples came out at unity. Believing
        # that split the library into 86 signatures where suppressing it
        # gives 24 (Anze, 2026-09-22).
        stop_q = self.stop_q if self.stop_q is not None else QUALITY_UNSEEN_STOP
        agree = abs(o.watts - watts) / max(o.watts, abs(watts), 1e-9)
        f_pair = min(1.0, max(0.0, 1.0 - agree / (2.0 * MATCH_EDGE_REL)))
        return Session(phases="", start=o.since, end=at, levels={"": levels},
                       quality=min(o.q, stop_q) * f_pair,
                       surge_w=o.surge,
                       pf=_pf_from(levels[0][1], q),
                       pf_mad=_pf_spread(levels[0][1], q, self.q_quantum),
                       samples=self._span(o.since, at), low=o.lo, high=o.hi, pair=pair)

    def active(self, now_ts: float) -> Optional[Tuple[float, float]]:
        """(since, watts) of everything believed to be running on this phase."""
        if not self.open_edges:
            return None
        return min(o.since for o in self.open_edges), sum(o.watts for o in self.open_edges)

    def to_dict(self) -> dict:
        return {"baseline": self.baseline, "noise": self.noise, "level": self.level,
                "q_level": self.q_level, "q_recent": list(self.q_recent), "interval": self.interval,
                "min_noise": self.min_noise, "noise_rel": self.noise_rel,
                "quantum": self.quantum, "q_quantum": self.q_quantum,
                "step_diffs": self.step_diffs[-QUANTUM_MIN_SAMPLES:], "last_w": self.last_w, "pv_level": self.pv_level, "seed": self.seed,
                "idle_diffs": self.idle_diffs[-120:], "pending": [list(x) for x in self.pending],
                "open_edges": [o.as_list() for o in self.open_edges], "last_ts": self.last_ts,
                "raw_last": list(self.raw_last) if self.raw_last else None, "lag": self.lag,
                "floor_zero": self.floor_zero, "moving_gaps": [round(x, 2) for x in self.moving_gaps]}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "PhaseState":
        if not d:
            return cls()
        return cls(baseline=d.get("baseline"), noise=d.get("noise", MIN_NOISE_W), level=d.get("level"),
                   q_level=d.get("q_level"), q_recent=list(d.get("q_recent") or []),
                   interval=d.get("interval", 0.0), min_noise=d.get("min_noise", MIN_NOISE_W), noise_rel=d.get("noise_rel", 0.0),
                   quantum=d.get("quantum", 0.0), q_quantum=d.get("q_quantum", 0.0),
                   step_diffs=list(d.get("step_diffs") or []), last_w=d.get("last_w"), pv_level=d.get("pv_level"), seed=list(d.get("seed") or []),
                   idle_diffs=list(d.get("idle_diffs") or []),
                   pending=[tuple(list(x) + [None] * (4 - len(x))) for x in d.get("pending") or []],
                   open_edges=[_Open.of(x) for x in d.get("open_edges") or []], last_ts=d.get("last_ts"),
                   raw_last=tuple(d["raw_last"]) if d.get("raw_last") else None, lag=float(d.get("lag") or 0.0),
                   floor_zero=bool(d.get("floor_zero", False)), moving_gaps=list(d.get("moving_gaps") or []))


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
    # number -> {"d": run-length stats, "g": start-gap stats}, each
    # [weight, sum x, sum x2, sum y, sum y2, sum xy] with y a logarithm - see
    # DRIVER_MIN_RUNS
    drivers: Dict[str, Dict[str, List[float]]] = field(default_factory=dict)
    # setting -> {"n": value -> runs seen in it, "e": value -> runs chance
    # would put there}, both recency-weighted - see INPUT_MIN_RUNS
    inputs: Dict[str, Dict[str, Dict[str, float]]] = field(default_factory=dict)
    # "surge" / "bump" / "flat" -> runs that started so (recency-weighted)
    # "setting=value" it was born in - see INPUT_SPLIT - and the ones it took
    # back from a born-in twin that turned out to be chance
    born_in: Optional[str] = None
    takes_in: List[str] = field(default_factory=list)
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
    # is the thing worth looking at (Anze, 2026-09-17).
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
    # an identical single-phase load was granted (Anze: "why don't we just
    # assume the noise level per phase everywhere", 2026-09-22).
    power_mad: float = 0.0
    pf_mad: float = 0.0
    # the last RUN_MEMORY windows this ran in, for "never two at once"
    runs: List[Tuple[float, float]] = field(default_factory=list)
    # how far the start towers over the run, averaged - see INRUSH_RATIO. A
    # motor does this and nothing else in a house does, so it is evidence
    # rather than noise once it is kept out of the power (Anze, 2026-09-22:
    # "that spike is a very good device signature, but it has to be taken
    # into account properly to not show as separate loads").
    inrush_w: float = 0.0
    duration_mad: float = 0.0
    interval_mad: Optional[float] = None

    def alike(self, other: "Signature", noise_w: float) -> bool:
        """Would these two be the same signature if they arrived now?

        The same test as ``matches``, between two signatures rather than a
        signature and a session."""
        if self.phases != other.phases or self is other:
            return False
        if self.name and other.name and self.name != other.name:
            return False                       # named apart on purpose
        if self.born_in != other.born_in:
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
        # (Anze asked why consolidation used a fixed figure, 2026-09-21).
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
        for hour, wh in other.hourly.items():
            self.hourly[hour] = self.hourly.get(hour, 0.0) + wh
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
        for name, row in other.drivers.items():
            mine = self.drivers.setdefault(name, {"d": [0.0] * 6, "g": [0.0] * 6})
            for key in ("d", "g"):
                mine[key] = [x + y for x, y in zip(mine[key], row.get(key) or [0.0] * 6)]
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

    def absorb(self, s: Session, tz) -> None:
        # A session pulls the running means by how well it was MEASURED, not
        # one-for-one. Eight samples of a 43-second run and forty-three of the
        # same run are not equally good evidence of its power, and letting the
        # first move the mean as hard as the second is how a well-established
        # figure gets dragged about by its worst sightings (Anze, 2026-09-18).
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

    def note_driver(self, name: str, key: str, x: float, y: float) -> None:
        """One run's length ("d") - or the gap before it ("g") - seen with
        the number at ``x``."""
        acc = self.drivers.setdefault(name, {"d": [0.0] * 6, "g": [0.0] * 6})[key]
        keep = 1.0 - 1.0 / ABSORB_WINDOW
        ly = math.log(y)
        for i, v in enumerate((1.0, x, x * x, ly, ly * ly, x * ly)):
            acc[i] = acc[i] * keep + v

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
        if runs < INPUT_MIN_RUNS or e <= 0:
            return None
        return n / e

    def files_in(self, context: Optional[str]) -> bool:
        """May a run made in ``context`` (None: in no rare value) join it?"""
        return self.born_in == context or (context is not None and context in self.takes_in)

    def strongest_input(self):
        """(setting, value, share, times chance) of the one it is most tied to, or None."""
        best = None
        for name in self.inputs:
            got = self.input_of(name)
            if got and (best is None or got[2] > best[3]):
                best = (name, *got)
        return best

    def driver_effect(self, name: str, key: str = "d") -> Optional[Tuple[float, float, float]]:
        """(share change per unit of the number, r², weight) for run length
        ("d") or the gap between starts ("g"); None until anything is known."""
        acc = (self.drivers.get(name) or {}).get(key)
        if not acc or acc[0] <= 1.0:
            return None
        w, sx, sxx, sy, syy, sxy = acc
        vx, vy, cxy = sxx / w - (sx / w) ** 2, syy / w - (sy / w) ** 2, sxy / w - (sx / w) * (sy / w)
        if vx <= 1e-12 or vy <= 1e-12:
            return None
        return math.exp(cxy / vx) - 1.0, (cxy * cxy) / (vx * vy), w

    def strongest_driver(self, key: str = "d"):
        """(name, share per unit, r²) of the number explaining the most, once
        believed - see DRIVER_MIN_RUNS - or None."""
        best = None
        for name in self.drivers:
            eff = self.driver_effect(name, key)
            if eff and eff[2] >= DRIVER_MIN_RUNS and eff[1] >= DRIVER_MIN_R2 and (best is None or eff[1] > best[2]):
                best = (name, eff[0], eff[1])
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
        (Anze, 2026-09-23: the naming page "does not fit all the text")."""
        # "runs" and "starts every", in so many words: a bare "2 min" read as
        # either how long it runs or how often it comes (Anze, 2026-09-23)
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
        day and its spacing read 5.5 h. A person counts starts, too (Anze,
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
        driver = self.strongest_driver("d")
        if driver:
            # what the number explains is not the load being loose about time
            spread *= max(0.0, 1.0 - driver[2]) ** 0.5
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
        something above a machine that runs every Saturday at noon (Anze,
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

    def _spread(self, s: Session, tz) -> None:
        """Put a session's energy into every hour and day it occupied.

        A run from 23:50 to 03:00 belongs to four hours and two days, not to
        the one it began in. And only the part of it this signature was not
        already on for: one device never runs twice at once, and a second
        run over the first is another device of its size. Counting both put
        1.96 kWh in one hour into Home's 635 W floor mat, more than the whole
        of Hiša used in it (2026-09-29)."""
        watts = sum(s.power_by_phase().values())
        if watts <= 0 or s.end <= s.start:
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
        cut = s.end - HOURLY_KEEP_S
        for hour in [h for h in self.hourly if h < cut]:
            del self.hourly[hour]

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

    def detail(self, tz, parents: Optional[Dict[str, Optional[str]]] = None) -> str:
        """Markdown for the naming form, once one is picked: what it is, when
        it runs, where it is, and how sure each of those is."""
        lines = [f"**{self.describe(tz)}**", ""]
        bars = hour_histogram(self.hour_wh)
        if bars:
            lines += [f"Where its energy goes, by hour of day - tallest bar "
                      f"{_fmt_wh(max(self.hour_wh))} of {_fmt_wh(sum(self.hour_wh))}:",
                      "", "```", *bars, "```", ""]
        week = day_histogram(self.day_wh)
        if week:
            # every chart is scaled to its own tallest bar, so without this
            # you cannot tell one sighting from twenty (Anze, 2026-09-17)
            lines += [f"And by day of week - tallest bar {_fmt_wh(max(self.day_wh))} "
                      f"of {_fmt_wh(sum(self.day_wh))}:", "", "```", *week, "```", ""]
        guess = self.guess()
        if guess.kind:
            both = guess.kind if not guess.alternative else f"{guess.kind} or {guess.alternative}"
            lines.append(f"Looks like {both} - {', '.join(guess.because)} "
                         f"(confidence {guess.confidence:.2f}).")
        elif guess.because:
            lines.append(guess.because[0].capitalize() + ".")
        lines.append(f"Where: {describe_location(self.locations, self.count, parents, self.phases)}.")
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
                "runs": [[round(x, 1), round(y, 1)] for x, y in self.runs[-RUN_MEMORY:]],
                "inrush_w": _trim(self.inrush_w, 1),
                "successor_id": self.successor_id, "carried_wh": _trim(self.carried_wh, 1),
                "low": _trim(self.low, 1), "high": _trim(self.high, 1),
                "duration_mad": _trim(self.duration_mad, 1),
                "interval_mad": _trim(self.interval_mad, 1),
                "drivers": {n: {k: [round(x, 4) for x in v] for k, v in row.items()} for n, row in self.drivers.items()},
                "inputs": {n: {k: {v: round(x, 3) for v, x in d.items()} for k, d in row.items()} for n, row in self.inputs.items()},
                "born_in": self.born_in, "takes_in": list(self.takes_in), "apart": list(self.apart),
                "born_ep": self.born_ep, "ep_seen": self.ep_seen, "last_ep": self.last_ep,
                "born_judged": self.born_judged,
                "hourly": {str(h): round(wh, 1) for h, wh in self.hourly.items() if wh >= 0.05},
                "edges": {role: {str(c): round(n, 2) for c, n in used.items()} for role, used in self.edges.items()}}

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
                   successor_id=d.get("successor_id"), carried_wh=d.get("carried_wh", 0.0),
                   low=d.get("low"), high=d.get("high"),
                   interval_mad=d.get("interval_mad"),
                   drivers={n: {k: list(v) for k, v in row.items()} for n, row in (d.get("drivers") or {}).items()},
                   inputs={n: {k: dict(v) for k, v in row.items()} for n, row in (d.get("inputs") or d.get("stages") or {}).items()},
                   born_in=d.get("born_in"),
                   takes_in=list(d.get("takes_in") or []), apart=list(d.get("apart") or []), born_ep=d.get("born_ep", 0.0),
                   ep_seen=d.get("ep_seen", 0.0), last_ep=d.get("last_ep"),
                   born_judged=d.get("born_judged", d.get("stage_judged", False)),
                   hourly={int(h): float(wh) for h, wh in (d.get("hourly") or {}).items()},
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
    better handle on a load than the size of its step (Anze, 2026-09-22).

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
        # twenty of Anze's twenty-four rows, distinguishing nothing from
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
    on RIGHT NOW turns naming into walking over and looking (Anze, 2026-09-22:
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
def _balanced(group: List[Session], candidate: Session) -> bool:
    """Could these be legs of ONE multi-phase load, by size?

    Coinciding in time is not enough. A real multi-phase load is balanced by
    design - a two-phase element, a three-phase motor - and over ten days at
    home the smallest-to-largest ratio inside kiln-sized groups had a median
    of 0.98 and only 4% below 0.4. Timing alone married a 2025 W load on A to
    a 163 W blip on C and called the pair one 2.2 kW two-phase load: a phantom
    invented, and the session stolen from the real single-phase one it
    belonged to (Anze, 2026-09-18).

    Below the threshold they are two loads that happened to start together,
    and saying so costs nothing - each is still filed on its own phase."""
    watts = [w for m in group for w in m.power_by_phase().values()]
    watts += list(candidate.power_by_phase().values())
    watts = [abs(w) for w in watts if w]
    if len(watts) < 2:
        return True
    return min(watts) >= PHASE_BALANCE_MIN * max(watts)


@dataclass
class EdgeCluster:
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
    angle: Optional[float] = None             # mean reactive angle, degrees - see EDGE_ANGLE
    where: str = ""                           # the meter its steps happened under - see EDGE_BY_METER
    angle_n: float = 0.0

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
                "where": self.where}

    @classmethod
    def from_dict(cls, d: dict) -> "EdgeCluster":
        return cls(id=int(d["id"]), phase=d["phase"], up=bool(d["up"]), watts=float(d["watts"]),
                   count=float(d.get("count", 0.0)), watts_mad=float(d.get("watts_mad", 0.0)), pf=d.get("pf"),
                   pf_mad=float(d.get("pf_mad", 0.0)), surges=float(d.get("surges", 0.0)),
                   first_seen=d.get("first_seen", 0.0), last_seen=d.get("last_seen", 0.0),
                   signals={n: dict(row) for n, row in (d.get("signals") or {}).items()},
                   lags={k: list(v) for k, v in (d.get("lags") or {}).items()},
                   values={k: list(v) for k, v in (d.get("values") or {}).items()},
                   keys=dict(d.get("keys") or {}), angle=d.get("angle"), angle_n=float(d.get("angle_n") or 0.0),
                   where=d.get("where") or "")


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


def valley_segments(hist: Dict[int, float], sd: Optional[float] = None) -> List[Tuple[int, int]]:
    """The bins of a size histogram cut into segments at the valleys of its
    smoothed density: (first bin, last bin) of each, where anything is.
    ``sd`` is the smoothing in bins - the size histogram's by default."""
    if not hist:
        return []
    sd = sd or EDGE_KERNEL / EDGE_BIN
    reach = int(4 * sd) + 1
    kernel = [math.exp(-0.5 * (k / sd) ** 2) for k in range(-reach, reach + 1)]
    total = sum(kernel)
    kernel = [k / total for k in kernel]
    lo, hi = min(hist) - reach, max(hist) + reach
    dens = [0.0] * (hi - lo + 1)
    for b, w in hist.items():
        base = b - lo - reach
        for k, kw in enumerate(kernel):
            dens[base + k] += w * kw
    floor = 0.1 * kernel[reach]                 # a tenth of one step's own peak
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


def lag_window(hist: Sequence[float]) -> Optional[Tuple[float, float]]:
    """Where an input's changes pile up against the meter's edges: (earliest,
    latest) lag in seconds, from its histogram of lags over
    -EDGE_LAG_REACH_S..+EDGE_LAG_REACH_S in EDGE_LAG_BIN_S bins. Chance puts
    changes evenly across the reach; a real link piles them in a few bins. None
    while too few are seen, or when nothing stands out of the even spread."""
    total = sum(hist)
    if total < EDGE_LAG_MIN:
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
    at = lambda i: -EDGE_LAG_REACH_S + (i + 0.5) * EDGE_LAG_BIN_S  # noqa: E731
    w = sum(hist[lo:hi + 1])
    mean = sum(at(i) * hist[i] for i in range(lo, hi + 1)) / w
    spread = math.sqrt(sum(hist[i] * (at(i) - mean) ** 2 for i in range(lo, hi + 1)) / w)
    half = max(EDGE_WINDOW_MIN_S, EDGE_WINDOW_SPREADS * spread, 0.5 * EDGE_LAG_BIN_S * (hi - lo + 1))
    return mean - half, mean + half


def edge_story(edges: Sequence[EdgeCluster], sig: "Signature") -> Dict[str, dict]:
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
    held: List[Session] = field(default_factory=list)          # closed, waiting for a partner phase
    signatures: List[Signature] = field(default_factory=list)
    recent: List[dict] = field(default_factory=list)           # last sessions with their signature id
    # ids a merge has retired, so a session filed before it still resolves to
    # the signature that swallowed it
    _moved: Dict[int, int] = field(default_factory=dict)
    # Names whose signature is gone - after a reset, or after an upgrade that
    # could not read the old library. They hold enough of a description to be
    # recognised again, and are handed back to the first signature that looks
    # like them (see _reclaim).
    orphan_names: List[dict] = field(default_factory=list)
    next_id: int = 1
    tz_offset_s: float = 0.0
    # number -> sorted [(ts, value)], each held until the next - see
    # DRIVER_MIN_RUNS; fed by the fleet, not persisted
    drivers: Dict[str, List[Tuple[float, float]]] = field(default_factory=dict)
    # setting -> sorted [(ts, value)], fed by the fleet like drivers; and
    # setting -> value -> seconds in it (recency-weighted), with how far that
    # has been counted - these two persisted
    inputs: Dict[str, List[Tuple[float, str]]] = field(default_factory=dict)
    input_time: Dict[str, Dict[str, float]] = field(default_factory=dict)
    input_until: Dict[str, float] = field(default_factory=dict)
    # setting -> value -> how many times it has come round - see INPUT_MIN_EPISODES
    input_episodes: Dict[str, Dict[str, float]] = field(default_factory=dict)
    # the edge library - see EDGE_LAG_REACH_S - and, per input, how its
    # changes fall against the meter's edges, in EDGE_LAG_BIN_S bins
    edges: List[EdgeCluster] = field(default_factory=list)
    next_edge_id: int = 1
    lag_hist: Dict[str, List[float]] = field(default_factory=dict)
    # the steps this pass took, (phase, since, watts, VAr, surge), for the
    # fleet to file as edges; and phase -> [(since, cluster id, watts)] of the
    # recent ones, so a run filed later still finds its edges. Never persisted.
    edge_at: Dict[str, List[tuple]] = field(default_factory=dict, repr=False, compare=False)
    # rises waiting the event window for companions on other phases; never persisted
    _pending: List[dict] = field(default_factory=list, repr=False, compare=False)
    # runs ended by a stop found inside a rise when its event formed - see _split_rise
    _split_closed: List[tuple] = field(default_factory=list, repr=False, compare=False)
    # the pass's work on the readings' clock - see _advance: what it released,
    # whether it files, the reading or deadline being handled, and when the
    # first waiting group is due (None: work it out again)
    _released: List[Session] = field(default_factory=list, repr=False, compare=False)
    _file_now: bool = field(default=True, repr=False, compare=False)
    _clock: Optional[float] = field(default=None, repr=False, compare=False)
    _held_due: Optional[float] = field(default=None, repr=False, compare=False)
    _q: Dict[str, Dict[float, float]] = field(default_factory=dict, repr=False, compare=False)
    # how far the inputs' changes are known on the fleet's clock (Fleet.process):
    # one a pass brought from after it is not known yet. None: all of them
    horizon: Optional[float] = field(default=None, repr=False, compare=False)
    _pv: Dict[str, Dict[float, float]] = field(default_factory=dict, repr=False, compare=False)
    # "rise>fall" cluster ids -> [runs, sum and sum of squares of log(stop/start),
    # sum and sum of squares of log(seconds)] - see PAIR_MIN_RUNS; and each
    # pair -> {signature id: runs filed there}
    pairs: Dict[str, List[float]] = field(default_factory=dict)
    # what the fleet read of the inputs for this pass: (changes, their times,
    # numbers); and what is worked out once a pass from the library
    signals: Optional[tuple] = field(default=None, repr=False, compare=False)
    _pidx: Optional[dict] = field(default=None, repr=False, compare=False)   # see _pair_index
    _by_id: Optional[Dict[int, "EdgeCluster"]] = field(default=None, repr=False, compare=False)
    _windows: Optional[Dict[str, tuple]] = field(default=None, repr=False, compare=False)
    _kinds: Optional[Dict[tuple, List["EdgeCluster"]]] = field(default=None, repr=False, compare=False)
    _by_sig: Optional[Dict[int, "Signature"]] = field(default=None, repr=False, compare=False)
    # start cluster -> {signature id: runs filed there} - see DEVICE_FILING
    start_home: Dict[str, Dict[int, float]] = field(default_factory=dict, repr=False, compare=False)
    # what _merge_devices keeps between filings, so a pass judges only the
    # devices whose signatures changed: start_home's votes by the signature
    # they count for now (devices in start_home order), each signature's
    # device, each device's place in start_home, the devices to judge again
    # and whether all of them are. None: built again from start_home.
    _votes: Optional[Dict[int, Dict[int, float]]] = field(default=None, repr=False, compare=False)
    _home: Dict[int, int] = field(default_factory=dict, repr=False, compare=False)
    _dev_pos: Dict[int, int] = field(default_factory=dict, repr=False, compare=False)
    _rejudge: Set[int] = field(default_factory=set, repr=False, compare=False)
    _rejudge_all: bool = field(default=True, repr=False, compare=False)
    # per phase|direction|inputs: bin -> recency-weighted steps, and when it
    # last faded - see EDGE_BATCH; its segments are cut again every EDGE_RECUT
    edge_hist: Dict[str, Dict[int, float]] = field(default_factory=dict, repr=False, compare=False)
    edge_hist_at: Dict[str, float] = field(default_factory=dict, repr=False, compare=False)
    edge_unit: Dict[str, float] = field(default_factory=dict, repr=False, compare=False)
    edge_hist_keys: Dict[str, Dict[str, Dict[int, float]]] = field(default_factory=dict, repr=False, compare=False)
    # "size bin:angle bin" -> weight, per phase pattern and direction - see EDGE_ANGLE
    edge_hist_angle: Dict[str, Dict[str, float]] = field(default_factory=dict, repr=False, compare=False)
    # (phase, since, window) -> [(meter, its step then)], set by the Fleet - see SPLIT_BY_METERS
    meter_steps: Optional[object] = field(default=None, repr=False, compare=False)
    # (phase, since, size, up, var) -> the innermost meter that saw all of the step, set by the Fleet - see EDGE_BY_METER
    step_meter: Optional[object] = field(default=None, repr=False, compare=False)
    # (meter, phase, since, up) -> it held its value through that moment, set by the Fleet - see _likely_meter
    meter_held: Optional[object] = field(default=None, repr=False, compare=False)
    # (phase, span start, span end, open runs) -> the run a one-device meter's own stop there ends, set by the Fleet
    meter_stop: Optional[object] = field(default=None, repr=False, compare=False)
    # (meter, phase, since, size) -> the meter whose step started that run still shows it on, set by the Fleet - see PhaseState.owned
    meter_on: Optional[object] = field(default=None, repr=False, compare=False)
    # (phase, since, size) -> the one-device meter whose own rise of that size came with a start the map could not place
    meter_started: Optional[object] = field(default=None, repr=False, compare=False)
    # (meter, phase) -> what the meter draws above its floor now, in the grid's terms - the size of a run that is its
    meter_level: Optional[object] = field(default=None, repr=False, compare=False)
    placement_conf: Optional[float] = field(default=None, repr=False, compare=False)   # the last placement's timing confidence
    _segs: Dict[str, list] = field(default_factory=dict, repr=False, compare=False)
    _recut: Dict[str, int] = field(default_factory=dict, repr=False, compare=False)
    _device_home: Optional[Dict[int, Dict[int, float]]] = field(default=None, repr=False, compare=False)

    # ------------------------------------------------ ingest
    def process(self, samples: Dict[str, Sequence[Tuple[float, float]]],
                q: Optional[Dict[str, Dict[float, float]]] = None, now_ts: Optional[float] = None,
                pv: Optional[Dict[str, Dict[float, float]]] = None,
                q_quantum: Optional[Dict[str, float]] = None, file: bool = True) -> List[Session]:
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
        self._closed(ph, self.phases[ph].process(ts, w, qm.get(ts), pvm.get(ts)))

    def finish(self, samples, latest: float, oldest: Optional[float] = None) -> List[Session]:
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
        best = None
        for ph, st in self.phases.items():
            d = st.silence_due()
            if d is not None and (best is None or d < best[0]):
                best = (d, 0, ph)
        if self._pending:
            d = min(x["since"] for x in self._pending) + self.event_wait()
            if best is None or d < best[0]:
                best = (d, 1, None)
        d = self._held_next()
        if d is not None and (best is None or d < best[0]):
            best = (d, 2, None)
        return best

    def fire(self, due: tuple) -> None:
        d, kind, ph = due
        self._clock = d if self._clock is None else max(self._clock, d)
        if kind == 0:
            self._closed(ph, self.phases[ph].stand_in(d))
        elif kind == 1:
            self._form_first()
        else:
            self._release_held()

    def _groups(self) -> List[List[Session]]:
        """The waiting runs as legs of one load: start AND end within
        MERGE_TOLERANCE_S, on other phases, balanced."""
        groups: List[List[Session]] = []
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
    def _group_due(g: List[Session]) -> float:
        """When a group stops waiting: at once with three legs, else
        HELD_TAIL_S after its last leg ended."""
        return -math.inf if len(g) >= 3 else max(m.end for m in g) + HELD_TAIL_S

    def _held_next(self) -> Optional[float]:
        if self._held_due is None:
            self._held_due = min((self._group_due(g) for g in self._groups()), default=math.inf)
        return None if self._held_due == math.inf else self._held_due

    def _release_held(self) -> None:
        """The group due first goes out as one session: filed, or handed to the
        fleet (``file`` False) - unless it is a blip."""
        g = min(self._groups(), key=lambda g: (self._group_due(g), g[0].start))
        self.held = [s for s in self.held if all(s is not m for m in g)]
        self._held_due = None
        s = self._combine(g)
        if s.energy_wh < NOISE_SESSION_WH and s.duration_s < NOISE_SESSION_S:
            return
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
        legs together, every time - Anze's point about co-occurrence - so one
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
            i = bisect.bisect_right(times, ts) - 1
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
        return check

    def signature_of(self, s: Session) -> Optional["Signature"]:
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
    def _combine(g: List[Session]) -> Session:
        if len(g) == 1:
            return g[0]
        levels = {}
        pfs = []
        for m in g:
            levels.update(m.levels)
            if m.pf is not None:
                pfs.append(m.pf)
        return Session(phases="".join(sorted(levels)), start=min(m.start for m in g), end=max(m.end for m in g),
                       levels=levels, pf=(sum(pfs) / len(pfs)) if pfs else None,
                       quality=min(m.quality for m in g),
                       surge_w=sum(m.surge_w for m in g),
                       pf_mad=max((m.pf_mad for m in g if m.pf is not None), default=0.0),
                       legs=[m.pair for m in g if m.pair])

    def _input_context(self, s: Session):
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
            share = spent.get(rows[i][1], 0.0) / total
            if share < INPUT_RARE_SHARE and (best is None or share < best[0]):
                best = (share, f"{name}={rows[i][1]}", rows[i][0])
        return (best[1], best[2]) if best else None

    def _file(self, s: Session, prefer: Optional[int] = None, avoid: Sequence[int] = ()) -> None:
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
            if want is not None and want.id not in avoid and want.files_in(context) and want.phases == s.phases:
                best = want
        if best is None and device is not None:
            best = self._device_signature(device, context, avoid, s.phases)
        if best is None:
            best = Signature(id=self.next_id, phases=s.phases, power=s.power_by_phase(), duration_s=s.duration_s,
                             pf=s.pf, count=0, first_seen=s.start, last_seen=s.start, level_count=float(s.level_count),
                             born_in=context, apart=sorted({self._current(x) for x in avoid}))
            if context:
                setting, _, value = context.partition("=")
                best.born_ep = (self.input_episodes.get(setting) or {}).get(value, 1.0) - 1.0
            self.next_id += 1
            self.signatures.append(best)
            best.absorb(s, tz)
            best.count = 1
            prev = None
        else:
            prev = best.last_start
            best.absorb(s, tz)
        for name, rows in self.drivers.items():
            # the run by the number as it started; the gap before it by the
            # number halfway through it - what the room was while it waited
            for key, at, y in (("d", s.start, s.duration_s),
                               ("g", (s.start + prev) / 2.0 if prev is not None else None,
                                s.start - prev if prev is not None else 0.0)):
                i = bisect.bisect_right(rows, (at, math.inf)) - 1 if at is not None else -1
                if i >= 0 and y > 0:
                    best.note_driver(name, key, rows[i][1], y)
        for name, rows in self.inputs.items():
            # the setting halfway through the run: a washer's heater starts
            # as its wash phase does, a moment either side of the change
            i = bisect.bisect_right(rows, ((s.start + s.end) / 2.0, "\uffff")) - 1
            total = sum((self.input_time.get(name) or {}).values())
            if i >= 0 and total > 0:
                best.note_input(name, rows[i][1], {v: t / total for v, t in self.input_time[name].items()}, s.start)
        if context and best.born_in == context and best.last_ep != episode:
            best.ep_seen += 1.0
            best.last_ep = episode
        s.signature_id = best.id
        self._touch(best.id)
        for pair in ([s.pair] if s.pair else []) + list(s.legs):
            if pair and pair[0] is not None:
                row = self.start_home.setdefault(str(pair[0]), {})
                row[best.id] = row.get(best.id, 0.0) + 1.0
                self._vote(str(pair[0]), best.id)
                if self._device_home is not None:
                    pooled = self._device_home.setdefault(pair[0], {})
                    pooled[best.id] = pooled.get(best.id, 0.0) + 1.0
        self.recent.append({"start": s.start, "end": s.end, "phases": s.phases, "kwh": round(s.energy_wh / 1000.0, 3),
                            "max_w": round(s.max_w), "levels": s.level_count, "signature": best.id})
        self.recent = self.recent[-MAX_RECENT_SESSIONS:]
        for sig in self._judge_born():
            self._touch(sig.id)
        if self.orphan_names:
            # a name lands on a signature as it stands after this filing
            for sig in self.signatures:
                self._reclaim(sig, noise)
        self._prune(s.end)

    def _judge_born(self) -> List["Signature"]:
        """A born-in signature that has seen its value come round
        INPUT_MIN_EPISODES times belongs to it if it ran in enough of them,
        and is ordinary again - running in that value now and then - if not.
        Returns the ones judged."""
        judged = []
        for sig in self.signatures:
            if not sig.born_in or sig.born_judged:
                continue
            setting, _, value = sig.born_in.partition("=")
            since = (self.input_episodes.get(setting) or {}).get(value, 0.0) - sig.born_ep
            if since < INPUT_MIN_EPISODES:
                continue
            if sig.ep_seen / since >= INPUT_MIN_COVERAGE:
                sig.born_judged = True
            else:
                sig.takes_in = sorted(set(sig.takes_in) | {sig.born_in})
                sig.born_in = None
            judged.append(sig)
        return judged

    def classify(self, ph: str, since: float, watts: float, var: Optional[float], surge: float) -> Optional[int]:
        """Which cluster a fall belongs to, at once. A rise gets its cluster
        through note_rise, once the event window has shown which other
        phases rose with it - see EVENT_WINDOW_INTERVALS."""
        if watts > 0:
            return None
        cluster = self._classify_step(ph, since, watts, var, surge)
        self.edge_at.setdefault(ph, []).append((since, cluster.id, watts))
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
        cluster, members = self._form_event(pend[0], pend, self.event_window())
        for m in members:
            st = self.phases.get(m["ph"])
            if st is not None:
                self._closed(m["ph"], st.end_older(cluster.id, m["since"], m["open"]))
        self._closed(None, [])                    # and what forming it split off (_split_rise)

    def _form_event(self, first: dict, pend: List[dict], window: float):
        """``first`` and whatever rose beside it within ``window`` on other
        phases become one event and get its cluster; the members leave the
        pending list. Returns (cluster, members)."""
        members, phases = [first], {first["ph"]}
        for q in sorted(pend, key=lambda x: abs(x["since"] - first["since"])):
            if q is first or q["ph"] in phases or abs(q["since"] - first["since"]) > window:
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
        cluster = self._classify_step(pattern, since, sum(m["watts"] for m in members),
                                      sum(vars_) if all(v is not None for v in vars_) else None,
                                      max(m["surge"] for m in members))
        conf, self.placement_conf = self.placement_conf, None
        for m in members:
            m["open"].cluster = cluster.id
            if cluster.where and conf is not None:
                m["open"].q *= 0.5 + 0.5 * conf     # how well the meters' timings agreed
                # placed by the meter's own step: its run - see PhaseState.owned
                # (unless a total-only meter's rise already took it, _split_rise)
                m["open"].meter = m["open"].meter or cluster.where
            elif len(members) > 1 and self.meter_started is not None and self.phases.get(m["ph"]) is not None:
                # a leg of an event on several phases: a total-only meter's
                # rise of all of it at this moment makes the leg its own, as
                # _split_rise does for a start alone - Susilna's plug switching
                # on in the same reading as a rise on phase C (09-21, 09-24)
                found = self.meter_started(m["ph"], m["since"], m["watts"])
                if found is not None:
                    st = self.phases[m["ph"]]
                    if m["watts"] - found[1] < max(st.noise_at(), MATCH_EDGE_REL * m["watts"]):
                        m["open"].meter = found[0]
            self.edge_at.setdefault(m["ph"], []).append((m["since"], cluster.id, m["watts"]))
        return cluster, members

    def _split_rise(self, m: dict) -> None:
        """A single-phase rise cut into the shares the meters below took with
        it - see SPLIT_BY_METERS. The first shares become rises of their own,
        each its meter's (PhaseState.owned); the last stays this one. A meter
        the map does not place is asked too (meter_started): a rise of its
        own at this moment that is all of this start makes the start its, one
        that is part of it takes that part and leaves the rest a start of its
        own - Home's Susilna plug (265 W) switching on with a 48 W fan, a +313
        W start the plug neither owned nor matched, cut minutes later with the
        fan's stop (09-21, 09-24; Anze, 2026-10-02: a separate fan in all
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
        first = next((x for x in self._pending if x["open"] is o), None)
        if first is not None:
            self._form_event(first, list(self._pending), self.event_window())

    def _classify_step(self, ph: str, since: float, watts: float, var: Optional[float], surge: float) -> "EdgeCluster":
        """File a step - or an all-phase event, ``ph`` then being its phase
        pattern and ``watts`` its total - as an edge (see EDGE_LAG_REACH_S)
        and return its cluster."""
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
        placed = len(ph) == 1 and watts > 0
        where = (self.step_meter(ph, since, size, watts > 0, var) or "") if placed and self.step_meter is not None else ""
        if self._kinds is None:
            self._kinds = {}
            for c in self.edges:
                self._kinds.setdefault((c.phase, c.up), []).append(c)
        kind = self._kinds.setdefault((ph, watts > 0), [])
        if placed and not where:
            where = self._likely_meter(ph, watts > 0, size, since)
        angle = math.degrees(math.atan2(var, size)) if EDGE_ANGLE and var is not None and size > 0 else None
        cluster, keys, where = self._by_density(ph, watts > 0, since, size, keyed, kind, angle, where)
        if cluster is None:
            cluster = EdgeCluster(id=self.next_edge_id, phase=ph, up=watts > 0, watts=size, keys=keys, where=where)
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
                    kind: List["EdgeCluster"], angle: Optional[float] = None, where: str = ""):
        """(the cluster whose segment of this phase and direction's size density
        the step falls in, or None for a new one; the keys a new one gets).

        What the inputs did is part of the clustering, not a split made before
        it (Anze, 2026-09-30): a step that came with an input's change joins a
        cluster of its own only where such steps pile up in the segment far
        above chance - the floor mat's +630 W with its thermostat, beside a
        look-alike's +630 W with nothing - and otherwise the segment's plain
        one. See EDGE_BATCH."""
        g = f"{ph}|{int(up)}" + (f"|{where}" if where else "")
        key = ",".join(f"{n}={k}" for n, k in sorted(keyed.items()) if k)
        if g not in self.edge_unit:
            noise = sum((self.phases[p].noise or MIN_NOISE_W) if p in self.phases else MIN_NOISE_W for p in ph)   # a pattern: its phases' noise together
            self.edge_unit[g] = EDGE_NOISE_SHARE * noise
        unit = self.edge_unit[g]
        b = int(math.floor(edge_scale(size, unit) / EDGE_BIN))
        h = self.edge_hist.setdefault(g, {})
        hk = self.edge_hist_keys.setdefault(g, {})
        ha = self.edge_hist_angle.setdefault(g, {})
        at = self.edge_hist_at.get(g)
        if at is None:
            self.edge_hist_at[g] = since
        elif since - at > 3600.0:
            fade = math.exp(-(since - at) / EDGE_TAU_S)
            for hist in [h, ha] + list(hk.values()):
                for k in list(hist):
                    hist[k] *= fade
                    if hist[k] < 1e-3:
                        del hist[k]
            self.edge_hist_at[g] = since
        h[b] = h.get(b, 0.0) + 1.0
        if key:
            row = hk.setdefault(key, {})
            row[b] = row.get(b, 0.0) + 1.0
        if angle is not None:
            ab = int(math.floor(angle / EDGE_ANGLE_BIN))
            ha[f"{b}:{ab}"] = ha.get(f"{b}:{ab}", 0.0) + 1.0
        segs = self._segs.get(g)
        if segs is None or self._recut.get(g, 0) >= EDGE_RECUT or not any(lo <= b <= hi for lo, hi in segs):
            segs = self._segs[g] = valley_segments(h)
            self._recut[g] = 0
        self._recut[g] += 1
        plain = {n: "" for n in keyed}
        seg = next(((lo, hi) for lo, hi in segs if lo <= b <= hi), None)
        if seg is None:
            return None, (keyed if key and self._keyed_above_chance(hk.get(key, {}), h, b, b, keyed) else plain), where
        members = [c for c in kind if c.where == where
                   and seg[0] <= int(math.floor(edge_scale(c.watts, unit) / EDGE_BIN)) <= seg[1]]
        if angle is not None:
            members = self._same_angle(ha, seg, angle, members)
        if key and self._keyed_above_chance(hk.get(key, {}), h, seg[0], seg[1], keyed):
            mine = [c for c in members if c.same_signals(keyed)]
            return (max(mine, key=lambda c: c.count) if mine else None), keyed, where
        mine = [c for c in members if c.same_signals(plain)]
        return (max(mine, key=lambda c: c.count) if mine else None), plain, where

    def _same_angle(self, ha: Dict[str, float], seg: Tuple[int, int], angle: float,
                    members: List["EdgeCluster"]) -> List["EdgeCluster"]:
        """The size segment's clusters whose reactive angle lies in the step's
        own band of the segment's angle density - see EDGE_ANGLE."""
        col: Dict[int, float] = {}
        for k, w in ha.items():
            sb, ab = k.split(":")
            if seg[0] <= int(sb) <= seg[1]:
                col[int(ab)] = col.get(int(ab), 0.0) + w
        ab = int(math.floor(angle / EDGE_ANGLE_BIN))
        band = next(((lo, hi) for lo, hi in valley_segments(col, EDGE_ANGLE_KERNEL / EDGE_ANGLE_BIN)
                     if lo <= ab <= hi), (ab, ab))
        return [c for c in members if c.angle is not None
                and band[0] <= int(math.floor(c.angle / EDGE_ANGLE_BIN)) <= band[1]]

    def _likely_meter(self, ph: str, up: bool, size: float, since: Optional[float] = None) -> str:
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
            g = f"{ph}|{int(up)}" + (f"|{where}" if where else "")
            unit, segs = self.edge_unit.get(g), self._segs.get(g)
            if not unit or not segs:
                return 0.0
            b = int(math.floor(edge_scale(size, unit) / EDGE_BIN))
            seg = next(((lo, hi) for lo, hi in segs if lo <= b <= hi), None)
            if seg is None:
                return 0.0
            return max((c.count for c in self._kinds.get((ph, up), []) if c.where == where
                        and seg[0] <= int(math.floor(edge_scale(c.watts, unit) / EDGE_BIN)) <= seg[1]), default=0.0)
        plain = busiest("")
        held = (lambda w: since is not None and self.meter_held is not None and self.meter_held(w, ph, since, up))
        best = max(((busiest(w), w) for w in {c.where for c in self._kinds.get((ph, up), [])} if w and not held(w)),
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
            window = self.window(name)
            near = sum(self.lag_hist.get(name) or [])
            if window is None or not near:
                return False
            p *= min(1.0, near / seen) * min(1.0, (window[1] - window[0]) / (2 * EDGE_LAG_REACH_S))
        return above_chance(n, p * sum(w for b, w in h.items() if lo <= b <= hi))

    def _device_signature(self, device: int, context: Optional[str], avoid: Sequence[int],
                          phases: str) -> Optional["Signature"]:
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
            if sig is not None and sig.id not in avoid and sig.files_in(context) and sig.phases == phases:
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
        if not moved:
            return 0
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

    def device_of(self, s: "Session") -> Optional[int]:
        """The start cluster of the run, or of its first leg: its device."""
        return next((pair[0] for pair in ([s.pair] if s.pair else []) + list(s.legs)
                     if pair and pair[0] is not None), None)

    def note_pair(self, start: Optional[int], stop: Optional[int], start_w: float, stop_w: float, secs: float) -> None:
        if start is None or stop is None or start_w <= 0 or stop_w <= 0:
            return
        idx = self._pair_index()                 # before this run counts in it
        key = f"{start}>{stop}"
        acc = self.pairs.setdefault(key, [0.0] * 6)
        if len(acc) < 6:
            acc.append(0.0)
        lr, ld = math.log(stop_w / start_w), math.log(max(secs, 1.0))
        acc[5] = max(acc[5], secs)
        acc[0] += 1.0
        acc[1] += lr
        acc[2] += lr * lr
        acc[3] += ld
        acc[4] += ld * ld
        c = self.cluster(start)
        self._count_pair(idx, key, start, stop, 1.0, idx["phase"].get(key) or (c.phase if c else "?"))

    def _pair_index(self) -> dict:
        """The pairs' running totals - closes per start, per stop and per
        phase - and those with PAIR_MIN_RUNS, by start and by stop: what
        partners asks, kept as each pair is learned (see PAIR_MIN_RUNS). Built
        from ``pairs`` on first use, so a restored library has it too."""
        if self._pidx is None:
            idx = self._pidx = {"start": {}, "stop": {}, "closes": {}, "phase": {}, "by_stop": {}, "by_start": {}}
            phase_of = {c.id: c.phase for c in self.edges}
            for key, acc in self.pairs.items():
                a, b = (int(x) for x in key.split(">"))
                self._count_pair(idx, key, a, b, acc[0], phase_of.get(a, "?"))
        return self._pidx

    @staticmethod
    def _count_pair(idx: dict, key: str, a: int, b: int, n: float, phase: str) -> None:
        idx["phase"][key] = phase
        idx["start"][a] = idx["start"].get(a, 0.0) + n
        idx["stop"][b] = idx["stop"].get(b, 0.0) + n
        idx["closes"][phase] = idx["closes"].get(phase, 0.0) + n
        idx["by_stop"].setdefault(b, {})[a] = key
        idx["by_start"].setdefault(a, {})[b] = key

    def _pair_model(self, idx: dict, a: int, b: int) -> Optional[tuple]:
        """(stop/start ratio, its spread, mean log seconds, its spread) of the
        pair a > b, or None while it is not accepted - see PAIR_MIN_RUNS."""
        key = f"{a}>{b}"
        acc = self.pairs.get(key)
        n = acc[0] if acc else 0.0
        if n < PAIR_MIN_RUNS or not above_chance(n, idx["start"][a] * idx["stop"][b] / idx["closes"][idx["phase"][key]]):
            return None
        m_lr, m_ld = acc[1] / n, acc[3] / n
        sd_lr = math.sqrt(max(0.0, acc[2] / n - m_lr * m_lr))
        sd_ld = math.sqrt(max(0.0, acc[4] / n - m_ld * m_ld))
        return (math.exp(m_lr), math.exp(m_lr) * sd_lr, m_ld, sd_ld)

    def window(self, name: str) -> Optional[Tuple[float, float]]:
        """This input's learned lag window (see lag_window), kept until its histogram changes."""
        if self._windows is None:
            self._windows = {}
        if name not in self._windows:
            self._windows[name] = lag_window(self.lag_hist.get(name) or [])
        return self._windows[name]

    def cluster(self, cid: int) -> Optional["EdgeCluster"]:
        c = self._by_id.get(cid) if self._by_id is not None else None
        if c is None:
            self._by_id = {x.id: x for x in self.edges}      # one born this pass
            c = self._by_id.get(cid)
        return c

    def input_end(self, cluster: int, since: float, now: float) -> Optional[float]:
        """When a run that started with this cluster must have stopped, on
        the meter's clock, if its input has changed back and the window for
        the stop has passed by ``now`` - else None. See INPUT_ENDS."""
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
                    return due if now > due + half else None
        return None

    def usual_length(self, start: int) -> Optional[float]:
        """Seconds a run that starts with this cluster usually lasts, over its
        accepted pairs - None when it has none."""
        idx = self._pair_index()
        lens = [(model[2], self.pairs[key][0]) for b, key in (idx["by_start"].get(start) or {}).items()
                for model in [self._pair_model(idx, start, b)] if model is not None]
        w = sum(n for _, n in lens)
        return math.exp(sum(ld * n for ld, n in lens) / w) if w else None

    def partners(self, stop: int) -> Dict[int, tuple]:
        """rise cluster -> (stop/start ratio, its spread, mean log seconds, its
        spread) for the accepted pairs this fall cluster ends - see
        PAIR_MIN_RUNS. As learned so far: worked out once a pass, a backfill's
        slice asked models six hours old, a live pass one minute's and one
        call none at all."""
        idx = self._pair_index()
        out = {}
        for a in (idx["by_stop"].get(stop) or {}):
            model = self._pair_model(idx, a, stop)
            if model is not None:
                out[a] = model
        return out

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
        evicted, the kiln founded and lost 296 times (Anze, 2026-09-18).

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

    def active_by_name(self, now_ts: float) -> Dict[str, float]:
        """Watts on right now per NAME - signatures sharing a name are one
        device, which is what giving two of them the same name means."""
        out: Dict[str, float] = {}
        for a in self.active(now_ts):
            if a.get("name"):
                out[a["name"]] = out.get(a["name"], 0.0) + float(a["watts"])
        return out

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

    def running_now(self, now_ts: float) -> set:
        """Signature ids believed to be on right now."""
        return {a.get("signature") for a in self.active(now_ts) if a.get("signature")}

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
            stub = Signature(id=-1, phases=orphan.get("phases") or "",
                             power=dict(orphan.get("power") or {}),
                             duration_s=orphan.get("duration_s") or 0.0,
                             pf=orphan.get("pf"), count=1, first_seen=0.0, last_seen=0.0)
            if stub.alike(sig, noise_w):
                sig.name = orphan.get("name")
                self.orphan_names.pop(i)
                self._touch(sig.id)
                return

    def rename(self, signature_id: int, name: Optional[str]) -> bool:
        for sig in self.signatures:
            if sig.id == signature_id:
                sig.name = (name or "").strip() or None
                self._touch(sig.id)
                return True
        return False

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
        should still describe this behaviour rather than an average of two."""
        old = self.predecessor_of(signature_id)
        if old is None or not old.name:
            return None
        name = old.name
        heir = next((s for s in self.signatures if s.id == signature_id), None)
        if heir is None:
            return None
        heir.carried_wh += old.energy_wh
        old.name, old.successor_id = None, None
        self._touch(old.id)
        self.rename(signature_id, name)
        return name

    # ------------------------------------------------ storage
    def to_dict(self) -> dict:
        return {"phases": {p: st.to_dict() for p, st in self.phases.items()}, "held": [s.to_dict() for s in self.held],
                "signatures": [s.to_dict() for s in self.signatures], "recent": self.recent, "next_id": self.next_id,
                "tz_offset_s": self.tz_offset_s, "orphan_names": self.orphan_names,
                "input_time": {n: {v: _trim(t, 1) for v, t in row.items()} for n, row in self.input_time.items()},
                "input_until": dict(self.input_until),
                "input_episodes": {n: dict(row) for n, row in self.input_episodes.items()},
                "edges": [e.to_dict() for e in self.edges], "next_edge_id": self.next_edge_id,
                "edge_hist": {g: {str(b): round(w, 3) for b, w in h.items() if w >= 0.01} for g, h in self.edge_hist.items()},
                "edge_hist_at": dict(self.edge_hist_at), "edge_unit": dict(self.edge_unit),
                "edge_hist_keys": {g: {k: {str(b): round(w, 3) for b, w in h.items() if w >= 0.01} for k, h in rows.items()}
                                   for g, rows in self.edge_hist_keys.items()},
                "edge_hist_angle": {g: {k: round(w, 3) for k, w in h.items() if w >= 0.01} for g, h in self.edge_hist_angle.items()},
                "start_home": {k: {str(i): n for i, n in v.items()} for k, v in self.start_home.items()},
                "lag_hist": {n: [_trim(x, 2) for x in h] for n, h in self.lag_hist.items()},
                "pairs": {k: [_trim(x, 4) for x in v] for k, v in self.pairs.items()},
                # rises still in their event window - a pass's end no longer forms them
                "pending_rises": [[m["ph"], i, m["since"], m["watts"], m["var"], m["surge"]] for m in self._pending
                                  for i, o in enumerate(self.phases[m["ph"]].open_edges if m["ph"] in self.phases else [])
                                  if o is m["open"]],
                }

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Detector":
        det = cls()
        if not d:
            return det
        det.phases = {p: PhaseState.from_dict((d.get("phases") or {}).get(p)) for p in PHASES}
        det.held = [Session.from_dict(x) for x in d.get("held") or []]
        det.signatures = [Signature.from_dict(x) for x in d.get("signatures") or []]
        det.recent = list(d.get("recent") or [])
        det.next_id = d.get("next_id", 1)
        det.tz_offset_s = d.get("tz_offset_s", 0.0)
        det.orphan_names = [x for x in (d.get("orphan_names") or []) if x.get("name")]
        det.input_time = {n: {v: float(t) for v, t in row.items()} for n, row in (d.get("input_time") or d.get("stage_time") or {}).items()}
        det.input_until = {n: float(t) for n, t in (d.get("input_until") or d.get("stage_until") or {}).items()}
        det.input_episodes = {n: {v: float(c) for v, c in row.items()} for n, row in (d.get("input_episodes") or d.get("stage_episodes") or {}).items()}
        det.edges = [EdgeCluster.from_dict(x) for x in d.get("edges") or []]
        det.edge_hist = {g: {int(b): float(w) for b, w in h.items()} for g, h in (d.get("edge_hist") or {}).items()}
        det.edge_hist_at = {g: float(t) for g, t in (d.get("edge_hist_at") or {}).items()}
        det.edge_unit = {g: float(u) for g, u in (d.get("edge_unit") or {}).items()}
        det.edge_hist_keys = {g: {k: {int(b): float(w) for b, w in h.items()} for k, h in rows.items()}
                              for g, rows in (d.get("edge_hist_keys") or {}).items()}
        det.edge_hist_angle = {g: {k: float(w) for k, w in h.items()} for g, h in (d.get("edge_hist_angle") or {}).items()}
        det.start_home = {k: {int(i): float(n) for i, n in v.items()} for k, v in (d.get("start_home") or {}).items()}
        det.next_edge_id = d.get("next_edge_id", 1)
        det.lag_hist = {n: [float(x) for x in h] for n, h in (d.get("lag_hist") or {}).items()}
        det.pairs = {k: [float(x) for x in v] for k, v in (d.get("pairs") or {}).items()}
        for ph, i, since, watts, var, surge in d.get("pending_rises") or []:
            opens = det.phases[ph].open_edges if ph in det.phases else []
            if 0 <= i < len(opens) and opens[i].since == since:
                det._pending.append({"since": since, "ph": ph, "watts": watts, "var": var, "surge": surge,
                                     "open": opens[i]})
        return det


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
    pending_sub: Dict[str, List[Session]] = field(default_factory=dict)
    # Each device's raw samples, kept long enough to answer "how much energy
    # did you record while this was running". A meter too slow to produce a
    # session of its own can still answer that.
    sub_rows: Dict[str, List[Tuple[float, float]]] = field(default_factory=dict)
    agnostic: Dict[str, bool] = field(default_factory=dict)      # meters that report only a total
    # meter -> "p" (power) / "q" (reactive) -> [mean log of grid's step over the
    # meter's, steps] - see METER_GAIN_MIN
    meter_gain: Dict[str, Dict[str, List[float]]] = field(default_factory=dict)
    # meter -> the meter it hangs under, as the Energy dashboard nests them; set by the runner
    parents: Dict[str, Optional[str]] = field(default_factory=dict)
    # the readings' clock all meters are read on: the meters' own, the grid
    # wait_cap_s behind them (see process); how far every meter's readings reach
    _now: float = field(default=0.0, repr=False, compare=False)
    # sub-meter sessions waiting to vote on their channel's phase: (when, meter, past the patience, session) - see _vote_blocked
    _unvoted: List[tuple] = field(default_factory=list, repr=False, compare=False)
    # house sessions handed over lately, (when, session): what a meter's session votes against
    _recent_main: List[tuple] = field(default_factory=list, repr=False, compare=False)
    _dues: Dict[str, Optional[tuple]] = field(default_factory=dict, repr=False, compare=False)   # see _advance
    _names: List[str] = field(default_factory=list, repr=False, compare=False)
    _phase_maps: Dict[str, tuple] = field(default_factory=dict, repr=False, compare=False)   # see phase_map
    _sub_last: Dict[str, Dict[str, float]] = field(default_factory=dict)   # each meter's channels' last values - see _keep_rows
    _stops_used: Dict[tuple, float] = field(default_factory=dict, repr=False, compare=False)   # see _meter_stop
    wait_cap_s: float = 0.0          # set by the runner - see METER_WAIT_CAP_S
    # how far behind its meters the grid is read now, and the MODEL_REFRESH_S
    # step of the clock it was worked out at - see _horizon
    _wait: float = 0.0
    _wait_at: Optional[int] = None
    # meter -> [[report lag, declaring lag], ...] against the grid, the last
    # LAG_SAMPLES of them, and the clock up to which grid steps were asked - see _learn_lags
    meter_lag: Dict[str, List[List[float]]] = field(default_factory=dict)
    _lag_from: Optional[float] = None
    # meters the runner says are not to set the horizon (the settings page) - see _horizon
    horizon_skip: set = field(default_factory=set, repr=False, compare=False)
    # the grid's readings, reactive and PV newer than the wait, for the next pass
    _carry: Dict[str, dict] = field(default_factory=dict, repr=False, compare=False)
    # switch -> {on moment: off moment, or None while on}, as the runner last
    # read them - see SWITCH_PREFIX. Never persisted: every pass re-reads its
    # window with a lookback.
    switch_on: Dict[str, Dict[float, Optional[float]]] = field(default_factory=dict)
    # meters that hold one device (True) or several (False), as the runner
    # works it out each pass - the user's answer, else a parent holds several,
    # else guess_one_device; a meter missing here is guessed
    single: Dict[str, bool] = field(default_factory=dict)
    # meter -> its own phase label -> house phase -> sessions that matched,
    # for phase_mapping
    phase_votes: Dict[str, Dict[str, Dict[str, int]]] = field(default_factory=dict)
    # house sessions not yet filed, waiting for a sub-meter partner: (when they stop waiting, session)
    unfiled: List[tuple] = field(default_factory=list)
    # meter -> its signature id -> the house signature its sessions joined
    identity: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def process(self, main_samples, sub_samples: Dict[str, Dict[str, Sequence[Tuple[float, float]]]],
                main_q=None, sub_q=None, now_ts: Optional[float] = None,
                agnostic: Optional[Dict[str, bool]] = None,
                pv: Optional[Dict[str, Dict[float, float]]] = None,
                main_q_quantum: Optional[Dict[str, float]] = None,
                single: Optional[Dict[str, bool]] = None,
                switches: Optional[Dict[str, Sequence[Tuple[float, Optional[float]]]]] = None,
                drivers: Optional[Dict[str, Sequence[Tuple[float, float]]]] = None,
                inputs: Optional[Dict[str, Sequence[Tuple[float, str]]]] = None) -> None:
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
        if agnostic:
            self.agnostic.update(agnostic)
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
        for store, fed, cast in ((self.main.drivers, drivers, float), (self.main.inputs, inputs, str)):
            for name, rows in (fed or {}).items():
                held = dict(store.get(name) or [])
                held.update((float(t), cast(v)) for t, v in rows)
                last = max((t for t in held if t < keep), default=None)   # still in force at the cut
                store[name] = sorted((t, v) for t, v in held.items() if t >= keep or t == last)
        if single is not None:
            self.single = dict(single)      # the whole declaration, so a withdrawn one lapses
        sub_samples = sub_samples or {}
        latest = max([now_ts or 0.0] + [r[-1][0] for byp in [main_samples or {}] + list(sub_samples.values())
                                         for r in byp.values() if r])
        end = now_ts if now_ts is not None else latest
        # only the main meter needs the array: a downstream meter sees the
        # house side of it and never the sun
        for name, rows_by_phase in sub_samples.items():
            self._keep_rows(name, rows_by_phase, end)
        events, numbers = self._signal_events()
        self.main.signals = (events, {n: [t for t, _ in evs] for n, evs in events.items()}, numbers)
        self.main.meter_steps = self._meter_steps
        self.main.step_meter = self._step_meter
        self.main.meter_held = self._meter_held
        self.main.meter_stop = self._meter_stop
        self.main.meter_on = self._meter_on
        self.main.meter_started = self._meter_started
        self.main.meter_level = self._meter_level
        # the grid's readings, the last pass's held ones in front: each is
        # read once the clock is the horizon past it (_run); the rest wait
        main_samples, main_q, pv = self._hold_back(main_samples, main_q, pv, math.inf)
        grid = self.main.begin(main_samples, main_q, pv, main_q_quantum, file=False)
        meters = []
        for name in sorted(set(self.subs) | set(sub_samples)):
            det = self.subs.setdefault(name, Detector())
            det.tz_offset_s = self.main.tz_offset_s
            meters += [(ts, name, ph, w) for ts, ph, w in det.begin(sub_samples.get(name) or {}, (sub_q or {}).get(name))]
        meters.sort(key=lambda r: r[:3])
        self._dues, self._names = {}, [""] + sorted(self.subs)
        if self._wait_at is None:
            first = min([end] + [r[0] for r in grid[:1] + meters[:1]])
            self._wait_at = math.floor(first / MODEL_REFRESH_S)
        done = self._run(grid, meters, end)
        left: Dict[str, list] = {}
        for ts, ph, w in grid[done:]:
            left.setdefault(ph, []).append((ts, w))
        self._hold_back(left, main_q, pv, -math.inf)          # kept, with their reactive and PV values
        self._now = max(self._now, end)
        self.main.finish(main_samples, self.main._clock or end, self._oldest_asked())
        for name, det in self.subs.items():
            det.finish(sub_samples.get(name) or {}, end)

    def _run(self, grid: List[tuple], meters: List[tuple], end: float) -> int:
        """Every reading and everything due, in ONE order on one clock, up to
        how far this pass's readings reach (``end``): a meter's reading at its
        own time, the grid's once the clock is the horizon past it (_wait, see
        _horizon), and what falls due between them - each meter's own
        (Detector.next_due; the grid's the horizon later), a house session
        done waiting for the meters (_decide), one more try at placing a
        filed one (_locate), a meter's session's vote (_vote_phases), the
        horizon worked out again. At one moment the readings first - the
        grid's, then the meters' - and then what is due, in that order.
        Returns how many of the grid's readings were read; the rest wait for
        the next pass."""
        im = isub = 0
        while True:
            best = (max(grid[im][0] + self._wait, self._now), 0, 0) if im < len(grid) else None
            if isub < len(meters) and (best is None or (meters[isub][0], 0, 1) < best):
                best = (meters[isub][0], 0, 1)
            for name in self._names:
                if name not in self._dues:
                    self._dues[name] = (self.subs[name] if name else self.main).next_due()
                d = self._dues[name]
                if d is not None:
                    c = (d[0] + self._wait, 1, 0) if not name else (d[0], 1, 1, name)
                    if best is None or c < best:
                        best = c
            for c in ((self.unfiled[0][0], 1, 2) if self.unfiled else None,
                      (self.pending_main[0][0], 1, 3) if self.pending_main else None,
                      (self._unvoted[0][0], 1, 4) if self._unvoted else None,
                      ((self._wait_at + 1) * MODEL_REFRESH_S, 1, 5)):
                if c is not None and (best is None or c < best):
                    best = c
            at, cls, rank = best[:3]
            if at > end or (cls == 1 and at >= end):
                return im
            self._now = self.main.horizon = max(self._now, at)
            if cls == 0 and rank == 0:
                ts, ph, w = grid[im]
                im += 1
                self.main.step(ts, ph, w)
                self._dues.pop("", None)
            elif cls == 0:
                ts, name, ph, w = meters[isub]
                isub += 1
                self.subs[name].step(ts, ph, w)
                self._dues.pop(name, None)
            elif rank <= 1:
                name = best[3] if rank else ""
                det = self.subs[name] if name else self.main
                det.fire(self._dues.pop(name))
                got, det._released = det._released, []
                for s in got:
                    self._on_main(s) if not name else self._on_sub(name, s)
            elif rank == 2:
                self._decide(self.unfiled.pop(0)[1])
            elif rank == 3:
                self._locate(self.pending_main.pop(0)[1], final=True)
            elif rank == 4:
                _, name, final, s = self._unvoted.pop(0)
                if not final and self._vote_blocked(name, s):
                    # the grid may still hand over a session started with it:
                    # it votes when the grid's last such run is out (_on_main),
                    # or once MATCH_PATIENCE_S is over, with what is out by then
                    self._insort(self._unvoted, (s.end + MATCH_PATIENCE_S, name, True, s))
                else:
                    self._vote(name, s)
            else:
                self._learn_lags(at)
                self._wait, self._wait_at = self._horizon(), math.floor(at / MODEL_REFRESH_S)
                self._dues = {}                   # every meter's latency may have moved

    def _horizon(self) -> float:
        """How far behind its meters the grid is read: the longest any meter
        takes to declare a step it shares with the grid (_declare_lag), so
        every meter has had its say on a grid step before the grid's is
        judged; at least EDGE_LAG_REACH_S where inputs are fed, whose changes
        are looked for that far either side of a step. Never past
        wait_cap_s (Anze, 2026-10-02: up to five minutes): a meter slower
        than that is judged with what it has said by then. Only a meter that
        has shared a step with the grid sets it - its lag once learned, twice
        its latency until then: one that never shares a step (Home's Server
        UPS, a 60 s heartbeat) has nothing the grid waits for (Anze,
        2026-10-02) - until then Home sat at the cap for it. Only the learned
        ones setting it left Kozolec's first hours at no horizon at all (its
        pond EVSE 99 -> 88 % captured). A meter in horizon_skip - the settings
        page's own choice, a meter that shares steps but reports them slowly -
        is still read, and does not set it. Worked out again every
        MODEL_REFRESH_S of the clock as the lags are learned - at a pass's
        start it would move with the slicing."""
        wait = max([0.0] + [self._declare_lag(name) for name in self.subs
                            if name not in self.horizon_skip and self.meter_lag.get(name)])
        if self.switch_on or self.main.inputs:
            wait = max(wait, EDGE_LAG_REACH_S)
        return min(self.wait_cap_s, wait)

    def _meter_latency(self, name: str) -> float:
        """A meter's latency - its channels' longest (PhaseState.latency)."""
        det = self.subs.get(name)
        return max((st.latency() for st in det.phases.values() if st.interval), default=0.0) if det else 0.0

    def _declare_lag(self, name: str) -> float:
        """How long after a step the grid shares with it this meter has
        declared its own: the LAG_PERCENTILE of what was learned, once
        LAG_MIN_SAMPLES are; until then its latency twice over - the report,
        then the silence that confirms it."""
        rows = self.meter_lag.get(name) or []
        if len(rows) >= LAG_MIN_SAMPLES:
            got = sorted(r[1] for r in rows)
            return max(0.0, got[int(LAG_PERCENTILE * (len(got) - 1))])
        return 2.0 * self._meter_latency(name)

    def _learn_lags(self, at: float) -> None:
        """Each meter's lag against the grid, from the steps the two share:
        for every clear grid step at least LAG_REACH_S old on the clock - so
        the meter has had its whole reach to declare its own, whatever the
        slicing - the meter's declared step that way of its size on a channel
        mapped to that phase, where each is the only one of its kind on its
        meter within the reach: (when it happened, when it was declared)
        less the grid step's time.
        The meter's report lag sets its latency (PhaseState.lag), the
        declaring lag the grid's horizon (_horizon)."""
        reach = LAG_REACH_S
        upto = at - reach
        # a lag is at most what the two meters' own reporting allows: a step
        # the meter shows 100 s after the grid is another step of a wandering
        # load, not a late report - Home's office plug (a computer) read a
        # 95th report lag of 109 s and a declaring lag of 190 s that way, and
        # set Home's horizon (2026-10-02)
        bound_g = self.main.phases and max(st.latency() for st in self.main.phases.values())
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
                chans = [c for c, h in self.phase_map(name).items() if h == ph and c in det.phases]
                if len(chans) != 1:
                    continue                      # a meter on several channels of it: whose step is it
                st = det.phases[chans[0]]
                gain, noise_m = self.gain(name, "p"), st.noise_at()
                rows = self.meter_lag.setdefault(name, [])
                bound = st.latency() + (bound_g or 0.0) + MERGE_TOLERANCE_S
                for g in steps:
                    tol = math.hypot(noise_g, noise_m) + METER_CAL_SLACK * abs(g[1])

                    def like(e, size_tol):
                        return ((e[1] > 0) == (g[1] > 0) and abs(abs(e[1]) * gain - abs(g[1])) <= size_tol
                                and abs(e[0] - g[0]) <= bound)
                    near = [e for e in self._near(st, g[0] - reach, g[0] + reach, reach) if like(e, tol)]
                    # one of its kind on both meters within the reach, or a
                    # cycling load's next run is taken for this one's late
                    # report and the lag reads minutes (Kozolec's boiler,
                    # pulsing every four: its 95th 256 s)
                    twins = [x for x in self._near(grid, g[0] - reach, g[0] + reach, reach)
                             if x is not g and (x[1] > 0) == (g[1] > 0) and abs(abs(x[1]) - abs(g[1])) <= 2.0 * tol]
                    if len(near) != 1 or twins:
                        continue
                    e = near[0]
                    rows.append([round(e[0] - g[0], 2), round((e[5] if len(e) > 5 else e[4]) - g[0], 2)])
                del rows[:-LAG_SAMPLES]
        for name, det in self.subs.items():
            rows = self.meter_lag.get(name) or []
            lag = 0.0
            if len(rows) >= LAG_MIN_SAMPLES:
                got = sorted(r[0] for r in rows)
                lag = max(0.0, got[int(LAG_PERCENTILE * (len(got) - 1))])
            for st in det.phases.values():
                st.lag = lag

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
        """A meter's readings for the energy answer (_energy_pairs), its
        channels summed - each held at its last value from the last pass
        until it writes again, not counted as nothing from the pass's start."""
        last = self._sub_last.setdefault(name, {})
        stamps = sorted({t for rows in rows_by_phase.values() for t, _ in rows})
        if not stamps:
            return
        at = {p: 0 for p in rows_by_phase}
        merged = []
        for ts in stamps:
            for p, rows in rows_by_phase.items():
                while at[p] < len(rows) and rows[at[p]][0] <= ts:
                    last[p] = rows[at[p]][1]
                    at[p] += 1
            merged.append((ts, sum(last.values())))
        kept = self.sub_rows.get(name, [])
        if kept and merged[0] <= kept[-1]:
            kept = sorted(kept + merged)              # a pass that overlapped the last one
        else:
            kept = kept + merged
        # Everything this pass brought, plus a tail before it. Trimming to
        # a fixed two hours looked thrifty and silently gutted the
        # backfill, whose slices are six hours long: the sessions being
        # placed were mostly older than the readings kept to place them
        # with (2026-09-19). Kept back to the oldest run or session still
        # to be placed, its idle window before it, whatever the slicing.
        cut = min(self._oldest_asked(), merged[0][0] - SUB_SAMPLE_TAIL_S) - IDLE_WINDOW_S
        i = max(0, bisect.bisect_left(kept, (cut, -math.inf)) - 1)     # with the value in force at the cut
        self.sub_rows[name] = kept[i:]

    def _on_main(self, m: Session) -> None:
        """A house session the grid's detector handed over: it waits for the
        meters' say (_ready_at), and is one a meter's session may vote for."""
        main_iv = max((st.interval for st in self.main.phases.values()), default=0.0)
        self._insort(self.unfiled, (self._ready_at(m, main_iv), m))
        self._recent_main.append((self._now, m))
        while self._recent_main and self._now - self._recent_main[0][0] >= MATCH_PATIENCE_S:
            self._recent_main.pop(0)
        for row in [r for r in self._unvoted if r[2] and not self._vote_blocked(r[1], r[3])]:
            self._unvoted.remove(row)
            self._vote(row[1], row[3])

    def _vote_tol(self, name: str) -> float:
        main_iv = max((st.interval for st in self.main.phases.values()), default=0.0)
        return max(MERGE_TOLERANCE_S, main_iv + self._meter_latency(name))

    def _vote_blocked(self, name: str, s: Session) -> bool:
        """Could the grid still hand over a session that started with the
        meter's ``s`` - a run still open, or waiting for a partner leg? A
        vote takes the start and the size, never the end: a grid run that
        stops late is still the one (09-25 11:02 at Kozolec: the IR panel's
        house session closed after the panel's and its vote went unheard)."""
        tol = self._vote_tol(name)
        main = self.main
        return (any(abs(o.since - s.start) <= tol for st in main.phases.values() for o in st.open_edges)
                or any(abs(m.start - s.start) <= tol for m in main.held)
                or any(st.pending and st.pending[0][0] <= s.start + tol for st in main.phases.values()))

    def _vote(self, name: str, s: Session) -> None:
        main_iv = max((st.interval for st in self.main.phases.values()), default=0.0)
        self._vote_phases({name: [s]}, main_iv, [m for _, m in self._recent_main])

    def _on_sub(self, name: str, s: Session) -> None:
        """A meter's session: a partner for a house session, and a vote on the
        meter's phase once the grid has been read as far (_vote_at)."""
        self.pending_sub.setdefault(name, []).append(s)
        self._insort(self._unvoted, (self._vote_at(name), name, False, s))

    @staticmethod
    def _insort(rows: list, row: tuple) -> None:
        """``row`` into ``rows`` by when it is due, and at one moment by its
        session's start - one order the data fixes, whatever the slicing
        (Anze, 2026-10-02)."""
        key = (row[0], row[-1].start)
        i = bisect.bisect_right([(r[0], r[-1].start) for r in rows], key)
        rows.insert(i, row)

    def _vote_at(self, name: str) -> float:
        """When a meter's session just handed over votes: once the grid has
        been read far enough to have handed over its own session of the same
        load - the grid's wait behind, plus the two meters' tolerance."""
        return self._now + self._wait + self._vote_tol(name)

    def _decide(self, m: Session) -> None:
        """File a house session done waiting (_ready_at), with its sub-meter
        partner's say in which signature it joins - see SUB_OVERRIDE - or
        else a one-device meter's energy's; placed where a meter saw it, if
        one did (_locate)."""
        main_iv = max((st.interval for st in self.main.phases.values()), default=0.0)
        for name in self.pending_sub:
            self.pending_sub[name] = [s for s in self.pending_sub[name] if self._now - s.end < 2.0 * MATCH_PATIENCE_S]
        pairs = sorted(self._session_pairs([m], main_iv), key=lambda x: (x[0], x[1]))
        if pairs:
            _, _, name, si = pairs[0]
            self._file_as(m, name, self.pending_sub[name].pop(si))
            return
        one = [p for p in sorted(self._energy_pairs([m]), key=lambda x: (x[0], x[1])) if self._one_device(p[2])]
        if one:
            self._place(m, one[0][2], self._meter_home(one[0][2]))
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
        on, and what each load learned against a number, go with the entity."""
        pairs = [(a, b) for old, new in renames.items()
                 for a, b in ((old, new), (SWITCH_PREFIX + old, SWITCH_PREFIX + new))]
        for det in [self.main, *self.subs.values()]:
            det._rejudge_all = True              # born_in and takes_in are read by _merge_devices
            for sig in det.signatures:
                for a, b in pairs:
                    if a in sig.locations:
                        sig.locations[b] = sig.locations.get(b, 0) + sig.locations.pop(a)
                    for held in (sig.drivers, sig.inputs):
                        if a in held:
                            held[b] = held.pop(a)
                    # "setting=value" markers name the setting's entity
                    if sig.born_in and sig.born_in.startswith(a + "="):
                        sig.born_in = b + sig.born_in[len(a):]
                    sig.takes_in = [b + t[len(a):] if t.startswith(a + "=") else t for t in sig.takes_in]
            for a, b in pairs:
                for held in (det.drivers, det.inputs, det.input_time, det.input_until, det.lag_hist):
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

    def _switch_for(self, m: Session, main_iv: float) -> Optional[str]:
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
        return events, self.main.drivers

    def _edges_of(self, m: Session) -> List[Tuple[str, tuple]]:
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

    def _file_main(self, m: Session, prefer: Optional[int] = None, avoid: Sequence[int] = ()) -> None:
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

    def _held_homes(self, m: Session) -> List[int]:
        """Signatures placed at a one-device meter that held its value
        through ``m``'s start - it did not start ``m``. The switch gate's test
        (SWITCH_GATE), with a meter's silence for the switch being off: a
        2.3 kW load's last 1,064 W step and an unmetered start netted with the
        pump's stop in one reading each landed in the plain cluster of 1 kW
        phase-A starts, whose runs go to the hidrofor, while its plug showed
        no start (2026-10-01). See Fleet._meter_held."""
        held = {name for name in self.subs if self.holds_one_device(name)
                and any(self._meter_held(name, ph, m.start, True) for ph in m.phases)}
        if not held:
            return []
        return [sig.id for sig in self.main.signatures if sig.count >= YOUNG_COUNT
                and any(sig.locations.get(name, 0) >= SWITCH_GATE * sig.count for name in held)]

    def _switched_off(self, m: Session, main_iv: float) -> List[int]:
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

    def _place(self, m: Session, name: Optional[str], prefer: Optional[int]) -> Optional["Signature"]:
        """File a house session - ``prefer`` the signature a meter's word
        says it joins - and credit it to the meter ``name`` that saw it, if
        any, and to the switch whose on-period it is. The switch is read
        before filing: filing moves the locations it is read from."""
        main_iv = max((st.interval for st in self.main.phases.values()), default=0.0)
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
        loads from naming and ties it to its device's phases. It is not what
        _one_device is: that one decides identity, and stays as it was benched."""
        if name.startswith(SWITCH_PREFIX):
            return True                   # a switch switches one load
        if name in self.single:
            return self.single[name]
        return self.guess_one_device(name)

    def guess_one_device(self, name: str) -> bool:
        """The library's shape, once it has something to say: at least
        METER_PHASES_MIN sightings, most of them one signature. A young meter -
        one renamed on the Energy dashboard starts again - holds several until
        it has shown otherwise, since hiding a meter's loads is the costly
        mistake (Kozolec's Inverter, three sightings in, looked like one)."""
        det = self.subs.get(name)
        return self._one_device(name) and sum(s.count for s in det.signatures) >= METER_PHASES_MIN

    def _one_device(self, name: str) -> bool:
        """Does this meter's own library look like ONE device? For identity
        (SUB_METER_IDENTITY), as benched."""
        det = self.subs.get(name)
        counts = [s.count for s in det.signatures] if det else []
        return bool(counts) and max(counts) >= SUB_DEVICE_SHARE * sum(counts)

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

    def _meter_home(self, name: str) -> Optional[int]:
        """The house signature most of this meter's sessions went to."""
        best = max(self.main.signatures, key=lambda s: s.locations.get(name, 0), default=None)
        return best.id if best is not None and best.locations.get(name, 0) else None

    def _ready_at(self, m: Session, main_iv: float) -> float:
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
        for name, det in self.subs.items():
            iv = max((st.interval for st in det.phases.values()), default=0.0)
            if not iv or 2.0 * iv > m.duration_s:
                continue
            due = max(due, m.end + max(MERGE_TOLERANCE_S, main_iv + self._meter_latency(name))
                      + self._declare_lag(name) + HELD_TAIL_S)
        return min(due, m.end + MATCH_PATIENCE_S)

    def _file_as(self, m: Session, name: str, s: Session) -> None:
        """File the house's session ``m`` as the sub-meter session ``s`` says."""
        det = self.subs.get(name)
        sub_sig = det.signature_of(s) if det is not None else None
        prefer = (self.identity.get(name) or {}).get(str(sub_sig.id)) if sub_sig is not None else None
        if not self._one_device(name):  # a circuit meter holds many loads: its sessions do not decide
            prefer = None
        sig = self._place(m, name, prefer)
        if sig is not None and sub_sig is not None:
            self.identity.setdefault(name, {})[str(sub_sig.id)] = sig.id

    def sub_quantum(self, name: str) -> float:
        """What a device meter can RESOLVE, as its own detector measured it
        (PhaseState.quantum). The energy answer is an integral of its rows, so
        their quantisation is its error bar - and Home has a workshop boiler
        publishing in 46 W steps about every seven minutes (Anze, 2026-09-22).
        Measured once a pass over the rows kept, it read ten days in one call
        and eight hours in a backfill's slice."""
        det = self.subs.get(name)
        return max((st.quantum for st in det.phases.values()), default=0.0) if det else 0.0

    def _energy_pairs(self, mains: List[Session]) -> list:
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
            for name, rows in self.sub_rows.items():
                if name in phases and not set(m.phases) <= set(phases[name]):
                    continue
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
                ratio = rose / want
                if ENERGY_MATCH_LO <= ratio <= ENERGY_MATCH_HI:
                    # slightly worse than a session match of the same quality,
                    # so a meter that CAN resolve the load still wins
                    pairs.append((0.5 + abs(ratio - 1.0), mi, name, None))
        return pairs

    def _session_pairs(self, mains: List[Session], main_iv: float) -> list:
        """(cost, main index, meter, sub index) for every house session and
        sub-meter session that could be the same load."""
        pairs = []
        phases = self.meter_phases()
        meters = []
        for name, subs in self.pending_sub.items():
            # the grid's reporting interval and the meter's latency, since a
            # step can land anywhere inside them, and never less than the
            # merge tolerance
            tol = max(MERGE_TOLERANCE_S, main_iv + self._meter_latency(name))
            agnostic = self.agnostic.get(name, False)
            # a three-phase meter's sessions under the HOUSE's phase names
            mp = {} if agnostic else self.phase_map(name)
            # by start, since only a session starting within tol can pair
            order = sorted(range(len(subs)), key=lambda i: subs[i].start)
            meters.append((name, subs, tol, agnostic, mp, order, [subs[i].start for i in order]))
        for mi, m in enumerate(mains):
            for name, subs, tol, agnostic, mp, order, starts in meters:
                if name in phases and not set(m.phases) <= set(phases[name]):
                    continue        # not on the phases this meter's one device uses
                near = order[bisect.bisect_left(starts, m.start - tol):bisect.bisect_right(starts, m.start + tol)]
                for si in sorted(near):
                    s = _relabel(subs[si], mp)
                    if _same_load(m, s, agnostic, tol):
                        pairs.append((_match_cost(m, s, agnostic, tol), mi, name, si))
        return pairs

    def _meter_steps(self, ph: str, since: float, window: float, up: Optional[bool] = None,
                     size: Optional[float] = None) -> List[Tuple[str, float]]:
        """The pieces of a grid step on phase ``ph`` at ``since`` that the
        meters below it explain - see SPLIT_BY_METERS. Each meter measured to
        carry that phase gives its own declared step there, in the grid's
        terms (its gain), less what its own sub-meters stepped: Blaž PC's step
        counts once, and Hiša adds only what else inside it changed. The
        pieces do not overlap."""
        steps = {n: (m[0] if m[0] is not None else (size or 0.0)) for n, m in self._meter_totals(ph, since, window, up).items()}
        return [(name, d - sum(x for k, x in steps.items() if self.parents.get(k) == name))
                for name, d in steps.items()]

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
        """The open run on grid phase ``ph`` that a one-device meter's own
        stop over the grid fall's span [a, b] ends: the newest the meter
        started - it rose within its sustain of the run's start - by at
        least half of what it fell. Whatever the grid's step measures: Home's
        pump started at +909 W, still settling, and stopped at -731 W while its
        plug fell 818 W to nothing; too unlike to pair by size, the fall
        closed a 689 W and a 118 W run together instead, and the pump's ran on
        two hours (20.09 00:39). Each meter fall ends one run. The run's start
        must be the meter's rise, though - within the pairing tolerance, in
        the grid's terms: a boiler rise 41 s after the IR panel's start, two
        hours earlier, let a 2 kW boiler fall close the panel's 514 W run and
        book it at 1,270 W (Kozolec 09-22 11:34, 2026-10-02)."""
        if len(self._stops_used) > 1000:
            self._stops_used = {k: t for k, t in self._stops_used.items() if t > a - 86400.0}
        for name, det in self.subs.items():
            if not self.holds_one_device(name):
                continue
            for c in [c for c, h in self.phase_map(name).items() if h == ph and c in det.phases]:
                st = det.phases[c]
                reach = st.latency()
                for e in self._near(st, a, b, reach):
                    if e[1] >= 0 or (name, e[0]) in self._stops_used:
                        continue
                    gain, grid = self.gain(name, "p"), self.main.phases[ph]
                    if fall is not None and abs(fall) < 0.5 * -e[1] * gain:
                        # not through this fall: it does not account for half
                        # the meter's. A 263 W blip ending a second before the
                        # EVSE's 3.6 kW fall, the plug's own fall within reach
                        # of its span, took the EVSE's run and booked 23
                        # minutes at 257 W (Kozolec 09-28 13:53, 2026-10-02).
                        # Home's pump (-731 W for a plug fall of 818) passes.
                        continue
                    for o in reversed(opens):
                        rises = [r[1] * gain for r in self._near(st, o.since - reach, o.since + reach, reach) if r[1] > 0
                                 and abs(r[1] * gain - o.watts) <= grid._tol(o.watts, r[1] * gain)]
                        if rises and -e[1] * gain >= 0.5 * max(rises):
                            self._stops_used[(name, e[0])] = e[0]
                            return o
        return None

    def _meter_on(self, name: str, ph: str, since: float, size: float) -> bool:
        """Does meter ``name``, whose own step started a grid run of ``size``
        on ``ph`` at ``since``, still show that load on - no fall of half its
        size declared since, on a channel mapped to that phase (every channel
        of a meter not mapped yet)? Read ahead of the grid by the horizon, a
        fall the meter has declared is known before the grid's own; one it
        has not is not its load stopping. See PhaseState.owned."""
        det = self.subs.get(name)
        if det is None:
            return False
        gain = self.gain(name, "p")
        chans = [c for c, h in self.phase_map(name).items() if h == ph and c in det.phases] or \
            [c for c, st in det.phases.items() if st.last_ts is not None]
        for c in chans:
            st = det.phases[c]
            # not a fall inside the meter's own latency of the start: a slow
            # plug reads a motor's surge as a reading of its own and its
            # settling as a fall (Susilna 09-24 18:00: 565 W, then 264)
            k = bisect.bisect_right(st.declared_t, since + st.latency())
            if any(e[1] < 0 and -e[1] * gain >= 0.5 * size for e in st.declared[k:]):
                return False
        return bool(chans)

    def _meter_started(self, ph: str, since: float, size: float) -> Optional[Tuple[str, float]]:
        """(meter, its rise in the grid's terms): the meter reporting a total
        whose own rise - no bigger than the grid's start of ``size`` at
        ``since`` on ``ph``, within the pairing tolerance - came within its
        latency of it; the largest such rise. Asked where the meters' own
        steps placed none (_step_meter): a meter the phase map does not place
        yet, its votes coming from runs that have to survive first (Home's
        Susilna plug, 0-10 of the 30 it needs in ten days), or one whose step
        and the grid's disagreed in size or span - the plug's 265 W inside a
        +313 W start with a fan (_split_rise). Only a meter reporting a total:
        its one channel is the load's. Whether it holds one device does not
        matter - a strip's load starting shows on the strip and the grid
        alike, and a fall of half the size on the strip frees the run again.
        See PhaseState.owned."""
        grid = self.main.phases.get(ph)
        if grid is None:
            return None
        best = None
        for name, det in self.subs.items():
            if not self.agnostic.get(name):
                continue
            gain = self.gain(name, "p")
            for st in det.phases.values():
                if st.last_ts is None:
                    continue
                reach = st.latency()
                for e in self._near(st, since - reach, since + reach, reach):
                    rise = e[1] * gain
                    if not (rise > 0 and rise <= size + grid._tol(size, rise)) or (best is not None and rise <= best[1]):
                        continue
                    # a rise the grid took as a step of its own - Kozolec's
                    # boiler pulsing within a breath of the EVSE's charge
                    # starting, its +1.9 kW a grid step 15 s later - is that
                    # step, not a part of this start
                    tol = math.hypot(grid.noise_at(), st.noise_at()) + METER_CAL_SLACK * rise
                    if any(abs(g[0] - since) > 0.01 and g[1] > 0 and g[1] >= rise - tol
                           for g in self._near(grid, e[3], e[4], reach)):
                        continue
                    best = (name, rise)
        return best

    def _meter_level(self, name: str, ph: str) -> Optional[float]:
        """What meter ``name`` draws above its floor now, in the grid's terms,
        on its channels mapped to ``ph`` (every channel of a meter not mapped
        yet) - the size of a run that is its (PhaseState.owned): Kozolec's
        Scala2 ramps 104-247 W, and its plug measures the ramp; the grid's
        level, followed while the run was alone, read a charger ramping up on
        the same phase as the Susilna plug's growth, 270 W to 5 kW (Home 09-26
        20:28, 2026-10-02). None while the meter has no level yet."""
        det = self.subs.get(name)
        if det is None:
            return None
        chans = [c for c, h in self.phase_map(name).items() if h == ph and c in det.phases] or \
            [c for c, st in det.phases.items() if st.last_ts is not None]
        known = [det.phases[c] for c in chans if det.phases[c].level is not None and det.phases[c].baseline is not None]
        if not known:
            return None
        return max(0.0, sum(st.level - st.baseline for st in known)) * self.gain(name, "p")

    def _meter_held(self, name: str, ph: str, since: float, up: bool) -> bool:
        """Did meter ``name`` hold its value through a step on grid phase
        ``ph`` at ``since``? As of the pass's end it has written nothing that
        moved for SUSTAIN_CADENCES of its cadence after it - silence is its
        value held - has nothing pending, and declared no step that way near
        it. Too soon to tell, or a meter not measured on ``ph``: no."""
        det = self.subs.get(name)
        chans = [c for c, h in self.phase_map(name).items() if h == ph and c in det.phases] if det else []
        if not chans:
            return False
        for c in chans:
            st = det.phases[c]
            reach = st.latency()
            if self._now < since + reach or st.pending:
                return False
            if any((e[1] > 0) == up for e in self._near(st, since - reach, since + reach, reach)):
                return False                          # it stepped: a placement missed, not a silence
        return True

    def _step_meter(self, ph: str, since: float, size: float, up: bool, var: Optional[float] = None) -> Optional[str]:
        """The innermost meter whose own declared step was all of a grid step
        on ``ph`` - within both meters' noise and METER_CAL_SLACK; of several,
        the one none of the others hangs under. Teaches the meter its gains."""
        main = self.main
        if ph not in main.phases:
            return None
        noise = main.phases[ph].noise_at()
        took = {}
        for n, (d, raw, raw_q, noise_m, conf) in self._meter_totals(ph, since, main.event_window(), up).items():
            if d is None:                   # all of the grid's step over the union of both spans
                if not self._meter_stepped(n, ph, since, 0.5 * size, up):
                    continue                # ...but its own steps are not half of it: not its
                took[n] = (size if up else -size, None, None, noise_m, conf)
            elif d and (d > 0) == up and abs(abs(d) - size) <= math.hypot(noise, noise_m) + METER_CAL_SLACK * size:
                took[n] = (d, raw, raw_q, noise_m, conf)

        def under(n: str, anc: str) -> bool:
            seen = set()
            while n in self.parents and n not in seen:
                seen.add(n)
                n = self.parents[n]
                if n == anc:
                    return True
            return False
        inner = [n for n in took if not any(under(o, n) for o in took if o != n)]
        if not inner:
            return None
        meter = min(inner, key=lambda n: abs(abs(took[n][0]) - size))
        _, raw, raw_q, noise_m, conf = took[meter]
        main.placement_conf = conf
        if raw is not None and size >= 10.0 * math.hypot(noise, noise_m):   # a step both saw clearly, alone, teaches the gains
            self._learn_gain(meter, "p", size, raw)
            if var is not None and raw_q is not None and min(abs(var), abs(raw_q)) >= 20.0:
                self._learn_gain(meter, "q", var, raw_q)
        return meter

    def _meter_stepped(self, name: str, ph: str, since: float, least: float, up: bool) -> bool:
        """Do the meter's own declared steps the way of a grid step at
        ``since`` - within its latency of it, on its channels carrying ``ph``
        (every channel of a meter not mapped yet) - add up to ``least``, in the
        grid's terms? Asked of the "all of it" word of _meter_totals (the
        meter's net over the union of both spans is the grid's) with half the
        step: the hidrofor's plug rose 924 W as the grid rose 942, a 3EM's kiln
        stop was netted into the rise, which grew to 3980 W, and over the union
        the nets agreed - the plug owned a run its 924 W fall, less than half,
        could never release; 15 hours, 60 kWh (Home 09-24 00:54, 2026-10-02).
        Half, since a meter's stop frees its run at half the run's size
        (_meter_on), and Hiša's +2,240 is rightly all of a +2,800 start with a
        580 W load off in the same span."""
        det = self.subs.get(name)
        if det is None:
            return False
        gain = self.gain(name, "p")
        chans = [c for c, h in self.phase_map(name).items() if h == ph and c in det.phases] or \
            [c for c, st in det.phases.items() if st.last_ts is not None]
        total = 0.0
        for c in chans:
            st = det.phases[c]
            reach = st.latency()
            total += sum(e[1] for e in self._near(st, since - reach, since + reach, reach) if (e[1] > 0) == up) * gain
        return abs(total) >= least

    def _learn_gain(self, name: str, kind: str, grid: float, meter: float) -> None:
        if not grid or not meter:
            return
        acc = self.meter_gain.setdefault(name, {}).setdefault(kind, [0.0, 0.0])
        acc[1] += 1.0
        acc[0] += (math.log(abs(grid) / abs(meter)) - acc[0]) / min(acc[1], 200.0)

    def gain(self, name: str, kind: str = "p") -> float:
        """What a meter's step is worth in the grid's terms - 1 until learned."""
        mean, n = (self.meter_gain.get(name) or {}).get(kind, [0.0, 0.0])
        if n < METER_GAIN_MIN:
            return 1.0
        return min(1.0 + METER_GAIN_BOUND, max(1.0 / (1.0 + METER_GAIN_BOUND), math.exp(mean)))

    @staticmethod
    def _near(st: "PhaseState", a: float, b: float, reach: float) -> List[tuple]:
        """``st``'s declared steps whose spans reach into [a, b]."""
        lo = bisect.bisect_left(st.declared_t, a - reach)
        hi = bisect.bisect_right(st.declared_t, b + reach)
        return [e for e in st.declared[lo:hi] if e[3] <= b and e[4] >= a]

    def _meter_totals(self, ph: str, since: float, window: float, up: Optional[bool] = None) -> Dict[str, tuple]:
        """Per meter measured to carry grid phase ``ph``: (its share of the grid
        step at ``since``, in the grid's terms - None when it is all of it -,
        the meter's own step raw where one clean step faces one, its reactive
        part, its noise, the timing confidence 0..1) - see UNION_CAP_WINDOWS."""
        out: Dict[str, tuple] = {}
        if ph not in self.main.phases:
            return out
        grid = self.main.phases[ph]
        k = bisect.bisect_left(grid.declared_t, since - 0.01)
        mine = grid.declared[k] if k < len(grid.declared) and abs(grid.declared[k][0] - since) <= 0.01 else None
        g_span = (mine[3], mine[4]) if mine else (since - window, since + window)
        noise_g, main_window = grid.noise_at(), self.main.event_window()
        for name, det in self.subs.items():
            votes = self.phase_votes.get(name) or {}
            if sum(sum(r.values()) for r in votes.values()) < PHASE_MAP_MIN_VOTES:
                continue
            chans = [c for c, h in self.phase_map(name).items() if h == ph and c in det.phases]
            if not chans:
                continue
            noise = max(det.phases[c].noise_at() for c in chans)
            cap = UNION_CAP_WINDOWS * max([main_window] + [det.phases[c].latency() for c in chans])
            a, b = g_span
            for _ in range(8):                       # the union, grown until it holds still
                ms = [e for c in chans for e in self._near(det.phases[c], a, b, cap)]
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
                out[name] = (0.0, None, None, noise, 0.0)
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
            q = own[0][2] if clean else None
            out[name] = (share, own[0][1] if clean else None, q, noise, conf)
        return out

    def _grid_took(self, grid: "PhaseState", e: tuple, gain: float, noise_g: float, noise_m: float,
                   reach: float) -> bool:
        """Did the grid declare a step of its own the way of the meter's step
        ``e``, over its span and big enough to hold it - within both meters'
        noise and METER_CAL_SLACK?"""
        size = abs(e[1]) * gain
        tol = math.hypot(noise_g, noise_m) + METER_CAL_SLACK * size
        return any((g[1] > 0) == (e[1] > 0) and abs(g[1]) >= size - tol for g in self._near(grid, e[3], e[4], reach))

    def _vote_phases(self, closed_sub: Dict[str, List[Session]], main_iv: float,
                     pool: List[Session]) -> None:
        """Each new single-channel session on a meter votes for the house phase
        whose single-phase session started with it at the same size - by size
        and moment only, never by label. A meter with no phases of its own
        votes too: only SPLIT_BY_METERS reads its answer."""
        for name, sessions in closed_sub.items():
            det = self.subs.get(name)
            tol = max(MERGE_TOLERANCE_S, main_iv + self._meter_latency(name))
            votes = self.phase_votes.setdefault(name, {})
            for s in sessions:
                if len(s.levels) != 1:
                    continue
                (own,), w = s.levels.keys(), sum(s.power_by_phase().values())
                best = None
                for m in pool:
                    if len(m.levels) != 1 or abs(m.start - s.start) > tol:
                        continue
                    mw = sum(m.power_by_phase().values())
                    if abs(mw - w) > max(MATCH_POWER_REL * max(mw, w), MIN_NOISE_W):
                        continue
                    if best is None or abs(m.start - s.start) < abs(best.start - s.start):
                        best = m
                if best is not None:
                    (house,) = best.levels.keys()
                    row = votes.setdefault(own, {})
                    row[house] = row.get(house, 0) + 1

    def phase_map(self, name: str) -> Dict[str, str]:
        """Which house phase each of the meter's channels carries, from its
        votes - worked out again only once they have changed: every grid
        step asks every meter, and the answer moves only when a session
        votes (_vote_phases). Shared, so callers read it and never write."""
        votes = self.phase_votes.get(name) or {}
        key = tuple((c, tuple(sorted(r.items()))) for c, r in sorted(votes.items()))
        hit = self._phase_maps.get(name)
        if hit is None or hit[0] != key:
            hit = self._phase_maps[name] = (key, phase_mapping(votes, min_votes=PHASE_MAP_MIN_VOTES))
        return hit[1]

    def _locate(self, m: Session, final: bool = False) -> bool:
        """Credit a filed house session to the meter that saw it: the BEST
        fit of a meter's own session and the energy its readings account
        for, not the first that passes - first fit let a session that merely
        fitted take the meter's session a better one needed, and at Kozolec a
        boiler with its own meter and 367 sightings collected thirteen
        locations (Anze, 2026-09-18). Tried when it is filed and once more
        MATCH_PATIENCE_S after its end, by when a slow meter has written past
        it."""
        sig = self.main.signature_of(m)
        if sig is None:
            return True
        main_iv = max((st.interval for st in self.main.phases.values()), default=0.0)
        pairs = self._session_pairs([m], main_iv) + self._energy_pairs([m])
        if not pairs:
            return False
        _, _, name, si = min(pairs, key=lambda x: (x[0], x[1]))
        if si is not None:
            self.pending_sub[name].pop(si)
        sig.locations[name] = sig.locations.get(name, 0) + 1
        return True

    def to_dict(self) -> dict:
        return {"main": self.main.to_dict(), "subs": {n: d.to_dict() for n, d in self.subs.items()},
                "pending_main": [[t, s.to_dict()] for t, s in self.pending_main],
                "pending_sub": {n: [s.to_dict() for s in v] for n, v in self.pending_sub.items()},
                "agnostic": self.agnostic, "phase_votes": self.phase_votes,
                "unfiled": [[t, s.to_dict()] for t, s in self.unfiled], "identity": self.identity,
                "sub_last": self._sub_last, "wait": self._wait, "wait_at": self._wait_at,
                "meter_lag": self.meter_lag, "lag_from": self._lag_from,
                "unvoted": [[t, n, final, s.to_dict()] for t, n, final, s in self._unvoted],
                "recent_main": [[t, s.to_dict()] for t, s in self._recent_main],
                "meter_gain": self.meter_gain,
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
        # (when, session); a store from before the one clock held sessions alone: due at once
        timed = lambda rows: [(x[0], Session.from_dict(x[1])) if isinstance(x, list) else (0.0, Session.from_dict(x))  # noqa: E731
                              for x in rows or []]
        f.pending_main = timed(d.get("pending_main"))
        f.pending_sub = {n: [Session.from_dict(x) for x in v] for n, v in (d.get("pending_sub") or {}).items()}
        f.agnostic = dict(d.get("agnostic") or {})
        f.phase_votes = {n: {p: dict(r) for p, r in v.items()}
                         for n, v in (d.get("phase_votes") or {}).items()}
        f.unfiled = timed(d.get("unfiled"))
        f._sub_last = {n: dict(v) for n, v in (d.get("sub_last") or {}).items()}
        f._wait, f._wait_at = float(d.get("wait") or 0.0), d.get("wait_at")
        f.meter_lag = {n: [list(r) for r in v] for n, v in (d.get("meter_lag") or {}).items()}
        f._lag_from = d.get("lag_from")
        f._unvoted = [(r[0], r[1], bool(r[2]) if len(r) > 3 else False, Session.from_dict(r[-1]))
                      for r in d.get("unvoted") or []]
        f._recent_main = [(t, Session.from_dict(x)) for t, x in d.get("recent_main") or []]
        f.identity = {n: dict(v) for n, v in (d.get("identity") or {}).items()}
        f.meter_gain = {n: {k: list(x) for k, x in v.items()} for n, v in (d.get("meter_gain") or {}).items()}
        carry = d.get("carry") or {}
        f._carry = {k: v for k, v in (
            ("rows", {p: [tuple(r) for r in v] for p, v in (carry.get("rows") or {}).items()}),
            *((k, {p: {t: x for t, x in v} for p, v in (carry.get(k) or {}).items()}) for k in ("q", "pv"))) if v}
        return f


def _relabel(s: Session, mapping: Dict[str, str]) -> Session:
    """A session under another meter's phase names."""
    if not mapping or all(mapping.get(p, p) == p for p in s.levels):
        return s
    levels = {mapping.get(p, p): lv for p, lv in s.levels.items()}
    return replace(s, phases="".join(sorted(levels)), levels=levels)


def _match_cost(a: Session, b: Session, phase_agnostic: bool, tol_s: float) -> float:
    """How well these two sessions fit, smaller being better.

    Only ever asked of a pair that already passed ``_same_load``; this is
    what decides which of several passing pairs is the real one."""
    pa, pb = a.power_by_phase(), b.power_by_phase()
    ta = sum(pa.values()) if phase_agnostic else sum(pa.get(ph, 0.0) for ph in a.phases)
    tb = sum(pb.values()) if phase_agnostic else sum(pb.get(ph, 0.0) for ph in a.phases)
    biggest = max(abs(ta), abs(tb), 1.0)
    when = abs(a.start - b.start) / max(tol_s, 1.0)
    size = abs(ta - tb) / biggest
    da, db = max(a.duration_s, 1.0), max(b.duration_s, 1.0)
    length = abs(math.log(da / db))
    # the moment and the size are what two meters can agree on; the length is
    # what a busy main meter gets wrong, so it only breaks ties
    return when + size + 0.25 * length


def _same_load(a: Session, b: Session, phase_agnostic: bool = False,
               tol_s: float = MERGE_TOLERANCE_S) -> bool:
    """Is the downstream session ``b`` the same load as the main-meter
    session ``a``? Always the same moment; then the same size.

    ``phase_agnostic`` is for a meter that reports only a total - most
    single-device meters do. It cannot say which phase the load is on, so
    only the magnitude is compared; the main meter's own session supplies the
    phase, which is how a device's phase gets learned for free."""
    # The START is the hard test: two meters seeing a load switch on at the
    # same instant, at the same size, are seeing the same load. The END is
    # not, and demanding it within the same fifteen seconds is what stopped
    # Kozolec placing anything - on a busy main meter a load's down-step can
    # pair with a different edge, so the session runs on. Measured there:
    # 443 boiler cycles started within a minute of a main-meter session and
    # their ends differed by 7 s at the median but 438 s at the third
    # quartile, so only 27 were accepted (Anze, 2026-09-18).
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
    pa, pb = a.power_by_phase(), b.power_by_phase()
    if phase_agnostic:
        # The PEAK, not the energy-weighted mean. A meter slower than the load
        # it watches dilutes that mean with the part of a sample where the
        # load was off: Kozolec's boiler runs 66 seconds and its Shelly
        # reports every 52, so its own sessions measured 953 W against the
        # 1813 W the main meter saw - a factor of two, and the size test threw
        # out 343 of 381 otherwise-good pairs on it (Anze, 2026-09-18). What
        # a load PEAKS at survives coarse sampling; what it averages does not.
        ta, tb = a.max_w, b.max_w
        return abs(ta - tb) <= max(MATCH_POWER_REL * max(ta, tb), MIN_NOISE_W)
    if a.phases != b.phases:
        return False
    for ph in a.phases:
        tol = max(MATCH_POWER_REL * max(pa[ph], pb.get(ph, 0.0)), MIN_NOISE_W)
        if abs(pa[ph] - pb.get(ph, 0.0)) > tol:
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
    older copy quietly wins. At Anze's house that meant the grid meter being
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
    work someone has already done rather than of their house (Anze,
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
    here" - Kozolec showing 6 of 12 and Home 14 of 60 (Anze's screenshots,
    2026-09-22). A page that lengthens as you name things has to be able to
    promise there is something to lengthen INTO, and that promise was reading
    as "this is all there is"."""
    clear = [s for s in worth
             if s.evidence >= min_evidence or s.name or (is_heir and is_heir(s.id))]
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
        # fifteen settings of the same machine (Anze's kiln, 2026-09-22).
        # Two loads named DIFFERENTLY were told apart on purpose.
        if a.name and b.name and a.name != b.name:
            return False
        # Identical phase sets, deliberately. A device with two elements does
        # draw on A alone, on C alone and on both - Anze's kiln does exactly
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
        # differs is the power. At Anze's house this is exactly the line
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
        # thing and a 2.7 kW one be offered as one device (Anze's house,
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


def on_phases(phases: str) -> str:
    """"on phase A", "on phases A and C", "on all three phases" - read
    rather than decoded. "(A)" was what the naming page said (Anze,
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
    anyone looking at five rows in five different sets (Anze, 2026-09-23)."""
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
    to fit on it or not appear at all (Anze, 2026-09-18)."""
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
    show it (Anze, 2026-09-17)."""
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
