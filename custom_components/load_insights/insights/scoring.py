"""Forecast scoring: what did we predict, what happened, how far off. Pure.

Every refresh records, for a few LEAD times, the value the forecast holds
for the hour exactly that far ahead - so as the hours arrive, each completed
hour has a prediction made 1 h, 24 h and 168 h before it. When an hour's
actual becomes known the pair is settled into an error history, and the
metrics are computed over a trailing window: mean absolute error, bias (the
signed mean, whose sign says "consistently over" or "under"), and for the
day-ahead lead the COVERAGE of the p10-p90 band, which ought to be near 0.8
and is the one number that says whether the spread is honest.

A daily headline sits beside the hourly ones: the "tomorrow" total the
forecast showed at noon, against what the day then used.

Keys are epoch seconds of the period start (see series._key). The ledger is
plain data so it round-trips through JSON into Home Assistant's .storage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

# The week-ahead lead is the horizon's LAST hour: a 168-row horizon starting
# at the current hour ends 167 h ahead, and "168 h ahead" is never in it.
LEADS = {"hour_ahead": 1, "day_ahead": 24, "week_ahead": 167}
LEADS_H = tuple(LEADS.values())
BAND_LEAD_H = LEADS["day_ahead"]
ERROR_WINDOW_DAYS = 30
METRIC_WINDOW_DAYS = 7
DAY_WINDOW_DAYS = 60
PENDING_GRACE_H = 48        # a pending hour with no actual after this long is a gap, dropped
NOON = 12


def _key(t: datetime) -> float:
    return t.timestamp()


def floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


@dataclass
class Ledger:
    # lead -> {hour_key: predicted_kwh}
    pending: Dict[int, Dict[float, float]] = field(default_factory=lambda: {L: {} for L in LEADS_H})
    # hour_key -> (p10, p90) at the band lead
    pending_band: Dict[float, Tuple[float, float]] = field(default_factory=dict)
    # lead -> [(hour_key, actual, predicted)]
    errors: Dict[int, List[Tuple[float, float, float]]] = field(default_factory=lambda: {L: [] for L in LEADS_H})
    # hour_key -> (actual, p10, p90) for settled band-lead hours
    band_errors: List[Tuple[float, float, float, float]] = field(default_factory=list)
    # day (iso date) -> predicted kWh for that day, taken at noon the day before
    pending_day: Dict[str, float] = field(default_factory=dict)
    # [(iso date, actual_kwh, predicted_kwh)]
    day_errors: List[Tuple[str, float, float]] = field(default_factory=list)

    # ------------------------------------------------------------ record
    def record(self, now: datetime, hourly: Sequence[Tuple[datetime, float]],
               bands: Sequence[Tuple[float, float]], tomorrow_kwh: Optional[float]) -> None:
        """Note what the forecast says for the hours exactly LEADS_H ahead,
        and at noon what it says for tomorrow."""
        cur = floor_hour(now)
        by_key = {_key(t): (v, b) for (t, v), b in zip(hourly, bands)}
        for lead in LEADS_H:
            target = _key(cur) + lead * 3600.0
            if target in by_key:
                self.pending.setdefault(lead, {})[target] = by_key[target][0]
                if lead == BAND_LEAD_H:
                    self.pending_band[target] = tuple(by_key[target][1])
        if tomorrow_kwh is not None and now.hour == NOON:
            tomorrow = (cur + timedelta(days=1)).date().isoformat()
            self.pending_day.setdefault(tomorrow, tomorrow_kwh)   # the first noon reading wins

    # ------------------------------------------------------------ settle
    def settle(self, now: datetime, actual: Sequence[Tuple[datetime, float]]) -> None:
        """Pair every pending hour whose actual is known; drop the stale."""
        cur_k = _key(floor_hour(now))
        act = {_key(t): v for t, v in actual if v is not None}
        for lead in LEADS_H:
            pend = self.pending.setdefault(lead, {})
            for k in sorted(pend):
                if k >= cur_k:
                    break
                if k in act:
                    self.errors.setdefault(lead, []).append((k, act[k], pend.pop(k)))
                elif cur_k - k > PENDING_GRACE_H * 3600.0:
                    pend.pop(k)
        for k in sorted(self.pending_band):
            if k >= cur_k:
                break
            if k in act:
                lo, hi = self.pending_band.pop(k)
                self.band_errors.append((k, act[k], lo, hi))
            elif cur_k - k > PENDING_GRACE_H * 3600.0:
                self.pending_band.pop(k)
        # days: settled once the day has ENDED and its hours are in the
        # actuals. The count only tolerates a DST-short day (23 hours); it is
        # not the completeness test - that would score a day at 23:00 with an
        # hour still to run, which is exactly what it did once.
        by_day: Dict[str, List[float]] = {}
        for t, v in actual:
            if v is not None and _key(t) < cur_k:
                by_day.setdefault(t.date().isoformat(), []).append(v)
        today = floor_hour(now).date().isoformat()
        for day in sorted(self.pending_day):
            if day >= today:
                break                 # still running, or in the future
            hours = by_day.get(day, [])
            if len(hours) >= 23:
                self.day_errors.append((day, sum(hours), self.pending_day.pop(day)))
            elif day < (floor_hour(now) - timedelta(days=3)).date().isoformat():
                self.pending_day.pop(day)  # actuals never came
        self._trim(cur_k)

    def _trim(self, cur_k: float) -> None:
        horizon = cur_k - ERROR_WINDOW_DAYS * 86400.0
        for lead in LEADS_H:
            self.errors[lead] = [e for e in self.errors.get(lead, []) if e[0] >= horizon]
        self.band_errors = [e for e in self.band_errors if e[0] >= horizon]
        self.day_errors = self.day_errors[-DAY_WINDOW_DAYS:]

    # ------------------------------------------------------------ metrics
    def metrics(self, now: datetime, lead: int, window_days: int = METRIC_WINDOW_DAYS) -> dict:
        since = _key(floor_hour(now)) - window_days * 86400.0
        rows = [e for e in self.errors.get(lead, []) if e[0] >= since]
        if not rows:
            return {"n": 0, "mae_w": None, "bias_w": None}
        errs = [a - p for _, a, p in rows]
        return {
            "n": len(rows),
            "mae_w": 1000.0 * sum(abs(e) for e in errs) / len(errs),
            "bias_w": 1000.0 * sum(errs) / len(errs),
        }

    def coverage(self, now: datetime, window_days: int = METRIC_WINDOW_DAYS) -> Optional[float]:
        since = _key(floor_hour(now)) - window_days * 86400.0
        rows = [e for e in self.band_errors if e[0] >= since]
        if not rows:
            return None
        return sum(1 for _, a, lo, hi in rows if lo <= a <= hi) / len(rows)

    def last_day(self) -> Optional[Tuple[str, float, float]]:
        return self.day_errors[-1] if self.day_errors else None

    def recent(self, now: datetime, lead: int = BAND_LEAD_H, hours: int = 48) -> List[dict]:
        since = _key(floor_hour(now)) - hours * 3600.0
        return [{"hour_key": k, "actual": a, "predicted": p} for k, a, p in self.errors.get(lead, []) if k >= since]

    # ------------------------------------------------------------ storage
    def to_dict(self) -> dict:
        return {
            "pending": {str(L): {repr(k): v for k, v in d.items()} for L, d in self.pending.items()},
            "pending_band": {repr(k): list(b) for k, b in self.pending_band.items()},
            "errors": {str(L): [list(e) for e in rows] for L, rows in self.errors.items()},
            "band_errors": [list(e) for e in self.band_errors],
            "pending_day": dict(self.pending_day),
            "day_errors": [list(e) for e in self.day_errors],
        }

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Ledger":
        led = cls()
        if not d:
            return led
        for L, rows in (d.get("pending") or {}).items():
            led.pending[int(L)] = {float(k): float(v) for k, v in rows.items()}
        led.pending_band = {float(k): (float(b[0]), float(b[1])) for k, b in (d.get("pending_band") or {}).items()}
        for L, rows in (d.get("errors") or {}).items():
            led.errors[int(L)] = [(float(k), float(a), float(p)) for k, a, p in rows]
        led.band_errors = [(float(k), float(a), float(lo), float(hi)) for k, a, lo, hi in (d.get("band_errors") or [])]
        led.pending_day = {str(k): float(v) for k, v in (d.get("pending_day") or {}).items()}
        led.day_errors = [(str(x), float(a), float(p)) for x, a, p in (d.get("day_errors") or [])]
        for L in LEADS_H:
            led.pending.setdefault(L, {})
            led.errors.setdefault(L, [])
        return led
