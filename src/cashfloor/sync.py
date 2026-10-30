"""Orchestrates one sync pass across all enabled providers.

For each provider: fetch -> upsert accounts -> upsert transactions ->
ingest statement files.  Then: apply config account overrides, roll
scheduled-item dates forward, run recurring detection, log results.
Failures are isolated per provider.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

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
    connection_errors: dict[str, str] = field(default_factory=dict)


def save_raw_response(cfg: Config, provider: str, payload: dict, keep: int = 30) -> Path | None:
    """Keep the last `keep` raw provider responses under data/raw/ so you can
    see exactly what the provider sends (and what cashfloor ignores)."""
    if not cfg.general.keep_raw_responses:
        return None
    d = cfg.general.data_dir / "raw"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{provider}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    for old in sorted(d.glob(f"{provider}-*.json"))[:-keep]:
        old.unlink(missing_ok=True)
    return path


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
    if only and not any(pc.name == only for pc in cfg.provider):
        names = ", ".join(pc.name for pc in cfg.provider) or "(none)"
        raise ValueError(f"no provider named {only!r} in config; configured providers: {names}")

    for pc in cfg.provider:
        if not pc.enabled or (only and pc.name != only):
            continue
        log_id = db.log_sync_start(pc.name)
        try:
            prov = build_provider(cfg, pc)
            res = prov.fetch()
            raw = getattr(res, "raw", None)
            if raw is not None:
                info = getattr(res, "server_info", None)
                save_raw_response(cfg, pc.name, {"_server_info": info, **raw} if info else raw)
            for conn in getattr(res, "connections", []) or []:
                db.upsert_connection(conn)
                if conn.error_code:
                    summary.connection_errors[conn.name] = f"{conn.error_code}: {conn.error_msg}"
            ext_to_id: dict[str, int] = {}
            for acct in res.accounts:
                saved = db.upsert_account(acct)
                ext_to_id[acct.external_id] = saved.id  # type: ignore[assignment]
                summary.accounts += 1
            for ext, holdings in (getattr(res, "holdings", None) or {}).items():
                if ext in ext_to_id:
                    for h in holdings:
                        h.account_id = ext_to_id[ext]
                    db.replace_holdings(ext_to_id[ext], holdings)
            txs = []
            for t in res.transactions:
                ext = t.__dict__.get("_account_external_id")
                if ext in ext_to_id:
                    t.account_id = ext_to_id[ext]
                    txs.append(t)
            summary.transactions += db.upsert_transactions(txs)
            # File what this run produced, plus anything a previous run left behind.
            leftovers = sorted((cfg.general.data_dir / "downloads" / pc.name).glob("*.pdf"))
            pending_files = list(dict.fromkeys([*res.statement_files, *leftovers]))
            file_errors = []
            for path in pending_files:
                if not path.exists():
                    continue
                try:
                    st = ingest_statement_file(cfg, db, path)
                except Exception as e:  # noqa: BLE001 — one bad PDF must not strand the rest
                    file_errors.append(f"{path.name}: {e}")
                    log.warning("filing %s failed: %s", path.name, e)
                    continue
                if st:
                    summary.statements += 1
                    summary.statements_review += int(st.needs_review)
            res.errors.extend(file_errors)
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
    proposals = detect_recurring(cfg, db.list_transactions(since=today.replace(year=today.year - 3)), today)
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
