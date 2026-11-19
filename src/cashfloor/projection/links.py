"""Suggest which account a scheduled payment pays (card autopay -> the card).

Token overlap between the item's name/payee pattern and each account's
institution/name/nickname, with a stoplist of words every bank statement
shares.  Returns the best account or None; the user confirms in the UI.
"""

from __future__ import annotations

import re

from ..models import Account, AccountType, ScheduledItem

STOP = {"autopay", "auto", "pay", "payment", "pymt", "epay", "online", "card", "credit", "crd", "crcardpmt",
        "bank", "services", "service", "inc", "llc", "co", "the", "of", "and", "ach", "withdrawal", "debit",
        "na", "n.a.", "visa", "mastercard", "mc", "amex", "platinum", "signature", "rewards"}
PAYABLE = (AccountType.CREDIT, AccountType.LOAN, AccountType.UTILITY)


def _tokens(s: str | None) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9&']+", (s or "").lower()) if len(t) > 1 and t not in STOP}


def suggest_counter_account(item: ScheduledItem, accounts: list[Account]) -> Account | None:
    if item.amount >= 0:
        return None  # only outflows pay something
    words = _tokens(item.name) | _tokens(item.payee_pattern)
    if not words:
        return None
    best, best_score = None, 0
    for a in accounts:
        if not a.active or a.type not in PAYABLE or a.id == item.account_id:
            continue
        cand = _tokens(a.institution) | _tokens(a.name) | _tokens(a.nickname) | _tokens(a.key)
        score = len(words & cand)
        if score > best_score:
            best, best_score = a, score
    return best
