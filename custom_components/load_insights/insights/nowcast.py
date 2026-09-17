"""Device-state nowcasts. Pure.

A device's own state says what it will do NEXT: a cold tank means the boiler
is about to run, a low level means the pump is. That reading has no forward
source - nothing tells you the tank's temperature next Tuesday - so it can
never inform the week. What it can do is sharpen the next few hours of that
device's forecast, and it fades: the state now says a lot about the coming
hour and little about the sixth.

Fitted per LEAD: for h = 0..LEADS-1, the device's residual h hours later
(actual minus the forecast's expectation) is regressed, recency-weighted, on
the state's hourly mean now, centred on its historical mean. Sign learned -
temperature comes out negative for a boiler, level negative for a pump. The
same guard as every covariate, judged on the best lead - the effect is
delayed by nature, so the current hour often explains nothing. Applied, the live
state shifts each of the next LEADS hours by its coefficient, and the band's
half-width shrinks by the share of residual variance that lead explains.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

LEADS = 6                  # the current hour and the five after it
MIN_HOURS = 336
MIN_EXPLAINED = 0.03
MIN_RESIDUAL_MS = 1e-12    # below this the residual is floating point, not a device


@dataclass(frozen=True)
class Nowcast:
    coefficients: tuple = ()        # LEADS x kWh per unit of state, centred
    explained: tuple = ()           # LEADS x share of residual variance removed
    state_mean: float = 0.0
    hours: int = 0
    engaged: bool = False

    def deltas(self, current_state: Optional[float]) -> List[float]:
        if not self.engaged or current_state is None:
            return [0.0] * LEADS
        x = current_state - self.state_mean
        return [c * x for c in self.coefficients]

    def band_shrink(self, lead: int) -> float:
        """Factor for the band's half-width at this lead: sqrt(1 - explained)."""
        if not self.engaged or lead >= len(self.explained):
            return 1.0
        return max(0.0, 1.0 - self.explained[lead]) ** 0.5


NONE = Nowcast()


def fit_nowcast(rows: Sequence[Tuple[float, float, Sequence[Optional[float]]]]) -> Nowcast:
    """``rows`` are (weight, state_at_t, residuals) with residuals[h] the
    device's actual-minus-expected h hours after t, None where unknown."""
    rows = [(w, x, r) for w, x, r in rows if w > 0 and x is not None]
    if len(rows) < MIN_HOURS:
        return NONE
    W = sum(w for w, _, _ in rows)
    mean = sum(w * x for w, x, _ in rows) / W
    coeffs: List[float] = []
    expl: List[float] = []
    for h in range(LEADS):
        sub = [(w, x - mean, r[h]) for w, x, r in rows if h < len(r) and r[h] is not None]
        if len(sub) < MIN_HOURS:
            coeffs.append(0.0)
            expl.append(0.0)
            continue
        sxx = sum(w * x * x for w, x, _ in sub)
        sxr = sum(w * x * r for w, x, r in sub)
        srr = sum(w * r * r for w, _, r in sub)
        w_total = sum(w for w, _, _ in sub)
        if sxx <= 1e-12 or srr <= 0 or srr < MIN_RESIDUAL_MS * w_total:
            coeffs.append(0.0)
            expl.append(0.0)
            continue
        c = sxr / sxx
        ssr = sum(w * (r - c * x) ** 2 for w, x, r in sub)
        coeffs.append(c)
        expl.append(max(0.0, 1.0 - ssr / srr))
    # Judged on the BEST lead, not the first: a state's effect is delayed by
    # nature (the tank's temperature now decides the boiler's NEXT hour), so
    # lead 0 explaining nothing is the normal case, not a failed fit.
    if not coeffs or max(expl) < MIN_EXPLAINED:
        return NONE
    return Nowcast(coefficients=tuple(coeffs), explained=tuple(expl), state_mean=mean, hours=len(rows), engaged=True)


def build_rows(state_hist: Dict[float, float], residual_by_key: Dict[float, float],
               weight_by_key: Dict[float, float]) -> List[Tuple[float, float, List[Optional[float]]]]:
    """Line the state at hour t up with the residuals at t..t+LEADS-1."""
    rows = []
    for k, x in state_hist.items():
        if k not in weight_by_key:
            continue
        res = [residual_by_key.get(k + h * 3600.0) for h in range(LEADS)]
        if res[0] is None:
            continue
        rows.append((weight_by_key[k], x, res))
    return rows
