"""Explanatory inputs fitted on the profile's residuals. Pure.

The profile explains the weekly habit; a covariate explains what is left.
Every covariate is fitted the same way - recency-weighted least squares of
the residual (actual minus what the profile expected WITHOUT that hour in it)
on the covariate's features - and kept only when the data says it is real:
the coefficient's t-value, with the variance clustered by DAY (``day_block_t``),
must clear Student's t at ALPHA. Hours inside a day are correlated in any way
they like, so a run of similar hours cannot fake evidence; a strong response
engages within MIN_DAYS days, a weak one waits for its data. Nothing else
gates: not a fixed span of time, not a share of the variance (it is still
computed and shown). That is what makes it safe to link an input "by default".

Features are CENTRED on the weighted historical mean of the same hour of day.
The slot means were fitted under the weather their own hours had, so the
response must describe the deviation from THAT, not the whole effect -
otherwise a cold 05:00 is counted twice, once as a night and once as cold.

Temperature is the first covariate: heating degree-hours below HEATING_BASE
and cooling degree-hours above COOLING_BASE, two coefficients, fixed bases -
no free parameter that twelve weeks could overfit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Dict, List, Optional, Sequence, Tuple

HEATING_BASE_C = 15.0
COOLING_BASE_C = 24.0
# The evidence rule (INS-15, swept in tests/forecast_bench.py).
ALPHA = 0.0005             # false-engagement rate per term per fit (upper tail, before Bonferroni)
MIN_DAYS = 4               # day blocks the test needs ...
MIN_DAY_ROWS = 12          # ... counting only days with this many rows
MIN_SPREAD_C = 1.0         # a temperature feature is fitted only if its weighted SD reaches this many degrees
# A residual this small is floating point, not consumption: a mean squared
# residual of 1e-12 kWh^2 is a mean error under a microwatt-hour. Dividing one
# piece of dust by another produces a confident number from nothing - see
# calendars.MIN_RESIDUAL_SHARE for the case that found this.
MIN_RESIDUAL_MS = 1e-12


def hdh(temp_c: float) -> float:
    return max(0.0, HEATING_BASE_C - temp_c)


def cdh(temp_c: float) -> float:
    return max(0.0, temp_c - COOLING_BASE_C)


def t_crit(df: float, p: float) -> float:
    """Student's t quantile with upper-tail probability ``p`` (Cornish-Fisher
    from the normal one: under 4 % off at df 3, under 0.5 % from df 8)."""
    z = NormalDist().inv_cdf(1.0 - p)
    return (z + (z ** 3 + z) / (4 * df) + (5 * z ** 5 + 16 * z ** 3 + 3 * z) / (96 * df ** 2)
            + (3 * z ** 7 + 19 * z ** 5 + 17 * z ** 3 - 15 * z) / (384 * df ** 3))


def inverse(a: Sequence[Sequence[float]]) -> Optional[List[List[float]]]:
    """Gauss-Jordan inverse of a small matrix; None when singular."""
    n = len(a)
    m = [list(row) + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[piv][c]) <= 1e-12:
            return None
        m[c], m[piv] = m[piv], m[c]
        d = m[c][c]
        m[c] = [x / d for x in m[c]]
        for r in range(n):
            if r != c and m[r][c] != 0.0:
                f = m[r][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [row[n:] for row in m]


def day_block_t(rows, beta: Sequence[float], a_inv) -> Tuple[List[float], int]:
    """t of every coefficient, its variance clustered by day (CR1) with a
    Newey-West lag of one day, the larger of the two. ``rows`` are
    (day, weight, x, u): u the fit's residual, x its k features; ``a_inv`` the
    inverse of sum(w x x'). -> (t list, G): G counts the days with at least
    MIN_DAY_ROWS rows; thinner days still enter the sums."""
    k = len(beta)
    blocks: Dict[int, List] = {}
    for day, w, x, u in rows:
        b = blocks.setdefault(day, [[0.0] * k, 0])
        for j in range(k):
            b[0][j] += w * x[j] * u
        b[1] += 1
    G = sum(1 for b in blocks.values() if b[1] >= MIN_DAY_ROWS)
    if G < 2:
        return [0.0] * k, G
    c = G / (G - 1.0)
    M = [[c * sum(b[0][p] * b[0][q] for b in blocks.values()) for q in range(k)] for p in range(k)]
    Mn = [row[:] for row in M]
    for d, b in blocks.items():
        nb = blocks.get(d + 1)
        if nb is not None:
            for p in range(k):
                for q in range(k):
                    Mn[p][q] += c * 0.5 * (b[0][p] * nb[0][q] + nb[0][p] * b[0][q])

    def diag(m):
        am = [[sum(a_inv[p][r] * m[r][q] for r in range(k)) for q in range(k)] for p in range(k)]
        return [sum(am[j][r] * a_inv[r][j] for r in range(k)) for j in range(k)]
    v1, v2 = diag(M), diag(Mn)
    out = []
    for j in range(k):
        var = max(v1[j], v2[j])
        out.append(beta[j] / var ** 0.5 if var > 0 else 0.0)
    return out, G


@dataclass(frozen=True)
class Centre:
    """Weighted sums of the heating and cooling degree-hours per hour of day
    and overall, over the hours that have a temperature. (Per weekday-and-hour
    slot it was worse: a slot holds a dozen weeks, and the dozen-sample mean of
    the weather adds noise a day-long cycle does not need.)"""
    hw: tuple = ()
    hwh: tuple = ()
    hwc: tuple = ()
    tw: float = 0.0
    twh: float = 0.0
    twc: float = 0.0

    @classmethod
    def build(cls, items) -> "Centre":
        """``items``: (hour of day, weight, hdh, cdh)."""
        hw, hwh, hwc = [0.0] * 24, [0.0] * 24, [0.0] * 24
        tw = twh = twc = 0.0
        for h, w, xh, xc in items:
            hw[h] += w
            hwh[h] += w * xh
            hwc[h] += w * xc
            tw += w
            twh += w * xh
            twc += w * xc
        return cls(tuple(hw), tuple(hwh), tuple(hwc), tw, twh, twc)

    def at(self, hour: int) -> Tuple[float, float]:
        """(heating, cooling) centre of this hour of day; overall when it has none."""
        if self.hw and self.hw[hour] > 0:
            return self.hwh[hour] / self.hw[hour], self.hwc[hour] / self.hw[hour]
        if self.tw > 0:
            return self.twh / self.tw, self.twc / self.tw
        return 0.0, 0.0

    def loo(self, hour: int, w: float, xh: float, xc: float) -> Tuple[float, float]:
        """The same with this hour's own weight and value taken out."""
        d = self.hw[hour] - w
        if d > 1e-9:
            return (self.hwh[hour] - w * xh) / d, (self.hwc[hour] - w * xc) / d
        d = self.tw - w
        if d > 1e-9:
            return (self.twh - w * xh) / d, (self.twc - w * xc) / d
        return 0.0, 0.0


@dataclass(frozen=True)
class TemperatureResponse:
    heating_kwh_per_degh: float     # kWh per hour per degree below the heating base
    cooling_kwh_per_degh: float
    explained: float                # share of residual variance removed (0 when not engaged); shown, not a gate
    hours: int                      # hours the fit saw
    # Why it is not engaged, in words. Every rejection used to return the
    # same row of zeros, so a site whose forecast ignored the weather could
    # not tell "no overlapping history" from "the fit ran and explained
    # nothing" - which at a mid-September site are very different answers
    # (2026-09-17).
    reason: str = ""
    t_heating: float = 0.0          # the t-value of each coefficient (what was tried, when not applied)
    t_cooling: float = 0.0
    t_needed: float = 0.0           # the critical t at ``days``
    days: int = 0                   # day blocks the test had
    centre: Centre = field(default_factory=Centre, repr=False)

    @property
    def engaged(self) -> bool:
        return self.heating_kwh_per_degh > 0 or self.cooling_kwh_per_degh > 0

    def delta(self, temp_c: Optional[float], hour: int = 0) -> float:
        """kWh to add to the slot value at this temperature, for a slot at
        this hour of day; 0 when the temperature is unknown."""
        if temp_c is None or not self.engaged:
            return 0.0
        ch, cc = self.centre.at(hour)
        return (self.heating_kwh_per_degh * (hdh(temp_c) - ch)
                + self.cooling_kwh_per_degh * (cdh(temp_c) - cc))


NONE = TemperatureResponse(0.0, 0.0, 0.0, 0, "not fitted")


def _no(reason: str, hours: int = 0, explained: float = 0.0, **kw) -> TemperatureResponse:
    """A response that does nothing, and says why."""
    return TemperatureResponse(0.0, 0.0, explained, hours, reason, **kw)


def _wls(rows, idx: Sequence[int]):
    """Weighted least squares of r on features ``idx`` of each row's x:
    (beta, A inverse), or None when singular. ``rows``: (day, w, r, x)."""
    k = len(idx)
    a = [[sum(w * x[idx[p]] * x[idx[q]] for _, w, _, x in rows) for q in range(k)] for p in range(k)]
    b = [sum(w * x[idx[p]] * r for _, w, r, x in rows) for p in range(k)]
    inv = inverse(a)
    if inv is None:
        return None
    return [sum(inv[p][q] * b[q] for q in range(k)) for p in range(k)], inv


def fit_temperature_response(rows: Sequence[Tuple[float, float, float, float, float]],
                             centre: Optional[Centre] = None) -> TemperatureResponse:
    """``rows`` are (key, weight, residual_kwh, x_heating, x_cooling): the
    features already centred (``Centre``), the residual against what the
    profile expected without that hour. A feature is fitted only if its
    weighted SD is MIN_SPREAD_C; a negative coefficient is not a heating
    response and is clipped; the coefficients that stay must pass the
    day-clustered t-test at ALPHA, refitted without the ones that did not, so
    the t reported belongs to the coefficient applied. Fewer than MIN_DAYS
    days, or none that passes, does nothing and says why."""
    centre = centre or Centre()
    data = [(int(k // 86400), w, r, (xh, xc)) for k, w, r, xh, xc in rows
            if w > 0 and r is not None and xh is not None and xc is not None]
    n = len(data)
    counts: Dict[int, int] = {}
    for d, *_ in data:
        counts[d] = counts.get(d, 0) + 1
    G = sum(1 for c in counts.values() if c >= MIN_DAY_ROWS)
    W = sum(w for _, w, _, _ in data)
    if G < MIN_DAYS or W <= 0:
        return _no(f"{G} days have both a residual and a temperature, {MIN_DAYS} needed", hours=n, days=G)
    sd = [(sum(w * x[j] * x[j] for _, w, _, x in data) / W) ** 0.5 for j in (0, 1)]
    active = [j for j in (0, 1) if sd[j] >= MIN_SPREAD_C]
    if not active:
        return _no(f"temperature varied by {max(sd):.1f} C over the fit, {MIN_SPREAD_C:.1f} needed", hours=n, days=G)
    srr = sum(w * r * r for _, w, r, _ in data)
    if srr <= 0 or srr < MIN_RESIDUAL_MS * W:
        return _no("the residual is numerically nothing - the profile already "
                   "accounts for this series", hours=n, days=G)
    crit = t_crit(G - 1, ALPHA)
    names = {0: "heating", 1: "cooling"}

    def test(idx):
        """-> (beta, t) per feature of ``idx``, or None."""
        fit = _wls(data, idx)
        if fit is None:
            return None
        beta, inv = fit
        u_rows = [(d, w, [x[j] for j in idx], r - sum(b * x[j] for b, j in zip(beta, idx)))
                  for d, w, r, x in data]
        ts, _ = day_block_t(u_rows, beta, inv)
        return beta, ts

    kept: Dict[int, Tuple[float, float]] = {}     # feature -> (beta, t) as applied
    tried: Dict[int, float] = {}
    res = test(active)
    if res is not None:
        good = [j for j, b, t in zip(active, *res) if b > 0 and t >= crit]
        for j, b, t in zip(active, *res):
            tried[j] = t if b > 0 else min(t, 0.0)
        if len(good) == len(active):
            kept = {j: (b, t) for j, b, t in zip(active, *res)}
        elif good:
            alone = test(good)
            if alone is not None and all(b > 0 and t >= crit for b, t in zip(*alone)):
                kept = {j: (b, t) for j, b, t in zip(good, *alone)}
    if not kept:
        best = None                                 # each feature alone, the strongest that passes
        for j in active:
            alone = test([j])
            if alone is None:
                continue
            tried[j] = alone[1][0] if alone[0][0] > 0 else min(alone[1][0], 0.0)
            if alone[0][0] > 0 and alone[1][0] >= crit and (best is None or alone[1][0] > best[1]):
                best = (j, alone[1][0], alone[0][0])
        if best is not None:
            kept = {best[0]: (best[2], best[1])}
    if not kept:
        j = max(tried, key=lambda x: tried[x]) if tried else active[0]
        t = tried.get(j, 0.0)
        if t <= 0:
            return _no("consumption does not rise with cold or with heat here", hours=n, days=G, t_needed=crit)
        return _no(f"{names[j]} t = {t:.1f}, {crit:.1f} needed at {G} days", hours=n, days=G,
                   t_heating=tried.get(0, 0.0), t_cooling=tried.get(1, 0.0), t_needed=crit)
    a = kept.get(0, (0.0, 0.0))
    b = kept.get(1, (0.0, 0.0))
    ssr = sum(w * (r - a[0] * x[0] - b[0] * x[1]) ** 2 for _, w, r, x in data)
    return TemperatureResponse(a[0], b[0], 1.0 - ssr / srr, n, "", a[1], b[1], crit, G, centre)


def interpolate_hourly(points: Sequence[Tuple[float, float]], hour_keys: Sequence[float]) -> Dict[float, float]:
    """Linear interpolation of (epoch_seconds, value) points onto the given
    hour keys; hours outside the points' span get nothing. Providers hand out
    hourly, 3-hourly or daily forecasts, and the profile wants every hour."""
    pts = sorted((k, v) for k, v in points if v is not None)
    out: Dict[float, float] = {}
    if not pts:
        return out
    i = 0
    for k in sorted(hour_keys):
        while i + 1 < len(pts) and pts[i + 1][0] <= k:
            i += 1
        if k < pts[0][0] or k > pts[-1][0]:
            continue
        if i + 1 >= len(pts) or pts[i][0] == k:
            out[k] = pts[i][1]
            continue
        (k0, v0), (k1, v1) = pts[i], pts[i + 1]
        out[k] = v0 + (v1 - v0) * (k - k0) / (k1 - k0) if k1 > k0 else v0
    return out
