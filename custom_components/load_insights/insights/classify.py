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
from typing import Dict, Optional, Tuple

HEATER = "a heating element"
MOTOR = "a motor"
SUPPLY = "electronics"
# Named for what someone would SEE, not for the electronics inside. It was
# "an inverter-driven appliance", which is jargon and, worse, collides with
# the solar inverters on their own configuration page - the same word for two
# unrelated things in one integration (Anze, 2026-09-18).
VARIABLE = "an appliance that varies its own power"
PROGRAMME = "an appliance running a programme"
CAR = "a car charging"

# One word each, for a place with no room for a sentence - a menu row
SHORT = {HEATER: "heater", MOTOR: "motor", SUPPLY: "electronics",
         VARIABLE: "variable", PROGRAMME: "programme", CAR: "car"}

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

    @property
    def tag(self) -> str:
        """One word, or nothing."""
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
        return f"maybe {both} ({', '.join(self.because)})" if self.because else f"maybe {both}"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "confidence": round(self.confidence, 2),
                "alternative": self.alternative, "because": list(self.because)}


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


def classify(watts: float, pf: Optional[float] = None, levels: float = 1.0,
             duration_s: float = 0.0) -> Guess:
    """``watts`` is the load's total across its phases."""
    scores: Dict[str, float] = {}
    steady = 1.0 if levels < 1.5 else 0.3
    stepped = 1.0 if levels >= 1.5 else 0.25
    if pf is not None:
        scores[HEATER] = (_band(pf, 0.93, 0.97, 1.01, 1.01) * steady
                          * _band(watts, 80, 300, 9000, 12000)
                          * _band(duration_s, 0, 0, 3600, 14400))
        scores[MOTOR] = _band(pf, 0.35, 0.55, 0.85, 0.93) * _band(watts, 20, 60, 4000, 7000)
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
    because.append("one level" if levels < 1.5 else f"{levels:.0f} levels")
    because.append(_fmt_w(watts))
    if duration_s >= 1800:
        because.append(f"for {_fmt_s(duration_s)}")
    return Guess(kind, round(confidence, 2), tuple(because), alternative)
