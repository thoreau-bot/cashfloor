from datetime import date, timedelta

from cashfloor.config import Config
from cashfloor.models import Frequency, Transaction
from cashfloor.projection.recurring import detect_recurring, normalize_payee

TODAY = date(2026, 10, 29)


def _tx(i, posted, amount, payee, acct=1):
    return Transaction(id=None, account_id=acct, external_id=f"t{i}", posted=posted, amount=amount, payee=payee)


def test_normalize_payee_strips_noise():
    assert normalize_payee("NETFLIX.COM 866-579-7172 CA #123456") == normalize_payee("NETFLIX.COM 866-579-7173 CA #999")
    assert normalize_payee("ACH DEBIT PG&E 10/01 WEB ID: 123") == "pg&e"
    # live feed vs statement wording of the same bill must land in one series
    assert normalize_payee("ACH WITHDRAWAL 092126 CITI AUTOPAY PAYMENT ***********9077") == normalize_payee("CITI AUTOPAY PAYMENT")
    assert normalize_payee("ACH DEP 091126 ACME WIDGETS INC PAYROLL") == normalize_payee("ACME WIDGETS INC PAYROLL")


def test_detects_biweekly_paycheck_and_monthly_bill():
    txs = []
    d = date(2026, 6, 5)
    for i in range(10):
        txs.append(_tx(f"p{i}", d + timedelta(days=14 * i), 312_345, "ACME CORP PAYROLL 0012345"))
    for i, m in enumerate(range(5, 11)):
        txs.append(_tx(f"n{i}", date(2026, m, 12), -1_599, "NETFLIX.COM"))
    found = {it.name: it for it in detect_recurring(Config(), txs, TODAY)}
    pay = next(v for k, v in found.items() if "acme" in k.lower())
    assert pay.frequency == Frequency.BIWEEKLY
    assert pay.amount == 312_345
    # last hit 2026-10-09; 10-23 is already past as of 10-29, so next is 11-06
    assert pay.next_date == date(2026, 11, 6)
    assert not pay.confirmed
    nf = next(v for k, v in found.items() if "netflix" in k.lower())
    assert nf.frequency == Frequency.MONTHLY
    assert nf.amount == -1_599
    assert nf.next_date == date(2026, 11, 12)


def test_variable_amount_bill_is_proposed_with_median_and_dead_series_is_not():
    amts = [200_000, 260_000, 205_000, 290_000, 210_000, 250_000, 215_000, 300_000, 220_000, 270_000]
    txs = [_tx(i, date(2026, 1, 6) + timedelta(days=30 * i), -amts[i], "APPFOLIO INC F") for i in range(10)]
    txs += [_tx(f"d{i}", date(2025, 1, 10) + timedelta(days=30 * i), -5000, "OLD GYM") for i in range(6)]
    txs += [_tx(f"n{i}", date(2026, 1, 7) + timedelta(days=30 * i), -5, "IOD INTEREST PAID") for i in range(10)]
    found = {it.payee_pattern: it for it in detect_recurring(Config(), txs, TODAY)}
    assert set(found) == {"appfolio inc f"}
    rent = found["appfolio inc f"]
    assert rent.frequency == Frequency.MONTHLY and "variable amount" in rent.notes
    import statistics
    assert rent.amount == -int(statistics.median(amts[-6:]))


def test_truncated_statement_name_merges_with_live_feed_name():
    txs = [_tx(f"s{i}", date(2026, 1, 9) + timedelta(days=14 * i), 300_000, "ACME WIDGETS INC PAYROLL") for i in range(18)]
    txs += [_tx(f"l{i}", date(2026, 1, 9) + timedelta(days=14 * (18 + i)), 300_000, "Acme Widgets Incorporated") for i in range(3)]
    found = detect_recurring(Config(), txs, TODAY)
    assert len(found) == 1 and found[0].frequency == Frequency.BIWEEKLY
    assert "21 transactions" in found[0].notes


def test_card_autopay_with_extra_manual_payments_keeps_the_anchored_series():
    from dateutil.relativedelta import relativedelta

    txs, i = [], 0
    m = date(2025, 10, 1)
    extras = {11, 1, 3, 4, 6, 8}  # months with an additional manual payment
    for k in range(13):
        d = m + relativedelta(months=k)
        txs.append(_tx(f"a{i}", d.replace(day=17 if k % 2 else 18), -(50_000 + 1_000 * k), "CAPITAL ONE CRCARDPMT"))
        i += 1
        if d.month in extras:
            txs.append(_tx(f"x{i}", d.replace(day=3 + k), -20_000, "CAPITAL ONE CRCARDPMT"))
            i += 1
    found = {it.payee_pattern: it for it in detect_recurring(Config(), txs, TODAY)}
    cap = found["capital one crcardpmt"]
    assert cap.frequency == Frequency.MONTHLY
    assert "6 extra payment(s) ignored" in cap.notes
    assert ("day 17" in cap.notes) or ("day 18" in cap.notes)
    assert cap.next_date > TODAY and cap.next_date.day in (17, 18)
    assert -62_000 <= cap.amount <= -57_000  # median of the last six anchored amounts, extras excluded


def test_accent_folding_merges_series():
    assert normalize_payee("ACMÉ WIDGETS INC PAYROLL") == normalize_payee("ACME WIDGETS INC PAYROLL")


def test_five_years_with_irregularities_still_proposes():
    from dateutil.relativedelta import relativedelta

    txs = []
    d = date(2021, 9, 24)
    i = 0
    while d < date(2026, 9, 1):
        if d != date(2023, 6, 24):  # one skipped month
            amt = -16_886 if d.year >= 2026 else -15_200  # rate change this year
            txs.append(_tx(f"i{i}", d, amt, "USAA P&C AUTOPAY"))
            i += 1
        if d == date(2024, 2, 24):  # one doubled payment
            txs.append(_tx("dbl", d + timedelta(days=5), -15_200, "USAA P&C AUTOPAY"))
        d += relativedelta(months=1)
    pay = date(2021, 9, 10)
    j = 0
    while pay < date(2026, 10, 25):
        txs.append(_tx(f"p{j}", pay, 319_299, "ACME WIDGETS INC PAYROLL"))
        pay += timedelta(days=14)
        j += 1
    # long-cycle: semi-annual registration, 2 hits
    txs += [_tx("r1", date(2026, 3, 3), -41_000, "WA DOL VEHICLE RENEWAL"),
            _tx("r2", date(2025, 9, 2), -41_000, "WA DOL VEHICLE RENEWAL")]
    found = {it.payee_pattern: it for it in detect_recurring(Config(), txs, TODAY)}
    ins = found["usaa p&c autopay"]
    assert ins.frequency == Frequency.MONTHLY and ins.amount == -16_886  # latest amount wins
    assert found["acme widgets inc"].frequency == Frequency.BIWEEKLY
    assert found["wa dol vehicle"].frequency == Frequency.SEMIANNUAL
    assert ins.confidence >= 0.8


def test_split_paycheck_series_merge_into_one_income_source():
    """36 biweekly paydays; 32 posted as $3,300.00 under one descriptor, 4 as
    $3,299.99 under another.  Each alone is a biweekly series with holes; the
    detector must see one paycheck, projected at the dominant amount."""
    cfg = Config()
    today = date(2026, 11, 19)
    first = today - timedelta(days=14 * 36 + 3)
    txs = []
    odd = {5, 11, 20, 29}
    for i in range(36):
        d = first + timedelta(days=14 * i)
        if i in odd:
            txs.append(_tx(i, d, 329_999, "ACME WIDGETS INC DIR DEP", acct=1))
        else:
            txs.append(_tx(i, d, 330_000, "ACME WIDGETS PAYROLL", acct=1))
    items = detect_recurring(cfg, txs, today=today)
    pay = [i for i in items if i.amount > 0]
    assert len(pay) == 1
    assert pay[0].amount == 330_000 and pay[0].frequency == Frequency.BIWEEKLY
    assert "same income source" in pay[0].notes
    assert pay[0].next_date == first + timedelta(days=14 * 37)  # first payday after today


def test_two_genuine_incomes_on_the_same_days_are_not_merged():
    """Two employers paying biweekly on the same Fridays, amounts 5% apart:
    the dates collide, so they stay separate."""
    cfg = Config()
    today = date(2026, 11, 19)
    first = today - timedelta(days=14 * 12 + 3)
    txs = []
    for i in range(12):
        d = first + timedelta(days=14 * i)
        txs.append(_tx(i, d, 200_000, "ACME WIDGETS PAYROLL", acct=1))
        txs.append(_tx(100 + i, d, 195_000, "FOURTH COFFEE PAYROLL", acct=1))
    items = detect_recurring(cfg, txs, today=today)
    assert sorted(i.amount for i in items if i.amount > 0) == [195_000, 200_000]


def test_interleaved_but_dissimilar_amounts_are_not_merged():
    """A weekly $500 side gig does fill the gaps of a biweekly $3,300 paycheck
    date-wise, but 85% apart in amount is not the same income."""
    cfg = Config()
    today = date(2026, 11, 19)
    first = today - timedelta(days=14 * 12 + 3)
    txs = []
    for i in range(12):
        txs.append(_tx(i, first + timedelta(days=14 * i), 330_000, "ACME WIDGETS PAYROLL", acct=1))
        txs.append(_tx(100 + i, first + timedelta(days=14 * i + 7), 50_000, "GIG PLATFORM PAYOUT", acct=1))
    items = detect_recurring(cfg, txs, today=today)
    assert sorted(i.amount for i in items if i.amount > 0) == [50_000, 330_000]


def test_merged_series_retires_its_old_item_and_carries_confirmation(tmp_path):
    """Kendall's case: an earlier sync proposed both halves; the user confirmed
    the 4-hit half.  After the merge the survivor must be the only item, and
    it must inherit the confirmation."""
    from cashfloor.db import Database
    from cashfloor.models import Account, AccountType, ItemSource, ScheduledItem
    from cashfloor.projection.recurring import analyse_recurring
    from cashfloor.sync import SyncSummary, reconcile_proposals

    cfg = Config()
    today = date(2026, 11, 19)
    db = Database(tmp_path / "t.sqlite")
    chk = db.upsert_account(Account(id=None, provider="t", external_id="c", name="Checking", institution="Bank",
                                    type=AccountType.CHECKING, balance=0))
    first = today - timedelta(days=14 * 36 + 3)
    txs = []
    for i in range(36):
        d = first + timedelta(days=14 * i)
        if i in {30, 31, 32, 33}:
            txs.append(_tx(i, d, 319_299, "ACME WIDGETS INC DIR DEP", acct=chk.id))
        else:
            txs.append(_tx(i, d, 319_300, "ACME WIDGETS PAYROLL", acct=chk.id))
    # stale state from before the merge existed
    db.save_item(ScheduledItem(id=None, name="Acme Widgets Inc Dir Dep", account_id=chk.id, amount=319_299,
                               frequency=Frequency.BIWEEKLY, next_date=today, source=ItemSource.DETECTED,
                               confirmed=True, payee_pattern=normalize_payee("ACME WIDGETS INC DIR DEP")))
    db.save_item(ScheduledItem(id=None, name="Acme Widgets Payroll", account_id=chk.id, amount=319_300,
                               frequency=Frequency.BIWEEKLY, next_date=today, source=ItemSource.DETECTED,
                               confirmed=False, payee_pattern="acme widgets payroll"))
    summary = SyncSummary()
    reconcile_proposals(db, analyse_recurring(cfg, txs, today), summary)
    items = db.list_items(active_only=False)
    assert len(items) == 1 and summary.retired_items == 1
    assert items[0].payee_pattern == "acme widgets payroll" and items[0].confirmed and items[0].amount == 319_300


def test_stale_key_duplicate_is_folded_into_the_current_series(tmp_path):
    """An item proposed by an older detector run (different payee key, only the
    live feed's 4 hits, confirmed by the user) lingers next to the current
    34-hit proposal.  Same cadence, amount within tolerance, same next date:
    fold it, keep the confirmation."""
    from cashfloor.db import Database
    from cashfloor.models import Account, AccountType, ItemSource, ScheduledItem
    from cashfloor.projection.recurring import analyse_recurring
    from cashfloor.sync import SyncSummary, reconcile_proposals

    cfg = Config()
    today = date(2026, 11, 19)
    db = Database(tmp_path / "t.sqlite")
    chk = db.upsert_account(Account(id=None, provider="t", external_id="c", name="Checking", institution="Bank",
                                    type=AccountType.CHECKING, balance=0))
    first = today - timedelta(days=14 * 34)
    txs = [_tx(i, first + timedelta(days=14 * i), 319_300 if i < 30 else 319_299, "ACME WIDGETS PAYROLL", acct=chk.id)
           for i in range(34)]
    stale = db.save_item(ScheduledItem(id=None, name="Acme Widgets Payroll", account_id=chk.id, amount=319_299,
                                       frequency=Frequency.BIWEEKLY, next_date=today, source=ItemSource.DETECTED,
                                       confirmed=True, payee_pattern="acme widgets payroll dep xx",
                                       notes="detected from 4 transactions"))
    summary = SyncSummary()
    reconcile_proposals(db, analyse_recurring(cfg, txs, today), summary)
    items = db.list_items(active_only=False)
    assert len(items) == 1 and summary.retired_items == 1 and summary.new_proposals == 1
    assert items[0].id != stale.id and items[0].confirmed and items[0].payee_pattern == "acme widgets payroll"
    assert items[0].next_date == today
