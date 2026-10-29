from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from cashfloor.config import Config
from cashfloor.db import Database
from cashfloor.models import Account, AccountType


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config()
    c.general.data_dir = tmp_path / "data"
    c.general.archive_dir = tmp_path / "statements"
    c.general.inbox_dir = tmp_path / "inbox"
    c.projection.safety_buffer = 500.0
    c.projection.default_payment_account = "chk"
    c.ensure_dirs()
    return c


@pytest.fixture
def db(cfg: Config) -> Database:
    d = Database(cfg.db_path)
    yield d
    d.close()


@pytest.fixture
def accounts(db: Database) -> dict[str, Account]:
    now = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    chk = db.upsert_account(Account(id=None, provider="t", external_id="chk", name="Checking",
                                    institution="Test Bank", type=AccountType.CHECKING, balance=250_000,
                                    last4="1234", balance_as_of=now))
    db.set_account_user_fields(chk.id, key="chk", in_pool=True)
    sav = db.upsert_account(Account(id=None, provider="t", external_id="sav", name="Savings",
                                    institution="Test Bank", type=AccountType.SAVINGS, balance=100_000,
                                    last4="5678", balance_as_of=now))
    db.set_account_user_fields(sav.id, key="sav", in_pool=True)
    card = db.upsert_account(Account(id=None, provider="t", external_id="card", name="Visa",
                                     institution="Chase", type=AccountType.CREDIT, balance=-80_000,
                                     last4="9999", statement_balance=60_000, minimum_payment=3_500,
                                     due_date=date(2026, 11, 15), balance_as_of=now))
    db.set_account_user_fields(card.id, key="visa")
    return {"chk": db.get_account(chk.id), "sav": db.get_account(sav.id), "card": db.get_account(card.id)}
