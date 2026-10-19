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
same evidence rule as every covariate (covariates.day_block_t), judged on the
best lead with the critical value split over the LEADS leads - the effect is
delayed by nature, so the current hour often explains nothing. Applied, the live
state shifts each of the next LEADS hours by its coefficient, and the band's
half-width shrinks by the share of residual variance that lead explains.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from . import covariates as _cov

LEADS = 6                  # the current hour and the five after it
MIN_RESIDUAL_MS = 1e-12    # below this the residual is floating point, not a device


@dataclass(frozen=True)
class Nowcast:
    coefficients: tuple = ()        # LEADS x kWh per unit of state, centred
    explained: tuple = ()           # LEADS x share of residual variance removed
    state_mean: float = 0.0
    hours: int = 0
    engaged: bool = False
    # which of the states given were kept, each one's mean, and LEADS rows of
    # one coefficient per kept state (``coefficients`` / ``state_mean`` are
    # the first kept state's)
    used: tuple = ()
    means: tuple = ()
    matrix: tuple = ()
    t: tuple = ()                   # per kept state, its t at the lead where it is strongest
    days: int = 0                   # day blocks at that lead

    def deltas(self, current_state) -> List[float]:
        """``current_state`` is the live value of every state the fit was
        given, in the order given."""
        if not self.engaged or current_state is None:
            return [0.0] * LEADS
        live = [current_state[i] if i < len(current_state) else None for i in self.used]
        if any(v is None for v in live):
            return [0.0] * LEADS
        xs = [v - m for v, m in zip(live, self.means)]
        return [sum(c * x for c, x in zip(row, xs)) for row in self.matrix]

    def band_shrink(self, lead: int) -> float:
        """Factor for the band's half-width at this lead: sqrt(1 - explained)."""
        if not self.engaged or lead >= len(self.explained):
            return 1.0
        return max(0.0, 1.0 - self.explained[lead]) ** 0.5


NONE = Nowcast()


# How much of the diagonal is added before solving, so two states that say
# almost the same thing - a room's air and its floor - cannot trade huge
# opposite coefficients. A share of each state's own variance: dimensionless.
JOINT_RIDGE = 1e-3


def _fit_lead(rows, idx: Sequence[int], means: Sequence[float], h: int):
    """As ``_fit_lead_joint``, with every coefficient's day-clustered t:
    (coefficients, explained, t list, G), or None when singular or under
    MIN_DAYS day blocks. The variance is the ridged fit's own."""
    sub = [(int(k // 86400), w, [x[i] - means[j] for j, i in enumerate(idx)], r[h]) for w, x, r, k in rows
           if h < len(r) and r[h] is not None and all(x[i] is not None for i in idx)]
    n = len(idx)
    if not sub:
        return None
    a = [[sum(w * xs[p] * xs[q] for _, w, xs, _ in sub) for q in range(n)] for p in range(n)]
    b = [sum(w * xs[p] * r for _, w, xs, r in sub) for p in range(n)]
    srr = sum(w * r * r for _, w, _, r in sub)
    w_total = sum(w for _, w, _, _ in sub)
    if srr <= 0 or srr < MIN_RESIDUAL_MS * w_total:
        return None
    for p in range(n):
        a[p][p] *= 1.0 + JOINT_RIDGE
    inv = _cov.inverse(a)
    if inv is None:
        return None
    c = [sum(inv[p][q] * b[q] for q in range(n)) for p in range(n)]
    u_rows = [(d, w, xs, r - sum(ci * x for ci, x in zip(c, xs))) for d, w, xs, r in sub]
    ts, G = _cov.day_block_t(u_rows, c, inv)
    if G < _cov.MIN_DAYS:
        return None
    ssr = sum(w * u * u for _, w, _, u in u_rows)
    return c, max(0.0, 1.0 - ssr / srr), ts, G


def _fit_joint(rows) -> Nowcast:
    """Forward selection on evidence: the states are ordered by their
    strongest single-state |t| over the leads, and one joins if, fitted with
    those already in, its own coefficient's |t| at its best lead clears the
    critical t at ALPHA / LEADS (the best of LEADS leads: Bonferroni). A
    near-copy of a kept state has a huge joint SE and is not added."""
    n = len(rows[0][1])
    means = []
    for i in range(n):
        have = [(w, x[i]) for w, x, _, _ in rows if x[i] is not None]
        W = sum(w for w, _ in have)
        means.append(sum(w * v for w, v in have) / W if W else 0.0)

    def fits_for(idx):
        return [_fit_lead(rows, idx, [means[i] for i in idx], h) for h in range(LEADS)]

    def strongest(fits, pos):
        """(margin over critical, |t|, G) of state ``pos`` at the lead where it is strongest."""
        best = None
        for f in fits:
            if f is None:
                continue
            crit = _cov.t_crit(f[3] - 1, _cov.ALPHA / LEADS)
            cand = (abs(f[2][pos]) - crit, abs(f[2][pos]), f[3])
            if best is None or cand[0] > best[0]:
                best = cand
        return best

    singles = {}
    for i in range(n):
        b = strongest(fits_for([i]), 0)
        singles[i] = b[1] if b else 0.0
    kept: List[int] = []
    best_fits = None
    for i in sorted(range(n), key=lambda i: -singles[i]):
        fits = fits_for(kept + [i])
        b = strongest(fits, len(kept))
        if b is not None and b[0] >= 0:
            kept, best_fits = kept + [i], fits
    if not kept:
        return NONE
    expl = tuple(f[1] if f else 0.0 for f in best_fits)
    matrix = tuple(tuple(f[0]) if f else tuple(0.0 for _ in kept) for f in best_fits)
    stats = [strongest(best_fits, p) for p in range(len(kept))]
    return Nowcast(coefficients=tuple(row[0] for row in matrix), explained=expl,
                   state_mean=means[kept[0]], hours=len(rows), engaged=True,
                   used=tuple(kept), means=tuple(means[i] for i in kept), matrix=matrix,
                   t=tuple(s[1] for s in stats), days=max(s[2] for s in stats))


def fit_joint(rows: Sequence[Tuple[float, Sequence[Optional[float]], Sequence[Optional[float]], float]]) -> Nowcast:
    """A device's states, one or several: ``rows`` are (weight, states at t,
    residuals h hours after t, None where unknown, hour key), states in a fixed order."""
    rows = [(w, list(x), r, k) for w, x, r, k in rows if w > 0]
    return _fit_joint(rows) if rows else NONE


def build_rows_joint(state_hists: Sequence[Dict[float, float]], residual_by_key: Dict[float, float],
                     weight_by_key: Dict[float, float]) -> List[Tuple[float, List[Optional[float]], List[Optional[float]]]]:
    """Line each state's value at hour t (None where a state has none) up
    with the residuals at t..t+LEADS-1."""
    keys = set().union(*[set(h) for h in state_hists]) if state_hists else set()
    rows = []
    for k in keys:
        if k not in weight_by_key:
            continue
        res = [residual_by_key.get(k + h * 3600.0) for h in range(LEADS)]
        if res[0] is None:
            continue
        rows.append((weight_by_key[k], [h.get(k) for h in state_hists], res, k))
    return rows
