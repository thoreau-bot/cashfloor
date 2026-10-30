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
import unicodedata
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
    p = unicodedata.normalize("NFKD", p or "")
    p = "".join(ch for ch in p if not unicodedata.combining(ch))  # CAFÉ -> CAFE
    p = GENERIC_LEAD.sub("", p)
    s = NOISE.sub(" ", p.lower())
    s = re.sub(r"[^a-z&' ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return " ".join(s.split()[:3])  # first three words carry the identity


RECENT_N = 14  # classify on the most recent occurrences; ancient irregularities don't matter

CYCLES = [  # (frequency, min median gap, max median gap, day tolerance)
    (Frequency.WEEKLY, 6, 8, 1),
    (Frequency.BIWEEKLY, 13, 16, 3),
    (Frequency.MONTHLY, 27, 33, 4),
    (Frequency.QUARTERLY, 85, 95, 7),
    (Frequency.SEMIANNUAL, 175, 190, 10),
    (Frequency.YEARLY, 355, 375, 14),
]


def classify_interval(gaps: list[int], days: list[date], min_regular: float = 0.6) -> Frequency | None:
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


P2P_RE = re.compile(r"^(zelle|paypal|venmo|cash app|cashapp|apple cash|stripe|square|sq \*|wise|revolut)\b", re.I)


def is_p2p(pattern: str) -> bool:
    return bool(P2P_RE.match(pattern or ""))


def monthly_aggregate(txs: list[Transaction], today: date) -> tuple[int, int, int, float] | None:
    """For series that don't fit a clean cadence: if most of the last 12
    months contain at least one payment, return (median monthly total in
    cents, typical day of month, active months, payments per month)."""
    if not txs or (today - txs[-1].posted).days > 45:
        return None
    start = (today.replace(day=1) - timedelta(days=365)).replace(day=1)
    buckets: dict[tuple[int, int], list[Transaction]] = defaultdict(list)
    for t in txs:
        if t.posted >= start:
            buckets[(t.posted.year, t.posted.month)].append(t)
    months = len(buckets)
    if months < 8:
        return None
    totals = [sum(abs(t.amount) for t in v) for v in buckets.values()]
    first_days = [min(t.posted.day for t in v) for v in buckets.values()]
    per_month = sum(len(v) for v in buckets.values()) / months
    return int(statistics.median(totals)), int(statistics.median(first_days)), months, per_month


def merge_truncated_names(groups: dict) -> dict:
    """Statements truncate descriptions ("ACME WIDGETS INC") while live feeds
    spell them out ("ACME WIDGETS INCORPORATED"): fold a series into another in the
    same account/sign whose key extends it."""
    keys = list(groups)
    alias: dict = {}
    for k in keys:
        acct, pat, sign = k
        if len(pat) < 8:
            continue
        longer = [o for o in keys if o != k and o[0] == acct and o[2] == sign
                  and o[1].startswith(pat) and len(o[1]) > len(pat)]
        if longer:
            alias[k] = max(longer, key=lambda o: len(groups[o]))  # fold into the biggest extension
    merged: dict = {}
    for k in keys:
        target = k
        seen = set()
        while target in alias and target not in seen:  # follow chains
            seen.add(target)
            target = alias[target]
        merged.setdefault(target, []).extend(groups[k])
    for v in merged.values():
        v.sort(key=lambda t: t.posted)
    return merged


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


def analyse_recurring(
    cfg: Config,
    txs: list[Transaction],
    today: date | None = None,
    aliases: dict[tuple[int, str], str] | None = None,
) -> list[Candidate]:
    """Every candidate series with its verdict — what `detect_recurring` is
    built on, exposed so you can see why something was or wasn't proposed.
    `aliases` maps (account_id, live-feed key) -> statement key, learned when
    a statement row de-duplicates against a live row."""
    today = today or date.today()
    rc = cfg.recurring
    aliases = aliases or {}
    groups: dict[tuple[int, str, int], list[Transaction]] = defaultdict(list)
    for t in txs:
        if t.pending or t.amount == 0:
            continue
        pat = normalize_payee(t.payee or t.memo)
        pat = aliases.get((t.account_id, pat), pat)
        key = (t.account_id, pat, 1 if t.amount > 0 else -1)
        if not key[1]:
            continue
        groups[key].append(t)

    groups = merge_truncated_names(groups)
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
            agg = monthly_aggregate(dedup, today)
            if agg is None:
                cand.verdict = f"irregular (median gap {statistics.median(gaps):.0f}d)"
                continue
            # Paid in several pieces most months (card payments, split rent):
            # treat as one monthly bill of the typical monthly total.
            amount, day, months, per_month = agg
            cand.frequency = Frequency.MONTHLY
            anchor = dedup[-1].posted.replace(day=min(day, 28))
            seed = ScheduledItem(
                id=None, name=_title(dedup[-1].payee or pattern), account_id=account_id,
                amount=amount * (1 if dedup[-1].amount > 0 else -1), frequency=Frequency.MONTHLY,
                next_date=anchor, source=ItemSource.DETECTED, confidence=round(0.5 + 0.02 * months, 2),
                confirmed=False, payee_pattern=pattern,
                notes=(f"monthly total across {months} of the last 12 months (~{per_month:.1f} payments/month); "
                       f"variable amount; median monthly total used"),
            )
            nd = advance_past(seed, today)
            seed.next_date = nd if nd > today else advance_past(seed, today + timedelta(days=1))
            cand.proposal = seed
            cand.verdict = "proposed (monthly total)"
            continue
        needed = 2 if freq in (Frequency.QUARTERLY, Frequency.SEMIANNUAL, Frequency.YEARLY) else rc.min_occurrences
        if len(dedup) < needed:
            cand.verdict = f"only {len(dedup)} occurrences, need {needed}"
            continue
        amounts = [abs(t.amount) for t in recent]
        latest_amt = amounts[-1]
        check = amounts[-min(needed, len(amounts)):]
        variable = any(abs(a - latest_amt) / max(latest_amt, 1) > rc.amount_tolerance for a in check)
        if variable:
            # Stable cadence, moving amount (rent with fees, insurance, loan autopay,
            # card autopay): still a bill.  Use the median of the last six.
            use_amt = int(statistics.median(amounts[-6:]))
            note = f"variable amount; median of last {min(6, len(amounts))} used"
        else:
            use_amt = int(latest_amt)
            note = ""
        if use_amt < 100:  # under $1: interest pennies, rounding — not a bill
            cand.verdict = "negligible amount"
            continue
        last = dates[-1]
        med = statistics.median(gaps)
        if (today - last).days > 2 * med + 7:
            cand.verdict = f"stopped (last {last}, expected every {med:.0f}d)"
            continue
        regular = sum(abs(gp - med) <= 4 for gp in gaps) / len(gaps)
        confidence = round(min(0.5 + 0.05 * len(dedup), 0.9) * (0.6 + 0.4 * regular) * (0.85 if variable else 1.0), 2)
        seed = ScheduledItem(
            id=None,
            name=_title(dedup[-1].payee or pattern),
            account_id=account_id,
            amount=use_amt * (1 if dedup[-1].amount > 0 else -1),
            frequency=freq,
            next_date=last,
            source=ItemSource.DETECTED,
            confidence=confidence,
            confirmed=False,
            payee_pattern=pattern,
            notes=(f"detected from {len(dedup)} transactions since {dedup[0].posted}; last on {last}"
                   + (f"; {note}" if note else "")),
        )
        nd = advance_past(seed, today)
        if nd == last:
            nd = advance_past(seed, last + timedelta(days=1))
        seed.next_date = nd
        cand.proposal = seed
        cand.verdict = "proposed (variable amount)" if variable else "proposed"
    return out


def detect_recurring(
    cfg: Config, txs: list[Transaction], today: date | None = None, aliases: dict[tuple[int, str], str] | None = None
) -> list[ScheduledItem]:
    return [c.proposal for c in analyse_recurring(cfg, txs, today, aliases) if c.proposal is not None]


def is_proposed(verdict: str) -> bool:
    return verdict.startswith("proposed")


def _title(s: str) -> str:
    s = re.sub(r"\s+", " ", s.strip())
    return s[:48].title() if s.isupper() else s[:48]
