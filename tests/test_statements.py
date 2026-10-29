from datetime import date
from decimal import Decimal
from pathlib import Path

from cashfloor.statements.organizer import build_archive_path, ingest_statement_file
from cashfloor.statements.parser import parse_statement_text

CARD_TEXT = """
CHASE  Freedom Unlimited
Account Number: XXXX XXXX XXXX 9999
Statement Date: 10/18/2026        Opening/Closing Date: 09/19/26 - 10/18/26
New Balance: $600.00
Minimum Payment Due: $35.00
Payment Due Date: 11/15/2026
"""

UTILITY_TEXT = """
Pacific Gas and Electric Company
Account No: 1234567890-1
Billing period: Sep 20, 2026 to Oct 19, 2026
Bill date: October 21, 2026
Total Amount Due: $184.37
Due by November 10, 2026
"""


def test_parse_card_statement():
    ps = parse_statement_text(CARD_TEXT)
    assert ps.institution == "Chase"
    assert ps.last4 == "9999"
    assert ps.statement_date == date(2026, 10, 18)
    assert ps.due_date == date(2026, 11, 15)
    assert ps.amount_due == Decimal("600.00")
    assert ps.minimum_due == Decimal("35.00")
    assert ps.confidence >= 0.9


def test_parse_utility_statement():
    ps = parse_statement_text(UTILITY_TEXT)
    assert ps.institution == "PGE"
    assert ps.last4 == "7890"
    assert ps.period_start == date(2026, 9, 20)
    assert ps.period_end == date(2026, 10, 19)
    assert ps.statement_date == date(2026, 10, 21)
    assert ps.due_date == date(2026, 11, 10)
    assert ps.amount_due == Decimal("184.37")


def test_archive_path_uses_key_and_date(cfg, accounts):
    ps = parse_statement_text(CARD_TEXT)
    p = build_archive_path(cfg, ps, accounts["card"])
    assert p == cfg.general.archive_dir / "Chase" / "visa" / "2026" / "2026-10-18_Chase_visa_statement.pdf"


def test_ingest_matches_account_and_updates_due(cfg, db, accounts, tmp_path: Path):
    src = tmp_path / "inbox" / "20261018-statements-9999-.pdf"
    src.write_bytes(b"%PDF-1.4 fake")
    st = ingest_statement_file(cfg, db, src, text=CARD_TEXT)
    assert st is not None and not st.needs_review
    assert Path(st.archived_path).exists() and not src.exists()
    card = db.get_account(accounts["card"].id)
    assert card.due_date == date(2026, 11, 15)
    assert card.statement_balance == 60_000
    # duplicate drop is discarded
    src.write_bytes(b"%PDF-1.4 fake")
    assert ingest_statement_file(cfg, db, src, text=CARD_TEXT) is None
    assert not src.exists()


def test_unknown_statement_goes_to_review(cfg, db, tmp_path: Path):
    src = tmp_path / "inbox" / "mystery.pdf"
    src.write_bytes(b"%PDF-1.4 other")
    st = ingest_statement_file(cfg, db, src, text="Some Credit Union\nThanks for banking with us.")
    assert st.needs_review
    assert "_review" in st.archived_path
