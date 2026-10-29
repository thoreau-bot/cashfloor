"""Orchestrates one sync pass across all enabled providers.

For each provider: fetch -> upsert accounts -> upsert transactions ->
ingest statement files.  Then: apply config account overrides, roll
scheduled-item dates forward, run recurring detection, log results.
Failures are isolated per provider.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from .config import Config
from .db import Database
from .models import AccountType, ItemSource
from .projection.recurrence import advance_past
from .projection.recurring import detect_recurring
from .providers import build_provider
from .statements.organizer import ingest_statement_file

log = logging.getLogger("cashfloor.sync")


@dataclass
class SyncSummary:
    providers_ok: list[str] = field(default_factory=list)
    providers_failed: dict[str, str] = field(default_factory=dict)
    accounts: int = 0
    transactions: int = 0
    statements: int = 0
    statements_review: int = 0
    new_proposals: int = 0


def apply_account_overrides(cfg: Config, db: Database) -> None:
    for ov in cfg.account:
        for a in db.list_accounts(active_only=False):
            if a.provider == ov.provider and a.external_id == ov.external_id:
                db.set_account_user_fields(
                    a.id,  # type: ignore[arg-type]
                    key=ov.key,
                    nickname=ov.nickname,
                    type=AccountType(ov.type),
                    in_pool=ov.in_pool,
                )


def run_sync(cfg: Config, db: Database, only: str | None = None, today: date | None = None) -> SyncSummary:
    cfg.ensure_dirs()
    today = today or date.today()
    summary = SyncSummary()

    for pc in cfg.provider:
        if not pc.enabled or (only and pc.name != only):
            continue
        log_id = db.log_sync_start(pc.name)
        try:
            prov = build_provider(cfg, pc)
            res = prov.fetch()
            ext_to_id: dict[str, int] = {}
            for acct in res.accounts:
                saved = db.upsert_account(acct)
                ext_to_id[acct.external_id] = saved.id  # type: ignore[assignment]
                summary.accounts += 1
            txs = []
            for t in res.transactions:
                ext = t.__dict__.get("_account_external_id")
                if ext in ext_to_id:
                    t.account_id = ext_to_id[ext]
                    txs.append(t)
            summary.transactions += db.upsert_transactions(txs)
            for path in res.statement_files:
                st = ingest_statement_file(cfg, db, path)
                if st:
                    summary.statements += 1
                    summary.statements_review += int(st.needs_review)
            msg = f"{len(res.accounts)} accounts, {len(txs)} tx, {len(res.statement_files)} files"
            if res.errors:
                msg += "; warnings: " + " | ".join(res.errors)
            db.log_sync_finish(log_id, ok=True, message=msg)
            summary.providers_ok.append(pc.name)
        except Exception as e:  # noqa: BLE001
            log.exception("provider %s failed", pc.name)
            db.log_sync_finish(log_id, ok=False, message=str(e))
            summary.providers_failed[pc.name] = str(e)

    apply_account_overrides(cfg, db)

    # Roll scheduled dates forward past today.
    for it in db.list_items():
        nd = advance_past(it, today)
        if nd != it.next_date:
            it.next_date = nd
            db.save_item(it)

    # Propose recurring items (never overwrite confirmed ones).
    proposals = detect_recurring(cfg, db.list_transactions(since=today.replace(year=today.year - 1)), today)
    for p in proposals:
        existing = db.find_item_by_pattern(p.account_id, p.payee_pattern or "")
        if existing is None:
            db.save_item(p)
            summary.new_proposals += 1
        elif not existing.confirmed and existing.source == ItemSource.DETECTED:
            existing.amount, existing.frequency = p.amount, p.frequency
            existing.next_date, existing.confidence, existing.notes = p.next_date, p.confidence, p.notes
            db.save_item(existing)
    return summary
