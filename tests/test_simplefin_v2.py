import sqlite3
from datetime import UTC, datetime, timedelta

from cashfloor.db import Database
from cashfloor.models import Account, AccountType
from cashfloor.projection.engine import project
from cashfloor.providers.simplefin import parse_accounts_payload
from cashfloor.sync import run_sync

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

V2_PAYLOAD = {
    "errlist": [
        {"code": "auth.reauth", "msg": "Chase needs you to log in again", "conn_id": "CONN-chase"},
        {"code": "acct.unavailable", "msg": "Temporarily unavailable", "conn_id": "CONN-fid", "account_id": "ACT-fid-1"},
    ],
    "errors": ["Chase needs you to log in again"],
    "connections": [
        {"conn_id": "CONN-chase", "name": "Chase", "org_id": "chase", "org_url": "chase.com",
         "sfin_url": "https://bridge.simplefin.org/simplefin"},
        {"conn_id": "CONN-fid", "name": "Fidelity", "org_id": "fidelity", "sfin_url": "https://bridge.simplefin.org/simplefin"},
    ],
    "accounts": [
        {"id": "ACT-chk", "conn_id": "CONN-chase", "name": "TOTAL CHECKING", "currency": "USD",
         "balance": "2500.00", "available-balance": "2100.00", "balance-date": int((NOW - timedelta(days=5)).timestamp()),
         "extra": {"credit-limit": None, "account-type": "checking"},
         "transactions": [{"id": "T1", "posted": int(NOW.timestamp()), "amount": "-10.00", "description": "COFFEE",
                           "extra": {"category": "Food"}}]},
        {"id": "ACT-fid-1", "conn_id": "CONN-fid", "name": "BROKERAGE", "currency": "USD",
         "balance": "10000.00", "balance-date": int(NOW.timestamp()), "transactions": [],
         "holdings": [{"id": "H1", "symbol": "VOO", "description": "Vanguard S&P 500", "shares": "12.5",
                       "cost_basis": "4000.00", "market_value": "5500.00", "currency": "USD", "created": 1700000000}]},
    ],
}


def test_parse_v2_everything():
    res = parse_accounts_payload(V2_PAYLOAD, "simplefin", as_of=NOW)
    assert {c.conn_id for c in res.connections} == {"CONN-chase", "CONN-fid"}
    chase = next(c for c in res.connections if c.conn_id == "CONN-chase")
    assert chase.error_code == "auth.reauth" and chase.org_url == "chase.com"
    chk, brk = res.accounts
    assert chk.available == 210_000 and chk.spendable == 210_000 and chk.conn_id == "CONN-chase"
    assert chk.extra == {"credit-limit": None, "account-type": "checking"}
    assert chk.sync_error == "Chase needs you to log in again"  # inherited from connection
    assert brk.type == AccountType.INVESTMENT and brk.sync_error == "Temporarily unavailable"
    assert res.transactions[0].extra == {"category": "Food"}
    h = res.holdings["ACT-fid-1"][0]
    assert h.symbol == "VOO" and h.shares == 12_500 and h.market_value == 550_000 and h.cost_basis == 400_000
    assert len(res.structured_errors) == 2 and len(res.errors) == 3  # v1 string + 2 structured
    assert res.raw is V2_PAYLOAD


def test_v1_payload_still_parses_and_synthesizes_connection():
    v1 = {"errors": [], "accounts": [{"org": {"domain": "ally.com", "name": "Ally", "id": "ally"}, "id": "A1",
                                      "name": "Savings", "currency": "USD", "balance": "5.00", "balance-date": 1,
                                      "transactions": []}]}
    res = parse_accounts_payload(v1, "simplefin", as_of=NOW)
    assert res.connections[0].conn_id == "ally" and res.connections[0].name == "Ally"
    assert res.accounts[0].conn_id == "ally" and res.accounts[0].available is None


def test_sync_persists_connections_holdings_raw(cfg, db, monkeypatch):
    from cashfloor.config import ProviderConfig
    from cashfloor.providers import simplefin as sf

    class Fake(sf.SimpleFINProvider):
        def fetch(self, balances_only=False):
            return sf.parse_accounts_payload(V2_PAYLOAD, self.name, as_of=NOW)

    monkeypatch.setattr("cashfloor.providers.build_provider", lambda c, pc: Fake(c, pc))
    monkeypatch.setattr("cashfloor.sync.build_provider", lambda c, pc: Fake(c, pc))
    cfg.provider = [ProviderConfig(name="simplefin", kind="simplefin")]
    s = run_sync(cfg, db, today=NOW.date())
    assert s.providers_ok == ["simplefin"]
    assert s.connection_errors == {"Chase": "auth.reauth: Chase needs you to log in again"}
    assert {c.name: c.error_code for c in db.list_connections()} == {"Chase": "auth.reauth", "Fidelity": None}
    hs = db.list_holdings()
    assert len(hs) == 1 and hs[0].symbol == "VOO" and hs[0].account_id is not None
    raw = list((cfg.general.data_dir / "raw").glob("simplefin-*.json"))
    assert len(raw) == 1 and '"holdings"' in raw[0].read_text()
    chk = next(a for a in db.list_accounts() if a.external_id == "ACT-chk")
    assert chk.extra["account-type"] == "checking" and chk.sync_error.startswith("Chase needs")
    assert db.list_transactions(chk.id)[0].extra == {"category": "Food"}


def test_projection_uses_available_and_warns(cfg, db):
    a = db.upsert_account(Account(id=None, provider="t", external_id="x", name="Chk", institution="B",
                                  type=AccountType.CHECKING, balance=100_000, available=80_000,
                                  balance_as_of=NOW - timedelta(days=6), sync_error=None))
    db.set_account_user_fields(a.id, key="chk", in_pool=True)
    cfg.projection.safety_buffer = 0
    r = project(db, cfg, as_of=NOW.date())
    assert r.starting_balance == 80_000
    assert r.warnings == ["Chk: balance is 6 days old"]
    cfg.projection.use_available_balance = False
    assert project(db, cfg, as_of=NOW.date()).starting_balance == 100_000


def test_migration_from_v1_schema(tmp_path):
    path = tmp_path / "old.sqlite"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('schema_version', '1');
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, provider TEXT NOT NULL, external_id TEXT NOT NULL,
          name TEXT NOT NULL, institution TEXT NOT NULL, type TEXT NOT NULL DEFAULT 'other', key TEXT UNIQUE,
          nickname TEXT, currency TEXT NOT NULL DEFAULT 'USD', last4 TEXT, balance INTEGER NOT NULL DEFAULT 0,
          available INTEGER, credit_limit INTEGER, statement_balance INTEGER, minimum_payment INTEGER,
          due_date TEXT, in_pool INTEGER NOT NULL DEFAULT 0, balance_as_of TEXT, last_synced TEXT,
          active INTEGER NOT NULL DEFAULT 1, UNIQUE (provider, external_id));
        INSERT INTO accounts(provider, external_id, name, institution) VALUES ('t', 'a', 'Old', 'Bank');
        CREATE TABLE transactions (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, external_id TEXT NOT NULL,
          posted TEXT NOT NULL, amount INTEGER NOT NULL, payee TEXT NOT NULL, memo TEXT NOT NULL DEFAULT '',
          pending INTEGER NOT NULL DEFAULT 0, category TEXT, transacted_at TEXT, UNIQUE (account_id, external_id));
    """)
    c.commit()
    c.close()
    db = Database(path)
    assert db.get_account(1).name == "Old" and db.get_account(1).extra is None
    assert db.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
    assert db.list_connections() == [] and db.list_holdings() == []
    db.close()
