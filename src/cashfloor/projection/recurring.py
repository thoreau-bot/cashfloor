"""Detect recurring transactions (paychecks, rent, subscriptions, utilities)
from transaction history and propose ScheduledItems.

Heuristic, deliberately conservative: a series must have at least
`min_occurrences` hits, consistent sign, amounts within `amount_tolerance`
of the median, and a stable interval.  Proposals land as *unconfirmed*
items; nothing enters the projection until you confirm it.
"""

from __future__ import annotations

import re
import statistics
from collections import defaultdict
from datetime import date, timedelta

from ..config import Config
from ..models import Frequency, ItemSource, ScheduledItem, Transaction
from .recurrence import advance_past

NOISE = re.compile(
    r"(\b\d{1,2}/\d{1,2}(/\d{2,4})?\b)|(\b\d{4,}\b)|(#\s*\d+)|(\bref\w*\b)|(\bid[:\s]*\w+)|"
    r"(\bpos\b)|(\bdebit\b)|(\bcredit\b)|(\bach\b)|(\bpayment\b)|(\bpmt\b)|(\bweb\b)|(\bonline\b)",
    re.I,
)


def normalize_payee(p: str) -> str:
    s = NOISE.sub(" ", p.lower())
    s = re.sub(r"[^a-z&' ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return " ".join(s.split()[:3])  # first three words carry the identity


def classify_interval(gaps: list[int], days: list[date]) -> Frequency | None:
    if not gaps:
        return None
    med = statistics.median(gaps)
    spread = statistics.pstdev(gaps) if len(gaps) > 1 else 0
    if 6 <= med <= 8 and spread <= 1.5:
        return Frequency.WEEKLY
    if 13 <= med <= 16 and spread <= 2.5:
        # biweekly vs semimonthly: semimonthly hits ~two fixed days of month
        doms = {d.day for d in days}
        if len(doms) <= 3 and all(any(abs(x - y) <= 1 for y in doms) for x in doms):
            return Frequency.SEMIMONTHLY
        return Frequency.BIWEEKLY
    if 27 <= med <= 32 and spread <= 4:
        return Frequency.MONTHLY
    if 85 <= med <= 95 and spread <= 7:
        return Frequency.QUARTERLY
    if 355 <= med <= 375:
        return Frequency.YEARLY
    return None


def detect_recurring(cfg: Config, txs: list[Transaction], today: date | None = None) -> list[ScheduledItem]:
    today = today or date.today()
    rc = cfg.recurring
    groups: dict[tuple[int, str, int], list[Transaction]] = defaultdict(list)
    for t in txs:
        if t.pending or t.amount == 0:
            continue
        key = (t.account_id, normalize_payee(t.payee or t.memo), 1 if t.amount > 0 else -1)
        if not key[1]:
            continue
        groups[key].append(t)

    proposals: list[ScheduledItem] = []
    for (account_id, pattern, _sign), g in groups.items():
        if len(g) < rc.min_occurrences:
            continue
        g.sort(key=lambda t: t.posted)
        # de-dupe same-day duplicates (split charges)
        dedup: list[Transaction] = []
        for t in g:
            if dedup and (t.posted - dedup[-1].posted).days < 3:
                continue
            dedup.append(t)
        if len(dedup) < rc.min_occurrences:
            continue
        amounts = [abs(t.amount) for t in dedup]
        med_amt = statistics.median(amounts)
        if med_amt == 0:
            continue
        if any(abs(a - med_amt) / med_amt > rc.amount_tolerance for a in amounts[-rc.min_occurrences:]):
            continue
        dates = [t.posted for t in dedup]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:], strict=False)]
        freq = classify_interval(gaps, dates)
        if freq is None:
            continue
        last = dates[-1]
        if (today - last).days > 2 * max(gaps):
            continue  # series appears to have stopped
        regularity = 1.0 - min(statistics.pstdev(gaps) / max(statistics.median(gaps), 1), 1.0)
        confidence = round(min(0.5 + 0.1 * len(dedup), 0.9) * (0.6 + 0.4 * regularity), 2)
        seed = ScheduledItem(
            id=None,
            name=_title(dedup[-1].payee or pattern),
            account_id=account_id,
            amount=int(round(med_amt)) * (1 if dedup[-1].amount > 0 else -1),
            frequency=freq,
            next_date=last,
            source=ItemSource.DETECTED,
            confidence=confidence,
            confirmed=False,
            payee_pattern=pattern,
            notes=f"detected from {len(dedup)} transactions; last on {last}",
        )
        nd = advance_past(seed, today)
        if nd == last:  # last occurrence is today or later; next is one step on
            nd = advance_past(seed, last + timedelta(days=1))
        seed.next_date = nd
        proposals.append(seed)
    return proposals


def _title(s: str) -> str:
    s = re.sub(r"\s+", " ", s.strip())
    return s[:48].title() if s.isupper() else s[:48]
