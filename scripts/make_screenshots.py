"""Render README screenshots from a throwaway, fully synthetic dataset.

Developer tooling only: builds a temporary cashfloor instance in a temp
directory (never touching your real install), seeds it with invented
institutions, payees and amounts, serves it on a spare port, and
screenshots each page with Playwright into docs/screenshots/.

    python scripts/make_screenshots.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cashfloor.config import Config  # noqa: E402
from cashfloor.db import Database  # noqa: E402
from cashfloor.models import (  # noqa: E402
    Account,
    AccountType,
    Connection,
    Frequency,
    Holding,
    ItemSource,
    ScheduledItem,
    Statement,
    Transaction,
)

PORT = 8491
OUT = ROOT / "docs" / "screenshots"
TODAY = date.today()
NOW = datetime.now(UTC)


def seed(cfg: Config) -> None:
    db = Database(cfg.db_path)
    db.set_setting("projection.safety_buffer", "750")
    db.set_setting("projection.default_payment_account", "checking")

    for c in (
        Connection("simplefin", "CONN-nw", "Northwind Bank", "northwind", "northwind.example", last_seen=NOW),
        Connection("simplefin", "CONN-ct", "Contoso Card Services", "contoso", "contoso.example", last_seen=NOW),
    ):
        db.upsert_connection(c)

    chk = db.upsert_account(Account(id=None, provider="simplefin", external_id="ACT-1", name="EVERYDAY CHECKING",
                                    institution="Northwind Bank", type=AccountType.CHECKING, balance=241_852,
                                    available=227_852, balance_as_of=NOW, last_synced=NOW, conn_id="CONN-nw"))
    db.set_account_user_fields(chk.id, key="checking", nickname="Everyday Checking", in_pool=True, last4="4471")
    sav = db.upsert_account(Account(id=None, provider="simplefin", external_id="ACT-2", name="HIGH YIELD SAVINGS",
                                    institution="Northwind Bank", type=AccountType.SAVINGS, balance=325_000,
                                    balance_as_of=NOW, last_synced=NOW, conn_id="CONN-nw"))
    db.set_account_user_fields(sav.id, key="savings", nickname="Rainy-Day Savings", in_pool=True, last4="5530")
    card = db.upsert_account(Account(id=None, provider="simplefin", external_id="ACT-3", name="CONTOSO PLATINUM",
                                     institution="Contoso Card Services", type=AccountType.CREDIT, balance=-186_420,
                                     statement_balance=142_310, minimum_payment=3_500,
                                     due_date=TODAY + timedelta(days=12), balance_as_of=NOW, last_synced=NOW,
                                     conn_id="CONN-ct"))
    db.set_account_user_fields(card.id, key="contoso", nickname="Contoso Platinum", last4="2208")
    power = db.upsert_account(Account(id=None, provider="inbox", external_id="util:fabrikam", name="Fabrikam Power & Light",
                                      institution="Fabrikam Power", type=AccountType.UTILITY, balance=0,
                                      statement_balance=16_240, due_date=TODAY + timedelta(days=19),
                                      balance_as_of=NOW, last_synced=NOW))
    db.set_account_user_fields(power.id, key="power", nickname="Fabrikam Power", last4="9077")
    # A card the Bridge reports without a due date: projected as an assumed monthly payoff.
    card2 = db.upsert_account(Account(id=None, provider="simplefin", external_id="ACT-4", name="PROSEWARE REWARDS VISA",
                                      institution="Proseware Bank", type=AccountType.CREDIT, balance=-41_275,
                                      balance_as_of=NOW, last_synced=NOW, conn_id="CONN-nw"))
    db.set_account_user_fields(card2.id, key="proseware", nickname="Proseware Rewards", last4="6610", pay_day=20)
    # Investments tab: a 401(k) with holdings, an HSA, and an FSA that will forfeit at year end.
    k401 = db.upsert_account(Account(id=None, provider="simplefin", external_id="ACT-5", name="401(K) SAVINGS PLAN",
                                     institution="Tailspin Retirement Services", type=AccountType.INVESTMENT,
                                     balance=8_742_315, balance_as_of=NOW, last_synced=NOW, conn_id="CONN-nw"))
    db.set_account_user_fields(k401.id, key="401k", nickname="401(k)")
    hsa = db.upsert_account(Account(id=None, provider="simplefin", external_id="ACT-6", name="HEALTH SAVINGS ACCOUNT",
                                    institution="Tailspin Retirement Services", type=AccountType.HSA, balance=612_080,
                                    balance_as_of=NOW, last_synced=NOW, conn_id="CONN-nw"))
    db.set_account_user_fields(hsa.id, key="hsa", nickname="HSA")
    db.add_manual_account(name="Health Care FSA", institution="Tailspin Retirement Services", type=AccountType.FSA,
                          key="fsa", balance=41_250)
    db.replace_holdings(k401.id, [
        Holding(k401.id, "H-1", "TSPX", "Tailspin Total Market Index", 412_337_000, 5_120_000, None, 6_904_210, as_of=NOW),
        Holding(k401.id, "H-2", "TSBX", "Tailspin Bond Index", 182_040_000, 1_700_000, None, 1_838_105, as_of=NOW),
    ])
    # Liabilities tab: a mortgage with a known installment, a car loan whose payment sync learned.
    mort = db.upsert_account(Account(id=None, provider="simplefin", external_id="ACT-7", name="HOME MORTGAGE",
                                     institution="Woodgrove Bank", type=AccountType.MORTGAGE, balance=-28_412_060,
                                     minimum_payment=184_900, due_date=TODAY.replace(day=1) + timedelta(days=32),
                                     balance_as_of=NOW, last_synced=NOW, conn_id="CONN-nw"))
    db.set_account_user_fields(mort.id, key="mortgage", nickname="Mortgage")
    car = db.upsert_account(Account(id=None, provider="simplefin", external_id="ACT-8", name="AUTO LOAN",
                                    institution="Woodgrove Bank", type=AccountType.LOAN, balance=-1_840_000,
                                    balance_as_of=NOW, last_synced=NOW, conn_id="CONN-nw"))
    db.set_account_user_fields(car.id, key="car", nickname="Car loan")
    db.set_payment_auto(car.id, 12, 41_233)
    # An account no provider supplies, kept by hand.
    db.add_manual_account(name="Homeowners Policy", institution="Woodgrove Mutual", type=AccountType.UTILITY,
                          key="home-ins", last4="3382", statement_balance=48_900, due_date=TODAY + timedelta(days=40))

    items = [
        ScheduledItem(None, "Acme Widgets Payroll", chk.id, 284_000, Frequency.BIWEEKLY, TODAY + timedelta(days=4),
                      ItemSource.DETECTED, 0.9, notes="detected from 41 transactions since 2024-03-08"),
        ScheduledItem(None, "Rent — Tailspin Property Mgmt", chk.id, -215_000, Frequency.MONTHLY,
                      TODAY.replace(day=1) + timedelta(days=32), ItemSource.DETECTED, 0.88, weekend_rule="previous",
                      notes="detected from 20 transactions since 2025-03-01"),
        ScheduledItem(None, "Auto Insurance — Woodgrove Mutual", chk.id, -16_886, Frequency.MONTHLY,
                      TODAY + timedelta(days=9), ItemSource.DETECTED, 0.85, notes="detected from 36 transactions"),
        ScheduledItem(None, "Student Loan — Adatum Servicing", chk.id, -20_247, Frequency.MONTHLY,
                      TODAY + timedelta(days=22), ItemSource.DETECTED, 0.82,
                      notes="variable amount; median of last 6 used"),
        ScheduledItem(None, "Fibre Internet — Litware", chk.id, -8_999, Frequency.MONTHLY, TODAY + timedelta(days=15),
                      ItemSource.MANUAL),
        ScheduledItem(None, "Property tax — 2nd half", chk.id, -265_000, Frequency.ONCE, TODAY + timedelta(days=22),
                      ItemSource.MANUAL, notes="one-off"),
        ScheduledItem(None, "Transfer to Savings", chk.id, -50_000, Frequency.MONTHLY, TODAY + timedelta(days=5),
                      ItemSource.TRANSFER, counter_account_id=sav.id),
        ScheduledItem(None, "Contoso Card Autopay", chk.id, -142_310, Frequency.MONTHLY, TODAY + timedelta(days=10),
                      ItemSource.DETECTED, 0.86, counter_account_id=card.id,
                      notes="pays Contoso Platinum; statement balance each month"),
        ScheduledItem(None, "Streaming — Fourth Coffee TV", chk.id, -1_599, Frequency.MONTHLY, TODAY + timedelta(days=2),
                      ItemSource.DETECTED, 0.79, confirmed=False, notes="detected from 7 transactions"),
        ScheduledItem(None, "Gym — Alpine Ski House", chk.id, -4_500, Frequency.MONTHLY, TODAY + timedelta(days=11),
                      ItemSource.DETECTED, 0.74, confirmed=False, notes="detected from 5 transactions"),
    ]
    for it in items:
        db.save_item(it)

    payees = [("ACME WIDGETS PAYROLL", 318_750), ("TAILSPIN PROPERTY MGMT", -215_000), ("WOODGROVE MUTUAL AUTOPAY", -16_886),
              ("GROCERY MART #214", -8_741), ("LITWARE FIBRE", -8_999), ("FOURTH COFFEE TV", -1_599),
              ("CONTOSO CARD AUTOPAY", -142_310), ("ADATUM SERVICING", -20_247), ("MARGIE'S TRAVEL", -32_000)]
    txs = []
    for i in range(60):
        name, amt = payees[i % len(payees)]
        txs.append(Transaction(None, chk.id, f"T{i}", TODAY - timedelta(days=i * 3 + 1), amt, name))
    db.upsert_transactions(txs)

    for n in range(6):
        d = (TODAY.replace(day=8) - timedelta(days=30 * n))
        db.save_statement(Statement(
            None, chk.id, "Northwind Bank", d - timedelta(days=30), d - timedelta(days=1), d, None, None, None,
            241_852 - n * 23_000, "4471", "inbox", f"statements/Northwind-Bank/checking/{d.year}/{d}_Northwind-Bank_checking_checking-statement.pdf",
            f"{'ab' * 8}{n:02d}" * 2, title="CHECKING STATEMENT"))
    for n in range(3):
        d = (TODAY.replace(day=20) - timedelta(days=30 * n))
        db.save_statement(Statement(
            None, card.id, "Contoso Card Services", d - timedelta(days=30), d, d, d + timedelta(days=25),
            142_310 - n * 9_000, 3_500, -(186_420 - n * 9_000), "2208", "inbox",
            f"statements/Contoso-Card-Services/contoso/{d.year}/{d}_Contoso-Card-Services_contoso_statement.pdf",
            f"{'cd' * 8}{n:02d}" * 2, title="Monthly Statement"))
    db.log_sync_finish(db.log_sync_start("simplefin"), True, "4 accounts, 61 tx, 0 files")
    db.log_sync_finish(db.log_sync_start("inbox"), True, "0 accounts, 0 tx, 2 files")
    db.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="cashfloor-shots-"))
    cfg_text = f'''
[general]
data_dir = "{(tmp / 'data').as_posix()}"
archive_dir = "{(tmp / 'statements').as_posix()}"
inbox_dir = "{(tmp / 'inbox').as_posix()}"
timezone = "America/Los_Angeles"
[projection]
default_payment_account = "checking"
[web]
port = {PORT}
'''
    (tmp / "config.toml").write_text(cfg_text)
    cfg = Config.model_validate({"general": {"data_dir": tmp / "data", "archive_dir": tmp / "statements", "inbox_dir": tmp / "inbox"}})
    cfg.ensure_dirs()
    seed(cfg)

    OUT.mkdir(parents=True, exist_ok=True)
    srv = subprocess.Popen([sys.executable, "-m", "cashfloor.cli", "--config", str(tmp / "config.toml"), "serve",
                            "--no-scheduler", "--port", str(PORT)], cwd=str(ROOT / "src"),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(3)
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=1.5, color_scheme="dark")
            for path, name in (("/", "dashboard"), ("/accounts", "accounts"), ("/items", "scheduled"),
                               ("/statements", "statements"), ("/settings", "settings")):
                page.goto(f"http://127.0.0.1:{PORT}{path}")
                page.wait_for_timeout(300)
                page.screenshot(path=str(OUT / f"{name}.png"), full_page=(name != "dashboard"))
            b.close()
    finally:
        srv.terminate()
    print(f"screenshots in {OUT}")


if __name__ == "__main__":
    main()
