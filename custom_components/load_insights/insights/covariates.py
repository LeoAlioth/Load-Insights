"""Explanatory inputs fitted on the profile's residuals. Pure.

The profile explains the weekly habit; a covariate explains what is left.
Every covariate is fitted the same way - recency-weighted least squares of
the residual (actual minus slot mean) on the covariate's features - and kept
only if it passes the same guard: it must explain a meaningful share of the
residual variance, or it is reported as nothing and the profile stands alone.
That guard is what makes it safe to link an input "by default": one that
means nothing for consumption cannot make the forecast worse.

Features are CENTRED on their weighted historical mean. The slot means were
fitted under average conditions, so the response must describe the deviation
from average, not the whole effect - otherwise it would be counted twice.

Temperature is the first covariate: heating degree-hours below HEATING_BASE
and cooling degree-hours above COOLING_BASE, two coefficients, fixed bases -
no free parameter that twelve weeks could overfit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

HEATING_BASE_C = 15.0
COOLING_BASE_C = 24.0
MIN_HOURS = 336            # two weeks of hours with a covariate value, or no fit
MIN_EXPLAINED = 0.03       # share of residual variance the fit must remove
# A residual this small is floating point, not consumption: a mean squared
# residual of 1e-12 kWh^2 is a mean error under a microwatt-hour. Dividing one
# piece of dust by another produces a confident number from nothing - see
# calendars.MIN_RESIDUAL_SHARE for the case that found this.
MIN_RESIDUAL_MS = 1e-12


def hdh(temp_c: float) -> float:
    return max(0.0, HEATING_BASE_C - temp_c)


def cdh(temp_c: float) -> float:
    return max(0.0, temp_c - COOLING_BASE_C)


@dataclass(frozen=True)
class TemperatureResponse:
    heating_kwh_per_degh: float     # kWh per hour per degree below the heating base
    cooling_kwh_per_degh: float
    hdh_mean: float                 # the centring, weighted historical means
    cdh_mean: float
    explained: float                # share of residual variance removed (0 when not engaged)
    hours: int                      # hours the fit saw
    # Why it is not engaged, in words. Every rejection used to return the
    # same row of zeros, so a site whose forecast ignored the weather could
    # not tell "no overlapping history" from "the fit ran and explained
    # nothing" - which at a mid-September site are very different answers
    # (Anze, 2026-09-17).
    reason: str = ""

    @property
    def engaged(self) -> bool:
        return self.heating_kwh_per_degh > 0 or self.cooling_kwh_per_degh > 0

    def delta(self, temp_c: Optional[float]) -> float:
        """kWh to add to the slot value at this temperature; 0 when unknown."""
        if temp_c is None or not self.engaged:
            return 0.0
        return (self.heating_kwh_per_degh * (hdh(temp_c) - self.hdh_mean)
                + self.cooling_kwh_per_degh * (cdh(temp_c) - self.cdh_mean))


NONE = TemperatureResponse(0.0, 0.0, 0.0, 0.0, 0.0, 0, "not fitted")


def _no(reason: str, hours: int = 0, explained: float = 0.0) -> TemperatureResponse:
    """A response that does nothing, and says why."""
    return TemperatureResponse(0.0, 0.0, 0.0, 0.0, explained, hours, reason)


def fit_temperature_response(rows: Sequence[Tuple[float, float, float]]) -> TemperatureResponse:
    """``rows`` are (weight, residual_kwh, temp_c) per hour.

    Weighted least squares without intercept on the centred features. A
    negative coefficient (consumption FALLING as it gets colder) is not a
    heating response and is clipped to zero; if both are zero or the guard
    fails, the result does nothing and carries the reason in words.
    """
    rows = [(w, r, t) for w, r, t in rows if w > 0 and t is not None and r is not None]
    if len(rows) < MIN_HOURS:
        return _no(f"{len(rows)} hours have both a residual and a temperature, "
                   f"{MIN_HOURS} needed", hours=len(rows))
    W = sum(w for w, _, _ in rows)
    h_mean = sum(w * hdh(t) for w, _, t in rows) / W
    c_mean = sum(w * cdh(t) for w, _, t in rows) / W
    # normal equations for r ~ a*x + b*y, x,y centred
    sxx = sxy = syy = sxr = syr = srr = 0.0
    for w, r, t in rows:
        x = hdh(t) - h_mean
        y = cdh(t) - c_mean
        sxx += w * x * x
        syy += w * y * y
        sxy += w * x * y
        sxr += w * x * r
        syr += w * y * r
        srr += w * r * r
    if srr <= 0 or srr < MIN_RESIDUAL_MS * W:
        return _no("the residual is numerically nothing - the profile already "
                   "accounts for this series", hours=len(rows))
    det = sxx * syy - sxy * sxy
    if det > 1e-12:
        a = (sxr * syy - syr * sxy) / det
        b = (syr * sxx - sxr * sxy) / det
    else:                      # one feature never varies (a summer or winter window)
        a = sxr / sxx if sxx > 1e-12 else 0.0
        b = syr / syy if syy > 1e-12 else 0.0
    a = max(0.0, a)
    b = max(0.0, b)
    if a == 0.0 and b == 0.0:
        return _no("consumption does not rise with cold or with heat here",
                   hours=len(rows))
    # residual variance after the (clipped) fit
    ssr = 0.0
    for w, r, t in rows:
        e = r - a * (hdh(t) - h_mean) - b * (cdh(t) - c_mean)
        ssr += w * e * e
    explained = 1.0 - ssr / srr
    if explained < MIN_EXPLAINED:
        return _no(f"the fit explains {explained:.1%} of what the profile leaves over, "
                   f"{MIN_EXPLAINED:.0%} needed", hours=len(rows), explained=explained)
    return TemperatureResponse(a, b, h_mean, c_mean, explained, len(rows))


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
