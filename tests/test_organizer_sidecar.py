import json
from datetime import date
from pathlib import Path

from cashfloor.models import Account, AccountType
from cashfloor.statements.organizer import infer_type, ingest_statement_file
from cashfloor.statements.parser import detect_institution

USAA_TEXT = """
USAA Federal Savings Bank
CLASSIC CHECKING  Account Number: 0123 4471
Statement period 09/09/2026 - 10/08/2026
Ending balance $2,285.34
10/01  CHASE CREDIT CRD AUTOPAY   -600.00    Your funds are automatically transferred
"""


def test_institution_detection_uses_word_boundaries_and_letterhead():
    assert detect_institution(USAA_TEXT) == "USAA"  # not "Ally" (automatically) nor "Chase" (a payee line)
    assert detect_institution("Welcome to Ally Bank. Ally is great.") == "Ally"
    assert detect_institution("totally normally finally") is None


def test_infer_type():
    assert infer_type("CHECKING STATEMENT") == AccountType.CHECKING
    assert infer_type("SAVINGS STATEMENT") == AccountType.SAVINGS
    assert infer_type("Auto and Property Insurance Statement") == AccountType.UTILITY
    assert infer_type("Amended Signature Card") is None


def _mk(db, ext, name, type_, key):
    a = db.upsert_account(Account(id=None, provider="simplefin", external_id=ext, name=name,
                                  institution="USAA", type=type_))
    db.set_account_user_fields(a.id, key=key)
    return a


def test_sidecar_drives_institution_date_and_account(cfg, db, tmp_path: Path):
    chk = _mk(db, "c", "USAA CLASSIC CHECKING", AccountType.CHECKING, "usaa-chk")
    _mk(db, "s", "USAA SAVINGS", AccountType.SAVINGS, "usaa-sav")
    src = tmp_path / "inbox" / "2026-10-08_USAA_4471_CHECKING-STATEMENT.pdf"
    src.write_bytes(b"%PDF-1.4 x")
    src.with_name(src.name + ".meta.json").write_text(json.dumps({
        "institution": "USAA", "title": "CHECKING STATEMENT", "account": "USAA CLASSIC CHECKING *4471",
        "date": "2026-10-08"}))
    # PDF text deliberately misleading: mentions Chase, no usable date
    st = ingest_statement_file(cfg, db, src, text="Payment to CHASE CARD. Thank you.")
    assert st.institution == "USAA" and st.account_id == chk.id and st.title == "CHECKING STATEMENT"
    assert st.statement_date == date(2026, 10, 8) and not st.needs_review
    assert Path(st.archived_path).name == "2026-10-08_USAA_usaa-chk_checking-statement.pdf"
    assert Path(st.archived_path + ".meta.json").exists() and not src.exists()
    # savings statement with no last4 anywhere matches by title-implied type
    src2 = tmp_path / "inbox" / "sav.pdf"
    src2.write_bytes(b"%PDF-1.4 y")
    src2.with_name(src2.name + ".meta.json").write_text(json.dumps({
        "institution": "USAA", "title": "SAVINGS STATEMENT", "account": "", "date": "2026-10-01"}))
    st2 = ingest_statement_file(cfg, db, src2, text="")
    assert st2.account_id != chk.id and st2.account_id is not None


def test_sidecar_document_without_account_is_filed_not_reviewed(cfg, db, tmp_path: Path):
    src = tmp_path / "inbox" / "sig.pdf"
    src.write_bytes(b"%PDF-1.4 z")
    src.with_name(src.name + ".meta.json").write_text(json.dumps({
        "institution": "USAA", "title": "Amended Signature Card", "account": "*4471", "date": "2025-04-10"}))
    st = ingest_statement_file(cfg, db, src, text="")
    assert not st.needs_review and "_review" not in st.archived_path
    assert Path(st.archived_path).name == "2025-04-10_USAA_x4471_amended-signature-card.pdf"
