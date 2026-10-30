from datetime import date

from cashfloor.statements.transactions import parse_transactions

# Modelled on a USAA Classic Checking statement as pdfplumber extracts it.
USAA_TEXT = """USAA Federal Savings Bank
USAA CLASSIC CHECKING
for Account Number:
Statement Period: 09/05/2026 to 10/07/2026
Activity Summary
Beginning Balance $872.93
Ending Balance $444.23
Transactions
Date Description Debits Credits Balance
09/05 Beginning Balance $872.93
09/08 ATM REBATE $3.50 $876.43
$103.50 ATM W/D P410221 on 09/05
09/08 ATM WITHDRAWAL $63.50 $812.93
090526 6011090526
P410221 SPRINGFIELD OR
09/10 ACH DEP 091126 $3,192.99 $4,005.92
ACME WIDGETS INC PAYROLL
***********7Q2P
09/14 ACH WITHDRAWAL 091426 $50.00 $3,955.92
PAYPAL TRANSFER
***********ANCE
09/14 ACH WITHDRAWAL 091426 $50.00 $3,905.92
PAYPAL TRANSFER
***********ANCE
Online: usaa.com Phone: 210-531-USAA (8722)
Page 1 of 5
USAA CLASSIC CHECKING
Statement Period: 09/05/2026 to 10/07/2026
Transactions (continued)
Date Description Debits Credits Balance
09/21 ACH WITHDRAWAL 092126 $365.95 $3,539.97
CITI AUTOPAY PAYMENT
***********9077
09/22 DEBIT CARD PURCHASE 092126 7800092126 $20.00 $3,519.97
GROCERY MART # SPRINGFIELD OR
09/24 ACH WITHDRAWAL 092426 $168.86 $3,351.11
USAA P&C AUTOPAY
***********1180
10/01 USAA CREDIT $500.00 $3,851.11
Zelle: SOMEONE
7917672223
Page 2 of 5
"""


def test_parse_usaa_rows_sign_from_balance():
    r = parse_transactions(USAA_TEXT)
    assert (r.period_start, r.period_end) == (date(2026, 9, 5), date(2026, 10, 7))
    tx = r.transactions
    assert len(tx) == 9 and r.skipped_rows == 0
    assert tx[0].posted == date(2026, 9, 8) and tx[0].amount == 350 and tx[0].payee == "ATM REBATE"
    assert tx[1].amount == -6350 and tx[1].payee == "P410221 SPRINGFIELD OR"
    assert tx[2].amount == 319_299 and tx[2].payee == "ACME WIDGETS INC PAYROLL"
    assert [t.amount for t in tx[3:5]] == [-5000, -5000]  # identical same-day rows both kept
    assert tx[5].payee == "CITI AUTOPAY PAYMENT" and tx[5].amount == -36_595
    assert tx[6].payee == "GROCERY MART # SPRINGFIELD OR" and tx[6].amount == -2000
    assert tx[7].payee == "USAA P&C AUTOPAY"
    assert tx[8].payee == "Zelle: SOMEONE" and tx[8].amount == 50_000 and tx[8].posted == date(2026, 10, 1)
    assert tx[8].balance_after == 385_111


def test_wrapped_and_balance_only_rows_use_the_balance_chain():
    text = """Statement Period: 09/05/2026 to 10/07/2026
Date Description Debits Credits Balance
09/05 Beginning Balance $1,000.00
09/21 ACH WITHDRAWAL 092126 $365.95
$634.05
CITI AUTOPAY PAYMENT
09/22 ACH WITHDRAWAL 092226 $584.05
PAYPAL TRANSFER
09/24 ACH DEP 092526 $3,000.00 $3,584.05
PAYROLL
"""
    r = parse_transactions(text)
    assert [(t.amount, t.balance_after) for t in r.transactions] == [
        (-36_595, 63_405),      # balance wrapped onto its own line
        (-5_000, 58_405),       # only the balance survived: amount = movement
        (300_000, 358_405),
    ]
    assert r.transactions[0].payee == "CITI AUTOPAY PAYMENT"


def test_year_boundary():
    text = """Statement Period: 12/08/2025 to 01/07/2026
Date Description Debits Credits Balance
12/08 Beginning Balance $100.00
12/20 ACH DEP 122025 $50.00 $150.00
PAYROLL
01/03 ACH WITHDRAWAL 010326 $20.00 $130.00
NETFLIX
"""
    r = parse_transactions(text)
    assert [t.posted for t in r.transactions] == [date(2025, 12, 20), date(2026, 1, 3)]


def test_misread_row_is_dropped_not_guessed():
    text = """Statement Period: 09/05/2026 to 10/07/2026
Date Description Debits Credits Balance
09/05 Beginning Balance $100.00
09/08 SOMETHING ODD $10.00 $500.00
09/09 ACH DEP 090926 $5.00 $505.00
PAYROLL
"""
    r = parse_transactions(text)
    # row 1: |delta| != amount and wording gives no hint -> skipped; chain resumes from its balance
    assert r.skipped_rows == 1 and len(r.transactions) == 1 and r.transactions[0].amount == 500


def test_filing_a_bank_statement_dedupes_against_live_feed_one_to_one(cfg, db, tmp_path):
    from datetime import UTC, datetime

    from cashfloor.models import Account, AccountType, Transaction
    from cashfloor.statements.organizer import ingest_statement_file

    a = db.upsert_account(Account(id=None, provider="simplefin", external_id="c", name="USAA CLASSIC CHECKING",
                                  institution="USAA", type=AccountType.CHECKING, last4="4471",
                                  balance_as_of=datetime(2026, 10, 29, tzinfo=UTC)))
    # live feed (SimpleFIN wording) already has three of them, incl. ONE of the two $50 PayPal rows
    db.upsert_transactions([
        Transaction(id=None, account_id=a.id, external_id="ACT-1", posted=date(2026, 9, 21), amount=-36_595, payee="Citi Card Online Payment"),
        Transaction(id=None, account_id=a.id, external_id="ACT-2", posted=date(2026, 9, 15), amount=-5000, payee="PayPal"),
        Transaction(id=None, account_id=a.id, external_id="ACT-3", posted=date(2026, 10, 1), amount=50_000, payee="Zelle payment"),
    ])
    src = tmp_path / "inbox" / "chk.pdf"
    src.write_bytes(b"%PDF-1.4 chk")
    st = ingest_statement_file(cfg, db, src, text=USAA_TEXT)
    assert st.account_id == a.id
    imported = [t for t in db.list_transactions(a.id) if t.external_id.startswith("stmt:")]
    # 9 parsed; 3 matched live rows skipped; the second $50 PayPal row is kept
    assert len(imported) == 6
    assert sum(1 for t in imported if t.amount == -5000) == 1
    assert not any(t.amount == -36_595 for t in imported)
    assert {t.payee for t in imported} >= {"ACME WIDGETS INC PAYROLL", "PAYPAL TRANSFER"}
    # idempotent
    from cashfloor.statements.organizer import import_statement_transactions
    import_statement_transactions(db, st, a, USAA_TEXT)
    assert len([t for t in db.list_transactions(a.id) if t.external_id.startswith("stmt:")]) == 6
