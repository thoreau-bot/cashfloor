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


def test_ignores_irregular_amounts_and_dead_series():
    txs = [_tx(i, date(2026, 1, 1) + timedelta(days=30 * i), -(1000 + 900 * i), "GROCERY") for i in range(5)]
    txs += [_tx(f"d{i}", date(2025, 1, 10) + timedelta(days=30 * i), -5000, "OLD GYM") for i in range(6)]
    assert detect_recurring(Config(), txs, TODAY) == []


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
