"""0.2.0: pays-account links, assumed card payoff, manual accounts, amount parsing."""

from datetime import date, datetime

from cashfloor.config import Config
from cashfloor.db import Database
from cashfloor.models import Account, AccountType, Frequency, ItemSource, ScheduledItem, to_cents
from cashfloor.projection.engine import project
from cashfloor.projection.links import suggest_counter_account


def _cfg(**proj):
    cfg = Config()
    cfg.projection.horizon_days = 60
    cfg.projection.safety_buffer = 0.0
    for k, v in proj.items():
        setattr(cfg.projection, k, v)
    return cfg


def _db(tmp_path):
    return Database(tmp_path / "t.sqlite")


def _checking(db, bal=500_00):
    return db.upsert_account(Account(id=None, provider="simplefin", external_id="chk", name="Checking",
                                     institution="Northwind Bank", type=AccountType.CHECKING, balance=bal,
                                     in_pool=True, balance_as_of=datetime.now(), last_synced=datetime.now()))


def _card(db, **kw):
    a = Account(id=None, provider="simplefin", external_id="card", name="Platinum Card",
                institution="Contoso Card Services", type=AccountType.CREDIT, balance=-120_00,
                balance_as_of=datetime.now(), last_synced=datetime.now())
    for k, v in kw.items():
        setattr(a, k, v)
    return db.upsert_account(a)


def test_to_cents_tolerates_formatting():
    assert to_cents("1,234.56") == 123456
    assert to_cents("$-12.00") == -1200
    assert to_cents("(12.00)") == -1200
    assert to_cents("") == 0
    assert to_cents(-3.5) == -350


def test_card_without_due_date_is_assumed_paid_off(tmp_path):
    db = _db(tmp_path)
    _checking(db)
    _card(db)
    today = date(2026, 11, 19)
    res = project(db, _cfg(default_card_pay_day=25), as_of=today)
    labels = [e for d in res.days for e in d.events]
    assert any("assumed payoff" in lbl for lbl in labels)
    # 120.00 leaves checking on the 25th, then again next month (est. repeat)
    by_date = {d.day: d.balance for d in res.days}
    assert by_date[date(2026, 11, 25)] == 380_00
    assert by_date[date(2026, 12, 25)] == 260_00


def test_assumed_payoff_can_be_disabled(tmp_path):
    db = _db(tmp_path)
    _checking(db)
    _card(db)
    res = project(db, _cfg(assume_card_payoff=False), as_of=date(2026, 11, 19))
    assert not any("assumed payoff" in e for d in res.days for e in d.events)
    assert res.floor == 500_00


def test_account_pay_day_overrides_default(tmp_path):
    db = _db(tmp_path)
    _checking(db)
    c = _card(db)
    db.set_account_user_fields(c.id, pay_day=10)
    assert db.get_account(c.id).pay_day == 10
    res = project(db, _cfg(default_card_pay_day=25), as_of=date(2026, 11, 19))
    dates = [d.day for d in res.days for e in d.events if "assumed payoff" in e]
    assert dates[0] == date(2026, 12, 10)


def test_linked_payment_suppresses_card_event(tmp_path):
    db = _db(tmp_path)
    chk = _checking(db)
    c = _card(db, due_date=date(2026, 12, 5), statement_balance=120_00)
    db.save_item(ScheduledItem(id=None, name="CONTOSO CARD AUTOPAY", account_id=chk.id, amount=-120_00,
                               frequency=Frequency.MONTHLY, next_date=date(2026, 12, 3), source=ItemSource.MANUAL,
                               confirmed=True))
    res = project(db, _cfg(), as_of=date(2026, 11, 19))
    # both counted (card due event AND autopay item), twice inside the 60-day horizon -> double hit
    assert res.floor == 500_00 - 4 * 120_00
    it = db.list_items()[0]
    db.set_item_counter_account(it.id, c.id)
    res = project(db, _cfg(), as_of=date(2026, 11, 19))
    assert res.floor == 500_00 - 2 * 120_00
    db.set_item_counter_account(it.id, None)
    assert db.get_item(it.id).counter_account_id is None


def test_suggest_counter_account_matches_by_name(tmp_path):
    db = _db(tmp_path)
    chk = _checking(db)
    c = _card(db)
    db.add_manual_account(name="Electric", institution="Fabrikam Power", type=AccountType.UTILITY)
    accts = db.list_accounts()
    item = ScheduledItem(id=None, name="CONTOSO CARD SERVICES AUTOPAY", account_id=chk.id, amount=-50_00,
                         frequency=Frequency.MONTHLY, next_date=date(2026, 12, 1), source=ItemSource.DETECTED)
    assert suggest_counter_account(item, accts).id == c.id
    power = ScheduledItem(id=None, name="FABRIKAM POWER EPAY", account_id=chk.id, amount=-80_00,
                          frequency=Frequency.MONTHLY, next_date=date(2026, 12, 1), source=ItemSource.DETECTED)
    assert suggest_counter_account(power, accts).institution == "Fabrikam Power"
    pay = ScheduledItem(id=None, name="ACME WIDGETS PAYROLL", account_id=chk.id, amount=2000_00,
                        frequency=Frequency.BIWEEKLY, next_date=date(2026, 12, 1), source=ItemSource.DETECTED)
    assert suggest_counter_account(pay, accts) is None
    generic = ScheduledItem(id=None, name="ONLINE PAYMENT", account_id=chk.id, amount=-20_00,
                            frequency=Frequency.MONTHLY, next_date=date(2026, 12, 1), source=ItemSource.DETECTED)
    assert suggest_counter_account(generic, accts) is None


def test_manual_account_roundtrip_and_unique_ids(tmp_path):
    db = _db(tmp_path)
    a = db.add_manual_account(name="Auto Policy", institution="Woodgrove Insurance", type=AccountType.UTILITY,
                              key="auto-ins", due_date=date(2026, 12, 15), statement_balance=98_40, pay_day=15)
    b = db.add_manual_account(name="Auto Policy", institution="Woodgrove Insurance", type=AccountType.UTILITY)
    assert a.provider == "manual" and a.external_id != b.external_id
    assert a.key == "auto-ins" and a.pay_day == 15 and a.due_date == date(2026, 12, 15)
    db.set_manual_balance(a.id, -250_00)
    assert db.get_account(a.id).balance == -250_00
    _checking(db)
    res = project(db, _cfg(), as_of=date(2026, 11, 19))
    assert any("Auto Policy" in e for d in res.days for e in d.events)


def test_web_flows(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from cashfloor.web.app import create_app

    cfg = Config()
    cfg.general.data_dir = tmp_path / "data"
    cfg.general.archive_dir = tmp_path / "statements"
    cfg.general.inbox_dir = tmp_path / "inbox"
    cfg.ensure_dirs()
    client = TestClient(create_app(cfg))

    r = client.post("/accounts/add", data={"institution": "Woodgrove Insurance", "name": "Auto Policy",
                                           "type": "utility", "balance": "", "due_date": "2026-12-15",
                                           "statement_balance": "1,098.40", "pay_day": "15"}, follow_redirects=False)
    assert r.status_code == 303
    db = Database(cfg.db_path)
    try:
        acct = db.list_accounts()[0]
        assert acct.provider == "manual" and acct.statement_balance == 1098_40 and acct.pay_day == 15
        r = client.post(f"/accounts/{acct.id}", data={"key": "ins", "type": "utility", "balance": "-250.00", "active": "true",
                                                       "pay_day": "", "due_date": "", "statement_balance": "",
                                                       "minimum_payment": ""}, follow_redirects=False)
        assert r.status_code == 303
        acct = db.get_account(acct.id)
        assert acct.balance == -250_00 and acct.key == "ins" and acct.pay_day is None
        chk = _checking(db)
        db.save_item(ScheduledItem(id=None, name="WOODGROVE INSURANCE EPAY", account_id=chk.id, amount=-98_40,
                                   frequency=Frequency.MONTHLY, next_date=date(2026, 12, 13),
                                   source=ItemSource.DETECTED))
        page = client.get("/items").text
        assert "(suggested)" in page
        it = db.list_items()[0]
        r = client.post(f"/items/{it.id}/pays", data={"counter_account_id": str(acct.id)}, follow_redirects=False)
        assert r.status_code == 303
        assert db.get_item(it.id).counter_account_id == acct.id
        assert "(suggested)" not in client.get("/items").text
        page = client.get("/settings").text
        assert "projection.assume_card_payoff" in page
    finally:
        db.close()


def test_hsa_fsa_guess_and_retype_migration(tmp_path):
    from decimal import Decimal

    from cashfloor.providers.simplefin import guess_account_type

    assert guess_account_type("FIDELITY HSA", Decimal("1200")) == AccountType.HSA
    assert guess_account_type("Health Savings Account", Decimal("1200"), has_holdings=True) == AccountType.HSA
    assert guess_account_type("Flexible Spending Account", Decimal("300")) == AccountType.FSA
    assert guess_account_type("401(K) PLAN", Decimal("90000"), has_holdings=True) == AccountType.INVESTMENT
    # Accounts that were guessed 'investment' before 0.2.0 are re-typed once from their name.
    db = _db(tmp_path)
    old = db.upsert_account(Account(id=None, provider="simplefin", external_id="h1", name="FIDELITY HSA",
                                    institution="Fidelity", type=AccountType.INVESTMENT, balance=1200_00,
                                    balance_as_of=datetime.now(), last_synced=datetime.now()))
    db.conn.execute("DELETE FROM meta WHERE key='migrated:retype_hsa_fsa'")
    db.conn.commit()
    db.migrate()
    assert db.get_account(old.id).type == AccountType.HSA


def test_fsa_year_end_warning(tmp_path):
    from cashfloor.projection.engine import fsa_warnings

    cfg = _cfg()
    fsa = Account(id=1, provider="manual", external_id="f", name="Health FSA", institution="Fabrikam Benefits",
                  type=AccountType.FSA, balance=412_50, balance_as_of=datetime.now(), last_synced=datetime.now())
    assert fsa_warnings(cfg, [fsa], date(2026, 9, 1)) == []  # more than 61 days out
    w = fsa_warnings(cfg, [fsa], date(2026, 11, 19))
    assert len(w) == 1 and "412.50" in w[0] and "2026-12-31" in w[0] and "42 days" in w[0]
    fsa.balance = 0
    assert fsa_warnings(cfg, [fsa], date(2026, 12, 1)) == []
    # and it surfaces through project() like the other warnings
    db = _db(tmp_path)
    _checking(db)
    db.add_manual_account(name="Health FSA", institution="Fabrikam Benefits", type=AccountType.FSA, balance=90_00)
    res = project(db, cfg, as_of=date(2026, 12, 1))
    assert any("FSA funds expire" in w for w in res.warnings)


def test_investments_page(tmp_path):
    from fastapi.testclient import TestClient

    from cashfloor.web.app import create_app

    cfg = Config()
    cfg.general.data_dir = tmp_path / "data"
    cfg.general.archive_dir = tmp_path / "statements"
    cfg.general.inbox_dir = tmp_path / "inbox"
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    db.upsert_account(Account(id=None, provider="simplefin", external_id="k", name="401(K) PLAN", institution="Fidelity",
                              type=AccountType.INVESTMENT, balance=90_000_00, balance_as_of=datetime.now(),
                              last_synced=datetime.now()))
    db.add_manual_account(name="Health FSA", institution="Fabrikam Benefits", type=AccountType.FSA, balance=412_50)
    db.close()
    client = TestClient(create_app(cfg))
    page = client.get("/investments").text
    assert "401(K) PLAN" in page and "90,000.00" in page
    assert "forfeits 412.50" in page
    assert client.get("/holdings", follow_redirects=False).status_code == 301
