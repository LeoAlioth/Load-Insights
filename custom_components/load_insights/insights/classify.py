"""What KIND of thing a signature might be. Pure.

Electrical shape cannot name a device - a hair dryer and a fan heater are
the same two kilowatts - but it does say which FAMILY a load belongs to, and
that is usually enough for someone to recognise their own house in the list.
Three readings carry it:

  * the POWER FACTOR, much the strongest. A resistive element draws its
    current in step with the voltage and reads about 1.00; a motor lags,
    between 0.45 and 0.85; a small unswitched supply sits lower still; an
    appliance that controls its own power electronically - an induction hob,
    a heat pump, a modern washing machine - corrects itself back up near 1
    and steps its draw as it goes.
  * the LEVELS, because a modulating appliance steps its draw during a run
    and an element does not.
  * the SIZE and the DURATION, which separate a car charging from a kettle.

Nothing here is ever stated as a fact. Each guess carries the confidence its
own margin earns, and where the best two families are close it names both -
a two-kilowatt hour-long run genuinely is either a water heater or a car,
and saying so is more use than picking one.

Without a power factor almost nothing can be said, and the guess says that
rather than inventing something from size alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

HEATER = "a heating element"
MOTOR = "a motor"
# A balanced three-phase motor is a MEASUREMENT, not a guess about a house:
# the same power on all three legs at a motor's power factor is a thing
# nothing else in a home does. It earns its own name in the family layer
# rather than the weaker appliance one (Anze, 2026-09-18).
MOTOR_3P = "a three-phase motor"
SUPPLY = "electronics"
# Named for what someone would SEE, not for the electronics inside. It was
# "an inverter-driven appliance", which is jargon and, worse, collides with
# the solar inverters on their own configuration page - the same word for two
# unrelated things in one integration (Anze, 2026-09-18).
VARIABLE = "an appliance that varies its own power"
PROGRAMME = "an appliance running a programme"
CAR = "a car charging"

# One word each, for a place with no room for a sentence - a menu row
SHORT = {HEATER: "heater", MOTOR: "motor", MOTOR_3P: "3-phase motor", SUPPLY: "electronics",
         VARIABLE: "variable", PROGRAMME: "programme", CAR: "car"}

# How far a reading may wander inside one run before it is not holding a
# level any more, as a fraction of the level itself. A heating element is
# flat; a variable-speed drive glides by a third or more.
RIPPLE_STEADY = 0.12
RIPPLE_VARIES = 0.3
MIN_SCORE = 0.2            # below this the evidence says nothing
CLOSE = 0.15               # two families this close are both named
NO_PF_CAP = 0.4            # a guess made without a power factor is never confident
# Shape alone can never identify a device, so no guess is ever certain: a
# hair dryer and a fan heater are the same reading, and the ceiling says so.
MAX_CONFIDENCE = 0.85


@dataclass(frozen=True)
class Guess:
    kind: Optional[str]                    # the family, or None when nothing can be said
    confidence: float                      # 0 to 1
    because: Tuple[str, ...] = ()          # the readings it rests on
    alternative: Optional[str] = None      # a family scoring nearly as well
    appliance: Optional[str] = None        # a specific thing it might be - a question, not a claim
    appliance_confidence: float = 0.0

    @property
    def tag(self) -> str:
        """One word, or nothing. The appliance when there is one, since
        "dishwasher?" tells someone more about their own house than
        "an appliance running a programme" ever does."""
        if self.appliance:
            return APPLIANCE_SHORT.get(self.appliance, "") + "?"
        return SHORT.get(self.kind or "", "")

    @property
    def short(self) -> str:
        """Just the family, for a line that already carries the readings."""
        if not self.kind:
            return ""
        return f"maybe {self.kind}" if not self.alternative else f"maybe {self.kind} or {self.alternative}"

    @property
    def words(self) -> str:
        """For the naming page: 'maybe a motor (power factor 0.72, 900 W)'."""
        if not self.kind:
            return ""
        both = self.kind if not self.alternative else f"{self.kind} or {self.alternative}"
        if self.appliance:
            both = f"{both} - {self.appliance}?"
        return f"maybe {both} ({', '.join(self.because)})" if self.because else f"maybe {both}"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "confidence": round(self.confidence, 2),
                "alternative": self.alternative, "because": list(self.because),
                "appliance": self.appliance,
                "appliance_confidence": round(self.appliance_confidence, 2)}


NOTHING = Guess(None, 0.0, ("no power factor: give the meter its voltage and current, "
                            "or a power factor sensor",))


def _band(x: Optional[float], lo: float, plateau_lo: float,
          plateau_hi: float, hi: float) -> float:
    """1 inside the plateau, falling to 0 at the edges - a soft window, so a
    reading just outside a family's range weakens it instead of excluding it."""
    if x is None:
        return 0.0
    if x <= lo or x >= hi:
        return 0.0
    if x < plateau_lo:
        return (x - lo) / (plateau_lo - lo)
    if x > plateau_hi:
        return (hi - x) / (hi - plateau_hi)
    return 1.0


def _fmt_w(watts: float) -> str:
    return f"{watts:.0f} W" if watts < 1000 else f"{watts / 1000:.1f} kW"


def _fmt_s(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


# ---------------------------------------------------------------- appliances
# The families above are physics: a power factor of 0.6 IS a motor. What
# follows is a layer of guesses about a HOUSE, which is a weaker kind of
# claim - "runs at six in the evening so it is cooking" is a prior, not a
# measurement, and it will be wrong in ways the reading cannot show. So an
# appliance is only ever offered as a question, needs several things to agree
# at once, and never outranks the family it sits inside.
#
# Built against the two houses these run in rather than a catalogue (Anze,
# 2026-09-18): a workshop of three-phase woodworking machines, two dishwashers,
# an industrial washer with NO heaters, a heat-pump washer-dryer, hot water
# tanks, a single-hob electric stove and a double induction hob, well and
# pressure pumps, an EV charger, a kiln.
WORKSHOP = "a three-phase workshop machine"
PUMP = "a pump"
WATER_TANK = "a hot water tank"
COOKING = "cooking"
DISHWASHER = "a dishwasher"
WASHER = "a washing machine"
DRYER = "a tumble dryer"
FRIDGE = "a fridge or freezer"

APPLIANCE_SHORT = {WORKSHOP: "workshop", PUMP: "pump", WATER_TANK: "hot water",
                   COOKING: "cooking", DISHWASHER: "dishwasher", WASHER: "washing machine",
                   DRYER: "dryer", FRIDGE: "fridge"}

APPLIANCE_MIN = 0.35       # below this it is not worth asking
APPLIANCE_MARGIN = 0.1     # and it must be clearly ahead of the next one

# cooking happens at meal times; a workshop runs in working hours; a fridge
# runs at every hour there is
MEALS = (7, 8, 11, 12, 13, 17, 18, 19, 20)
WORKING = tuple(range(8, 20))


def _share(hour_wh: Optional[Sequence[float]], hours: Sequence[int]) -> Optional[float]:
    """What fraction of this load's energy falls in those hours."""
    if not hour_wh:
        return None
    total = sum(hour_wh)
    if total <= 0:
        return None
    return sum(hour_wh[h] for h in hours) / total


def _flatness(hour_wh: Optional[Sequence[float]]) -> Optional[float]:
    """1 when a load runs equally at every hour, 0 when it runs at one.

    A fridge is the flattest thing in a house and almost nothing else is."""
    if not hour_wh:
        return None
    total = sum(hour_wh)
    if total <= 0:
        return None
    busy = sum(1 for w in hour_wh if w > total / 96.0)     # a quarter of even
    return busy / 24.0


def _regularity(interval_s: Optional[float], interval_mad: Optional[float]) -> Optional[float]:
    """1 when a load repeats like clockwork, 0 when its spacing is random."""
    if not interval_s or interval_s <= 0 or interval_mad is None:
        return None
    return max(0.0, 1.0 - min(1.0, (interval_mad / interval_s) / 0.5))


def appliance(family: Optional[str], watts: float, pf: Optional[float], levels: float,
              duration_s: float, phases: str = "", interval_s: Optional[float] = None,
              interval_mad: Optional[float] = None,
              hour_wh: Optional[Sequence[float]] = None) -> Tuple[Optional[str], float]:
    """A specific appliance this might be, and how well it fits.

    Every profile requires its FAMILY first, so nothing here can turn a motor
    into a heating element - it only asks which motor."""
    balanced = len(set(phases)) >= 3
    s: Dict[str, float] = {}

    if family == MOTOR_3P:
        # The strongest signature in either house: almost nothing else in a
        # home is a balanced three-phase motor, and a workshop is full of them.
        s[WORKSHOP] = (_band(watts, 700, 1200, 7000, 12000)
                       * _band(duration_s, 2, 6, 600, 3600)
                       * (0.4 + 0.6 * (_share(hour_wh, WORKING) or 0.5)))
    # A pump comes in two electrical flavours, and both of Anze's houses have
    # one of each (2026-09-18). Straight to the line, it is an induction motor
    # and reads like one: the Metabo at home, about a kilowatt at a motor's
    # power factor. Behind a variable-speed drive it corrects its own factor
    # back to near unity and modulates to hold pressure: the Grundfos Scala2
    # at Kozolec, about 250 W. A power-factor window that fits the first
    # EXCLUDES the second, so size and burst length carry the variable case.
    if family in (MOTOR, MOTOR_3P):
        s[PUMP] = (_band(pf, 0.5, 0.6, 0.88, 0.94)
                   * _band(watts, 250, 450, 2200, 3500)
                   * _band(duration_s, 20, 45, 900, 2400)
                   * (0.7 if not balanced else 0.25))
    if family == VARIABLE:
        s[PUMP] = max(s.get(PUMP, 0.0),
                      _band(watts, 80, 140, 700, 1200)
                      * _band(duration_s, 20, 45, 600, 1800))
    if family == MOTOR:
        s[FRIDGE] = (_band(watts, 30, 60, 350, 700)
                     * _band(duration_s, 300, 600, 3600, 7200)
                     * (0.3 + 0.7 * (_flatness(hour_wh) or 0.3))
                     * (0.4 + 0.6 * (_regularity(interval_s, interval_mad) or 0.4)))
    if family == HEATER:
        s[WATER_TANK] = (_band(watts, 800, 1200, 4000, 6000)
                         * _band(duration_s, 900, 1800, 18000, 28800))
        s[COOKING] = (_band(watts, 700, 1000, 3500, 5000)
                      * _band(duration_s, 120, 240, 3600, 7200)
                      * (0.2 + 0.8 * (_share(hour_wh, MEALS) or 0.3)))
    if family == VARIABLE:
        s[COOKING] = max(s.get(COOKING, 0.0),
                         _band(watts, 800, 1200, 3700, 6000)
                         * _band(duration_s, 120, 300, 3600, 7200)
                         * (0.2 + 0.8 * (_share(hour_wh, MEALS) or 0.3)))
    if family == PROGRAMME or levels >= 2.5:
        # A dishwasher heats twice and runs long; a washer is shorter. The
        # industrial washer at home has NO heaters, so it leans on levels and
        # duration alone, which is why neither profile asks for a heat spike.
        s[DISHWASHER] = (_band(duration_s, 2700, 4500, 9000, 14400)
                         * _band(watts, 400, 700, 2500, 3500)
                         * _band(levels, 1.8, 2.5, 6, 9))
        s[WASHER] = (_band(duration_s, 900, 1800, 6000, 10800)
                     * _band(watts, 200, 350, 2500, 3500)
                     * _band(levels, 1.8, 2.5, 6, 9))
        s[DRYER] = (_band(duration_s, 1800, 2700, 10800, 18000)
                    * _band(watts, 300, 500, 2800, 4000)
                    * _band(levels, 1.2, 1.5, 4, 7))

    ranked = sorted(((v, k) for k, v in s.items() if v > 0), reverse=True)
    if not ranked or ranked[0][0] < APPLIANCE_MIN:
        return None, 0.0
    best, name = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else 0.0
    if best - second < APPLIANCE_MARGIN:
        return None, 0.0
    return name, round(min(MAX_CONFIDENCE, best), 2)


def classify(watts: float, pf: Optional[float] = None, levels: float = 1.0,
             duration_s: float = 0.0, phases: str = "",
             interval_s: Optional[float] = None, interval_mad: Optional[float] = None,
             hour_wh: Optional[Sequence[float]] = None,
             ripple: Optional[float] = None) -> Guess:
    """``watts`` is the load's total across its phases."""
    scores: Dict[str, float] = {}
    # A load either holds its level or it does not, and there are two ways to
    # not hold it: stepping between levels, which LEVELS counts, and gliding,
    # which only RIPPLE sees. A pressure pump behind a variable-speed drive
    # runs 104 to 247 W without ever taking a step, so it read as one flat
    # level and classified as a small heater (Anze, 2026-09-18).
    glides = ripple is not None and ripple >= RIPPLE_VARIES
    holds = ripple is None or ripple <= RIPPLE_STEADY
    steady = (1.0 if levels < 1.5 else 0.3) * (0.25 if glides else 1.0)
    stepped = 1.0 if (levels >= 1.5 or glides) else 0.25
    if pf is not None:
        scores[HEATER] = (_band(pf, 0.93, 0.97, 1.01, 1.01) * steady
                          * _band(watts, 80, 300, 9000, 12000)
                          * _band(duration_s, 0, 0, 3600, 14400))
        motor = _band(pf, 0.35, 0.55, 0.85, 0.93) * _band(watts, 20, 60, 4000, 7000)
        if len(set(phases)) >= 3:
            # all three legs, at a motor's power factor: a three-phase motor,
            # and the band runs higher because they are bigger machines
            scores[MOTOR_3P] = _band(pf, 0.35, 0.55, 0.88, 0.95) * _band(watts, 300, 700, 9000, 15000)
        else:
            scores[MOTOR] = motor
        scores[SUPPLY] = _band(pf, 0.2, 0.4, 0.75, 0.9) * _band(watts, 1, 5, 300, 600)
        scores[VARIABLE] = (_band(pf, 0.88, 0.94, 1.01, 1.01) * stepped
                            * _band(watts, 100, 300, 9000, 12000))
        scores[CAR] = (_band(pf, 0.93, 0.97, 1.01, 1.01) * steady
                       * _band(watts, 1200, 1400, 11500, 23000)
                       * _band(duration_s, 1800, 3600, 86400, 86400))
    # a programme steps through its stages whatever its factor, so this one
    # stands without a power factor at all
    if levels >= 2.5 and duration_s >= 900:
        scores[PROGRAMME] = min(1.0, (levels - 1.5) / 2.0)

    ranked = sorted(((v, k) for k, v in scores.items() if v > 0), reverse=True)
    if not ranked or ranked[0][0] < MIN_SCORE:
        return NOTHING if pf is None else Guess(None, 0.0, (f"power factor {pf:.2f}",))
    top, kind = ranked[0]
    second, runner_up = ranked[1] if len(ranked) > 1 else (0.0, None)
    margin = top - second
    alternative = runner_up if (margin < CLOSE and second >= MIN_SCORE) else None
    confidence = MAX_CONFIDENCE * top * (0.55 + 0.45 * min(1.0, margin / 0.3))
    if pf is None:
        confidence = min(confidence, NO_PF_CAP)

    because = []
    if pf is not None:
        because.append(f"power factor {pf:.2f}")
    because.append("one steady level" if (levels < 1.5 and holds) else
                   (f"varies by {ripple * 100:.0f}% as it runs" if glides and levels < 1.5
                    else f"{levels:.0f} levels"))
    because.append(_fmt_w(watts))
    if duration_s >= 1800:
        because.append(f"for {_fmt_s(duration_s)}")
    which, how_sure = appliance(kind, watts, pf, levels, duration_s, phases,
                                interval_s, interval_mad, hour_wh)
    return Guess(kind, round(confidence, 2), tuple(because), alternative,
                 appliance=which, appliance_confidence=how_sure)
