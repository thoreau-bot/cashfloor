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
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Config
from .db import Database
from .models import AccountType, Frequency, ItemSource
from .projection.engine import POSTED_LOOKAHEAD, POSTED_LOOKBACK, already_posted
from .projection.recurrence import advance_past
from .projection.recurring import analyse_recurring
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
    retired_items: int = 0  # detected items retired because their series merged into another
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

    # Where a card/loan has no due date, learn the day it usually gets paid.
    from .models import LONG_TERM_TYPES
    from .projection.payday import LOOKBACK_DAYS, infer_payment

    card_txs = db.list_transactions(since=today - timedelta(days=LOOKBACK_DAYS))
    for a in db.list_accounts(active_only=False):
        if a.type in (AccountType.CREDIT, *LONG_TERM_TYPES):
            day, amount = infer_payment(a, card_txs, today) or (None, None)
            if (day, amount) != (a.pay_day_auto, a.payment_auto):
                db.set_payment_auto(a.id, day, amount)  # type: ignore[arg-type]

    # Roll scheduled dates forward past today — and past an occurrence that has
    # already posted (this morning's payroll), so the Scheduled page shows the
    # genuinely next date.
    recent = db.list_transactions(since=today - timedelta(days=POSTED_LOOKBACK + POSTED_LOOKAHEAD))
    aliases = db.get_payee_aliases()
    acct_by_id = {a.id: a for a in db.list_accounts(active_only=False)}
    for it in db.list_items():
        nd = advance_past(it, today)
        acct = acct_by_id.get(it.account_id)
        bal_day = acct.balance_as_of.date() if acct and acct.balance_as_of else None
        if it.frequency != Frequency.ONCE and already_posted(cfg, it, nd, today, recent, aliases, None, bal_day):
            nd = advance_past(it, nd + timedelta(days=1))
        if nd != it.next_date:
            it.next_date = nd
            db.save_item(it)

    # Propose recurring items (never overwrite confirmed ones).
    candidates = analyse_recurring(cfg, db.list_transactions(since=today.replace(year=today.year - 3)), today,
                                   db.get_payee_aliases())
    reconcile_proposals(db, candidates, summary, cfg.recurring.income_merge_tolerance)
    return summary


def _same_series(a, b, tolerance: float) -> bool:
    """Two detected items on one account describing the same stream: same
    sign and cadence, amounts within `tolerance`, next dates in phase."""
    if a.account_id != b.account_id or a.frequency != b.frequency or (a.amount > 0) != (b.amount > 0):
        return False
    if abs(abs(a.amount) - abs(b.amount)) / max(abs(a.amount), 1) > tolerance:
        return False
    return abs((a.next_date - b.next_date).days) <= 3


def _fold(db: Database, old, survivor) -> None:
    """Retire `old` in favour of `survivor`, carrying over what the user set."""
    changed = False
    if old.confirmed and old.active and not survivor.confirmed:
        survivor.confirmed, survivor.active = True, True
        changed = True
    if old.counter_account_id and not survivor.counter_account_id:
        survivor.counter_account_id = old.counter_account_id
        changed = True
    if old.weekend_rule != "none" and survivor.weekend_rule == "none":
        survivor.weekend_rule = old.weekend_rule
        changed = True
    if changed:
        db.save_item(survivor)
    db.delete_item(old.id)


def reconcile_proposals(db: Database, candidates: list, summary: SyncSummary, tolerance: float = 0.10) -> None:
    """Save new proposals, refresh unconfirmed ones, and retire stale
    duplicates of a series.  A duplicate arises two ways: the detector folded
    one payee key into another (split paycheck), or an item was proposed under
    a key an older detector produced and the series now lives under a new key
    (statement wording learned later, a changed normaliser).  Either way the
    survivor inherits any confirmation, pays-link or weekend rule the user
    gave the retired item, so a merge never silently un-confirms income."""
    for p in (c.proposal for c in candidates if c.proposal is not None):
        existing = db.find_item_by_pattern(p.account_id, p.payee_pattern or "")
        if existing is None:
            db.save_item(p)
            summary.new_proposals += 1
        elif not existing.confirmed and existing.source == ItemSource.DETECTED:
            existing.amount, existing.frequency = p.amount, p.frequency
            existing.next_date, existing.confidence, existing.notes = p.next_date, p.confidence, p.notes
            db.save_item(existing)
    live_keys = {(c.account_id, c.pattern) for c in candidates if c.proposal is not None}
    for c in candidates:
        if not c.merged_into:
            continue
        old = db.find_item_by_pattern(c.account_id, c.pattern)
        survivor = db.find_item_by_pattern(c.account_id, c.merged_into)
        if old is None or survivor is None or old.source != ItemSource.DETECTED or old.id == survivor.id:
            continue
        _fold(db, old, survivor)
        summary.retired_items += 1
    # Stale keys: a detected item whose pattern no current series produces, but
    # which matches a current proposal in cadence, amount and phase.
    items = [i for i in db.list_items(active_only=False) if i.source == ItemSource.DETECTED]
    current = [i for i in items if (i.account_id, i.payee_pattern or "") in live_keys]
    for old in items:
        if (old.account_id, old.payee_pattern or "") in live_keys:
            continue
        match = next((s for s in current if s.id != old.id and _same_series(old, s, tolerance)), None)
        if match is None:
            continue
        _fold(db, old, match)
        summary.retired_items += 1
