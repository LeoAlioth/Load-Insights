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
import math
import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from .classify import MAX_CONFIDENCE as MAX_APPLIANCE, Guess, classify

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
NOISE_MAD_FACTOR = 4.0
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
SUSTAIN_INTERVALS = 1.5
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
SUSTAIN_AGREE = 1
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
STEP_AT_HALFWAY = 1
# How a reading's sample interval is estimated. Home Assistant records only a
# CHANGE, so a running mean of the gaps measures how often the value changes,
# not how often the meter reports: Home's grid meter reports every 6 s on all
# three phases (mode 6 s), but phase A is quieter, records fewer changes, and
# its mean came out 7.1 s against C's 6.0. One meter then judged its own two
# legs by different sustain thresholds (10.7 s against 9.0 s). A low
# percentile of recent gaps is the cadence, since the shortest regular gap is
# the one where the value did change. The MEDIAN of the last INTERVAL_GAPS
# gaps puts all three of Home's phases at 6.0 s. Swept 0.1-0.5: the median
# also takes the kiln's full-size sessions 305 -> 337 and its spurious ladder
# 130 -> 92, and on held-out days Home 76.7 -> 77.6 % purity, Kozolec's
# hidrofor 65 -> 75. It does NOT fix the single-leg problem by itself -
# see MATCHED_STOP_SAMPLES (2026-09-23). 0 keeps the running mean.
INTERVAL_PERCENTILE = 0.5
# A step DOWN the size of a load already running is a stop its own start
# vouches for, and may be accepted on less than SUSTAIN demands of a new,
# unexplained level. Home's kiln is on ~48 s and off only ~9 s - one or two
# readings - so the strict guard swallowed its off-gaps, glued consecutive
# pulses into one long session on whichever leg rejected the gap, and that leg
# then could not merge with the other: 69 of 71 single-level long legs had
# real off-gaps inside them (2026-09-23).
#
# OFF, deliberately. Swept as a global rule it works on the kiln - full-size
# sessions 337 -> 364-381 against 441 real pulses, single-leg 154 -> 114 -
# and costs every load that DIPS without stopping: Kozolec's Scala2 hidrofor,
# which ramps, 140 -> 106, and at one reading Home's NASA station, which
# wanders, 114 -> 65. A drop the size of an open edge means "stopped" for a
# switched load and "dipped" for a ramping one. It needs corroboration - the
# other leg of a multi-phase start stopping too - before it can be switched
# on. See "Sessions filed on one leg" in AGENTS.md for the plan.
MATCHED_STOP_SAMPLES = 0
MATCHED_STOP_INTERVALS = 0.0
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
CORROBORATED_STOP_SAMPLES = 1
CORROBORATED_STOP_INTERVALS = 0.0
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
CORROBORATED_CLOSES_ITS_EDGE = 1
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
CORROBORATED_SPLIT = 1
INTERVAL_GAPS = 60
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
PV_MIN_SWING_W = 200.0         # below this the sun hardly moved and there is nothing to tell
PV_MIN_SAMPLES = 30
# A reading that CONTAINS generation goes below zero whenever the site
# exports; one that carries the house alone cannot. Both thresholds are
# deliberately slack: a meter's noise sits well inside 50 W, and half a per
# cent of a day is minutes, not a spike.
EXPORT_FLOOR_W = 50.0
EXPORT_SHARE = 0.005
# Below this an AC input is carrying nothing; a generator sits here almost
# always, while a utility connection crosses zero and moves on.
SOURCE_IDLE_W = 25.0
SOURCE_IDLE_SHARE = 0.9
# a reference circuit has to carry something this often to be one
LIVE_SHARE = 0.2
SOURCE_UTILITY = "utility"
SOURCE_GENERATOR = "generator"
SOURCE_NONE = "none"
MATCH_EDGE_REL = 0.15          # a step down pairs with a step up this close in size, or the noise
# How much better a size match must be before it overrides RECENCY when a
# step down chooses which open start it closes. 0 is pure best-fit, 1 treats
# every passing candidate as tied and takes the newest - which is what this
# did for its whole life. Swept on both sites; see the comment in _pair.
PAIR_TIE_BAND = 1.0
MAX_OPEN_S = 24 * 3600.0       # a start whose stop never came is given up on after this
# ...or sooner: once it has been open this many times longer than any load of
# its size on its phase has been seen to run. A start whose stop was taken by
# another edge otherwise stays open for the whole day above, and meanwhile
# closes against stops that belong to real loads - Home had 194 sessions over
# three hours in ten days. A size the library has never seen keeps the flat
# day, so a new load that runs long can still be learned. 0 is off.
#
# OFF, for its side effects (swept 3 and 8, 2026-09-23). At 3 the sessions
# over three hours halve (157 -> 84) and the kiln gains a little (409/115 vs
# 407/118), but a real long-running load of a size that other loads run
# briefly loses its starts: Home's dryer went 34 -> 21 sessions, 20 -> 29 pump
# runs were missed outright, Kozolec's boiler 500 -> 496. At 8 the dryer still
# lost 9 and the kiln got worse. Kept for a better test of "this start has
# outlived its load" than size alone.
ORPHAN_MARGIN = 0.0
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
# Duration CAN be part of a load's fingerprint and is not necessarily one
# (Anze, 2026-09-22). A kettle boils the same volume every time and always
# takes about two minutes; a thermostat runs for twenty seconds or for
# twenty minutes depending how cold the tank is. Measured against the
# submeters at Kozolec, the boiler's runs spread by 0.12 of their median and
# the pressure pump's by 0.48 - so whether duration identifies a load is
# something the load itself says, and the library already records it as
# duration_mad. A signature that has shown it keeps a clock is held to
# MATCH_DURATION_FACTOR; one that has shown it does not gets only the loose
# bound below, which exists to stop a minute-long load joining an
# afternoon-long one rather than to tell two appliances apart.
LOOSE_DURATION_FACTOR = 30.0
# Before this many sightings a signature has not said anything about its own
# duration yet, and judging it on one or two would freeze whatever the first
# runs happened to be - which is the failure the whole change is about, since
# a signature that enforces a duration it has not earned never absorbs the
# runs that would have taught it otherwise.
DURATION_IDENTITY_COUNT = 4
DURATION_IDENTITY_SPREAD = 0.35
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
SUB_OVERRIDE = 1
# ...and takes the sub-meter's POWER as well, when that meter is the quieter
# one and measured the run with at least as many readings. OFF: measured
# slightly worse (Home's NASA station held out 26 -> 22).
SUB_POWER = 0
# A house session whose ENERGY a device meter accounts for - the meter too slow
# or too coarse to have a session of its own for it - joins the house
# signature most of that meter's sessions went to, when it fits there. Only
# for a meter that holds one device: one whose own library puts at least
# SUB_DEVICE_SHARE of its sightings in a single signature (Home's hidrofor
# plug 99 %, its Hiša circuit 55 %). 0 is off.
SUB_METER_IDENTITY = 1
SUB_DEVICE_SHARE = 0.5
# Whether a CIRCUIT meter's session - one holding many loads, like Home's Hiša
# 3EM - may decide a signature too, or only a meter that holds one device.
# Off: one-device meters only was better at Home (79.1 / 80.7 % against
# 78.5 / 80.5) and identical everywhere else.
SUB_IDENTITY_CIRCUITS = 0
# How far back to look for what the device was drawing ANYWAY. Capped,
# because a session lasting hours would otherwise want hours of readings
# before it - further back than the tail we keep - and the longest sessions
# are exactly the ones energy matching answers best.
IDLE_WINDOW_S = 900.0
MATCH_PF_TOL = 0.15


def power_tolerance(base: float, *mads: float) -> float:
    """How far apart two powers may sit on one phase and still be one load.

    ``base`` is the flat part - a tenth of the larger reading, or the measured
    noise, whichever is bigger - and the rest is what the signatures already
    know about how much they WANDER. power_mad was computed from the first
    sighting and then read only by the evidence score: swallow even folds the
    distance between two merged means into it, and nothing consulted the
    result when deciding what to merge next (Anze, 2026-09-22).

    A load that genuinely repeats has a small mad and keeps the tight old
    tolerance; one that has always wandered stops being cut into pieces for
    wandering again."""
    return base + sum(mads)


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

    @property
    def ripple(self) -> Optional[float]:
        """How far it wandered, as a fraction of its own level - which is what
        a threshold can be set on, where watts cannot."""
        if self.low is None or self.high is None:
            return None
        middle = 0.5 * (self.low + self.high)
        return max(0.0, (self.high - self.low) / middle) if middle > MIN_NOISE_W else None

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
                "levels": {ph: [list(x) for x in lv] for ph, lv in self.levels.items()}}

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(phases=d["phases"], start=d["start"], end=d["end"], pf=d.get("pf"),
                   pf_mad=d.get("pf_mad", 0.0), surge_w=d.get("surge_w", 0.0),
                   # written by to_dict all along and never read back, so a
                   # session that waited across a restart came back unsampled
                   samples=d.get("samples", 0), low=d.get("low"), high=d.get("high"),
                   levels={ph: [tuple(x) for x in lv] for ph, lv in d["levels"].items()})


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


def delta_correlation(samples: Sequence[Tuple[float, float]],
                      other_by_ts: Dict[float, float],
                      min_swing_w: float = PV_MIN_SWING_W) -> Optional[float]:
    """How the two series' CHANGES move together, between -1 and 1.

    Levels would say almost nothing - two readings of one site are both
    large and both wander - while their changes say exactly how one responds
    to the other. None when the second series hardly moved."""
    xs: List[float] = []
    ys: List[float] = []
    prev: Optional[Tuple[float, float]] = None
    for ts, w in samples:
        p = other_by_ts.get(ts)
        if p is None:
            continue
        if prev is not None:
            xs.append(w - prev[0])
            ys.append(p - prev[1])
        prev = (w, p)
    if len(xs) < PV_MIN_SAMPLES or (max(ys) - min(ys)) < min_swing_w:
        return None
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    sxx = sum((a - mx) ** 2 for a in xs)
    syy = sum((b - my) ** 2 for b in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


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
            max_skew_s: float = 0.0, settle_s: float = 0.0) -> List[Tuple[float, float]]:
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

    ``max_skew_s`` is what keeps a sum honest when its inputs do not tick
    together. A template sensor recomputes whenever EITHER input updates,
    against the other's last value, so a cloud puts one term 3 kW out of date
    for a few seconds and the result has a step in it that no load made.
    Above zero, a sample is only emitted when every term has reported within
    that long of it.
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
            if max_skew_s and ts - rows[j][0] > max_skew_s:
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
        # max_skew_s was meant for this and cannot do it on recorder data:
        # Home Assistant only records a CHANGE, so an inverter sitting at 0 W
        # all night looks hours stale and every night-time sample is dropped,
        # the kiln with them (347 -> 119 sessions). A burst needs no judgement
        # about freshness, only about what came after (2026-09-23).
        out = [r for r, nxt in zip(out, out[1:] + [None])
               if nxt is None or nxt[0] - r[0] > settle_s]
    return out


def _as_of(rows: Sequence[Tuple[float, float]], ts: float, i: int) -> int:
    """Index of the last row at or before ``ts``, walking forward from ``i``;
    -1 when the series has not started yet."""
    if not rows or rows[0][0] > ts:
        return -1
    i = max(i, 0)
    while i + 1 < len(rows) and rows[i + 1][0] <= ts:
        i += 1
    return i


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


def energy_between(rows: Sequence[Tuple[float, float]], start: float, end: float) -> Optional[float]:
    """Watt-hours a reading accounts for between two instants, sample and hold.

    This is what a coarse meter CAN answer. A Shelly reporting once a minute
    cannot describe a 66-second run - it gets three samples and its session
    power comes out at half the truth - but the energy it recorded over that
    minute is right, because energy integrates and sampling error cancels
    where power's does not (Anze, 2026-09-19, on a Zigbee meter that cannot
    be made faster at all).

    None when the window is not covered by the reading.
    """
    if not rows or end <= start:
        return None
    # Both ends, not just the near one. Sample-and-hold carries the last
    # reading forward for as long as you let it, so a meter that fell silent
    # an hour ago will answer for a window it never saw - and the answer,
    # "it drew exactly what it was drawing before", is indistinguishable
    # from a device that really did stay put. The docstring promised this
    # check; the code only ever made half of it (2026-09-19).
    if rows[0][0] > start or rows[-1][0] < end:
        return None
    total = 0.0
    # Seek, don't walk. _as_of scans forward from the index it is handed, and
    # a session waits MATCH_PATIENCE_S for an answer - so every pending
    # session asks every device meter twice, every pass, and each of those
    # questions would otherwise re-read the whole retained tail from the
    # front (2026-09-19).
    i = bisect.bisect_right(rows, (start, float("inf"))) - 1
    if i < 0:
        return None
    at = start
    while at < end:
        nxt = rows[i + 1][0] if i + 1 < len(rows) else end
        until = min(nxt, end)
        total += rows[i][1] * (until - at)
        at = until
        if i + 1 < len(rows):
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
                        "pf": sig.get("pf"),
                        # the meter reading comes across too, or it steps down
                        "energy_wh": (sum(sig.get("hour_wh") or [])
                                      + (sig.get("carried_wh") or 0.0))})
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
    if len(rows) < PV_MIN_SAMPLES:
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
    ordered, i, seen = sorted(generation), 0, []
    at = sorted(busy)
    rows = sorted(grid_rows)
    j = 0
    for ts in at:
        while j + 1 < len(rows) and rows[j + 1][0] <= ts:
            j += 1
        if rows and rows[0][0] <= ts:
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
    through (Anze, 2026-09-18)."""
    if len(rows) < PV_MIN_SAMPLES:
        return False
    live = sum(1 for _, value in rows if abs(value) > SOURCE_IDLE_W)
    return live >= LIVE_SHARE * len(rows)


def classify_source(rows: Sequence[Tuple[float, float]]) -> Optional[str]:
    """What is behind an AC input, from the reading alone.

    The reading cannot tell a utility meter from a generator's - both are
    watts at an input port - but the BEHAVIOUR separates them, and neither
    is a preference anyone should have to type (Anze, 2026-09-18):

      * only a utility ABSORBS a surplus, so a reading that goes usefully
        negative is the grid and nothing else;
      * a generator is off far more than it is on, so a source that spends
        almost all its life at zero and never absorbs is one;
      * a port that has never carried anything at all is, as far as the data
        goes, not connected.

    The last two are the same reading for a generator that has not run in
    the window, which is the one case the setting is worth overriding for -
    "you will need the generator" and "you will go dark" are not the same
    warning. Callers should keep the most informative verdict they have seen
    rather than following this down to ``none`` again.

    None when there is too little to look at."""
    if len(rows) < PV_MIN_SAMPLES:
        return None
    if any(value < -EXPORT_FLOOR_W for _, value in rows):
        return SOURCE_UTILITY
    live = sum(1 for _, value in rows if abs(value) > SOURCE_IDLE_W)
    if not live:
        return SOURCE_NONE
    return SOURCE_GENERATOR if live <= (1.0 - SOURCE_IDLE_SHARE) * len(rows) else SOURCE_UTILITY


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


@dataclass
class _Open:
    """A load believed to be running: the step that started it, what is still
    running of it, and every level it has held."""
    since: float
    watts: float
    var: Optional[float]                          # the reactive step it started with
    levels: List[Tuple[float, float]] = field(default_factory=list)
    surge: float = 0.0                            # see PhaseState._declare_surge
    # how long this may stay open before it is given up on - see ORPHAN_MARGIN.
    # Worked out once, when first needed; never persisted.
    limit: Optional[float] = None
    # Lowest and highest the phase read while this was the ONLY load running.
    # A resistive element holds its level; anything behind a variable-speed
    # drive glides between them without ever taking a step big enough to be
    # a LEVEL. Kozolec's Grundfos Scala2 runs 104 to 247 W and reads as one
    # flat level, which is why it classified as a small heater (Anze,
    # 2026-09-18).
    lo: Optional[float] = None
    hi: Optional[float] = None

    def as_list(self) -> list:
        return [self.since, self.watts, self.var, [list(x) for x in self.levels], self.lo, self.hi,
                self.surge]

    @classmethod
    def of(cls, raw) -> "_Open":
        since, watts, var = raw[0], raw[1], raw[2]
        levels = [tuple(x) for x in (raw[3] if len(raw) > 3 else [])] or [(since, watts)]
        return cls(since=since, watts=watts, var=var, levels=levels,
                   lo=raw[4] if len(raw) > 4 else None,
                   hi=raw[5] if len(raw) > 5 else None,
                   surge=raw[6] if len(raw) > 6 else 0.0)


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
    gaps: List[float] = field(default_factory=list)       # recent sample gaps, see INTERVAL_PERCENTILE
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
    # Set by the Detector for a pass: how long a load of a given size on this
    # phase has been seen to run, or None. See ORPHAN_MARGIN.
    longest: Optional[object] = field(default=None, repr=False, compare=False)
    # the measured share of the running level that is noise, and the samples
    # it is measured from
    noise_rel: float = 0.0
    rel_diffs: List[float] = field(default_factory=list)
    seed: List[float] = field(default_factory=list)
    idle_diffs: List[float] = field(default_factory=list)
    pending: List[Tuple[float, float, Optional[float], Optional[float]]] = field(default_factory=list)
    open_edges: List[_Open] = field(default_factory=list)   # believed to be running
    last_ts: Optional[float] = None
    # this phase's own sampling interval, as a slow mean of the gaps between
    # samples - the meter's rate, not a setting, so turning a poll up from
    # 5 s to 1 s is noticed rather than configured
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
                pv: Optional[float] = None) -> List[Session]:
        """One sample: seconds, watts, reactive VAr where the meter gives
        enough to work it out, and what the array was making at the time.
        Returns the sessions this sample closed - more than one when several
        loads stopped together."""
        if self.last_ts is not None and ts <= self.last_ts:
            return []
        if self.last_ts is not None:
            gap = ts - self.last_ts
            if 0.0 < gap < 120.0:        # a restart gap is not a sampling rate
                if INTERVAL_PERCENTILE:
                    # the meter's CADENCE, not the gap between recorded
                    # changes - see INTERVAL_PERCENTILE
                    self.gaps.append(gap)
                    del self.gaps[:-INTERVAL_GAPS]
                    ordered = sorted(self.gaps)
                    self.interval = ordered[int(INTERVAL_PERCENTILE * (len(ordered) - 1))]
                else:
                    self.interval = gap if not self.interval else self.interval + 0.05 * (gap - self.interval)
        self.last_ts = ts
        if self.floor_zero and w < -GLITCH_FLOOR_W:
            return []                 # a house cannot draw less than nothing; skip it
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

        if self.open_edges:
            if ORPHAN_MARGIN and self.longest:
                for e in self.open_edges:
                    if e.limit is None:
                        seen = self.longest(e.watts)
                        e.limit = min(MAX_OPEN_S, ORPHAN_MARGIN * seen) if seen else MAX_OPEN_S
            if any(ts - e.since > (e.limit or MAX_OPEN_S) for e in self.open_edges):
                # a start whose stop was never seen: give up rather than pair
                # it with an unrelated load hours later
                self.open_edges = [e for e in self.open_edges
                                   if ts - e.since <= (e.limit or MAX_OPEN_S)]

        if abs(w - self.level) < self.noise_at(self.level):
            self.pending = []
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
            if self.level is not None and abs(self.level) >= self.rel_floor:
                self.rel_diffs.append(abs(w - self.level) / abs(self.level))
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
                self.level = self.baseline
                self.idle_diffs.append(abs(w - self.baseline))
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
        sustain = (SUSTAIN_INTERVALS * self.interval if self.interval
                   else SUSTAIN_SECONDS)
        need = SUSTAIN_SAMPLES
        if CORROBORATED_STOP_SAMPLES and self._corroborated_stop(ts):
            # another leg of the same load is stopping at the same moment
            need = CORROBORATED_STOP_SAMPLES
            sustain = CORROBORATED_STOP_INTERVALS * self.interval if self.interval else 0.0
        elif MATCHED_STOP_SAMPLES and self._matched_stop():
            # a stop the open start already vouches for - see MATCHED_STOP_SAMPLES
            need = MATCHED_STOP_SAMPLES
            sustain = MATCHED_STOP_INTERVALS * self.interval if self.interval else 0.0
        if len(self.pending) < need or (ts - self.pending[0][0]) < sustain:
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
        if STEP_AT_HALFWAY:
            half = 0.5 * abs(new_level - self.level)
            since = next((p[0] for p in self.pending if abs(p[1] - self.level) >= half), since)
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
        if step > 0:
            self.open_edges.append(_Open(since, step, step_q, [(since, step)], surge=surge))
            if len(self.open_edges) > MAX_OPEN_EDGES:
                self.open_edges.pop(0)
            return []
        closed = self._pair(since, -step, None if step_q is None else -step_q, new_level)
        if self.open_edges and new_level <= self.baseline + self.noise:
            # back at the idle floor, so whatever was still open has stopped
            # without us seeing it go. Holding those starts open would have
            # them pair with an unrelated load hours later, and meanwhile
            # count as running.
            self.open_edges = []
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
        if not SUSTAIN_AGREE or len(self.pending) < 2:
            return self.pending
        w = self.pending[-1][1]
        # Either inside the noise, or close enough that the step measured from
        # either reading would pair with the same edges - the pairing
        # tolerance, applied to the step being made.
        tol = SUSTAIN_AGREE_TOL * self.noise_at(w)
        if SUSTAIN_AGREE_REL and self.level is not None:
            tol = max(tol, SUSTAIN_AGREE_REL * abs(w - self.level))
        k = len(self.pending) - 1
        while k > 0 and abs(self.pending[k - 1][1] - w) <= tol:
            k -= 1
        return self.pending[k:] if k else self.pending

    def _matched_edge(self):
        """The open edge whose size what is pending has dropped by, if any."""
        if self.level is None or not self.open_edges:
            return None
        drop = self.level - _median([x for _, x, _, _ in self._held()])
        if drop <= self.noise_at(self.level):
            return None
        return next((o for o in reversed(self.open_edges)
                     if abs(o.watts - drop) <= self._tol(o.watts, drop)), None)

    def _matched_stop(self) -> bool:
        """Is what is pending a DROP the size of a load already running?"""
        return self._matched_edge() is not None

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
        if CORROBORATED_SPLIT:
            # a start bigger than the drop, whose partner legs match the DROP
            for o in reversed(self.open_edges):
                if o.watts - drop > self._tol(o.watts, drop) and self.corroborate(o.since, drop, ts, True):
                    self.close_hint = o
                    return True
        return False

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
        hint, self.close_hint = self.close_hint, None
        if hint is not None and CORROBORATED_CLOSES_ITS_EDGE:
            for i, o in enumerate(self.open_edges):
                if o is hint and abs(o.watts - watts) <= self._tol(o.watts, watts):
                    self.open_edges.pop(i)
                    self._remember_close(o, at)
                    return [self._close(o, at, watts, var)]
                if o is hint and CORROBORATED_SPLIT and o.watts - watts > self._tol(o.watts, watts):
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
            tol = self._tol(o.watts, watts)
            gap = abs(o.watts - watts)
            if gap <= tol:
                cands.append((i, gap, tol))
        if cands:
            best_gap = min(g for _, g, _ in cands)
            band = PAIR_TIE_BAND * max(t for _, _, t in cands)
            i = max(i for i, g, _ in cands if g <= best_gap + band)   # newest of the tied
            o = self.open_edges.pop(i)
            self._remember_close(o, at)
            return [self._close(o, at, watts, var)]
        for i in range(len(self.open_edges) - 1, -1, -1):
            o = self.open_edges[i]
            if o.watts - watts > self._tol(o.watts, watts):
                o.watts -= watts
                o.levels.append((at, o.watts))
                return []
        # several loads going together - the oven and its fan, a programme
        # ending - leave one step too big for any of them alone. Take them
        # largest first while the step still covers them, or nothing.
        order = sorted(range(len(self.open_edges)), key=lambda i: -self.open_edges[i].watts)
        taken, left = [], watts
        for i in order:
            o = self.open_edges[i]
            if o.watts <= left + self._tol(o.watts, left):
                taken.append(i)
                left -= o.watts
                if left <= self.noise:
                    break
        if len(taken) >= 2 and abs(left) <= self._tol(watts, watts):
            out = []
            for i in sorted(taken, reverse=True):
                o = self.open_edges.pop(i)
                out.append(self._close(o, at, o.watts, None))
            return sorted(out, key=lambda x: x.start)
        if not self.open_edges:
            # nothing was running: the floor itself moved
            self.baseline = max(new_level, 0.0) if self.floor_zero else new_level
        return []

    def _span(self, since: float, until: float) -> int:
        """How many meter samples a run of that length was measured over, from
        this phase's own observed sampling interval."""
        if not self.interval or self.interval <= 0:
            return 0
        return max(1, int(round((until - since) / self.interval)) + 1)

    def _close(self, o: _Open, at: float, watts: float, var: Optional[float]) -> Session:
        levels = list(o.levels)
        if len(levels) == 1:
            # one level throughout: both steps measure the same load, so
            # average them, and its power factor with them
            levels = [(o.since, 0.5 * (levels[0][1] + watts))]
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
        return Session(phases="", start=o.since, end=at, levels={"": levels},
                       surge_w=o.surge,
                       pf=_pf_from(levels[0][1], q),
                       pf_mad=_pf_spread(levels[0][1], q, self.q_quantum),
                       samples=self._span(o.since, at), low=o.lo, high=o.hi)

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
                "open_edges": [o.as_list() for o in self.open_edges], "last_ts": self.last_ts}

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
                   open_edges=[_Open.of(x) for x in d.get("open_edges") or []], last_ts=d.get("last_ts"))


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
    # How many sightings caught a surge, and how big it was when they did. The
    # mean above is diluted by every start the meter missed: Home's Kompresor
    # caught 3 of 396, so its mean said nothing although each catch was 3x.
    # Recorded, not yet used: weighing the when-seen size instead was tried and
    # inflated the small electronic loads' false surges 5-20x (see AGENTS.md).
    # What would separate a motor from a coincidence is CONSISTENCY.
    inrush_seen: int = 0
    inrush_when_seen: float = 0.0
    duration_mad: float = 0.0
    interval_mad: Optional[float] = None

    def matches(self, s: Session, noise_w: float) -> Optional[float]:
        """A score in (0, 1] when ``s`` fits, None when it does not.

        A coarsely-measured session gets a wider power band: a 43-second run
        read every 5 s is eight numbers, and holding that to the same
        tolerance as a run read forty-three times is asking the meter for
        precision it never had."""
        if s.phases != self.phases:
            return None
        pw = s.power_by_phase()
        score = 1.0
        rel = MATCH_POWER_REL / max(0.25, s.confidence)
        for ph in self.phases:
            mine, theirs = self.power.get(ph, 0.0), pw.get(ph, 0.0)
            tol = power_tolerance(max(rel * max(mine, theirs), noise_w), self.power_mad)
            if abs(mine - theirs) > tol:
                return None
            score *= 1.0 - abs(mine - theirs) / (2 * tol)
        ratio = max(s.duration_s, 1.0) / max(self.duration_s, 1.0)
        factor = self.duration_factor
        if ratio > factor or ratio < 1.0 / factor:
            return None
        if self.pf is not None and s.pf is not None and \
                abs(self.pf - s.pf) > pf_tolerance(self.pf_mad, s.pf_mad):
            return None
        return score

    def alike(self, other: "Signature", noise_w: float) -> bool:
        """Would these two be the same signature if they arrived now?

        The same test as ``matches``, between two signatures rather than a
        signature and a session."""
        if self.phases != other.phases or self is other:
            return False
        if self.name and other.name and self.name != other.name:
            return False                       # named apart on purpose
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
        ratio = max(other.duration_s, 1.0) / max(self.duration_s, 1.0)
        factor = min(self.duration_factor, other.duration_factor)
        if ratio > factor or ratio < 1.0 / factor:
            return False
        if self.pf is not None and other.pf is not None and \
                abs(self.pf - other.pf) > pf_tolerance(self.pf_mad, other.pf_mad):
            return False
        return True

    def swallow(self, other: "Signature") -> None:
        """Take another signature's sightings into this one, by weight."""
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
        seen = self.inrush_seen + other.inrush_seen
        if seen:
            self.inrush_when_seen = ((self.inrush_when_seen * self.inrush_seen
                                      + other.inrush_when_seen * other.inrush_seen) / seen)
        self.inrush_seen = seen
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
        if s.inrush_w > 0:
            self.inrush_when_seen = ((self.inrush_when_seen * self.inrush_seen + s.inrush_w)
                                     / (self.inrush_seen + 1))
            self.inrush_seen += 1
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

    def row(self, tz, now: Optional[float] = None, running: bool = False) -> str:
        """One line for a menu, and a MENU ROW IS NARROW - it truncated at
        about sixty characters and took the useful half with it (Anze,
        2026-09-18). So: what it draws, what it costs a week and a run, how
        long it runs, how often, one word for what it might be, and the week
        itself in seven characters."""
        phases = "+".join(p.upper() for p in self.phases)
        # How often was left off entirely, as being the least use for telling
        # one row from another. That is true of an irregular load and quite
        # wrong for a REGULAR one: Kozolec's hot water cycles 66 seconds every
        # five minutes for twelve hours a day, and "every 5 min" is the thing
        # its owner would recognise before any other number on the line
        # (Anze, 2026-09-18). So it appears only when the load keeps a clock.
        how_long = _fmt_s(self.duration_s)
        if self.regular and self.interval_s:
            how_long = f"{how_long} every {_fmt_s(self.interval_s)}"
        bits = [f"{_fmt_w(self.watts)} ({phases})",
                f"{_fmt_wh(self.weekly_wh)}/{_fmt_wh(self.per_run_wh)}",
                how_long]
        tag = self.guess().tag
        if tag:
            bits.append(tag)
        generally = self.when
        if generally:
            bits.append(generally)
        when = last_run_phrase(self.last_seen, now, running)
        if when:
            bits.append(when)
        line = ", ".join(bits)
        # The sparkline is seven characters of the week, and "weekdays" says
        # the same thing in eight - so only one of them earns its place on a
        # row this narrow. The words win: they are read rather than decoded,
        # and the detail view keeps both the day and hour histograms for
        # anyone who wants the actual shape.
        bars = "" if generally else sparkline(self.day_wh)
        return f"{line} {bars}" if bars else line

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
        if self.regular and self.interval_s:
            rest.append(f"starts every {_fmt_s(self.interval_s)}")
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
    def keeps_time(self) -> bool:
        """Has this load shown that its duration is part of what it is?"""
        if self.count < DURATION_IDENTITY_COUNT:
            return False
        return self.duration_mad / max(self.duration_s, 1.0) <= DURATION_IDENTITY_SPREAD

    @property
    def duration_factor(self) -> float:
        """How far a sighting's duration may stray and still be this load: 3x
        for a load that keeps time, 30x for one that does not.

        It looks like a cliff that ought to be continuous, and was swept as one
        - exp(z * measured spread), held between the same bounds, z from 2 to 6
        at both sites (2026-09-23). Tight values lose clearly (Home's workshop
        boiler halves, Kozolec's hidrofor 142 -> 99) and loose ones converge
        back to what the cliff already does. The cliff measures as right."""
        return MATCH_DURATION_FACTOR if self.keeps_time else LOOSE_DURATION_FACTOR

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
        tight_d = 1.0 - min(1.0, (self.duration_mad / max(self.duration_s, 1.0)) / 0.5)
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
        the one it began in."""
        watts = sum(s.power_by_phase().values())
        if watts <= 0 or s.end <= s.start:
            return
        t = s.start
        while t < s.end:
            moment = datetime.fromtimestamp(t, tz)
            into = moment.minute * 60 + moment.second + moment.microsecond / 1e6
            step = min(s.end - t, max(3600.0 - into, 1.0))
            wh = watts * step / 3600.0
            self.hour_wh[moment.hour] += wh
            self.day_wh[moment.weekday()] += wh
            t += step

    def describe(self, tz, now: Optional[float] = None, running: bool = False) -> str:
        """Words for the naming page: '6.1 kW on A+C, ~80 s, every 3 min, seen
        258 times - maybe a heating element (power factor 1.00, one level)'."""
        dur = _fmt_s(self.duration_s)
        gap = f", starts every {_fmt_s(self.interval_s)}" if self.interval_s else ""
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
                "inrush_w": _trim(self.inrush_w, 1), "inrush_seen": self.inrush_seen,
                "inrush_when_seen": _trim(self.inrush_when_seen, 1),
                "successor_id": self.successor_id, "carried_wh": _trim(self.carried_wh, 1),
                "low": _trim(self.low, 1), "high": _trim(self.high, 1),
                "duration_mad": _trim(self.duration_mad, 1),
                "interval_mad": _trim(self.interval_mad, 1)}

    @classmethod
    def from_dict(cls, d: dict) -> "Signature":
        return cls(id=d["id"], phases=d["phases"], power=dict(d["power"]), duration_s=d["duration_s"], pf=d.get("pf"),
                   count=d["count"], first_seen=d["first_seen"], last_seen=d["last_seen"], interval_s=d.get("interval_s"),
                   hour_wh=list(d.get("hour_wh") or [0.0] * 24),
                   day_wh=list(d.get("day_wh") or [0.0] * 7),
                   level_count=d.get("level_count", 1.0), name=d.get("name"),
                   last_start=d.get("last_start"), locations=dict(d.get("locations") or {}),
                   power_mad=d.get("power_mad", 0.0), pf_mad=d.get("pf_mad", 0.0),
                   runs=[tuple(x) for x in (d.get("runs") or [])], inrush_w=d.get("inrush_w", 0.0), inrush_seen=d.get("inrush_seen", 0),
                   inrush_when_seen=d.get("inrush_when_seen", 0.0), duration_mad=d.get("duration_mad", 0.0),
                   successor_id=d.get("successor_id"), carried_wh=d.get("carried_wh", 0.0),
                   low=d.get("low"), high=d.get("high"),
                   interval_mad=d.get("interval_mad"))


def _fmt_w(x: float) -> str:
    """Watts below a kilowatt, kilowatts above.

    Everything was printed as kilowatts to one decimal, which is fine for a
    kettle and useless for everything a submeter sees: a whole library of a
    computer, a UPS and an office plug read "0.0 kW on A" line after line,
    every row identical and none of them wrong (Anze, 2026-09-22)."""
    return f"{x:.0f} W" if abs(x) < 1000 else f"{x / 1000:.1f} kW"


def _fmt_wh(x: float) -> str:
    return f"{x:.0f} Wh" if x < 1000 else f"{x / 1000:.1f} kWh"


def _fmt_s(x: Optional[float]) -> str:
    if x is None:
        return "?"
    if x < 90:
        return f"{x:.0f} s"
    if x < 5400:
        return f"{x / 60:.0f} min"
    if x < 172800:                      # past two days, hours stop being readable
        return f"{x / 3600:.1f} h"
    return f"{x / 86400:.1f} days"


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
class Detector:
    phases: Dict[str, PhaseState] = field(default_factory=lambda: {p: PhaseState() for p in PHASES})
    held: List[Session] = field(default_factory=list)          # closed, waiting for a partner phase
    signatures: List[Signature] = field(default_factory=list)
    recent: List[dict] = field(default_factory=list)           # last sessions with their signature id
    # ids that consolidation has retired, so a session filed before a merge
    # still resolves to the signature that swallowed it
    _moved: Dict[int, int] = field(default_factory=dict)
    # Names whose signature is gone - after a reset, or after an upgrade that
    # could not read the old library. They hold enough of a description to be
    # recognised again, and are handed back to the first signature that looks
    # like them (see _reclaim).
    orphan_names: List[dict] = field(default_factory=list)
    # name -> watt-hours its meter had already reached. A rebuilt library
    # covers ten days where the old one had accumulated since it was
    # installed, so without this the meter steps DOWN when a name comes back.
    energy_floor: Dict[str, float] = field(default_factory=dict)
    next_id: int = 1
    tz_offset_s: float = 0.0

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
        derived from it. Returns the sessions this batch closed."""
        closed: List[Session] = []
        latest = now_ts or 0.0
        # ALL phases in time order, not one phase after another. Each phase's
        # state is its own, so the order changes nothing by itself - but it
        # means that when one leg of a load is judged, the other legs' state is
        # as of the same moment rather than the end of the previous batch,
        # which is what lets one leg vouch for another (see _corroborate).
        stream = []
        for ph, rows in samples.items():
            if ph not in self.phases:
                continue
            st = self.phases[ph]
            if q_quantum and q_quantum.get(ph):
                st.q_quantum = q_quantum[ph]
            st.corroborate = self._corroborate(ph, samples)
            st.longest = self._longest(ph)
            stream.extend((ts, i, ph, w) for i, (ts, w) in enumerate(rows))
        stream.sort()
        for ts, _, ph, w in stream:
            latest = max(latest, ts)
            qm = (q or {}).get(ph) or {}
            pvm = (pv or {}).get(ph) or {}
            for s in self.phases[ph].process(ts, w, qm.get(ts), pvm.get(ts)):
                s.phases = ph
                s.levels = {ph: s.levels.pop("")}
                closed.append(s)
        out = self._merge_and_file(closed, latest, file)
        # once per pass, not once per session: it walks the whole
        # library for every named load, and nothing about it changes
        # between one filing and the next
        if latest:
            self._link_successors(latest)
        return out

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
        for oph, rows in samples.items():
            if oph != ph and oph in self.phases and rows:
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

    def _longest(self, ph: str):
        """How long a load of a given size on this phase has been seen to run:
        the longest of its recent runs, or its typical run plus the usual
        margin for noise, whichever is more - over every signature with a leg
        that size, since two loads of one size are not told apart here. None
        when the library knows no such load. See ORPHAN_MARGIN."""
        def seen(watts: float) -> Optional[float]:
            st = self.phases[ph]
            best = None
            for sig in self.signatures:
                w = sig.power.get(ph)
                if not w or sig.count < YOUNG_COUNT or abs(w - watts) > st._tol(w, watts):
                    continue
                d = max([e - s for s, e in sig.runs]
                        + [sig.duration_s + NOISE_MAD_FACTOR * sig.duration_mad])
                best = d if best is None else max(best, d)
            return best
        return seen

    def _merge_and_file(self, closed: List[Session], latest: float, file: bool = True) -> List[Session]:
        pool = self.held + closed
        pool.sort(key=lambda s: s.start)
        groups: List[List[Session]] = []
        for s in pool:
            for g in groups:
                if (abs(g[0].start - s.start) <= MERGE_TOLERANCE_S and abs(g[0].end - s.end) <= MERGE_TOLERANCE_S
                        and all(s.phases not in m.phases for m in g)
                        and _balanced(g, s)):
                    g.append(s)
                    break
            else:
                groups.append([s])
        done: List[Session] = []
        self.held = []
        for g in groups:
            # a group still young enough that a partner phase may yet close waits
            if latest - max(m.end for m in g) < HELD_TAIL_S and len(g) < 3:
                self.held.extend(g)
                continue
            done.append(self._combine(g))
        out = []
        for s in done:
            if s.energy_wh < NOISE_SESSION_WH and s.duration_s < NOISE_SESSION_S:
                continue
            if file:
                self._file(s)
            out.append(s)
        return out

    def signature_of(self, s: Session) -> Optional["Signature"]:
        """The signature a just-filed session went into.

        From the session itself. Reading it back out of ``recent`` worked only
        while a pass filed fewer sessions than that list keeps."""
        sid = s.signature_id
        if sid is None:
            return None
        seen = set()
        while sid in self._moved and sid not in seen:
            seen.add(sid)
            sid = self._moved[sid]
        return next((x for x in self.signatures if x.id == sid), None)

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
                       surge_w=sum(m.surge_w for m in g),
                       pf_mad=max((m.pf_mad for m in g if m.pf is not None), default=0.0))

    def _file(self, s: Session, prefer: Optional[int] = None) -> None:
        """File a closed session into the library. ``prefer`` is a signature
        a sub-meter's own detection says this load belongs to; it wins over
        the best-scoring one whenever it fits at all (see SUB_OVERRIDE)."""
        tz = timezone.utc if not self.tz_offset_s else timezone(__import__("datetime").timedelta(seconds=self.tz_offset_s))
        noise = max(self.phases[p].noise for p in s.phases) if s.phases else MIN_NOISE_W
        best, best_score = None, 0.0
        if prefer is not None:
            seen = set()
            while prefer in self._moved and prefer not in seen:
                seen.add(prefer)
                prefer = self._moved[prefer]
            want = next((x for x in self.signatures if x.id == prefer), None)
            if want is not None and want.matches(s, noise) is not None:
                best = want
        for sig in ([] if best is not None else self.signatures):
            sc = sig.matches(s, noise)
            if sc is not None and sc > best_score:
                best, best_score = sig, sc
        if best is None:
            best = Signature(id=self.next_id, phases=s.phases, power=s.power_by_phase(), duration_s=s.duration_s,
                             pf=s.pf, count=0, first_seen=s.start, last_seen=s.start, level_count=float(s.level_count))
            self.next_id += 1
            self.signatures.append(best)
            best.absorb(s, tz)
            best.count = 1
        else:
            best.absorb(s, tz)
        s.signature_id = best.id
        self.recent.append({"start": s.start, "end": s.end, "phases": s.phases, "kwh": round(s.energy_wh / 1000.0, 3),
                            "max_w": round(s.max_w), "levels": s.level_count, "signature": best.id})
        self.recent = self.recent[-MAX_RECENT_SESSIONS:]
        self.consolidate(noise)
        if self.orphan_names:
            # after consolidate, so a name lands on the signature that survived
            # the merge rather than on one about to be swallowed
            for sig in self.signatures:
                self._reclaim(sig, noise)
        self._prune(s.end)

    def pf_floor(self, phases: str) -> float:
        """The load size at which these amps' resolution costs a tenth of a
        factor - what the sensor publishes so a site can see where its power
        factors stop meaning much. Nothing is gated on it any more; the error
        bar each factor carries does that work now, load by load."""
        worst = max((self.phases[p].q_quantum for p in phases if p in self.phases),
                    default=0.0)
        return PF_MIN_QUANTA * worst

    def consolidate(self, noise_w: float = MIN_NOISE_W) -> int:
        """Merge signatures that have BECOME alike, and say how many went.

        Power and duration are running MEANS, so two signatures that are now
        indistinguishable need not have been when the second was created.
        Kozolec had one 1.8 kW load split five ways - 230, 136, 50, 27 and 18
        sightings, all within 4 % of each other - because the first one's
        mean duration was different at the moment the second arrived. Filing
        only ever looks at the signatures as they stand, and nothing came
        back to them afterwards; this does, by the same rule.
        """
        gone = 0
        again = True
        while again:
            again = False
            self.signatures.sort(key=lambda x: -x.count)
            for i, keep in enumerate(self.signatures):
                doomed = [j for j in range(i + 1, len(self.signatures))
                          if keep.alike(self.signatures[j], noise_w)]
                if not doomed:
                    continue
                moved = {}
                # Re-ask on every one. ``doomed`` was judged against ``keep``
                # as it stood BEFORE any of them went in, and each swallow
                # moves its mean - so a list gathered in one breath could
                # carry it somewhere none of the later entries would have
                # been admitted to. Reverse order keeps the lower indices
                # valid as they are popped, and one that no longer fits is
                # simply left where it is (2026-09-21).
                for j in reversed(doomed):
                    if not keep.alike(self.signatures[j], noise_w):
                        continue
                    other = self.signatures.pop(j)
                    moved[other.id] = keep.id
                    keep.swallow(other)
                    gone += 1
                if not moved:
                    continue
                for r in self.recent:            # the sessions still point at them
                    if r.get("signature") in moved:
                        r["signature"] = moved[r["signature"]]
                for s in self.held:
                    if s.signature_id in moved:
                        s.signature_id = moved[s.signature_id]
                self._moved.update(moved)
                again = True
                break
        return gone

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

    def energy_by_name(self) -> Dict[str, float]:
        """Watt-hours each NAME has used over everything ever seen of it.

        Signatures sharing a name are one device, so their energy adds. Only
        ever grows: hour_wh accumulates and a merge sums both sides, so this
        is safe to publish as a total-increasing meter."""
        out: Dict[str, float] = {}
        for sig in self.signatures:
            if sig.name:
                out[sig.name] = out.get(sig.name, 0.0) + sig.energy_wh
        # A library rebuilt from ten days does not know what the meter read
        # before it, so the old reading is a FLOOR rather than something to
        # add: the two periods overlap, and adding them would count those ten
        # days twice. Once the rebuilt library has accumulated past the old
        # total the floor stops mattering of its own accord. It is kept per
        # NAME rather than on the signature so that per_run_wh and the hour
        # and weekday charts still describe the load, not its lifetime
        # (Anze, 2026-09-22: "is there a way we could also fix this?").
        for name, floor in self.energy_floor.items():
            if floor > out.get(name, 0.0):
                out[name] = floor
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
        totals = self.energy_by_name()
        return [{"name": sig.name, "phases": sig.phases, "power": dict(sig.power),
                 "duration_s": sig.duration_s, "pf": sig.pf,
                 "energy_wh": totals.get(sig.name, 0.0)}
                for sig in self.signatures if sig.name]

    def carry_names(self, descriptors: List[dict]) -> None:
        """Take names, and the meter readings they had, into a fresh library."""
        self.orphan_names = [dict(d) for d in descriptors if d.get("name")]
        self.energy_floor = {d["name"]: float(d.get("energy_wh") or 0.0)
                             for d in self.orphan_names}

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
                return

    def rename(self, signature_id: int, name: Optional[str]) -> bool:
        for sig in self.signatures:
            if sig.id == signature_id:
                sig.name = (name or "").strip() or None
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
        self.rename(signature_id, name)
        return name

    # ------------------------------------------------ storage
    def to_dict(self) -> dict:
        return {"phases": {p: st.to_dict() for p, st in self.phases.items()}, "held": [s.to_dict() for s in self.held],
                "signatures": [s.to_dict() for s in self.signatures], "recent": self.recent, "next_id": self.next_id,
                "tz_offset_s": self.tz_offset_s, "orphan_names": self.orphan_names,
                "energy_floor": {k: _trim(v, 1) for k, v in self.energy_floor.items()}}

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
        det.energy_floor = {k: float(v) for k, v in (d.get("energy_floor") or {}).items()}
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
    pending_main: List[Session] = field(default_factory=list)    # main sessions awaiting a downstream partner
    pending_sub: Dict[str, List[Session]] = field(default_factory=dict)
    # Each device's raw samples, kept long enough to answer "how much energy
    # did you record while this was running". A meter too slow to produce a
    # session of its own can still answer that.
    sub_rows: Dict[str, List[Tuple[float, float]]] = field(default_factory=dict)
    agnostic: Dict[str, bool] = field(default_factory=dict)      # meters that report only a total
    # What each device meter can RESOLVE, measured from the rows above. The
    # energy answer is an integral of those rows, so their quantisation is
    # its error bar - and Home has a workshop boiler publishing in 46 W steps
    # about every seven minutes (Anze, 2026-09-22).
    sub_quantum: Dict[str, float] = field(default_factory=dict)
    # meter -> its own phase label -> house phase -> sessions that matched,
    # for phase_mapping
    phase_votes: Dict[str, Dict[str, Dict[str, int]]] = field(default_factory=dict)
    # house sessions not yet filed, waiting for a sub-meter partner
    unfiled: List[Session] = field(default_factory=list)
    # meter -> its signature id -> the house signature its sessions joined
    identity: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def process(self, main_samples, sub_samples: Dict[str, Dict[str, Sequence[Tuple[float, float]]]],
                main_q=None, sub_q=None, now_ts: Optional[float] = None,
                agnostic: Optional[Dict[str, bool]] = None,
                pv: Optional[Dict[str, Dict[float, float]]] = None,
                main_q_quantum: Optional[Dict[str, float]] = None,
                sub_q_quantum: Optional[Dict[str, Dict[str, float]]] = None) -> None:
        latest = now_ts or 0.0
        if agnostic:
            self.agnostic.update(agnostic)
        # only the main meter needs the array: a downstream meter sees the
        # house side of it and never the sun
        latest_seen = now_ts or 0.0
        for name, rows_by_phase in (sub_samples or {}).items():
            merged: List[Tuple[float, float]] = []
            for series in rows_by_phase.values():
                merged = _sum_series(merged, list(series))
            if not merged:
                continue
            kept = self.sub_rows.get(name, []) + merged
            kept.sort()
            latest_seen = max(latest_seen, kept[-1][0])
            # Everything this pass brought, plus a tail before it. Trimming to
            # a fixed two hours looked thrifty and silently gutted the
            # backfill, whose slices are six hours long: the sessions being
            # placed were mostly older than the readings kept to place them
            # with (2026-09-19).
            oldest = min((r[0] for r in merged), default=latest_seen)
            cut = min(oldest, latest_seen) - SUB_SAMPLE_TAIL_S
            self.sub_rows[name] = [r for r in kept if r[0] >= cut]
            q = measure_quantum([v for _, v in self.sub_rows[name]])
            if q:
                self.sub_quantum[name] = q
        closed_main = self.main.process(main_samples, main_q, now_ts, pv, main_q_quantum,
                                        file=not SUB_OVERRIDE)
        closed_sub = {}
        for name, samples in sub_samples.items():
            det = self.subs.setdefault(name, Detector())
            det.tz_offset_s = self.main.tz_offset_s
            closed_sub[name] = det.process(samples, (sub_q or {}).get(name), now_ts,
                                           None, (sub_q_quantum or {}).get(name))
        if SUB_OVERRIDE:
            self._file_waiting(closed_main, closed_sub, latest)
            closed_main, closed_sub = [], {}
        self._locate(closed_main, closed_sub, latest)

    def _file_waiting(self, closed_main: List[Session], closed_sub: Dict[str, List[Session]],
                      latest: float) -> None:
        """File the house's sessions, each with its sub-meter partner's say
        in which signature it joins - see SUB_OVERRIDE. What is filed without
        a partner goes on to _locate, which places it as it always has."""
        self.unfiled += closed_main
        main_iv = max((st.interval for st in self.main.phases.values()), default=0.0)
        self._vote_phases(closed_sub, main_iv, self.unfiled + self.pending_main)
        for name, sessions in closed_sub.items():
            self.pending_sub.setdefault(name, []).extend(sessions)
        pairs = sorted(self._session_pairs(self.unfiled, main_iv), key=lambda x: (x[0], x[1]))
        taken_m, taken_s = set(), set()
        for _, mi, name, si in pairs:
            if mi in taken_m or (name, si) in taken_s:
                continue
            taken_m.add(mi)
            taken_s.add((name, si))
            self._file_as(self.unfiled[mi], name, self.pending_sub[name][si])
        for name in self.pending_sub:
            self.pending_sub[name] = [s for i, s in enumerate(self.pending_sub[name])
                                      if (name, i) not in taken_s]
        waiting, ready = [], []
        for i, m in enumerate(self.unfiled):
            if i in taken_m:
                continue
            (ready if self._heard_from_all(m, main_iv, latest) else waiting).append(m)
        self.unfiled = waiting
        by_energy = {}
        if SUB_METER_IDENTITY:
            for cost, mi, name, _ in sorted(self._energy_pairs(ready), key=lambda x: (x[0], x[1])):
                if mi not in by_energy and self._one_device(name):
                    by_energy[mi] = name
        for mi, m in enumerate(ready):
            name = by_energy.get(mi)
            if name is None:
                self.main._file(m)
                self.pending_main.append(m)          # placed later, as ever
                continue
            self.main._file(m, prefer=self._meter_home(name))
            sig = self.main.signature_of(m)
            if sig is not None:
                sig.locations[name] = sig.locations.get(name, 0) + 1

    def _one_device(self, name: str) -> bool:
        """Does this meter hold ONE device, by its own library's shape?"""
        det = self.subs.get(name)
        counts = [s.count for s in det.signatures] if det else []
        return bool(counts) and max(counts) >= SUB_DEVICE_SHARE * sum(counts)

    def _meter_home(self, name: str) -> Optional[int]:
        """The house signature most of this meter's sessions went to."""
        best = max(self.main.signatures, key=lambda s: s.locations.get(name, 0), default=None)
        return best.id if best is not None and best.locations.get(name, 0) else None

    def _heard_from_all(self, m: Session, main_iv: float, latest: float) -> bool:
        """Has every sub-meter that COULD have a session for ``m`` reported
        long enough past its end to have closed one? A meter is only waited
        for if it reads at least twice within the run - Home's workshop boiler
        meter reports every seven minutes and can partner no two-minute run -
        and never past MATCH_PATIENCE_S."""
        if latest - m.end >= MATCH_PATIENCE_S:
            return True
        for name, det in self.subs.items():
            iv = max((st.interval for st in det.phases.values()), default=0.0)
            if not iv or 2.0 * iv > m.duration_s:
                continue
            heard = max((st.last_ts or 0.0 for st in det.phases.values()), default=0.0)
            need = m.end + max(MERGE_TOLERANCE_S, main_iv + iv) + (SUSTAIN_INTERVALS + 1.0) * iv
            if heard < need:
                return False
        return True

    def _file_as(self, m: Session, name: str, s: Session) -> None:
        """File the house's session ``m`` as the sub-meter session ``s`` says."""
        det = self.subs.get(name)
        mp = {} if self.agnostic.get(name, False) else self.phase_map(name)
        own = _relabel(s, mp)
        if SUB_POWER and det is not None and own.samples >= m.samples > 0:
            quiet = max((st.noise for st in det.phases.values() if st.baseline is not None), default=None)
            loud = max((self.main.phases[p].noise for p in m.phases if p in self.main.phases), default=None)
            got, had = sum(own.power_by_phase().values()), sum(m.power_by_phase().values())
            if quiet is not None and loud is not None and quiet < loud and had > 0 and got > 0:
                k = got / had
                m.levels = {p: [(ts, w * k) for ts, w in lv] for p, lv in m.levels.items()}
        sub_sig = det.signature_of(s) if det is not None else None
        prefer = (self.identity.get(name) or {}).get(str(sub_sig.id)) if sub_sig is not None else None
        if not SUB_IDENTITY_CIRCUITS and not self._one_device(name):
            prefer = None
        self.main._file(m, prefer=prefer)
        sig = self.main.signature_of(m)
        if sig is not None:
            sig.locations[name] = sig.locations.get(name, 0) + 1
            if sub_sig is not None:
                self.identity.setdefault(name, {})[str(sub_sig.id)] = sig.id

    def _energy_pairs(self, mains: List[Session]) -> list:
        """(cost, main index, meter, None) for every house session whose energy
        a device meter's own readings account for."""
        pairs = []
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
                got = energy_between(rows, m.start, m.end)
                if got is None:
                    continue
                # What the device was drawing ANYWAY, over a window of the
                # same length just before. Without this a device that merely
                # happened to be running lands inside the band on coincidence:
                # a workshop meter already making 6 kW collects a heater's
                # 167 Wh without the heater being anywhere near it. The
                # detector matches edges everywhere else for the same reason.
                look = min(span, IDLE_WINDOW_S)
                before = energy_between(rows, m.start - look, m.start)
                if before is None:
                    continue
                rose = got - before * (span / look)
                if rose <= 0:
                    continue
                # Both integrals are sample-and-hold over a reading that can
                # only express whole quanta, so each carries about one
                # quantum-hour of error and their difference carries two. A
                # rise inside that is not evidence of anything.
                floor = (ENERGY_MIN_QUANTA * self.sub_quantum.get(name, 0.0)
                         * span / 3600.0)
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
        for mi, m in enumerate(mains):
            for name, subs in self.pending_sub.items():
                det = self.subs.get(name)
                sub_iv = max((st.interval for st in det.phases.values()), default=0.0) if det else 0.0
                # one full reporting interval each, since a step can land
                # anywhere inside one, and never less than the merge tolerance
                tol = max(MERGE_TOLERANCE_S, main_iv + sub_iv)
                agnostic = self.agnostic.get(name, False)
                # a three-phase meter's sessions under the HOUSE's phase names
                mp = {} if agnostic else self.phase_map(name)
                for si, s in enumerate(subs):
                    s = _relabel(s, mp)
                    if _same_load(m, s, agnostic, tol):
                        pairs.append((_match_cost(m, s, agnostic, tol), mi, name, si))
        return pairs

    def _vote_phases(self, closed_sub: Dict[str, List[Session]], main_iv: float,
                     pool: Optional[List[Session]] = None) -> None:
        """Each new single-channel session on a three-phase meter votes for
        the house phase whose single-phase session started with it at the
        same size - by size and moment only, never by label."""
        for name, sessions in closed_sub.items():
            if self.agnostic.get(name, False):
                continue
            det = self.subs.get(name)
            sub_iv = max((st.interval for st in det.phases.values()), default=0.0) if det else 0.0
            tol = max(MERGE_TOLERANCE_S, main_iv + sub_iv)
            votes = self.phase_votes.setdefault(name, {})
            for s in sessions:
                if len(s.levels) != 1:
                    continue
                (own,), w = s.levels.keys(), sum(s.power_by_phase().values())
                best = None
                for m in (self.pending_main if pool is None else pool):
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
        return phase_mapping(self.phase_votes.get(name) or {})

    def _locate(self, closed_main: List[Session], closed_sub: Dict[str, List[Session]], latest: float) -> None:
        self.pending_main += closed_main
        if closed_sub:
            self._vote_phases(closed_sub, max((st.interval for st in self.main.phases.values()), default=0.0))
        for name, sessions in closed_sub.items():
            self.pending_sub.setdefault(name, []).extend(sessions)
        # BEST fit, not first fit. Taking the first session that passed and
        # popping it is order-dependent, and at Kozolec it was the whole
        # reason a boiler with its own meter and 367 sightings collected
        # thirteen locations: a main session that merely fitted consumed the
        # downstream session a better-matching one needed, and loosening the
        # test made it worse rather than better (Anze, 2026-09-18). Every
        # passing pair is scored, the closest is taken first, and each
        # session is spent once.
        main_iv = max((st.interval for st in self.main.phases.values()), default=0.0)
        pairs = self._session_pairs(self.pending_main, main_iv)
        pairs += self._energy_pairs(self.pending_main)
        pairs.sort(key=lambda x: (x[0], x[1]))
        taken_main, taken_sub = set(), set()
        for _, mi, name, si in pairs:
            if mi in taken_main or (si is not None and (name, si) in taken_sub):
                continue
            sig = self.main.signature_of(self.pending_main[mi])
            if sig is None:
                continue
            taken_main.add(mi)
            if si is not None:
                taken_sub.add((name, si))
            sig.locations[name] = sig.locations.get(name, 0) + 1
        for name in self.pending_sub:
            self.pending_sub[name] = [s for i, s in enumerate(self.pending_sub[name])
                                      if (name, i) not in taken_sub]
        still = [m for i, m in enumerate(self.pending_main)
                 if i not in taken_main and latest - m.end < MATCH_PATIENCE_S]
        self.pending_main = still
        for name in list(self.pending_sub):
            # the same patience on both sides: _same_load already demands the
            # two starts be within tol_s of each other, so holding a session
            # longer only lets a slow meter's own session find the partner
            # that is still waiting for it - it cannot pair two unrelated ones
            self.pending_sub[name] = [s for s in self.pending_sub[name]
                                      if latest - s.end < MATCH_PATIENCE_S]

    def to_dict(self) -> dict:
        return {"main": self.main.to_dict(), "subs": {n: d.to_dict() for n, d in self.subs.items()},
                "pending_main": [s.to_dict() for s in self.pending_main],
                "pending_sub": {n: [s.to_dict() for s in v] for n, v in self.pending_sub.items()},
                "agnostic": self.agnostic, "phase_votes": self.phase_votes,
                "unfiled": [s.to_dict() for s in self.unfiled], "identity": self.identity}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Fleet":
        f = cls()
        if not d:
            return f
        f.main = Detector.from_dict(d.get("main"))
        f.subs = {n: Detector.from_dict(v) for n, v in (d.get("subs") or {}).items()}
        f.pending_main = [Session.from_dict(x) for x in d.get("pending_main") or []]
        f.pending_sub = {n: [Session.from_dict(x) for x in v] for n, v in (d.get("pending_sub") or {}).items()}
        f.agnostic = dict(d.get("agnostic") or {})
        f.phase_votes = {n: {p: dict(r) for p, r in v.items()}
                         for n, v in (d.get("phase_votes") or {}).items()}
        f.unfiled = [Session.from_dict(x) for x in d.get("unfiled") or []]
        f.identity = {n: dict(v) for n, v in (d.get("identity") or {}).items()}
        return f


def phase_mapping(votes: Dict[str, Dict[str, int]], phases: Sequence[str] = PHASES,
                  min_votes: Optional[int] = None) -> Dict[str, str]:
    """Which house phase each of a meter's own channels carries.

    ``votes[channel][house_phase]`` counts sessions that started on that
    channel and on that house phase at the same moment and the same size.
    The answer is a PERMUTATION - each channel its own phase - chosen to agree
    with the most sessions, not a separate vote per channel: a two-phase load
    such as Home's kiln steps on two house phases at once, and one channel on
    its own would tie between them. With fewer than ``min_votes`` sessions in
    all, a meter's own labels stand (identity), and a tie keeps them too.

    Pure and self-contained on purpose: Load Juggler has the same question to
    answer about a charger's phases (Anze, 2026-09-23)."""
    channels = sorted(votes)
    ident = {c: c for c in channels}
    if min_votes is None:
        min_votes = PHASE_MAP_MIN_VOTES
    if not channels or sum(sum(r.values()) for r in votes.values()) < min_votes:
        return ident
    pool = sorted(set(phases) | set(channels))
    best, best_score = ident, sum(votes[c].get(c, 0) for c in channels)
    for perm in itertools.permutations(pool, len(channels)):
        score = sum(votes[c].get(p, 0) for c, p in zip(channels, perm))
        if score > best_score:
            best, best_score = dict(zip(channels, perm)), score
    return best


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


def hour_histogram(counts: Sequence[int], rows: int = HISTOGRAM_ROWS,
                   width: int = HISTOGRAM_COL) -> List[str]:
    """The day as a block chart, for a form that renders markdown.

    A config flow cannot draw a graph. It can print one: 24 columns two
    characters wide, five rows tall, half-blocks for the halves - which says
    a good deal more than one line of sparkline did.
    """
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
