"""SQLite persistence.  Deliberately plain sqlite3 — no ORM — so the schema is
readable and the database is easy to inspect with any SQLite tool.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from .models import (
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

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id                INTEGER PRIMARY KEY,
    provider          TEXT NOT NULL,
    external_id       TEXT NOT NULL,
    name              TEXT NOT NULL,
    institution       TEXT NOT NULL,
    type              TEXT NOT NULL DEFAULT 'other',
    key               TEXT UNIQUE,
    nickname          TEXT,
    currency          TEXT NOT NULL DEFAULT 'USD',
    last4             TEXT,
    balance           INTEGER NOT NULL DEFAULT 0,
    available         INTEGER,
    credit_limit      INTEGER,
    statement_balance INTEGER,
    minimum_payment   INTEGER,
    due_date          TEXT,
    in_pool           INTEGER NOT NULL DEFAULT 0,
    balance_as_of     TEXT,
    last_synced       TEXT,
    active            INTEGER NOT NULL DEFAULT 1,
    UNIQUE (provider, external_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    external_id   TEXT NOT NULL,
    posted        TEXT NOT NULL,
    amount        INTEGER NOT NULL,
    payee         TEXT NOT NULL,
    memo          TEXT NOT NULL DEFAULT '',
    pending       INTEGER NOT NULL DEFAULT 0,
    category      TEXT,
    transacted_at TEXT,
    UNIQUE (account_id, external_id)
);
CREATE INDEX IF NOT EXISTS ix_tx_account_posted ON transactions(account_id, posted);

CREATE TABLE IF NOT EXISTS balance_history (
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    day         TEXT NOT NULL,
    balance     INTEGER NOT NULL,
    PRIMARY KEY (account_id, day)
);

CREATE TABLE IF NOT EXISTS scheduled_items (
    id                 INTEGER PRIMARY KEY,
    name               TEXT NOT NULL,
    account_id         INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    amount             INTEGER NOT NULL,
    frequency          TEXT NOT NULL,
    next_date          TEXT NOT NULL,
    source             TEXT NOT NULL DEFAULT 'manual',
    confidence         REAL NOT NULL DEFAULT 1.0,
    active             INTEGER NOT NULL DEFAULT 1,
    confirmed          INTEGER NOT NULL DEFAULT 1,
    end_date           TEXT,
    counter_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    payee_pattern      TEXT,
    notes              TEXT NOT NULL DEFAULT '',
    weekend_rule       TEXT NOT NULL DEFAULT 'none'
);

CREATE TABLE IF NOT EXISTS statements (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    institution     TEXT NOT NULL,
    period_start    TEXT,
    period_end      TEXT,
    statement_date  TEXT,
    due_date        TEXT,
    amount_due      INTEGER,
    minimum_due     INTEGER,
    closing_balance INTEGER,
    last4           TEXT,
    source_path     TEXT NOT NULL,
    archived_path   TEXT,
    sha256          TEXT NOT NULL UNIQUE,
    parsed_ok       INTEGER NOT NULL DEFAULT 1,
    needs_review    INTEGER NOT NULL DEFAULT 0,
    imported_at     TEXT NOT NULL,
    title           TEXT
);

CREATE TABLE IF NOT EXISTS connections (
    provider   TEXT NOT NULL,
    conn_id    TEXT NOT NULL,
    name       TEXT NOT NULL,
    org_id     TEXT,
    org_url    TEXT,
    sfin_url   TEXT,
    error_code TEXT,
    error_msg  TEXT,
    last_seen  TEXT,
    PRIMARY KEY (provider, conn_id)
);

CREATE TABLE IF NOT EXISTS holdings (
    account_id     INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    external_id    TEXT NOT NULL,
    symbol         TEXT,
    description    TEXT NOT NULL DEFAULT '',
    shares         INTEGER,
    cost_basis     INTEGER,
    purchase_price INTEGER,
    market_value   INTEGER,
    currency       TEXT NOT NULL DEFAULT 'USD',
    created        TEXT,
    as_of          TEXT,
    PRIMARY KEY (account_id, external_id)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY,
    provider    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    message     TEXT
);
"""


def _d(v: date | None) -> str | None:
    return v.isoformat() if v else None


def _dt(v: datetime | None) -> str | None:
    return v.isoformat(timespec="seconds") if v else None


def _pd(v: str | None) -> date | None:
    return date.fromisoformat(v) if v else None


def _pdt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, detect_types=0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    # -- lifecycle ---------------------------------------------------------
    # Columns added after v1; applied to existing databases on open.
    _ADDED_COLUMNS = {
        "accounts": [("conn_id", "TEXT"), ("extra", "TEXT"), ("sync_error", "TEXT")],
        "transactions": [("extra", "TEXT")],
        "statements": [("title", "TEXT")],
    }

    def migrate(self) -> None:
        self.conn.executescript(SCHEMA)
        for table, cols in self._ADDED_COLUMNS.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -- settings (GUI overrides of config.toml) ---------------------------
    def get_settings(self) -> dict[str, str]:
        return {
            r["key"][4:]: r["value"]
            for r in self.conn.execute("SELECT key, value FROM meta WHERE key LIKE 'set:%'")
        }

    def set_setting(self, key: str, value: str | None) -> None:
        with self.tx() as c:
            if value is None or value == "":
                c.execute("DELETE FROM meta WHERE key=?", (f"set:{key}",))
            else:
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (f"set:{key}", value))

    # -- accounts ----------------------------------------------------------
    @staticmethod
    def _row_to_account(r: sqlite3.Row) -> Account:
        return Account(
            id=r["id"],
            provider=r["provider"],
            external_id=r["external_id"],
            name=r["name"],
            institution=r["institution"],
            type=AccountType(r["type"]),
            key=r["key"],
            nickname=r["nickname"],
            currency=r["currency"],
            last4=r["last4"],
            balance=r["balance"],
            available=r["available"],
            credit_limit=r["credit_limit"],
            statement_balance=r["statement_balance"],
            minimum_payment=r["minimum_payment"],
            due_date=_pd(r["due_date"]),
            in_pool=bool(r["in_pool"]),
            balance_as_of=_pdt(r["balance_as_of"]),
            last_synced=_pdt(r["last_synced"]),
            active=bool(r["active"]),
            conn_id=r["conn_id"],
            extra=json.loads(r["extra"]) if r["extra"] else None,
            sync_error=r["sync_error"],
        )

    def upsert_account(self, a: Account) -> Account:
        """Insert or update by (provider, external_id).  User-set fields
        (key, nickname, type, in_pool) are preserved on update."""
        with self.tx() as c:
            existing = c.execute(
                "SELECT * FROM accounts WHERE provider=? AND external_id=?",
                (a.provider, a.external_id),
            ).fetchone()
            if existing is None:
                cur = c.execute(
                    """INSERT INTO accounts(provider, external_id, name, institution, type, key,
                       nickname, currency, last4, balance, available, credit_limit,
                       statement_balance, minimum_payment, due_date, in_pool,
                       balance_as_of, last_synced, active, conn_id, extra, sync_error)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        a.provider, a.external_id, a.name, a.institution, a.type.value, a.key,
                        a.nickname, a.currency, a.last4, a.balance, a.available, a.credit_limit,
                        a.statement_balance, a.minimum_payment, _d(a.due_date), int(a.in_pool),
                        _dt(a.balance_as_of), _dt(a.last_synced), int(a.active),
                        a.conn_id, json.dumps(a.extra) if a.extra else None, a.sync_error,
                    ),
                )
                a.id = cur.lastrowid
            else:
                a.id = existing["id"]
                c.execute(
                    """UPDATE accounts SET name=?, institution=?, currency=?,
                       last4=COALESCE(?, last4), balance=?, available=?,
                       credit_limit=COALESCE(?, credit_limit),
                       statement_balance=COALESCE(?, statement_balance),
                       minimum_payment=COALESCE(?, minimum_payment),
                       due_date=COALESCE(?, due_date),
                       balance_as_of=?, last_synced=?, active=1,
                       conn_id=COALESCE(?, conn_id), extra=?, sync_error=?
                       WHERE id=?""",
                    (
                        a.name, a.institution, a.currency, a.last4, a.balance, a.available,
                        a.credit_limit, a.statement_balance, a.minimum_payment, _d(a.due_date),
                        _dt(a.balance_as_of), _dt(a.last_synced),
                        a.conn_id, json.dumps(a.extra) if a.extra else None, a.sync_error, a.id,
                    ),
                )
            if a.balance_as_of:
                c.execute(
                    "INSERT OR REPLACE INTO balance_history(account_id, day, balance) VALUES (?,?,?)",
                    (a.id, a.balance_as_of.date().isoformat(), a.balance),
                )
        return self.get_account(a.id)  # type: ignore[arg-type]

    def set_account_user_fields(
        self,
        account_id: int,
        *,
        key: str | None = None,
        nickname: str | None = None,
        type: AccountType | None = None,
        in_pool: bool | None = None,
        active: bool | None = None,
        last4: str | None = None,
    ) -> None:
        sets, vals = [], []
        if last4 is not None:
            sets.append("last4=?"); vals.append(last4.strip()[-4:] or None)
        if key is not None:
            sets.append("key=?"); vals.append(key or None)
        if nickname is not None:
            sets.append("nickname=?"); vals.append(nickname or None)
        if type is not None:
            sets.append("type=?"); vals.append(type.value)
        if in_pool is not None:
            sets.append("in_pool=?"); vals.append(int(in_pool))
        if active is not None:
            sets.append("active=?"); vals.append(int(active))
        if not sets:
            return
        vals.append(account_id)
        with self.tx() as c:
            c.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE id=?", vals)

    def set_account_due(
        self,
        account_id: int,
        *,
        due_date: date | None,
        statement_balance: int | None,
        minimum_payment: int | None,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE accounts SET due_date=?, statement_balance=?, minimum_payment=? WHERE id=?",
                (_d(due_date), statement_balance, minimum_payment, account_id),
            )

    def get_account(self, account_id: int) -> Account | None:
        r = self.conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return self._row_to_account(r) if r else None

    def get_account_by_key(self, key: str) -> Account | None:
        r = self.conn.execute("SELECT * FROM accounts WHERE key=?", (key,)).fetchone()
        return self._row_to_account(r) if r else None

    def find_account(self, *, institution: str | None = None, last4: str | None = None) -> Account | None:
        q = "SELECT * FROM accounts WHERE active=1"
        vals: list = []
        if last4:
            q += " AND last4=?"; vals.append(last4)
        if institution:
            q += " AND lower(institution) LIKE ?"; vals.append(f"%{institution.lower()}%")
        rows = self.conn.execute(q, vals).fetchall()
        return self._row_to_account(rows[0]) if len(rows) == 1 else None

    def list_accounts(self, active_only: bool = True) -> list[Account]:
        q = "SELECT * FROM accounts" + (" WHERE active=1" if active_only else "")
        q += " ORDER BY institution, name"
        return [self._row_to_account(r) for r in self.conn.execute(q)]

    # -- transactions ------------------------------------------------------
    def upsert_transactions(self, txs: list[Transaction]) -> int:
        n = 0
        with self.tx() as c:
            for t in txs:
                cur = c.execute(
                    """INSERT INTO transactions(account_id, external_id, posted, amount, payee,
                       memo, pending, category, transacted_at, extra)
                       VALUES (?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(account_id, external_id) DO UPDATE SET
                         posted=excluded.posted, amount=excluded.amount, payee=excluded.payee,
                         memo=excluded.memo, pending=excluded.pending, extra=excluded.extra""",
                    (
                        t.account_id, t.external_id, t.posted.isoformat(), t.amount, t.payee,
                        t.memo, int(t.pending), t.category, _dt(t.transacted_at),
                        json.dumps(t.extra) if t.extra else None,
                    ),
                )
                n += cur.rowcount
        return n

    def earliest_live_transaction(self, account_id: int) -> date | None:
        """Earliest transaction from a live feed (SimpleFIN/OFX/CSV), i.e. not
        extracted from a statement PDF.  Statement imports stop here."""
        r = self.conn.execute(
            "SELECT MIN(posted) FROM transactions WHERE account_id=? AND external_id NOT LIKE 'stmt:%'",
            (account_id,),
        ).fetchone()
        return date.fromisoformat(r[0]) if r and r[0] else None

    def list_transactions(
        self, account_id: int | None = None, since: date | None = None, limit: int | None = None
    ) -> list[Transaction]:
        q = "SELECT * FROM transactions WHERE 1=1"
        vals: list = []
        if account_id is not None:
            q += " AND account_id=?"; vals.append(account_id)
        if since is not None:
            q += " AND posted>=?"; vals.append(since.isoformat())
        q += " ORDER BY posted DESC, id DESC"
        if limit:
            q += f" LIMIT {int(limit)}"
        return [
            Transaction(
                id=r["id"], account_id=r["account_id"], external_id=r["external_id"],
                posted=date.fromisoformat(r["posted"]), amount=r["amount"], payee=r["payee"],
                memo=r["memo"], pending=bool(r["pending"]), category=r["category"],
                transacted_at=_pdt(r["transacted_at"]),
                extra=json.loads(r["extra"]) if r["extra"] else None,
            )
            for r in self.conn.execute(q, vals)
        ]

    # -- connections -------------------------------------------------------
    def upsert_connection(self, c: Connection) -> None:
        with self.tx() as x:
            x.execute(
                """INSERT INTO connections(provider, conn_id, name, org_id, org_url, sfin_url,
                   error_code, error_msg, last_seen) VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(provider, conn_id) DO UPDATE SET name=excluded.name,
                     org_id=COALESCE(excluded.org_id, org_id), org_url=COALESCE(excluded.org_url, org_url),
                     sfin_url=COALESCE(excluded.sfin_url, sfin_url), error_code=excluded.error_code,
                     error_msg=excluded.error_msg, last_seen=excluded.last_seen""",
                (c.provider, c.conn_id, c.name, c.org_id, c.org_url, c.sfin_url,
                 c.error_code, c.error_msg, _dt(c.last_seen)),
            )

    def list_connections(self) -> list[Connection]:
        return [
            Connection(provider=r["provider"], conn_id=r["conn_id"], name=r["name"], org_id=r["org_id"],
                       org_url=r["org_url"], sfin_url=r["sfin_url"], error_code=r["error_code"],
                       error_msg=r["error_msg"], last_seen=_pdt(r["last_seen"]))
            for r in self.conn.execute("SELECT * FROM connections ORDER BY name")
        ]

    # -- holdings ----------------------------------------------------------
    def replace_holdings(self, account_id: int, holdings: list[Holding]) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM holdings WHERE account_id=?", (account_id,))
            for h in holdings:
                c.execute(
                    """INSERT INTO holdings(account_id, external_id, symbol, description, shares,
                       cost_basis, purchase_price, market_value, currency, created, as_of)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (account_id, h.external_id, h.symbol, h.description, h.shares, h.cost_basis,
                     h.purchase_price, h.market_value, h.currency, _dt(h.created), _dt(h.as_of)),
                )

    def list_holdings(self, account_id: int | None = None) -> list[Holding]:
        q = "SELECT * FROM holdings" + (" WHERE account_id=?" if account_id is not None else "")
        rows = self.conn.execute(q, (account_id,) if account_id is not None else ())
        return [
            Holding(account_id=r["account_id"], external_id=r["external_id"], symbol=r["symbol"],
                    description=r["description"], shares=r["shares"], cost_basis=r["cost_basis"],
                    purchase_price=r["purchase_price"], market_value=r["market_value"],
                    currency=r["currency"], created=_pdt(r["created"]), as_of=_pdt(r["as_of"]))
            for r in rows
        ]

    # -- scheduled items ---------------------------------------------------
    @staticmethod
    def _row_to_item(r: sqlite3.Row) -> ScheduledItem:
        return ScheduledItem(
            id=r["id"], name=r["name"], account_id=r["account_id"], amount=r["amount"],
            frequency=Frequency(r["frequency"]), next_date=date.fromisoformat(r["next_date"]),
            source=ItemSource(r["source"]), confidence=r["confidence"], active=bool(r["active"]),
            confirmed=bool(r["confirmed"]), end_date=_pd(r["end_date"]),
            counter_account_id=r["counter_account_id"], payee_pattern=r["payee_pattern"],
            notes=r["notes"], weekend_rule=r["weekend_rule"],
        )

    def save_item(self, it: ScheduledItem) -> ScheduledItem:
        with self.tx() as c:
            if it.id is None:
                cur = c.execute(
                    """INSERT INTO scheduled_items(name, account_id, amount, frequency, next_date,
                       source, confidence, active, confirmed, end_date, counter_account_id,
                       payee_pattern, notes, weekend_rule) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        it.name, it.account_id, it.amount, it.frequency.value,
                        it.next_date.isoformat(), it.source.value, it.confidence, int(it.active),
                        int(it.confirmed), _d(it.end_date), it.counter_account_id,
                        it.payee_pattern, it.notes, it.weekend_rule,
                    ),
                )
                it.id = cur.lastrowid
            else:
                c.execute(
                    """UPDATE scheduled_items SET name=?, account_id=?, amount=?, frequency=?,
                       next_date=?, source=?, confidence=?, active=?, confirmed=?, end_date=?,
                       counter_account_id=?, payee_pattern=?, notes=?, weekend_rule=? WHERE id=?""",
                    (
                        it.name, it.account_id, it.amount, it.frequency.value,
                        it.next_date.isoformat(), it.source.value, it.confidence, int(it.active),
                        int(it.confirmed), _d(it.end_date), it.counter_account_id,
                        it.payee_pattern, it.notes, it.weekend_rule, it.id,
                    ),
                )
        return it

    def get_item(self, item_id: int) -> ScheduledItem | None:
        r = self.conn.execute("SELECT * FROM scheduled_items WHERE id=?", (item_id,)).fetchone()
        return self._row_to_item(r) if r else None

    def list_items(self, active_only: bool = True, source: ItemSource | None = None) -> list[ScheduledItem]:
        q = "SELECT * FROM scheduled_items WHERE 1=1"
        vals: list = []
        if active_only:
            q += " AND active=1"
        if source:
            q += " AND source=?"; vals.append(source.value)
        q += " ORDER BY next_date, name"
        return [self._row_to_item(r) for r in self.conn.execute(q, vals)]

    def find_item_by_pattern(self, account_id: int, payee_pattern: str) -> ScheduledItem | None:
        r = self.conn.execute(
            "SELECT * FROM scheduled_items WHERE account_id=? AND payee_pattern=?",
            (account_id, payee_pattern),
        ).fetchone()
        return self._row_to_item(r) if r else None

    def delete_item(self, item_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM scheduled_items WHERE id=?", (item_id,))

    # -- statements --------------------------------------------------------
    def statement_exists(self, sha256: str) -> bool:
        return self.conn.execute("SELECT 1 FROM statements WHERE sha256=?", (sha256,)).fetchone() is not None

    def save_statement(self, s: Statement) -> Statement:
        with self.tx() as c:
            cur = c.execute(
                """INSERT INTO statements(account_id, institution, period_start, period_end,
                   statement_date, due_date, amount_due, minimum_due, closing_balance, last4,
                   source_path, archived_path, sha256, parsed_ok, needs_review, imported_at, title)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    s.account_id, s.institution, _d(s.period_start), _d(s.period_end),
                    _d(s.statement_date), _d(s.due_date), s.amount_due, s.minimum_due,
                    s.closing_balance, s.last4, s.source_path, s.archived_path, s.sha256,
                    int(s.parsed_ok), int(s.needs_review), _dt(s.imported_at), s.title,
                ),
            )
            s.id = cur.lastrowid
        return s

    def list_statements(self, account_id: int | None = None, needs_review: bool | None = None) -> list[Statement]:
        q = "SELECT * FROM statements WHERE 1=1"
        vals: list = []
        if account_id is not None:
            q += " AND account_id=?"; vals.append(account_id)
        if needs_review is not None:
            q += " AND needs_review=?"; vals.append(int(needs_review))
        q += " ORDER BY COALESCE(statement_date, period_end) DESC"
        return [
            Statement(
                id=r["id"], account_id=r["account_id"], institution=r["institution"],
                period_start=_pd(r["period_start"]), period_end=_pd(r["period_end"]),
                statement_date=_pd(r["statement_date"]), due_date=_pd(r["due_date"]),
                amount_due=r["amount_due"], minimum_due=r["minimum_due"],
                closing_balance=r["closing_balance"], last4=r["last4"],
                source_path=r["source_path"], archived_path=r["archived_path"],
                sha256=r["sha256"], parsed_ok=bool(r["parsed_ok"]),
                needs_review=bool(r["needs_review"]),
                imported_at=_pdt(r["imported_at"]) or datetime.now(),
                title=r["title"],
            )
            for r in self.conn.execute(q, vals)
        ]

    def delete_all_statements(self) -> int:
        with self.tx() as c:
            return c.execute("DELETE FROM statements").rowcount

    # -- sync log ----------------------------------------------------------
    def log_sync_start(self, provider: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO sync_log(provider, started_at) VALUES (?, ?)",
                (provider, _dt(datetime.now())),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def log_sync_finish(self, log_id: int, ok: bool, message: str = "") -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE sync_log SET finished_at=?, ok=?, message=? WHERE id=?",
                (_dt(datetime.now()), int(ok), message, log_id),
            )

    def last_syncs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
