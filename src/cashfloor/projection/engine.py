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
    to_cents,
)
from .recurrence import occurrences

DUE_TYPES = (AccountType.CREDIT, AccountType.LOAN, AccountType.UTILITY)


def pool_accounts(cfg: Config, accounts: list[Account]) -> list[Account]:
    flagged = [a for a in accounts if a.in_pool and a.active]
    if flagged:
        return flagged
    return [a for a in accounts if a.active and a.type.value in cfg.projection.pool_account_types]


def _event(events: dict[date, list[tuple[int, str]]], d: date, amount: int, label: str) -> None:
    events[d].append((amount, label))


def build_events(
    cfg: Config,
    accounts: list[Account],
    items: list[ScheduledItem],
    as_of: date,
    horizon_end: date,
) -> tuple[dict[date, list[tuple[int, str]]], int]:
    """Return {date: [(amount_cents, label), ...]} and count of unconfirmed items."""
    pool = pool_accounts(cfg, accounts)
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


def project(db: Database, cfg: Config, as_of: date | None = None) -> ProjectionResult:
    as_of = as_of or date.today()
    horizon = cfg.projection.horizon_days
    horizon_end = as_of + timedelta(days=horizon)
    accounts = db.list_accounts()
    items = db.list_items()
    pool = pool_accounts(cfg, accounts)
    starting = sum((a.spendable if cfg.projection.use_available_balance else a.balance) for a in pool)
    events, unconfirmed = build_events(cfg, accounts, items, as_of, horizon_end)
    warnings: list[str] = []
    for a in pool:
        if a.sync_error:
            warnings.append(f"{a.display_name}: {a.sync_error}")
        elif a.balance_as_of and (as_of - a.balance_as_of.date()).days > cfg.projection.stale_after_days:
            warnings.append(f"{a.display_name}: balance is {(as_of - a.balance_as_of.date()).days} days old")

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
