"""SimpleFIN Bridge provider.

Protocol summary (https://www.simplefin.org/protocol.html):

  1. User creates a one-time *setup token* at the Bridge.  The token is
     base64 of a *claim URL*.
  2. POST to the claim URL once -> response body is the *access URL*
     (https://user:pass@bridge.simplefin.org/simplefin).  Store it in the
     keyring; the setup token is now dead.
  3. GET {access_url}/accounts?version=2&start-date=<unix>&pending=1
     -> JSON with connections, accounts, balances, transactions, errlist.

We request protocol v2 and parse everything the spec defines plus the
Bridge's extensions (`org` on each account, `holdings` on investment
accounts, `extra` bags).  Balances/transactions only — statement PDFs are
not part of the protocol.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from .. import secrets
from ..config import Config, ProviderConfig
from ..models import Account, AccountType, Connection, Holding, Transaction, to_cents
from .base import Provider, SyncResult

DEFAULT_SECRET = "simplefin_access_url"
PROTOCOL_VERSION = "2"


@dataclass
class SimpleFINError:
    code: str
    msg: str
    conn_id: str | None = None
    account_id: str | None = None


@dataclass
class SimpleFINResult(SyncResult):
    connections: list[Connection] = field(default_factory=list)
    holdings: dict[str, list[Holding]] = field(default_factory=dict)  # account external_id -> holdings
    structured_errors: list[SimpleFINError] = field(default_factory=list)
    raw: dict | None = None
    server_info: dict | None = None


def claim_access_url(setup_token: str, timeout: float = 30.0) -> str:
    """Exchange a one-time setup token for a permanent access URL."""
    claim_url = base64.b64decode(setup_token.strip()).decode("utf-8")
    r = httpx.post(claim_url, timeout=timeout)
    r.raise_for_status()
    access_url = r.text.strip()
    if not access_url.startswith("http"):
        raise ValueError(f"Unexpected claim response: {access_url[:80]!r}")
    return access_url


def guess_account_type(name: str, balance: Decimal, has_holdings: bool = False) -> AccountType:
    n = name.lower()
    if has_holdings or any(k in n for k in ("401", "ira", "brokerage", "invest", "roth", "hsa")):
        return AccountType.INVESTMENT
    if any(k in n for k in ("credit", "card", "visa", "mastercard", "amex", "discover")):
        return AccountType.CREDIT
    if any(k in n for k in ("saving", "money market", "mma")):
        return AccountType.SAVINGS
    if any(k in n for k in ("check", "chk", "spending", "everyday")):
        return AccountType.CHECKING
    if any(k in n for k in ("loan", "mortgage", "auto", "student")):
        return AccountType.LOAN
    if balance < 0:
        return AccountType.CREDIT
    return AccountType.OTHER


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _cents(v) -> int | None:
    d = _dec(v)
    return to_cents(d) if d is not None else None


def _ts(v, default: datetime | None = None) -> datetime | None:
    if isinstance(v, int | float) and v > 0:
        return datetime.fromtimestamp(v, UTC)
    return default


def parse_accounts_payload(payload: dict, provider_name: str, as_of: datetime | None = None) -> SimpleFINResult:
    """Pure function: SimpleFIN JSON (v1 or v2 shape) -> SimpleFINResult."""
    res = SimpleFINResult(provider=provider_name, raw=payload)
    now = as_of or datetime.now(UTC)

    # --- errors: v2 errlist (structured) + deprecated v1 strings
    for e in payload.get("errlist", []) or []:
        res.structured_errors.append(SimpleFINError(
            code=str(e.get("code", "unknown")), msg=str(e.get("msg", "")),
            conn_id=e.get("conn_id"), account_id=e.get("account_id"),
        ))
    for e in payload.get("errors", []) or []:
        res.errors.append(str(e))
    res.errors.extend(f"{e.code}: {e.msg}" for e in res.structured_errors)

    # --- connections (v2); v1 payloads synthesize them from per-account org
    conns: dict[str, Connection] = {}
    for c in payload.get("connections", []) or []:
        conn = Connection(provider=provider_name, conn_id=c["conn_id"], name=c.get("name", c["conn_id"]),
                          org_id=c.get("org_id"), org_url=c.get("org_url"), sfin_url=c.get("sfin_url"),
                          last_seen=now)
        conns[conn.conn_id] = conn
    for e in res.structured_errors:
        # account-specific errors carry account_id; only connection-wide ones mark the connection
        if e.conn_id and e.conn_id in conns and not e.account_id:
            conns[e.conn_id].error_code, conns[e.conn_id].error_msg = e.code, e.msg

    for a in payload.get("accounts", []) or []:
        org = a.get("org", {}) or {}
        conn_id = a.get("conn_id")
        if conn_id is None and org:
            # v1 shape: derive a stable connection id from the org
            conn_id = org.get("id") or org.get("domain") or org.get("name") or "unknown"
            if conn_id not in conns:
                conns[conn_id] = Connection(provider=provider_name, conn_id=conn_id,
                                            name=org.get("name") or org.get("domain") or conn_id,
                                            org_id=org.get("id"), org_url=org.get("url") or org.get("domain"),
                                            sfin_url=org.get("sfin-url"), last_seen=now)
        conn = conns.get(conn_id) if conn_id else None
        institution = (conn.name if conn else None) or org.get("name") or org.get("domain") or "Unknown"

        bal = _dec(a.get("balance")) or Decimal(0)
        raw_holdings = a.get("holdings", []) or []
        name = a.get("name", "Account")
        acct_errors = [e for e in res.structured_errors if e.account_id == a["id"]]
        conn_error = conn.error_msg if conn and conn.error_msg else None
        acct = Account(
            id=None,
            provider=provider_name,
            external_id=a["id"],
            name=name,
            institution=institution,
            type=guess_account_type(name, bal, bool(raw_holdings)),
            currency=a.get("currency", "USD"),
            balance=to_cents(bal),
            available=_cents(a.get("available-balance")),
            balance_as_of=_ts(a.get("balance-date"), now),
            last_synced=now,
            conn_id=conn_id,
            extra=a.get("extra") or None,
            sync_error=("; ".join(e.msg for e in acct_errors) or conn_error) or None,
        )
        res.accounts.append(acct)

        for t in a.get("transactions", []) or []:
            posted = _ts(t.get("posted")) or _ts(t.get("transacted_at")) or now
            tx = Transaction(
                id=None,
                account_id=-1,  # resolved by the sync service via _account_external_id
                external_id=t["id"],
                posted=posted.date(),
                amount=to_cents(_dec(t.get("amount")) or Decimal(0)),
                payee=(t.get("payee") or t.get("description") or "").strip(),
                memo=(t.get("memo") or t.get("description") or "").strip(),
                pending=bool(t.get("pending", False)),
                transacted_at=_ts(t.get("transacted_at")),
                extra=t.get("extra") or None,
            )
            tx.__dict__["_account_external_id"] = a["id"]
            res.transactions.append(tx)

        if raw_holdings:
            res.holdings[a["id"]] = [
                Holding(
                    account_id=-1,
                    external_id=h.get("id") or f"{h.get('symbol')}:{h.get('description')}",
                    symbol=h.get("symbol"),
                    description=h.get("description", ""),
                    shares=int(_dec(h.get("shares")) * 1000) if _dec(h.get("shares")) is not None else None,
                    cost_basis=_cents(h.get("cost_basis")),
                    purchase_price=_cents(h.get("purchase_price")),
                    market_value=_cents(h.get("market_value")),
                    currency=h.get("currency") or a.get("currency", "USD"),
                    created=_ts(h.get("created")),
                    as_of=acct.balance_as_of,
                )
                for h in raw_holdings
            ]

    res.connections = list(conns.values())
    return res


class SimpleFINProvider(Provider):
    kind = "simplefin"

    def __init__(self, cfg: Config, pc: ProviderConfig):
        super().__init__(cfg, pc)
        self.secret_name = pc.secret or DEFAULT_SECRET

    def access_url(self) -> str:
        url = secrets.get_secret(self.secret_name)
        if not url:
            raise RuntimeError(
                f"No SimpleFIN access URL in keyring under {self.secret_name!r}. "
                "Connect it on the Settings page or run: cashfloor simplefin setup <SETUP_TOKEN>"
            )
        return url

    def server_info(self) -> dict | None:
        try:
            r = httpx.get(self.access_url().rstrip("/") + "/info", timeout=30.0)
            return r.json() if r.status_code == 200 else {"status": r.status_code}
        except Exception as e:  # noqa: BLE001 — informational only
            return {"error": str(e)}

    def fetch(self, balances_only: bool = False) -> SimpleFINResult:
        start = date.today() - timedelta(days=self.pc.lookback_days)
        params: dict = {
            "version": PROTOCOL_VERSION,
            "start-date": int(datetime.combine(start, datetime.min.time(), UTC).timestamp()),
            "pending": 1,
        }
        if balances_only:
            params["balances-only"] = 1
        url = self.access_url().rstrip("/") + "/accounts"
        r = httpx.get(url, params=params, timeout=120.0)
        r.raise_for_status()
        res = parse_accounts_payload(r.json(), self.name)
        res.server_info = self.server_info()
        return res
