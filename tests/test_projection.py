from datetime import date

from cashfloor.models import Frequency, ItemSource, ScheduledItem
from cashfloor.projection.engine import project
from cashfloor.projection.recurrence import occurrences, step

TODAY = date(2026, 10, 29)


def test_monthly_step_keeps_anchor_day():
    anchor = date(2026, 1, 31)
    assert step(anchor, Frequency.MONTHLY, anchor) == date(2026, 2, 28)
    assert step(date(2026, 2, 28), Frequency.MONTHLY, anchor) == date(2026, 3, 31)


def test_semimonthly_occurrences():
    it = ScheduledItem(id=None, name="pay", account_id=1, amount=1, frequency=Frequency.SEMIMONTHLY,
                       next_date=date(2026, 11, 1))
    occ = occurrences(it, date(2026, 11, 1), date(2026, 12, 31))
    assert occ == [date(2026, 11, 1), date(2026, 11, 16), date(2026, 12, 1), date(2026, 12, 16)]


def test_weekend_rule_previous():
    it = ScheduledItem(id=None, name="rent", account_id=1, amount=-1, frequency=Frequency.MONTHLY,
                       next_date=date(2026, 11, 1), weekend_rule="previous")  # Nov 1 2026 is a Sunday
    assert occurrences(it, date(2026, 10, 29), date(2026, 11, 30)) == [date(2026, 10, 30)]


def test_projection_floor_and_discretionary(db, cfg, accounts):
    chk = accounts["chk"].id
    # paycheck +3000 biweekly from Nov 6; rent -2200 on the 1st; card pays 600 on Nov 15 (from account due date)
    db.save_item(ScheduledItem(id=None, name="Paycheck", account_id=chk, amount=300_000,
                               frequency=Frequency.BIWEEKLY, next_date=date(2026, 11, 6)))
    db.save_item(ScheduledItem(id=None, name="Rent", account_id=chk, amount=-220_000,
                               frequency=Frequency.MONTHLY, next_date=date(2026, 11, 1)))
    r = project(db, cfg, as_of=TODAY)
    assert r.starting_balance == 350_000
    # Nov 1: 3500-2200 = 1300 -> that's the floor before the first paycheck
    assert r.floor == 130_000
    assert r.floor_date == date(2026, 11, 1)
    assert r.discretionary == 130_000 - 50_000
    # card due event present on Nov 15 and estimated again Dec 15
    by_day = {d.day: d for d in r.days}
    assert by_day[date(2026, 11, 15)].outflows == -60_000
    assert any("(est.)" in e for e in by_day[date(2026, 12, 15)].events)


def test_unconfirmed_items_excluded(db, cfg, accounts):
    chk = accounts["chk"].id
    db.save_item(ScheduledItem(id=None, name="Mystery", account_id=chk, amount=-999_999,
                               frequency=Frequency.MONTHLY, next_date=date(2026, 11, 2),
                               source=ItemSource.DETECTED, confirmed=False))
    r = project(db, cfg, as_of=TODAY)
    assert r.unconfirmed_count == 1
    assert r.floor > 0


def test_internal_transfer_is_neutral(db, cfg, accounts):
    chk, sav = accounts["chk"].id, accounts["sav"].id
    db.save_item(ScheduledItem(id=None, name="To savings", account_id=chk, amount=-50_000,
                               frequency=Frequency.MONTHLY, next_date=date(2026, 11, 3),
                               source=ItemSource.TRANSFER, counter_account_id=sav))
    r = project(db, cfg, as_of=TODAY)
    assert r.days[5].balance == r.starting_balance  # Nov 3, nothing else scheduled yet


def test_explicit_card_payment_item_suppresses_auto_due(db, cfg, accounts):
    chk, card = accounts["chk"].id, accounts["card"].id
    db.save_item(ScheduledItem(id=None, name="Pay Visa", account_id=chk, amount=-20_000,
                               frequency=Frequency.MONTHLY, next_date=date(2026, 11, 10),
                               source=ItemSource.TRANSFER, counter_account_id=card))
    r = project(db, cfg, as_of=TODAY)
    by_day = {d.day: d for d in r.days}
    assert by_day[date(2026, 11, 15)].outflows == 0
    assert by_day[date(2026, 11, 10)].outflows == -20_000
