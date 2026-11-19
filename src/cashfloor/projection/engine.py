"""Daily balance projection over the cash pool.

Definitions
-----------
cash pool      The set of accounts whose money you can actually spend —
               normally checking + savings.  Flag with `in_pool` per account
               or by type via projection.pool_account_types.
floor          The minimum end-of-day pool balance across the horizon.
discretionary  floor - safety_buffer.  This is the answer to "how much extra
               money do I have?": the most you could spend *today* without
               the pool ever dipping below the buffer in the next N days,
               assuming every known inflow/outflow happens as scheduled.

What moves the balance
----------------------
1. Scheduled items (manual, confirmed-detected, transfers).
2. Credit-card / loan / utility due dates: statement balance (or minimum,
   per config) leaves the default payment account on the due date, and —
   if `estimate_future_card_payments` — repeats monthly at the same amount
   for the rest of the horizon, marked "(est.)".
3. Transfers between two pool accounts net to zero and are ignored.

Unconfirmed detected items are *not* included; their count is reported so
the UI can nag you to confirm or dismiss them.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

from ..config import Config
from ..db import Database
from ..models import (
    Account,
    AccountType,
    ProjectionDay,
    ProjectionResult,
    ScheduledItem,
    Transaction,
    to_cents,
)
from .recurrence import occurrences
from .recurring import normalize_payee

DUE_TYPES = (AccountType.CREDIT, AccountType.LOAN, AccountType.UTILITY)


def pool_accounts(cfg: Config, accounts: list[Account]) -> list[Account]:
    flagged = [a for a in accounts if a.in_pool and a.active]
    if flagged:
        return flagged
    return [a for a in accounts if a.active and a.type.value in cfg.projection.pool_account_types]


def _event(events: dict[date, list[tuple[int, str]]], d: date, amount: int, label: str) -> None:
    events[d].append((amount, label))


POSTED_LOOKAHEAD = 3  # an occurrence due within this many days may already have posted
POSTED_LOOKBACK = 4  # ...and the posting may sit this many days before the due date (early payday)


def already_posted(
    cfg: Config,
    it: ScheduledItem,
    occ: date,
    as_of: date,
    recent: list[Transaction],
    aliases: dict[tuple[int, str], str] | None = None,
    pending_in_balance: set[int] | None = None,
) -> bool:
    """True when a transaction matching this item's next occurrence has already
    hit the account: the balance includes it, so the projection must not add it
    again.  Checked only for the first few days of the horizon — today's payroll
    that posted this morning, a bill drafted a day early.

    A posting counts when it is on the item's account, in the same direction,
    within POSTED_LOOKBACK days before the due date (up to as_of), and either
    (a) its payee normalises to the item's pattern — via the learned statement
    alias, since detected items carry the statement wording — or (b) its amount
    is within $1 of the item's: an exact-amount hit a day or two from the due
    date is the same payment whatever the bank called it.  Otherwise the amount
    must be within recurring.amount_tolerance *and* the payee must match.
    Pending postings count only for accounts whose balance already includes
    them (available balance in use)."""
    if (occ - as_of).days > POSTED_LOOKAHEAD:
        return False
    aliases = aliases or {}
    pending_ok = pending_in_balance or set()
    tol = cfg.recurring.amount_tolerance
    pat = it.payee_pattern
    for t in recent:
        if t.account_id != it.account_id or (t.pending and t.account_id not in pending_ok):
            continue
        if not (occ - timedelta(days=POSTED_LOOKBACK) <= t.posted <= as_of):
            continue
        if (t.amount > 0) != (it.amount > 0):
            continue
        diff = abs(abs(t.amount) - abs(it.amount))
        if diff <= 100:
            return True
        if diff > tol * abs(it.amount):
            continue
        key = normalize_payee(t.payee or t.memo)
        key = aliases.get((t.account_id, key), key)
        if pat and key != pat and not key.startswith(pat) and not pat.startswith(key):
            continue
        return True
    return False


def build_events(
    cfg: Config,
    accounts: list[Account],
    items: list[ScheduledItem],
    as_of: date,
    horizon_end: date,
    recent_txs: list[Transaction] | None = None,
    aliases: dict[tuple[int, str], str] | None = None,
) -> tuple[dict[date, list[tuple[int, str]]], int]:
    """Return {date: [(amount_cents, label), ...]} and count of unconfirmed items.
    `recent_txs` (the last week or so of postings) lets an occurrence that has
    already hit the account be skipped instead of counted twice."""
    recent = recent_txs or []
    pool = pool_accounts(cfg, accounts)
    pending_in_balance = (
        {a.id for a in pool if a.available is not None} if cfg.projection.use_available_balance else set()
    )
    pool_ids = {a.id for a in pool}
    events: dict[date, list[tuple[int, str]]] = defaultdict(list)
    unconfirmed = 0
    # accounts that already have an explicit payment item -> skip auto due events
    covered_counter_ids: set[int] = set()

    for it in items:
        if not it.active:
            continue
        if not it.confirmed:
            unconfirmed += 1
            continue
        src_in = it.account_id in pool_ids
        ctr_in = it.counter_account_id in pool_ids if it.counter_account_id else False
        if it.counter_account_id:
            covered_counter_ids.add(it.counter_account_id)
        if src_in and ctr_in:
            continue  # internal transfer; pool total unchanged
        if not src_in and not ctr_in:
            continue  # nothing to do with the pool
        sign = 1 if src_in else -1  # money leaving `account_id` enters the counter account
        for d in occurrences(it, as_of, horizon_end):
            if recent and already_posted(cfg, it, d, as_of, recent, aliases, pending_in_balance):
                continue  # already in the balance
            _event(events, d, sign * it.amount, it.name)

    pay_from = (
        next((a for a in pool if a.key == cfg.projection.default_payment_account), None)
        or (pool[0] if pool else None)
    )
    if pay_from is not None:
        for a in accounts:
            if a.type not in DUE_TYPES or not a.active or a.id in covered_counter_ids:
                continue
            if a.due_date is None:
                # No due date known: assume the card is paid in full each month
                # (conservative) unless the user turned that off.
                if a.type != AccountType.CREDIT or not cfg.projection.assume_card_payoff:
                    continue
                owed = -a.balance if a.balance < 0 else 0
                if owed <= 0:
                    continue
                day = a.pay_day or cfg.projection.default_card_pay_day
                d = as_of.replace(day=min(day, 28))
                if d < as_of:
                    d = d + relativedelta(months=1)
                guard = 0
                while d <= horizon_end and guard < 24:
                    guard += 1
                    _event(events, d, -owed, f"{a.display_name} payment (assumed payoff)")
                    if not cfg.projection.estimate_future_card_payments:
                        break
                    d = d + relativedelta(months=1)
                continue
            amt = a.minimum_payment if cfg.projection.card_payment_mode == "minimum" else a.statement_balance
            if amt is None:
                amt = a.minimum_payment
            if not amt or amt <= 0:
                continue
            d = a.due_date
            first = True
            guard = 0
            while d <= horizon_end and guard < 24:
                guard += 1
                if d >= as_of:
                    label = f"{a.display_name} payment" + ("" if first else " (est.)")
                    _event(events, d, -amt, label)
                if not cfg.projection.estimate_future_card_payments:
                    break
                first = False
                d = d + relativedelta(months=1)
    return events, unconfirmed


def fsa_year_end(as_of: date) -> date:
    return date(as_of.year, 12, 31)


def fsa_warnings(cfg: Config, accounts: list[Account], as_of: date) -> list[str]:
    """Use-it-or-lose-it: an FSA forfeits whatever is left at plan-year end.
    Warn once inside `fsa_warn_days` of Dec 31 while it still holds money."""
    out = []
    end = fsa_year_end(as_of)
    left = (end - as_of).days
    for a in accounts:
        if a.type == AccountType.FSA and a.active and a.balance > 0 and left <= cfg.projection.fsa_warn_days:
            out.append(f"{a.display_name}: {a.balance / 100:,.2f} in FSA funds expire {end} ({left} days) — spend it")
    return out


def project(db: Database, cfg: Config, as_of: date | None = None) -> ProjectionResult:
    as_of = as_of or date.today()
    horizon = cfg.projection.horizon_days
    horizon_end = as_of + timedelta(days=horizon)
    accounts = db.list_accounts()
    items = db.list_items()
    pool = pool_accounts(cfg, accounts)
    starting = sum((a.spendable if cfg.projection.use_available_balance else a.balance) for a in pool)
    recent = db.list_transactions(since=as_of - timedelta(days=POSTED_LOOKBACK + POSTED_LOOKAHEAD))
    events, unconfirmed = build_events(cfg, accounts, items, as_of, horizon_end, recent, db.get_payee_aliases())
    warnings: list[str] = []
    for a in pool:
        if a.sync_error:
            warnings.append(f"{a.display_name}: {a.sync_error}")
        elif a.balance_as_of and (as_of - a.balance_as_of.date()).days > cfg.projection.stale_after_days:
            warnings.append(f"{a.display_name}: balance is {(as_of - a.balance_as_of.date()).days} days old")
    warnings += fsa_warnings(cfg, accounts, as_of)

    days: list[ProjectionDay] = []
    bal = starting
    floor, floor_date = starting, as_of
    d = as_of
    while d <= horizon_end:
        todays = events.get(d, [])
        inflow = sum(a for a, _ in todays if a > 0)
        outflow = sum(a for a, _ in todays if a < 0)
        bal += inflow + outflow
        days.append(ProjectionDay(day=d, balance=bal, inflows=inflow, outflows=outflow,
                                  events=[f"{lbl}: {amt/100:+,.2f}" for amt, lbl in todays]))
        if bal < floor:
            floor, floor_date = bal, d
        d += timedelta(days=1)

    buffer = to_cents(cfg.projection.safety_buffer)
    return ProjectionResult(
        as_of=as_of,
        horizon_days=horizon,
        starting_balance=starting,
        days=days,
        floor=floor,
        floor_date=floor_date,
        safety_buffer=buffer,
        discretionary=floor - buffer,
        unconfirmed_count=unconfirmed,
        warnings=warnings,
    )


def explain(result: ProjectionResult) -> str:
    lines = [
        f"As of {result.as_of}: pool balance {result.starting_balance/100:,.2f}",
        f"Cash floor (lowest balance in next {result.horizon_days} days): "
        f"{result.floor/100:,.2f} on {result.floor_date}",
        f"Safety buffer: {result.safety_buffer/100:,.2f}",
        f"Discretionary now: {result.discretionary/100:,.2f}",
    ]
    if result.unconfirmed_count:
        lines.append(f"({result.unconfirmed_count} detected recurring items await confirmation and are NOT included)")
    for w in result.warnings:
        lines.append(f"WARNING: {w}")
    return "\n".join(lines)
