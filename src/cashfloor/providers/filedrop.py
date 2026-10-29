"""File-drop provider: anything placed in the inbox folder.

* PDFs are handed to the statement pipeline (parsed, renamed, archived).
* OFX/QFX files are parsed for balances + transactions.
* CSV files are parsed with a small set of common column layouts.

This is the universal fallback — any portal that can't be scraped can still
be covered by downloading files by hand and dropping them here.
"""

from __future__ import annotations

import csv
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..models import Account, AccountType, Transaction, to_cents
from .base import Provider, SyncResult

OFX_TAG = re.compile(r"<([A-Z0-9.]+)>([^<\r\n]*)")


def parse_ofx(text: str, provider_name: str, source: str) -> tuple[Account | None, list[Transaction]]:
    """Minimal SGML/XML OFX reader for the fields we need."""
    tags = dict(OFX_TAG.findall(text))
    acct_id = tags.get("ACCTID") or tags.get("CARDNUMBER") or source
    org = tags.get("ORG") or tags.get("FID") or "Unknown"
    acct_type_raw = (tags.get("ACCTTYPE") or "").upper()
    if "CREDITCARD" in text.upper() or "CCSTMTRS" in text.upper():
        atype = AccountType.CREDIT
    elif acct_type_raw == "SAVINGS":
        atype = AccountType.SAVINGS
    elif acct_type_raw == "CHECKING":
        atype = AccountType.CHECKING
    else:
        atype = AccountType.OTHER
    bal = tags.get("BALAMT")
    as_of_raw = tags.get("DTASOF") or tags.get("DTSERVER")
    as_of = _ofx_dt(as_of_raw) if as_of_raw else datetime.now(UTC)

    acct = Account(
        id=None,
        provider=provider_name,
        external_id=f"ofx:{org}:{acct_id}",
        name=f"{org} {acct_type_raw.title() or atype.value} ...{acct_id[-4:]}",
        institution=org,
        type=atype,
        last4=acct_id[-4:],
        balance=to_cents(bal) if bal else 0,
        balance_as_of=as_of,
        last_synced=datetime.now(UTC),
    )
    txs: list[Transaction] = []
    for block in re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|$)", text, re.S):
        t = dict(OFX_TAG.findall(block))
        if "DTPOSTED" not in t or "TRNAMT" not in t:
            continue
        txs.append(
            Transaction(
                id=None,
                account_id=-1,
                external_id=t.get("FITID") or f"{t['DTPOSTED']}:{t['TRNAMT']}:{t.get('NAME', '')}",
                posted=_ofx_dt(t["DTPOSTED"]).date(),
                amount=to_cents(t["TRNAMT"]),
                payee=(t.get("NAME") or t.get("PAYEE") or "").strip(),
                memo=(t.get("MEMO") or "").strip(),
            )
        )
        txs[-1].__dict__["_account_external_id"] = acct.external_id
    return acct, txs


def _ofx_dt(s: str) -> datetime:
    s = s.strip()[:14]
    fmt = "%Y%m%d%H%M%S" if len(s) == 14 else "%Y%m%d"
    return datetime.strptime(s[: 14 if fmt.endswith("S") else 8], fmt).replace(tzinfo=UTC)


CSV_DATE_KEYS = ("date", "posted date", "transaction date", "post date")
CSV_AMOUNT_KEYS = ("amount", "transaction amount")
CSV_DEBIT_KEYS = ("debit", "withdrawal", "withdrawals")
CSV_CREDIT_KEYS = ("credit", "deposit", "deposits")
CSV_PAYEE_KEYS = ("description", "payee", "name", "merchant", "details")


def parse_csv(path: Path, provider_name: str) -> tuple[Account, list[Transaction]]:
    """Best-effort CSV import.  Account identity is the file's stem; the user
    then maps it to a real account in the UI."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError("empty CSV")
    cols = {c.lower().strip(): c for c in rows[0].keys() if c}

    def pick(keys: tuple[str, ...]) -> str | None:
        for k in keys:
            if k in cols:
                return cols[k]
        return None

    dcol, acol, pcol = pick(CSV_DATE_KEYS), pick(CSV_AMOUNT_KEYS), pick(CSV_PAYEE_KEYS)
    debcol, crecol = pick(CSV_DEBIT_KEYS), pick(CSV_CREDIT_KEYS)
    if not dcol or not (acol or debcol or crecol):
        raise ValueError(f"unrecognized CSV columns: {list(cols)}")

    acct = Account(
        id=None, provider=provider_name, external_id=f"csv:{path.stem}", name=path.stem,
        institution=path.stem.split("_")[0], type=AccountType.OTHER,
        last_synced=datetime.now(UTC),
    )
    txs: list[Transaction] = []
    for i, r in enumerate(rows):
        try:
            posted = _parse_date(r[dcol])
            if acol:
                amt = _dec(r[acol])
            else:
                amt = _dec(r.get(crecol, "0") if crecol else "0") - _dec(r.get(debcol, "0") if debcol else "0")
        except (ValueError, InvalidOperation):
            continue
        payee = (r.get(pcol, "") if pcol else "").strip()
        txs.append(
            Transaction(
                id=None, account_id=-1,
                external_id=f"{posted.isoformat()}:{amt}:{payee}:{i}",
                posted=posted, amount=to_cents(amt), payee=payee,
            )
        )
        txs[-1].__dict__["_account_external_id"] = acct.external_id
    return acct, txs


def _dec(s: str | None) -> Decimal:
    s = (s or "").strip().replace("$", "").replace(",", "")
    if not s:
        return Decimal(0)
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    return Decimal(s)


def _parse_date(s: str) -> date:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%b %d, %Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unparsed date {s!r}")


class FileDropProvider(Provider):
    kind = "filedrop"

    def supports_statements(self) -> bool:
        return True

    def fetch(self) -> SyncResult:
        res = SyncResult(provider=self.name)
        inbox = self.cfg.general.inbox_dir
        inbox.mkdir(parents=True, exist_ok=True)
        for p in sorted(inbox.iterdir()):
            if p.is_dir() or p.name.startswith("."):
                continue
            ext = p.suffix.lower()
            try:
                if ext == ".pdf":
                    res.statement_files.append(p)
                elif ext in (".ofx", ".qfx"):
                    acct, txs = parse_ofx(p.read_text(errors="ignore"), self.name, p.stem)
                    if acct:
                        res.accounts.append(acct)
                    res.transactions.extend(txs)
                elif ext == ".csv":
                    acct, txs = parse_csv(p, self.name)
                    res.accounts.append(acct)
                    res.transactions.extend(txs)
            except Exception as e:  # noqa: BLE001 — one bad file must not stop the rest
                res.errors.append(f"{p.name}: {e}")
        return res
