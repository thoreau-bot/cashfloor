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
from dataclasses import dataclass
from datetime import date, timedelta

from ..config import Config
from ..models import Frequency, ItemSource, ScheduledItem, Transaction
from .recurrence import advance_past

NOISE = re.compile(
    r"(\b\d{1,2}/\d{1,2}(/\d{2,4})?\b)|(\b\d{4,}\b)|(#\s*\d+)|(\bref\w*\b)|(\bid[:\s]*\w+)|"
    r"(\bpos\b)|(\bdebit\b)|(\bcredit\b)|(\bach\b)|(\bpayment\b)|(\bpmt\b)|(\bweb\b)|(\bonline\b)",
    re.I,
)


GENERIC_LEAD = re.compile(
    r"^\s*(ach (withdrawal|dep(osit)?|credit|debit)|debit card purchase|atm (withdrawal|rebate)|usaa (debit|credit)|"
    r"pos (purchase|debit)|online (transfer|payment)|recurring|electronic|withdrawal|deposit)\b[\s\d]*",
    re.I,
)


def normalize_payee(p: str) -> str:
    p = GENERIC_LEAD.sub("", p or "")
    s = NOISE.sub(" ", p.lower())
    s = re.sub(r"[^a-z&' ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return " ".join(s.split()[:3])  # first three words carry the identity


RECENT_N = 14  # classify on the most recent occurrences; ancient irregularities don't matter

CYCLES = [  # (frequency, min median gap, max median gap, day tolerance)
    (Frequency.WEEKLY, 6, 8, 1),
    (Frequency.BIWEEKLY, 13, 16, 2),
    (Frequency.MONTHLY, 27, 33, 4),
    (Frequency.QUARTERLY, 85, 95, 7),
    (Frequency.SEMIANNUAL, 175, 190, 10),
    (Frequency.YEARLY, 355, 375, 14),
]


def classify_interval(gaps: list[int], days: list[date], min_regular: float = 0.7) -> Frequency | None:
    """Frequency from the median gap, accepted when at least `min_regular` of
    the gaps fall within the cycle's tolerance of the median.  Robust to a
    skipped month or a doubled payment years ago, unlike a std-dev test."""
    if not gaps:
        return None
    med = statistics.median(gaps)
    for freq, lo, hi, tol in CYCLES:
        if lo <= med <= hi:
            regular = sum(abs(g - med) <= tol for g in gaps) / len(gaps)
            if regular < min_regular:
                return None
            if freq == Frequency.BIWEEKLY:
                doms = {d.day for d in days}
                if len(doms) <= 3 and all(any(abs(x - y) <= 1 for y in doms) for x in doms):
                    return Frequency.SEMIMONTHLY
            return freq
    return None


@dataclass
class Candidate:
    account_id: int
    pattern: str
    count: int
    frequency: Frequency | None
    last: date | None
    latest_amount: int
    verdict: str  # "proposed" or the rejection reason
    proposal: ScheduledItem | None = None


def analyse_recurring(cfg: Config, txs: list[Transaction], today: date | None = None) -> list[Candidate]:
    """Every candidate series with its verdict — what `detect_recurring` is
    built on, exposed so you can see why something was or wasn't proposed."""
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

    out: list[Candidate] = []
    for (account_id, pattern, _sign), g in groups.items():
        g.sort(key=lambda t: t.posted)
        dedup: list[Transaction] = []
        for t in g:
            if dedup and (t.posted - dedup[-1].posted).days < 3:
                continue
            dedup.append(t)
        cand = Candidate(account_id, pattern, len(dedup), None, dedup[-1].posted if dedup else None,
                         abs(dedup[-1].amount) if dedup else 0, "")
        out.append(cand)
        if len(dedup) < 2:
            cand.verdict = "only one occurrence"
            continue
        recent = dedup[-RECENT_N:]
        dates = [t.posted for t in recent]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:], strict=False)]
        freq = classify_interval(gaps, dates)
        cand.frequency = freq
        if freq is None:
            cand.verdict = f"irregular (median gap {statistics.median(gaps):.0f}d)"
            continue
        needed = 2 if freq in (Frequency.QUARTERLY, Frequency.SEMIANNUAL, Frequency.YEARLY) else rc.min_occurrences
        if len(dedup) < needed:
            cand.verdict = f"only {len(dedup)} occurrences, need {needed}"
            continue
        amounts = [abs(t.amount) for t in recent]
        latest_amt = amounts[-1]
        if latest_amt == 0:
            cand.verdict = "zero amount"
            continue
        check = amounts[-min(needed, len(amounts)):]
        if any(abs(a - latest_amt) / latest_amt > rc.amount_tolerance for a in check):
            cand.verdict = "amounts vary too much"
            continue
        last = dates[-1]
        med = statistics.median(gaps)
        if (today - last).days > 2 * med + 7:
            cand.verdict = f"stopped (last {last}, expected every {med:.0f}d)"
            continue
        regular = sum(abs(gp - med) <= 4 for gp in gaps) / len(gaps)
        confidence = round(min(0.5 + 0.05 * len(dedup), 0.9) * (0.6 + 0.4 * regular), 2)
        seed = ScheduledItem(
            id=None,
            name=_title(dedup[-1].payee or pattern),
            account_id=account_id,
            amount=int(latest_amt) * (1 if dedup[-1].amount > 0 else -1),
            frequency=freq,
            next_date=last,
            source=ItemSource.DETECTED,
            confidence=confidence,
            confirmed=False,
            payee_pattern=pattern,
            notes=f"detected from {len(dedup)} transactions since {dedup[0].posted}; last on {last}",
        )
        nd = advance_past(seed, today)
        if nd == last:
            nd = advance_past(seed, last + timedelta(days=1))
        seed.next_date = nd
        cand.proposal = seed
        cand.verdict = "proposed"
    return out


def detect_recurring(cfg: Config, txs: list[Transaction], today: date | None = None) -> list[ScheduledItem]:
    return [c.proposal for c in analyse_recurring(cfg, txs, today) if c.proposal is not None]


def _title(s: str) -> str:
    s = re.sub(r"\s+", " ", s.strip())
    return s[:48].title() if s.isupper() else s[:48]
