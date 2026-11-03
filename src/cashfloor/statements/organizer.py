"""Name and file statements, and push due-date info back onto accounts.

Archive layout (configurable only by editing `build_archive_path`):

    <archive_dir>/<Institution>/<AccountLabel>/<YYYY>/<YYYY-MM-DD>_<Institution>_<AccountLabel>_<doc-type>.pdf
    <archive_dir>/_review/<original-name>__<sha8>.pdf        (low confidence)

Provider sidecars: a scraper that already knows a document's institution,
title, account and date writes `<file>.meta.json` next to the PDF.  When
present it is authoritative over anything parsed from the PDF text.

AccountLabel = account nickname/key if the statement matched a known
account, else "x<last4>", else "unknown".  Names are filesystem-safe and
sort chronologically.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

from ..config import Config
from ..db import Database
from ..models import Account, AccountType, Statement, Transaction, to_cents
from .parser import ParsedStatement, parse_statement_text
from ..projection.recurring import normalize_payee
from .transactions import parse_transactions

TX_ACCOUNT_TYPES = (AccountType.CHECKING, AccountType.SAVINGS)

REVIEW_THRESHOLD = 0.6


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_text(path: Path, max_pages: int = 3) -> str:
    """First few pages are enough for header fields; keeps big statements fast."""
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages[:max_pages]:
            parts.append(page.extract_text() or "")
    return "\n".join(parts)


def extract_all_text(path: Path) -> str:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return "\n".join((pg.extract_text() or "") for pg in pdf.pages)


def import_statement_transactions(db: Database, st: Statement, account: Account, text: str | None = None) -> int:
    """Pull the transaction table out of a bank statement into `transactions`.

    Rows that the live feed (SimpleFIN/OFX/CSV) already has are skipped,
    matched one-to-one by amount and date (±3 days) so identical same-day
    rows are kept as long as the feed has the same number of them.  This
    keeps the statement's consistent wording for the whole history, which
    the recurring detector depends on.  Idempotent: ids are stmt:<sha>:<n>.
    Returns the number of rows inserted or updated."""
    if account.type not in TX_ACCOUNT_TYPES or not st.archived_path:
        return 0
    text = text if text is not None else extract_all_text(Path(st.archived_path))
    parsed = parse_transactions(text)
    if not parsed.transactions:
        return 0
    lo = min(t.posted for t in parsed.transactions) - timedelta(days=3)
    hi = max(t.posted for t in parsed.transactions) + timedelta(days=3)
    live = [t for t in db.list_transactions(account.id, since=lo)
            if t.posted <= hi and not t.external_id.startswith("stmt:")]
    unmatched = list(live)
    rows = []
    for t in parsed.transactions:
        ext = f"stmt:{st.sha256[:12]}:{t.index}"
        hit = next((lv for lv in unmatched if lv.amount == t.amount and abs((lv.posted - t.posted).days) <= 3), None)
        if hit is not None:
            unmatched.remove(hit)
            # Same transaction under two names: remember that the feed's wording
            # means this statement payee, so the detector sees one series.
            db.set_payee_alias(account.id, normalize_payee(hit.payee or hit.memo), normalize_payee(t.payee))  # type: ignore[arg-type]
            continue  # the live feed already has this one
        rows.append(Transaction(id=None, account_id=account.id, external_id=ext,  # type: ignore[arg-type]
                                posted=t.posted, amount=t.amount, payee=t.payee, memo=t.memo))
    return db.upsert_transactions(rows) if rows else 0


def safe(s: str) -> str:
    s = re.sub(r"[^\w\-]+", "-", s.strip())
    return re.sub(r"-{2,}", "-", s).strip("-") or "unknown"


def doc_type_slug(title: str | None) -> str:
    """'CHECKING STATEMENT' -> 'checking-statement'; default 'statement'."""
    if not title:
        return "statement"
    return safe(title.lower())[:60] or "statement"


def build_archive_path(cfg: Config, ps: ParsedStatement, account: Account | None, title: str | None = None,
                       label_hint: str | None = None) -> Path:
    inst = safe(account.institution if account else (ps.institution or "Unknown"))
    if account:
        label = safe(account.key or account.nickname or f"x{account.last4 or ps.last4 or ''}")
    else:
        label = f"x{ps.last4}" if ps.last4 else safe(label_hint or "unknown")
    d = ps.best_date
    year = str(d.year) if d else "undated"
    stamp = d.isoformat() if d else "undated"
    filename = f"{stamp}_{inst}_{label}_{doc_type_slug(title)}.pdf"
    return cfg.general.archive_dir / inst / label / year / filename


def read_sidecar(path: Path) -> dict | None:
    """`<file>.meta.json` written by a provider: {institution, title, account, date, last4?}."""
    sc = path.with_name(path.name + ".meta.json")
    if not sc.exists():
        return None
    try:
        return json.loads(sc.read_text())
    except Exception:  # noqa: BLE001
        return None


def apply_sidecar(ps: ParsedStatement, meta: dict) -> ParsedStatement:
    if meta.get("institution"):
        ps.institution = meta["institution"]
    if meta.get("date"):
        try:
            d = date.fromisoformat(str(meta["date"])[:10])
            ps.statement_date = ps.statement_date or d
            if ps.best_date != d and ps.statement_date and abs((ps.statement_date - d).days) > 45:
                ps.statement_date = d  # PDF text picked some unrelated date
        except ValueError:
            pass
    digits = re.findall(r"\d{4}", str(meta.get("last4") or meta.get("account") or ""))
    if digits:
        ps.last4 = digits[-1]
    ps.confidence = max(ps.confidence, 0.9 if (ps.institution and ps.best_date) else ps.confidence)
    return ps


TYPE_WORDS = {
    "checking": AccountType.CHECKING, "savings": AccountType.SAVINGS, "credit": AccountType.CREDIT,
    "visa": AccountType.CREDIT, "mastercard": AccountType.CREDIT, "loan": AccountType.LOAN,
    "mortgage": AccountType.LOAN, "insurance": AccountType.UTILITY, "utility": AccountType.UTILITY,
    "brokerage": AccountType.INVESTMENT, "ira": AccountType.INVESTMENT,
}


def infer_type(title: str | None) -> AccountType | None:
    t = (title or "").lower()
    for w, at in TYPE_WORDS.items():
        if re.search(r"(?<![a-z])" + w + r"(?![a-z])", t):
            return at
    return None


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    i = 2
    while True:
        cand = p.with_name(f"{p.stem}_{i}{p.suffix}")
        if not cand.exists():
            return cand
        i += 1


def match_account(db: Database, ps: ParsedStatement, title: str | None = None) -> Account | None:
    if ps.last4:
        a = db.find_account(institution=ps.institution, last4=ps.last4) or db.find_account(last4=ps.last4)
        if a:
            return a
    if ps.institution:
        cands = [a for a in db.list_accounts() if ps.institution.lower() in a.institution.lower()]
        # Narrow by the account type the document title implies (e.g. "SAVINGS STATEMENT")
        want = infer_type(title)
        if want and len(cands) > 1:
            narrowed = [a for a in cands if a.type == want]
            if narrowed:
                cands = narrowed
        # Accounts whose name carries the last 4 (SimpleFIN often does: "CHECKING *4471")
        if ps.last4 and len(cands) > 1:
            by_name = [a for a in cands if ps.last4 in (a.name or "")]
            if by_name:
                cands = by_name
        if len(cands) == 1:
            return cands[0]
    return None


def ingest_statement_file(
    cfg: Config, db: Database, path: Path, *, move: bool = True, text: str | None = None
) -> Statement | None:
    """Parse, name, archive, record.  Returns None if already imported."""
    digest = sha256_of(path)
    if db.statement_exists(digest):
        if move:
            path.unlink(missing_ok=True)  # duplicate drop: discard silently
        return None

    meta = read_sidecar(path)
    try:
        raw = text if text is not None else extract_text(path)
        ps = parse_statement_text(raw)
        parsed_ok = True
    except Exception as e:  # noqa: BLE001
        ps = ParsedStatement(notes=[f"extract failed: {e}"])
        parsed_ok = False
    title = (meta or {}).get("title")
    if meta:
        ps = apply_sidecar(ps, meta)

    account = match_account(db, ps, title)
    # A document the provider fully described is filed even if no account matches;
    # only PDF-only guesses with no account go to review.
    needs_review = (not parsed_ok) or ps.confidence < REVIEW_THRESHOLD or (account is None and not meta)

    if needs_review and not ps.best_date:
        dest = cfg.general.archive_dir / "_review" / f"{safe(path.stem)}__{digest[:8]}.pdf"
    else:
        dest = build_archive_path(cfg, ps, account, title, (meta or {}).get("label_hint"))
        if needs_review:
            dest = dest.parent / "_review" / dest.name
    dest = unique_path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    (shutil.move if move else shutil.copy2)(str(path), str(dest))
    sidecar = path.with_name(path.name + ".meta.json")
    if sidecar.exists():
        sidecar_dest = dest.with_name(dest.name + ".meta.json")
        (shutil.move if move else shutil.copy2)(str(sidecar), str(sidecar_dest))

    st = Statement(
        id=None,
        account_id=account.id if account else None,
        institution=account.institution if account else (ps.institution or "Unknown"),
        period_start=ps.period_start,
        period_end=ps.period_end,
        statement_date=ps.statement_date,
        due_date=ps.due_date,
        amount_due=to_cents(ps.amount_due) if ps.amount_due is not None else None,
        minimum_due=to_cents(ps.minimum_due) if ps.minimum_due is not None else None,
        closing_balance=to_cents(ps.closing_balance) if ps.closing_balance is not None else None,
        last4=ps.last4,
        source_path=str(path),
        archived_path=str(dest),
        sha256=digest,
        parsed_ok=parsed_ok,
        needs_review=needs_review,
        imported_at=datetime.now(),
        title=title,
    )
    db.save_statement(st)

    # Bank statements carry a transaction table: harvest it for history.
    if account and account.type in TX_ACCOUNT_TYPES and parsed_ok:
        try:
            full = text if text is not None else extract_all_text(dest)
            import_statement_transactions(db, st, account, full)
        except Exception as e:  # noqa: BLE001 — history is a bonus, never block filing
            ps.notes.append(f"transaction import failed: {e}")

    # Feed due-date info back to the account so the projection sees it.
    if account and ps.due_date and account.type in (AccountType.CREDIT, AccountType.LOAN, AccountType.UTILITY):
        newer = account.due_date is None or ps.due_date >= account.due_date
        if newer:
            db.set_account_due(
                account.id,  # type: ignore[arg-type]
                due_date=ps.due_date,
                statement_balance=st.amount_due,
                minimum_payment=st.minimum_due,
            )
    return st
