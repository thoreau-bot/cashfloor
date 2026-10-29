"""SimpleFIN Bridge provider.

Protocol summary (https://www.simplefin.org/protocol.html):

  1. User creates a one-time *setup token* at the Bridge.  The token is
     base64 of a *claim URL*.
  2. POST to the claim URL once -> response body is the *access URL*
     (https://user:pass@bridge.simplefin.org/simplefin).  Store it in the
     keyring; the setup token is now dead.
  3. GET {access_url}/accounts?start-date=<unix>&end-date=<unix>&pending=1
     -> JSON with accounts, balances and transactions.

Balances/transactions only.  Statement PDFs are not part of the protocol.
"""

from __future__ import annotations

import base64
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx

from .. import secrets
from ..config import Config, ProviderConfig
from ..models import Account, AccountType, Transaction, to_cents
from .base import Provider, SyncResult

DEFAULT_SECRET = "simplefin_access_url"


def claim_access_url(setup_token: str, timeout: float = 30.0) -> str:
    """Exchange a one-time setup token for a permanent access URL."""
    claim_url = base64.b64decode(setup_token.strip()).decode("utf-8")
    r = httpx.post(claim_url, timeout=timeout)
    r.raise_for_status()
    access_url = r.text.strip()
    if not access_url.startswith("http"):
        raise ValueError(f"Unexpected claim response: {access_url[:80]!r}")
    return access_url


def guess_account_type(name: str, balance: Decimal) -> AccountType:
    n = name.lower()
    if any(k in n for k in ("credit", "card", "visa", "mastercard", "amex", "discover")):
        return AccountType.CREDIT
    if any(k in n for k in ("saving", "money market", "mma")):
        return AccountType.SAVINGS
    if any(k in n for k in ("check", "chk", "spending", "everyday")):
        return AccountType.CHECKING
    if any(k in n for k in ("loan", "mortgage", "auto", "student")):
        return AccountType.LOAN
    if any(k in n for k in ("401", "ira", "brokerage", "invest", "roth")):
        return AccountType.INVESTMENT
    if balance < 0:
        return AccountType.CREDIT
    return AccountType.OTHER


def parse_accounts_payload(payload: dict, provider_name: str, as_of: datetime | None = None) -> SyncResult:
    """Pure function: SimpleFIN JSON -> SyncResult.  Separated for testability."""
    res = SyncResult(provider=provider_name)
    res.errors.extend(str(e) for e in payload.get("errors", []))
    now = as_of or datetime.now(UTC)
    for a in payload.get("accounts", []):
        org = a.get("org", {}) or {}
        bal = Decimal(str(a.get("balance", "0")))
        avail = a.get("available-balance")
        bal_date = a.get("balance-date")
        name = a.get("name", "Account")
        acct = Account(
            id=None,
            provider=provider_name,
            external_id=a["id"],
            name=name,
            institution=org.get("name") or org.get("domain") or "Unknown",
            type=guess_account_type(name, bal),
            currency=a.get("currency", "USD"),
            balance=to_cents(bal),
            available=to_cents(avail) if avail is not None else None,
            balance_as_of=(
                datetime.fromtimestamp(bal_date, UTC) if isinstance(bal_date, int | float) else now
            ),
            last_synced=now,
        )
        res.accounts.append(acct)
        for t in a.get("transactions", []) or []:
            posted_ts = t.get("posted") or t.get("transacted_at") or 0
            posted = datetime.fromtimestamp(posted_ts, UTC).date() if posted_ts else now.date()
            tat = t.get("transacted_at")
            res.transactions.append(
                Transaction(
                    id=None,
                    account_id=-1,  # resolved by sync service via _account_external_id
                    external_id=t["id"],
                    posted=posted,
                    amount=to_cents(t.get("amount", "0")),
                    payee=(t.get("payee") or t.get("description") or "").strip(),
                    memo=(t.get("memo") or t.get("description") or "").strip(),
                    pending=bool(t.get("pending", False)),
                    transacted_at=datetime.fromtimestamp(tat, UTC) if tat else None,
                )
            )
            # stash the owning account's external id on the object for the sync service
            res.transactions[-1].__dict__["_account_external_id"] = a["id"]
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
                "Run: cashfloor simplefin setup <SETUP_TOKEN>"
            )
        return url

    def fetch(self) -> SyncResult:
        start = date.today() - timedelta(days=self.pc.lookback_days)
        params = {
            "start-date": int(datetime.combine(start, datetime.min.time(), UTC).timestamp()),
            "pending": 1,
        }
        url = self.access_url().rstrip("/") + "/accounts"
        r = httpx.get(url, params=params, timeout=120.0)
        r.raise_for_status()
        return parse_accounts_payload(r.json(), self.name)
