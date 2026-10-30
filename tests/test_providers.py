from datetime import UTC, date, datetime
from pathlib import Path

from cashfloor.models import AccountType
from cashfloor.providers.filedrop import parse_csv, parse_ofx
from cashfloor.providers.simplefin import parse_accounts_payload
from cashfloor.sync import run_sync

SIMPLEFIN_PAYLOAD = {
    "errors": [],
    "accounts": [
        {
            "org": {"domain": "chase.com", "name": "Chase", "sfin-url": "https://bridge.simplefin.org/simplefin"},
            "id": "ACT-1",
            "name": "TOTAL CHECKING",
            "currency": "USD",
            "balance": "2500.00",
            "available-balance": "2450.00",
            "balance-date": 1761739200,
            "transactions": [
                {"id": "TRN-1", "posted": 1761652800, "amount": "-45.10", "description": "SHELL OIL 12345", "pending": False},
                {"id": "TRN-2", "posted": 1761566400, "amount": "3123.45", "description": "ACME PAYROLL", "pending": False},
            ],
        },
        {
            "org": {"domain": "chase.com", "name": "Chase"},
            "id": "ACT-2",
            "name": "FREEDOM VISA",
            "currency": "USD",
            "balance": "-800.00",
            "balance-date": 1761739200,
            "transactions": [],
        },
    ],
}

OFX = """OFXHEADER:100
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>123<ACCTID>00112233<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20261001<DTEND>20261028
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261003<TRNAMT>-12.50<FITID>F1<NAME>COFFEE SHOP</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261005120000<TRNAMT>1500.00<FITID>F2<NAME>PAYROLL</STMTTRN>
</BANKTRANLIST><LEDGERBAL><BALAMT>1987.50<DTASOF>20261028</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


def test_simplefin_parse():
    res = parse_accounts_payload(SIMPLEFIN_PAYLOAD, "simplefin", as_of=datetime(2026, 10, 29, tzinfo=UTC))
    assert len(res.accounts) == 2
    chk, card = res.accounts
    assert chk.type == AccountType.CHECKING and chk.balance == 250_000 and chk.available == 245_000
    assert card.type == AccountType.CREDIT and card.balance == -80_000
    assert len(res.transactions) == 2
    assert res.transactions[1].amount == 312_345
    assert res.transactions[0].__dict__["_account_external_id"] == "ACT-1"


def test_ofx_parse():
    acct, txs = parse_ofx(OFX, "inbox", "file")
    assert acct.type == AccountType.CHECKING and acct.balance == 198_750 and acct.last4 == "2233"
    assert [t.amount for t in txs] == [-1_250, 150_000]
    assert txs[1].posted == date(2026, 10, 5)


def test_csv_parse(tmp_path: Path):
    p = tmp_path / "MyCU_checking.csv"
    p.write_text("Date,Description,Debit,Credit\n10/01/2026,RENT,\"2,200.00\",\n10/03/2026,DEPOSIT,,500.00\n")
    acct, txs = parse_csv(p, "inbox")
    assert acct.institution == "MyCU"
    assert [t.amount for t in txs] == [-220_000, 50_000]


def test_run_sync_filedrop_end_to_end(cfg, db, monkeypatch):
    from cashfloor.config import ProviderConfig

    cfg.provider = [ProviderConfig(name="inbox", kind="filedrop")]
    (cfg.general.inbox_dir / "bank.ofx").write_text(OFX)
    s = run_sync(cfg, db, today=date(2026, 10, 29))
    assert s.providers_ok == ["inbox"] and s.accounts == 1 and s.transactions == 2
    assert db.list_accounts()[0].balance == 198_750


def test_sync_files_leftovers_and_survives_bad_pdf(cfg, db):
    from cashfloor.config import ProviderConfig

    cfg.provider = [ProviderConfig(name="inbox", kind="filedrop")]
    left = cfg.general.data_dir / "downloads" / "inbox"
    left.mkdir(parents=True)
    (left / "stranded.pdf").write_bytes(b"%PDF-1.4 stranded")
    (left / "stranded.pdf.meta.json").write_text('{"institution":"USAA","title":"CHECKING STATEMENT","date":"2026-01-08"}')
    (left / "broken.pdf").write_bytes(b"not a pdf at all")
    s = run_sync(cfg, db, today=date(2026, 10, 29))
    assert s.providers_ok == ["inbox"]
    titles = {st.title for st in db.list_statements()}
    assert "CHECKING STATEMENT" in titles            # leftover was filed
    assert not (left / "stranded.pdf").exists()
