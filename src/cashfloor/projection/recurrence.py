"""Turn a ScheduledItem's frequency into concrete dates inside a window."""

from __future__ import annotations

from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

from ..models import Frequency, ScheduledItem


def step(d: date, freq: Frequency, anchor: date) -> date:
    """Next occurrence strictly after `d`, keeping the anchor's day-of-month
    where that makes sense (so a bill on the 31st lands on the 28th/30th in
    short months rather than drifting)."""
    if freq == Frequency.ONCE:
        return date.max
    if freq == Frequency.WEEKLY:
        return d + timedelta(days=7)
    if freq == Frequency.BIWEEKLY:
        return d + timedelta(days=14)
    if freq == Frequency.SEMIMONTHLY:
        # Two payments a month: anchor day and anchor day + 15 (clamped), the
        # classic 1st/15th or 15th/30th pattern.
        a = anchor.day
        b = min(a + 15, 28) if a <= 15 else a - 15
        first, second = sorted((a, b))
        nxt_month = d + relativedelta(months=1)
        candidates = [
            _clamp(d.year, d.month, first),
            _clamp(d.year, d.month, second),
            _clamp(nxt_month.year, nxt_month.month, first),
            _clamp(nxt_month.year, nxt_month.month, second),
        ]
        return min(c for c in candidates if c > d)
    if freq == Frequency.MONTHLY:
        n = d + relativedelta(months=1)
        return _clamp(n.year, n.month, anchor.day)
    if freq == Frequency.QUARTERLY:
        n = d + relativedelta(months=3)
        return _clamp(n.year, n.month, anchor.day)
    if freq == Frequency.YEARLY:
        n = d + relativedelta(years=1)
        return _clamp(n.year, n.month, anchor.day)
    raise ValueError(freq)


def _clamp(y: int, m: int, day: int) -> date:
    last = (date(y, m, 1) + relativedelta(months=1) - timedelta(days=1)).day
    return date(y, m, min(day, last))


def apply_weekend_rule(d: date, rule: str) -> date:
    if rule == "previous":
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    elif rule == "next":
        while d.weekday() >= 5:
            d += timedelta(days=1)
    return d


def occurrences(item: ScheduledItem, start: date, end: date) -> list[date]:
    """All occurrence dates in [start, end], inclusive."""
    out: list[date] = []
    d = item.next_date
    anchor = item.next_date
    guard = 0
    while d <= end and guard < 1000:
        guard += 1
        if item.end_date and d > item.end_date:
            break
        eff = apply_weekend_rule(d, item.weekend_rule)
        if start <= eff <= end:
            out.append(eff)
        nxt = step(d, item.frequency, anchor)
        if nxt == date.max:
            break
        d = nxt
    return out


def advance_past(item: ScheduledItem, today: date) -> date:
    """Roll next_date forward so it is >= today (used after a sync, so items
    whose date has passed move to their next occurrence)."""
    d = item.next_date
    anchor = item.next_date
    guard = 0
    while d < today and guard < 1000:
        guard += 1
        nxt = step(d, item.frequency, anchor)
        if nxt == date.max:
            break
        d = nxt
    return d
