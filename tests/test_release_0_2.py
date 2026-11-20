"""0.2.0: pays-account links, assumed card payoff, manual accounts, amount parsing."""

from datetime import date, datetime, timedelta

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


def test_occurrence_that_already_posted_is_not_counted_again(tmp_path):
    """Payroll posted this morning; the balance includes it; the item's next
    date is today.  Day 0 must not add it a second time, and the following
    payday must still count."""
    from cashfloor.models import Transaction

    db = _db(tmp_path)
    today = date(2026, 11, 19)
    chk = _checking(db, bal=5864_79)
    db.upsert_transactions([Transaction(None, chk.id, "p1", today, 3193_00, "ACME WIDGETS PAYROLL")])
    db.save_item(ScheduledItem(id=None, name="Acme Widgets Payroll", account_id=chk.id, amount=3193_00,
                               frequency=Frequency.BIWEEKLY, next_date=today, source=ItemSource.DETECTED,
                               confirmed=True, payee_pattern="acme widgets payroll"))
    res = project(db, _cfg(), as_of=today)
    by_day = {d.day: d for d in res.days}
    assert by_day[today].balance == 5864_79 and by_day[today].events == []
    assert by_day[today + timedelta(days=14)].balance == 5864_79 + 3193_00


def test_early_posting_and_unrelated_posting(tmp_path):
    from cashfloor.models import Transaction

    db = _db(tmp_path)
    today = date(2026, 11, 19)
    chk = _checking(db, bal=1000_00)
    # rent drafted a day early (yesterday) for a due date of tomorrow -> skip
    db.upsert_transactions([Transaction(None, chk.id, "r1", today - timedelta(days=1), -600_00, "TAILSPIN PROPERTY")])
    db.save_item(ScheduledItem(id=None, name="Rent", account_id=chk.id, amount=-600_00, frequency=Frequency.MONTHLY,
                               next_date=today + timedelta(days=1), source=ItemSource.MANUAL, confirmed=True))
    # a same-amount posting today does NOT cancel a bill due in ten days
    db.upsert_transactions([Transaction(None, chk.id, "g1", today, -89_99, "GROCERY MART")])
    db.save_item(ScheduledItem(id=None, name="Internet", account_id=chk.id, amount=-89_99, frequency=Frequency.MONTHLY,
                               next_date=today + timedelta(days=10), source=ItemSource.MANUAL, confirmed=True))
    res = project(db, _cfg(), as_of=today)
    by_day = {d.day: d for d in res.days}
    assert by_day[today + timedelta(days=1)].events == []
    assert by_day[today + timedelta(days=10)].events == ["Internet: -89.99"] or \
        any("Internet" in e for e in by_day[today + timedelta(days=10)].events)


def test_sync_roll_skips_posted_occurrence(tmp_path):
    from cashfloor.models import Transaction
    from cashfloor.sync import run_sync

    cfg = _cfg()
    cfg.general.data_dir = tmp_path / "data"
    cfg.general.archive_dir = tmp_path / "statements"
    cfg.general.inbox_dir = tmp_path / "inbox"
    cfg.provider = []
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    today = date(2026, 11, 19)
    chk = _checking(db)
    db.upsert_transactions([Transaction(None, chk.id, "p1", today, 3193_00, "ACME WIDGETS PAYROLL")])
    it = db.save_item(ScheduledItem(id=None, name="Acme Widgets Payroll", account_id=chk.id, amount=3193_00,
                                    frequency=Frequency.BIWEEKLY, next_date=today, source=ItemSource.DETECTED,
                                    confirmed=True, payee_pattern="acme widgets payroll"))
    run_sync(cfg, db, today=today)
    assert db.get_item(it.id).next_date == today + timedelta(days=14)


def test_already_posted_matches_statement_alias_and_exact_amount(tmp_path):
    """Detected items carry the statement wording; the live feed words the same
    deposit differently.  The alias learned at import bridges them, and an
    exact-amount hit counts even with no alias at all."""
    from cashfloor.models import Transaction
    from cashfloor.projection.engine import already_posted

    cfg = _cfg()
    today = date(2026, 11, 19)
    it = ScheduledItem(id=1, name="Payroll", account_id=7, amount=3193_00, frequency=Frequency.BIWEEKLY,
                       next_date=today, source=ItemSource.DETECTED, confirmed=True, payee_pattern="acme widgets payroll")
    live = Transaction(None, 7, "x", today, 3193_00, "DEPOSIT ACME WIDGETS INC DIRECT DEP 1234567")
    assert already_posted(cfg, it, today, today, [live])  # exact amount, wording irrelevant
    off = Transaction(None, 7, "y", today, 3100_00, "DEPOSIT ACME WIDGETS INC DIRECT DEP 1234567")
    assert not already_posted(cfg, it, today, today, [off])  # 3% off and payee key differs, no alias
    aliases = {(7, "acme widgets inc"): "acme widgets payroll"}
    assert already_posted(cfg, it, today, today, [off], aliases)  # alias bridges the wording
    pend = Transaction(None, 7, "z", today, 3193_00, "ACME WIDGETS PAYROLL", pending=True)
    assert not already_posted(cfg, it, today, today, [pend])
    assert already_posted(cfg, it, today, today, [pend], None, {7})


def test_inflow_due_today_without_posting_is_assumed_landed(tmp_path):
    """Kendall's case: the bank's balance already includes this morning's
    payroll but SimpleFIN hasn't listed the transaction yet.  Inflows due on
    or before the balance date are taken as in the balance; outflows are not."""
    db = _db(tmp_path)
    today = date(2026, 11, 19)
    chk = _checking(db, bal=5864_79)  # balance_as_of = now (today)
    db.save_item(ScheduledItem(id=None, name="Payroll", account_id=chk.id, amount=3193_00, frequency=Frequency.BIWEEKLY,
                               next_date=today, source=ItemSource.DETECTED, confirmed=True, payee_pattern="acme"))
    db.save_item(ScheduledItem(id=None, name="Car loan via Zelle", account_id=chk.id, amount=-675_00,
                               frequency=Frequency.MONTHLY, next_date=today, source=ItemSource.DETECTED, confirmed=True,
                               payee_pattern="zelle transfer to"))
    res = project(db, _cfg(), as_of=date.today() if date.today() == today else today)
    day0 = res.days[0]
    assert all("Payroll" not in e for e in day0.events)
    assert any("Car loan" in e for e in day0.events)
    # switched off: payroll is counted again
    res = project(db, _cfg(assume_inflows_landed=False), as_of=today)
    assert any("Payroll" in e for e in res.days[0].events)


def test_mark_paid_drops_occurrence_and_rolls_item(tmp_path):
    from fastapi.testclient import TestClient

    from cashfloor.web.app import create_app

    cfg = Config()
    cfg.general.data_dir = tmp_path / "data"
    cfg.general.archive_dir = tmp_path / "statements"
    cfg.general.inbox_dir = tmp_path / "inbox"
    cfg.projection.safety_buffer = 0.0
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    today = date.today()
    chk = _checking(db, bal=1000_00)
    it = db.save_item(ScheduledItem(id=None, name="Car loan via Zelle", account_id=chk.id, amount=-675_00,
                                    frequency=Frequency.MONTHLY, next_date=today, source=ItemSource.MANUAL,
                                    confirmed=True))
    far = db.save_item(ScheduledItem(id=None, name="Rent", account_id=chk.id, amount=-500_00, frequency=Frequency.MONTHLY,
                                     next_date=today + timedelta(days=10), source=ItemSource.MANUAL, confirmed=True))
    card = _card(db, due_date=today + timedelta(days=2), statement_balance=120_00)
    client = TestClient(create_app(cfg))
    page = client.get("/").text
    assert page.count(">Paid<") == 2  # the Zelle item and the card due in 2 days; rent (10 days out) has none
    r = client.post("/paid", data={"kind": "item", "ref_id": str(it.id), "day": today.isoformat()},
                    follow_redirects=False)
    assert r.status_code == 303
    res = project(db, cfg, as_of=today)
    assert res.days[0].balance == 1000_00 and res.days[0].events == []
    assert db.get_item(it.id).next_date > today
    assert "Undo" in client.get("/").text
    # the card's due event can be marked paid too
    client.post("/paid", data={"kind": "account", "ref_id": str(card.id), "day": (today + timedelta(days=2)).isoformat()})
    res = project(db, cfg, as_of=today)
    assert all("Platinum" not in e for e in res.days[2].events)
    # undo restores the item occurrence
    client.post("/paid", data={"kind": "item", "ref_id": str(it.id), "day": today.isoformat(), "undo": "true"})
    assert db.get_item(it.id).next_date == today
    assert any("Car loan" in e for e in project(db, cfg, as_of=today).days[0].events)
    assert far.id is not None


def test_pay_day_inferred_from_card_payment_history(tmp_path):
    from cashfloor.models import Transaction
    from cashfloor.projection.payday import infer_pay_day
    from cashfloor.sync import run_sync

    today = date(2026, 11, 19)
    db = _db(tmp_path)
    card = _card(db)
    txs = []
    for m, extra in ((6, False), (7, False), (8, True), (9, False), (10, False)):
        txs.append(Transaction(None, card.id, f"p{m}", date(2026, m, 17), 180_00, "PAYMENT - THANK YOU"))
        if extra:
            txs.append(Transaction(None, card.id, f"x{m}", date(2026, m, 3), 50_00, "ONLINE PAYMENT"))  # early extra
        txs.append(Transaction(None, card.id, f"c{m}", date(2026, m, 9), -42_10, "GROCERY MART"))  # a charge
    db.upsert_transactions(txs)
    assert infer_pay_day(card, db.list_transactions(), today) == 17
    # projection uses it; a typed pay_day overrides it
    cfg = _cfg()
    cfg.general.data_dir = tmp_path / "data"
    cfg.general.archive_dir = tmp_path / "statements"
    cfg.general.inbox_dir = tmp_path / "inbox"
    cfg.provider = []
    cfg.ensure_dirs()
    _checking(db)
    run_sync(cfg, db, today=today)
    assert db.get_account(card.id).pay_day_auto == 17
    res = project(db, cfg, as_of=today)
    days = [d.day for d in res.days for e in d.events if "assumed payoff" in e]
    assert days[0] == date(2026, 12, 17) and "day seen in history" in res.days[28].events[0]
    db.set_account_user_fields(card.id, pay_day=5)
    res = project(db, cfg, as_of=today)
    assert [d.day for d in res.days for e in d.events if "assumed payoff" in e][0] == date(2026, 12, 5)


def test_mortgage_balance_is_never_an_event(tmp_path):
    """A mortgage connected with its whole balance and a statement whose
    'amount due' was parsed as the balance: the projection must subtract the
    monthly installment, not the loan, and not three times."""
    db = _db(tmp_path)
    today = date(2026, 11, 19)
    _checking(db, bal=6000_00)
    m = db.upsert_account(Account(id=None, provider="simplefin", external_id="m", name="HOME MORTGAGE",
                                  institution="Woodgrove Bank", type=AccountType.MORTGAGE, balance=-285_000_00,
                                  statement_balance=285_000_00, due_date=date(2026, 12, 1),
                                  balance_as_of=datetime.now(), last_synced=datetime.now()))
    res = project(db, _cfg(horizon_days=90), as_of=today)
    assert res.floor == 6000_00  # nothing sane known -> not projected, but warned
    assert any("monthly payment unknown" in w for w in res.warnings)
    # a real payment figure (min / payment field) -> one installment per month on the due date
    db.set_account_due(m.id, due_date=date(2026, 12, 1), statement_balance=285_000_00, minimum_payment=1_850_00)
    res = project(db, _cfg(horizon_days=90), as_of=today)
    hits = [(d.day, e) for d in res.days for e in d.events if "MORTGAGE" in e.upper()]
    assert [h[0] for h in hits] == [date(2026, 12, 1), date(2027, 1, 1), date(2027, 2, 1)]
    assert res.floor == 6000_00 - 3 * 1_850_00 and "installment" in hits[0][1]


def test_loan_payment_inferred_from_history(tmp_path):
    from cashfloor.models import Transaction
    from cashfloor.projection.payday import infer_payment

    db = _db(tmp_path)
    today = date(2026, 11, 19)
    car = db.upsert_account(Account(id=None, provider="simplefin", external_id="c", name="AUTO LOAN",
                                    institution="Woodgrove Bank", type=AccountType.LOAN, balance=-18_400_00,
                                    balance_as_of=datetime.now(), last_synced=datetime.now()))
    txs = [Transaction(None, car.id, f"p{m}", date(2026, m, 12), 412_33, "PAYMENT - THANK YOU") for m in range(6, 11)]
    txs.append(Transaction(None, car.id, "x", date(2026, 8, 20), 100_00, "ONLINE PAYMENT"))  # extra one
    db.upsert_transactions(txs)
    assert infer_payment(car, txs, today) == (12, 412_33)
    db.set_payment_auto(car.id, 12, 412_33)
    _checking(db, bal=5000_00)
    res = project(db, _cfg(horizon_days=60), as_of=today)
    hits = [(d.day, e) for d in res.days for e in d.events if "AUTO LOAN" in e.upper()]
    assert [h[0] for h in hits] == [date(2026, 12, 12), date(2027, 1, 12)] and "seen in history" in hits[0][1]
    assert res.floor == 5000_00 - 2 * 412_33


def test_mortgage_filed_as_credit_is_retyped_and_never_assumed_paid_off(tmp_path):
    """Kendall's servicer: institution '<X> Mortgage', accounts named
    'Account (1234)', guessed as credit -> whole balance 'assumed payoff'."""
    from decimal import Decimal

    from cashfloor.providers.simplefin import guess_account_type

    assert guess_account_type("Account (1234)", Decimal("-531482.63"), institution="Contoso Mortgage") == AccountType.MORTGAGE
    db = _db(tmp_path)
    today = date(2026, 11, 19)
    _checking(db, bal=6000_00)
    a = db.upsert_account(Account(id=None, provider="simplefin", external_id="m1", name="Account (1234)",
                                  institution="Contoso Mortgage", type=AccountType.CREDIT, balance=-563_900_00,
                                  balance_as_of=datetime.now(), last_synced=datetime.now()))
    # even before the retype, the cap stops the absurd payoff and explains itself
    res = project(db, _cfg(), as_of=today)
    assert res.floor == 6000_00 and any("too much to assume" in w for w in res.warnings)
    db.conn.execute("DELETE FROM meta WHERE key='migrated:retype_mortgage2'")
    db.conn.commit()
    db.migrate()
    assert db.get_account(a.id).type == AccountType.MORTGAGE
    res = project(db, _cfg(), as_of=today)
    assert res.floor == 6000_00 and any("monthly payment unknown" in w for w in res.warnings)


def test_later_card_payments_do_not_repeat_an_unusual_balance(tmp_path):
    """Balance is temporarily high; next month's payment clears it, after
    that the card pays its usual amount, not the high balance again."""
    from cashfloor.models import Transaction
    from cashfloor.projection.payday import infer_spend

    db = _db(tmp_path)
    today = date(2026, 11, 19)
    _checking(db, bal=10_000_00)
    card = _card(db, balance=-1_938_97)  # unusually high
    db.set_payment_auto(card.id, 2, 65_00)  # has been paying ~$65 (minimums) lately
    res = project(db, _cfg(horizon_days=90), as_of=today)
    hits = [(d.day, e) for d in res.days for e in d.events if "Platinum" in e]
    assert hits[0][0] == date(2026, 12, 2) and "-1,938.97" in hits[0][1] and "assumed payoff" in hits[0][1]
    assert "-65.00" in hits[1][1] and "usual payment" in hits[1][1]
    assert "-65.00" in hits[2][1]
    assert res.floor == 10_000_00 - 1_938_97 - 2 * 65_00
    # "repeat" restores the old behaviour; "minimum" prefers the issuer's figure
    res = project(db, _cfg(horizon_days=90, later_card_payments="repeat"), as_of=today)
    assert res.floor == 10_000_00 - 3 * 1_938_97
    db.set_account_due(card.id, due_date=date(2026, 12, 2), statement_balance=1_938_97, minimum_payment=40_00)
    res = project(db, _cfg(horizon_days=90, later_card_payments="minimum"), as_of=today)
    hits = [e for d in res.days for e in d.events if "Platinum" in e]
    assert "-1,938.97" in hits[0] and "-40.00" in hits[1] and "minimum" in hits[1]
    # usual spend from the card's own charges
    txs = []
    for m in range(5, 11):
        txs += [Transaction(None, card.id, f"a{m}", date(2026, m, 4), -120_00, "GROCERY MART"),
                Transaction(None, card.id, f"b{m}", date(2026, m, 18), -80_00, "FUEL STOP")]
    db.upsert_transactions(txs)
    assert infer_spend(card, txs, today) == 200_00
