"""Command-line interface.  `cashfloor --help`."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import typer
from rich import print as rprint
from rich.table import Table

from . import __version__, secrets
from .config import apply_overrides, load_config
from .db import Database
from .models import AccountType, Frequency, ItemSource, ScheduledItem, from_cents, to_cents
from .projection.engine import explain, project
from .statements.organizer import ingest_statement_file
from .sync import run_sync

app = typer.Typer(help="cashfloor — local cash-flow floor projector and statement archiver.", no_args_is_help=True)
accounts_app = typer.Typer(help="Inspect and label accounts.")
items_app = typer.Typer(help="Scheduled inflows/outflows used by the projection.")
secret_app = typer.Typer(help="Store secrets in the OS keyring.")
statements_app = typer.Typer(help="Statement archive maintenance.")
simplefin_app = typer.Typer(help="SimpleFIN Bridge setup.")
app.add_typer(accounts_app, name="accounts")
app.add_typer(items_app, name="items")
app.add_typer(secret_app, name="secret")
app.add_typer(statements_app, name="statements")
app.add_typer(simplefin_app, name="simplefin")

_state: dict = {}


@app.callback()
def _main(
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to config.toml"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    _state["cfg"] = load_config(config)


def _db() -> Database:
    """Open the DB and fold GUI-stored settings into the active config."""
    cfg = _state["cfg"]
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    _state["cfg"] = apply_overrides(cfg, db.get_settings())
    return db


@app.command()
def version() -> None:
    rprint(f"cashfloor {__version__}")


@app.command()
def init(path: Path = typer.Argument(Path("config.toml"))) -> None:
    """Write a starter config.toml next to you (does nothing if it exists)."""
    if path.exists():
        rprint(f"[yellow]{path} already exists[/yellow]")
        raise typer.Exit(1)
    example = Path(__file__).resolve().parents[2] / "config.example.toml"
    path.write_text(example.read_text())
    rprint(f"[green]wrote {path}[/green] — edit it, then: cashfloor simplefin setup <TOKEN>")


@app.command()
def sync(only: str | None = typer.Option(None, help="Run just this provider name")) -> None:
    """Fetch from every enabled provider, archive statements, refresh proposals."""
    db = _db()
    try:
        s = run_sync(_state["cfg"], db, only=only)  # _db() applied GUI overrides first
    finally:
        db.close()
    rprint(f"ok: {s.providers_ok}  failed: {s.providers_failed}")
    rprint(f"accounts {s.accounts} · transactions {s.transactions} · statements {s.statements} "
           f"(review {s.statements_review}) · new proposals {s.new_proposals}")
    if s.providers_failed:
        raise typer.Exit(2)


@app.command()
def ingest(paths: list[Path] = typer.Argument(..., help="PDF files or folders"), copy: bool = typer.Option(False, help="Copy instead of move")) -> None:
    """File statement PDFs without running a provider sync."""
    db = _db()
    cfg = _state["cfg"]
    n = 0
    try:
        for p in paths:
            files = sorted(p.glob("*.pdf")) if p.is_dir() else [p]
            for f in files:
                st = ingest_statement_file(cfg, db, f, move=not copy)
                if st:
                    n += 1
                    flag = "[yellow]REVIEW[/yellow]" if st.needs_review else "[green]ok[/green]"
                    rprint(f"{flag} {f.name} -> {st.archived_path}")
                else:
                    rprint(f"[dim]dup  {f.name}[/dim]")
    finally:
        db.close()
    rprint(f"{n} filed")


@app.command()
def floor(as_json: bool = typer.Option(False, "--json"), as_of: str | None = typer.Option(None, help="YYYY-MM-DD")) -> None:
    """Print the lowest projected balance and the discretionary amount."""
    db = _db()
    try:
        r = project(db, _state["cfg"], date.fromisoformat(as_of) if as_of else None)
    finally:
        db.close()
    if as_json:
        print(json.dumps(r.to_dict(), indent=2))
    else:
        rprint(explain(r))


@app.command()
def discretionary() -> None:
    """Just the number."""
    db = _db()
    try:
        r = project(db, _state["cfg"])
    finally:
        db.close()
    print(f"{from_cents(r.discretionary):.2f}")


@app.command()
def serve(
    host: str | None = None,
    port: int | None = None,
    no_scheduler: bool = False,
    open_browser: bool = typer.Option(False, "--open", help="Open the dashboard in your browser"),
) -> None:
    """Run the local web dashboard (and the daily sync scheduler)."""
    import threading
    import webbrowser

    import uvicorn

    from .web.app import create_app

    cfg = _state["cfg"]
    cfg.ensure_dirs()
    h, p = host or cfg.web.host, port or cfg.web.port
    application = create_app(cfg, with_scheduler=not no_scheduler)
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://{h}:{p}/")).start()
    uvicorn.run(application, host=h, port=p, log_level="info", proxy_headers=True, forwarded_allow_ips="127.0.0.1")


# -- accounts ---------------------------------------------------------------
@accounts_app.command("list")
def accounts_list(all_: bool = typer.Option(False, "--all")) -> None:
    db = _db()
    try:
        rows = db.list_accounts(active_only=not all_)
    finally:
        db.close()
    t = Table("id", "key", "institution", "name", "type", "pool", "balance", "due", "stmt bal", "synced")
    for a in rows:
        t.add_row(str(a.id), a.key or "", a.institution, a.display_name, a.type.value, "✓" if a.in_pool else "",
                  f"{from_cents(a.balance):,.2f}", a.due_date.isoformat() if a.due_date else "",
                  f"{from_cents(a.statement_balance):,.2f}" if a.statement_balance is not None else "",
                  a.last_synced.strftime("%Y-%m-%d %H:%M") if a.last_synced else "")
    rprint(t)


@accounts_app.command("set")
def accounts_set(
    account_id: int,
    key: str | None = typer.Option(None),
    nickname: str | None = typer.Option(None),
    type_: str | None = typer.Option(None, "--type"),
    pool: bool | None = typer.Option(None, "--pool/--no-pool"),
    active: bool | None = typer.Option(None, "--active/--inactive"),
    last4: str | None = typer.Option(None, help="Last 4 digits, used to match statements"),
) -> None:
    """Label an account: short key, nickname, type, last-4, and whether it's in the cash pool."""
    db = _db()
    try:
        db.set_account_user_fields(account_id, key=key, nickname=nickname,
                                   type=AccountType(type_) if type_ else None, in_pool=pool, active=active,
                                   last4=last4)
    finally:
        db.close()
    rprint("[green]updated[/green]")


@accounts_app.command("due")
def accounts_due(account_id: int, due: str, amount: float, minimum: float | None = None) -> None:
    """Manually set a card/loan/utility due date and amount (YYYY-MM-DD)."""
    db = _db()
    try:
        db.set_account_due(account_id, due_date=date.fromisoformat(due), statement_balance=to_cents(amount),
                           minimum_payment=to_cents(minimum) if minimum is not None else None)
    finally:
        db.close()
    rprint("[green]updated[/green]")


# -- items -------------------------------------------------------------------
@items_app.command("list")
def items_list(pending: bool = typer.Option(False, help="Only unconfirmed proposals")) -> None:
    db = _db()
    try:
        rows = [i for i in db.list_items() if (not pending or not i.confirmed)]
        accts = {a.id: a for a in db.list_accounts(active_only=False)}
    finally:
        db.close()
    t = Table("id", "name", "account", "amount", "freq", "next", "src", "conf", "ok")
    for i in rows:
        t.add_row(str(i.id), i.name, accts[i.account_id].display_name if i.account_id in accts else "?",
                  f"{from_cents(i.amount):+,.2f}", i.frequency.value, i.next_date.isoformat(), i.source.value,
                  f"{i.confidence:.2f}", "✓" if i.confirmed else "[yellow]?[/yellow]")
    rprint(t)


@items_app.command("add")
def items_add(
    name: str, account_id: int, amount: float, frequency: str, next_date: str,
    counter_account_id: int | None = typer.Option(None, help="For transfers: destination account id"),
    weekend: str = typer.Option("none", help="none|previous|next"),
    end: str | None = typer.Option(None, help="YYYY-MM-DD"),
) -> None:
    """Add a scheduled item.  Amount is signed from the account's view (-1200 = money out)."""
    db = _db()
    try:
        it = ScheduledItem(id=None, name=name, account_id=account_id, amount=to_cents(amount),
                           frequency=Frequency(frequency), next_date=date.fromisoformat(next_date),
                           source=ItemSource.TRANSFER if counter_account_id else ItemSource.MANUAL,
                           counter_account_id=counter_account_id, weekend_rule=weekend,
                           end_date=date.fromisoformat(end) if end else None)
        db.save_item(it)
    finally:
        db.close()
    rprint(f"[green]added #{it.id}[/green]")


@items_app.command("confirm")
def items_confirm(item_ids: list[int]) -> None:
    db = _db()
    try:
        for i in item_ids:
            it = db.get_item(i)
            if it:
                it.confirmed = True
                db.save_item(it)
    finally:
        db.close()
    rprint("[green]confirmed[/green]")


@items_app.command("dismiss")
def items_dismiss(item_ids: list[int]) -> None:
    """Deactivate (keeps the pattern so it isn't re-proposed)."""
    db = _db()
    try:
        for i in item_ids:
            it = db.get_item(i)
            if it:
                it.active = False
                it.confirmed = True
                db.save_item(it)
    finally:
        db.close()
    rprint("[green]dismissed[/green]")


@items_app.command("delete")
def items_delete(item_ids: list[int]) -> None:
    db = _db()
    try:
        for i in item_ids:
            db.delete_item(i)
    finally:
        db.close()
    rprint("[green]deleted[/green]")


# -- statements ---------------------------------------------------------------
@statements_app.command("reset")
def statements_reset(
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation"),
    delete_files: bool = typer.Option(False, help="Also delete the archive folder and provider download manifests"),
) -> None:
    """Forget every archived statement so they can be re-downloaded / re-filed.
    Use after a parser fix.  Keeps the PDFs unless --delete-files."""
    import shutil

    cfg = _state["cfg"]
    if not yes and not typer.confirm("Delete all statement records" + (" and files" if delete_files else "") + "?"):
        raise typer.Exit(1)
    db = _db()
    try:
        n = db.delete_all_statements()
    finally:
        db.close()
    if delete_files:
        shutil.rmtree(cfg.general.archive_dir, ignore_errors=True)
        for m in (cfg.general.data_dir / "downloads").glob("*/.*_downloaded.json"):
            m.unlink(missing_ok=True)
    rprint(f"[green]removed {n} statement records[/green]" + (" and files" if delete_files else ""))


# -- secrets ----------------------------------------------------------------
@secret_app.command("set")
def secret_set(name: str, value: str | None = typer.Argument(None)) -> None:
    secrets.set_secret(name, value or typer.prompt("value", hide_input=True))
    rprint(f"[green]stored {name}[/green]")


@secret_app.command("set-json")
def secret_set_json(name: str, value: str) -> None:
    """e.g. cashfloor secret set-json mybank_login '{"username":"u","password":"p"}'"""
    secrets.set_json_secret(name, json.loads(value))
    rprint(f"[green]stored {name}[/green]")


@secret_app.command("delete")
def secret_delete(name: str) -> None:
    secrets.delete_secret(name)
    rprint(f"[green]deleted {name}[/green]")


# -- simplefin --------------------------------------------------------------
@simplefin_app.command("setup")
def simplefin_setup(setup_token: str, secret_name: str = "simplefin_access_url") -> None:
    """Exchange a one-time SimpleFIN setup token for an access URL and store it."""
    from .providers.simplefin import claim_access_url

    url = claim_access_url(setup_token)
    secrets.set_secret(secret_name, url)
    rprint(f"[green]access URL stored in keyring as {secret_name!r}[/green]  →  now run: cashfloor sync")


if __name__ == "__main__":  # pragma: no cover
    app()
