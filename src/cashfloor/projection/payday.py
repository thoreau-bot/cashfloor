"""Infer the day of month a card or loan is usually paid, from its own
transaction feed.  Payments show up on the card side as positive postings
("PAYMENT - THANK YOU", "AUTOPAY", "ONLINE PAYMENT"); the median of their
day-of-month over the last few months is a far better guess than the 1st
when the provider gives no due date.  A pay_day the user typed always wins."""

from __future__ import annotations

import re
import statistics
from datetime import date, timedelta

from ..models import LONG_TERM_TYPES, Account, AccountType, Transaction

PAYMENT_RE = re.compile(r"payment|pymt|autopay|auto pay|thank you|epay|e-pay|online pmt|mobile pmt|ach", re.I)
LOOKBACK_DAYS = 200
MIN_HITS = 2


def infer_payment(account: Account, txs: list[Transaction], today: date) -> tuple[int, int] | None:
    """(day of month, usual amount in cents) of the regular payment on a card
    or loan, from its own feed; None when there isn't enough history."""
    if account.type not in (AccountType.CREDIT, *LONG_TERM_TYPES):
        return None
    since = today - timedelta(days=LOOKBACK_DAYS)
    pays = [t for t in txs if t.account_id == account.id and t.amount > 0 and not t.pending and t.posted >= since
            and PAYMENT_RE.search(f"{t.payee} {t.memo}")]
    if len(pays) < MIN_HITS:
        return None
    # one hit per month at most: keep the largest payment in each month (the
    # regular one), so extra early payments don't drag the day around
    by_month: dict[tuple[int, int], Transaction] = {}
    for t in pays:
        k = (t.posted.year, t.posted.month)
        if k not in by_month or t.amount > by_month[k].amount:
            by_month[k] = t
    regular = list(by_month.values())
    if len(regular) < MIN_HITS:
        return None
    day = max(1, min(28, int(statistics.median(t.posted.day for t in regular))))
    amount = int(statistics.median(t.amount for t in regular[-6:]))
    return day, amount


def infer_pay_day(account: Account, txs: list[Transaction], today: date) -> int | None:
    r = infer_payment(account, txs, today)
    return r[0] if r else None


def infer_spend(account: Account, txs: list[Transaction], today: date) -> int | None:
    """Usual monthly charges on a card (cents): median of the monthly sums of
    its negative postings over the last six complete months with activity.
    This is what paying in full would cost each month."""
    if account.type != AccountType.CREDIT:
        return None
    since = today - timedelta(days=LOOKBACK_DAYS)
    this_month = (today.year, today.month)
    by_month: dict[tuple[int, int], int] = {}
    for t in txs:
        if t.account_id != account.id or t.amount >= 0 or t.pending or t.posted < since:
            continue
        k = (t.posted.year, t.posted.month)
        if k == this_month:
            continue  # incomplete
        by_month[k] = by_month.get(k, 0) + (-t.amount)
    sums = [v for _k, v in sorted(by_month.items())][-6:]
    if len(sums) < MIN_HITS:
        return None
    return int(statistics.median(sums))
