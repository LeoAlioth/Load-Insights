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
    # With SEVERAL states (fit_joint): which of them were kept, each one's
    # mean, and LEADS rows of one coefficient per kept state. Empty for one.
    used: tuple = ()
    means: tuple = ()
    matrix: tuple = ()

    def deltas(self, current_state) -> List[float]:
        """``current_state`` is the live value - or, for a joint fit, the live
        value of every state it was given, in the order given."""
        if not self.engaged or current_state is None:
            return [0.0] * LEADS
        if self.matrix:
            live = [current_state[i] if i < len(current_state) else None for i in self.used]
            if any(v is None for v in live):
                return [0.0] * LEADS
            xs = [v - m for v, m in zip(live, self.means)]
            return [sum(c * x for c, x in zip(row, xs)) for row in self.matrix]
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


def _solve(a: List[List[float]], b: List[float]) -> Optional[List[float]]:
    """Gaussian elimination with partial pivoting, for the few states a
    device has; None when the system is singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[piv][c]) <= 1e-12:
            return None
        m[c], m[piv] = m[piv], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


# How much of the diagonal is added before solving, so two states that say
# almost the same thing - a room's air and its floor - cannot trade huge
# opposite coefficients. A share of each state's own variance: dimensionless.
JOINT_RIDGE = 1e-3


def _fit_lead_joint(rows, idx: Sequence[int], means: Sequence[float], h: int):
    """Weighted least squares of the lead-h residual on the states ``idx``:
    (coefficients, explained), or None when too few rows or singular."""
    sub = [(w, [x[i] - means[k] for k, i in enumerate(idx)], r[h]) for w, x, r in rows
           if h < len(r) and r[h] is not None and all(x[i] is not None for i in idx)]
    if len(sub) < MIN_HOURS:
        return None
    k = len(idx)
    a = [[sum(w * xs[p] * xs[q] for w, xs, _ in sub) for q in range(k)] for p in range(k)]
    b = [sum(w * xs[p] * r for w, xs, r in sub) for p in range(k)]
    srr = sum(w * r * r for w, _, r in sub)
    w_total = sum(w for w, _, _ in sub)
    if srr <= 0 or srr < MIN_RESIDUAL_MS * w_total:
        return None
    for p in range(k):
        a[p][p] *= 1.0 + JOINT_RIDGE
    c = _solve(a, b)
    if c is None:
        return None
    ssr = sum(w * (r - sum(ci * x for ci, x in zip(c, xs))) ** 2 for w, xs, r in sub)
    return c, max(0.0, 1.0 - ssr / srr)


def fit_joint(rows: Sequence[Tuple[float, Sequence[Optional[float]], Sequence[Optional[float]]]]) -> Nowcast:
    """Several states for one device at once: ``rows`` are (weight, states at t,
    residuals), states in a fixed order.

    Forward, one at a time (Anze, 2026-09-28: a floor mat's thermostat reads
    the room AND the floor): the state that explains the most on its own goes
    in first, and each further one only if, fitted jointly with those already
    in, it adds MIN_EXPLAINED at the best lead. Two readings of one room tell
    much the same story, and taking both blindly is how a fit learns +5 on one
    and -5 on the other."""
    rows = [(w, list(x), r) for w, x, r in rows if w > 0]
    if not rows:
        return NONE
    n = len(rows[0][1])
    means = []
    for i in range(n):
        have = [(w, x[i]) for w, x, _ in rows if x[i] is not None]
        W = sum(w for w, _ in have)
        means.append(sum(w * v for w, v in have) / W if W else 0.0)

    def score(idx):
        fits = [_fit_lead_joint(rows, idx, [means[i] for i in idx], h) for h in range(LEADS)]
        expl = [f[1] if f else 0.0 for f in fits]
        return max(expl) if expl else 0.0, fits

    kept: List[int] = []
    best_gain, best_fits = 0.0, None
    order = sorted(range(n), key=lambda i: -score([i])[0])
    for i in order:
        gain, fits = score(kept + [i])
        if gain >= best_gain + MIN_EXPLAINED:
            kept, best_gain, best_fits = kept + [i], gain, fits
    if not kept or best_gain < MIN_EXPLAINED:
        return NONE
    matrix = tuple(tuple(f[0]) if f else tuple(0.0 for _ in kept) for f in best_fits)
    expl = tuple(f[1] if f else 0.0 for f in best_fits)
    return Nowcast(coefficients=tuple(row[0] for row in matrix), explained=expl,
                   state_mean=means[kept[0]], hours=len(rows), engaged=True,
                   used=tuple(kept), means=tuple(means[i] for i in kept), matrix=matrix)


def build_rows_joint(state_hists: Sequence[Dict[float, float]], residual_by_key: Dict[float, float],
                     weight_by_key: Dict[float, float]) -> List[Tuple[float, List[Optional[float]], List[Optional[float]]]]:
    """build_rows for several states: the hour's value of each (None where a
    state has none), lined up with the residuals at t..t+LEADS-1."""
    keys = set().union(*[set(h) for h in state_hists]) if state_hists else set()
    rows = []
    for k in keys:
        if k not in weight_by_key:
            continue
        res = [residual_by_key.get(k + h * 3600.0) for h in range(LEADS)]
        if res[0] is None:
            continue
        rows.append((weight_by_key[k], [h.get(k) for h in state_hists], res))
    return rows
