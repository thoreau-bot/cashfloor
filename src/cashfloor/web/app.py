"""Local web dashboard.  Server-rendered, zero CDN dependencies, binds to
127.0.0.1 by default.  JSON endpoints under /api for scripts and widgets."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__, secrets
from ..config import GUI_SETTINGS, Config, apply_overrides
from ..db import Database
from ..models import (
    INVESTMENT_TYPES,
    LONG_TERM_TYPES,
    AccountType,
    Frequency,
    ItemSource,
    ScheduledItem,
    frequency_label,
    from_cents,
    to_cents,
)
from ..projection.engine import project
from ..sync import run_sync

TEMPLATES = Path(__file__).parent / "templates"
STATIC = Path(__file__).parent / "static"


def svg_chart(days: list[dict], buffer: float, width: int = 900, height: int = 260) -> str:
    """Tiny inline SVG line chart of projected balance, no JS."""
    if not days:
        return ""
    pad_l, pad_r, pad_t, pad_b = 60, 16, 12, 28
    vals = [d["balance"] for d in days] + [buffer, 0.0]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi = lo + 1
    span = hi - lo
    n = len(days)

    def x(i: int) -> float:
        return pad_l + (width - pad_l - pad_r) * i / max(n - 1, 1)

    def y(v: float) -> float:
        return pad_t + (height - pad_t - pad_b) * (1 - (v - lo) / span)

    pts = " ".join(f"{x(i):.1f},{y(d['balance']):.1f}" for i, d in enumerate(days))
    area = f"{x(0):.1f},{y(lo):.1f} {pts} {x(n-1):.1f},{y(lo):.1f}"
    floor_i = min(range(n), key=lambda i: days[i]["balance"])
    ticks = []
    for frac in (0, 0.25, 0.5, 0.75, 1):
        v = lo + span * frac
        ticks.append(f'<text x="{pad_l-6}" y="{y(v)+4:.1f}" text-anchor="end" class="tick">{v:,.0f}</text>'
                     f'<line x1="{pad_l}" x2="{width-pad_r}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>')
    labels = []
    for i in range(0, n, max(n // 6, 1)):
        labels.append(f'<text x="{x(i):.1f}" y="{height-8}" text-anchor="middle" class="tick">{days[i]["day"][5:]}</text>')
    return f"""<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="projected balance">
  {''.join(ticks)}
  <line x1="{pad_l}" x2="{width-pad_r}" y1="{y(buffer):.1f}" y2="{y(buffer):.1f}" class="buffer"/>
  <line x1="{pad_l}" x2="{width-pad_r}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="zero"/>
  <polygon points="{area}" class="area"/>
  <polyline points="{pts}" class="line"/>
  <circle cx="{x(floor_i):.1f}" cy="{y(days[floor_i]['balance']):.1f}" r="5" class="floor"/>
  {''.join(labels)}
</svg>"""


def create_app(cfg: Config, with_scheduler: bool = False) -> FastAPI:
    app = FastAPI(title="cashfloor", docs_url="/api/docs")
    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters["freq"] = frequency_label
    templates.env.filters["money"] = lambda c: f"{from_cents(c):,.2f}" if c is not None else ""
    templates.env.globals["asset_v"] = __version__  # static URLs carry ?v=<version> so updates are live on reload

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(STATIC / "favicon.ico")

    if with_scheduler:
        from ..scheduler import start_scheduler

        app.state.scheduler = start_scheduler(cfg)

    def db() -> Database:
        return Database(cfg.db_path)

    def effective(d: Database) -> Config:
        """config.toml + GUI-stored overrides."""
        return apply_overrides(cfg, d.get_settings())

    def simplefin_secret_name() -> str:
        for pc in cfg.provider:
            if pc.kind == "simplefin":
                return pc.secret or "simplefin_access_url"
        return "simplefin_access_url"

    def simplefin_connected() -> bool | None:
        """True/False, or None if the keyring itself is unavailable."""
        try:
            return bool(secrets.get_secret(simplefin_secret_name()))
        except Exception:  # noqa: BLE001 — no keyring backend (headless box)
            return None

    def d_paid_list(as_of: date) -> list[dict]:
        """Occurrences the user marked paid in the last few days, for undo."""
        from ..projection.engine import POSTED_LOOKBACK

        d = db()
        try:
            rows = d.get_settings_prefix("paid:")
            accts = {a.id: a for a in d.list_accounts(active_only=False)}
            items = {i.id: i for i in d.list_items(active_only=False)}
        finally:
            d.close()
        out = []
        for key in rows:
            _p, kind, ref, day = key.split(":", 3)
            when = date.fromisoformat(day)
            if (as_of - when).days > POSTED_LOOKBACK:
                continue
            ref_id = int(ref)
            name = (items[ref_id].name if kind == "item" and ref_id in items
                    else accts[ref_id].display_name if ref_id in accts else "?")
            out.append({"kind": kind, "id": ref_id, "day": day, "name": name})
        return sorted(out, key=lambda x: x["day"])

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        d = db()
        try:
            c = effective(d)
            r = project(d, c)
            accounts = d.list_accounts()
            pending = [i for i in d.list_items() if not i.confirmed]
            syncs = d.last_syncs(8)
            connections = d.list_connections()
            invest_total = sum(a.balance for a in accounts if a.type in INVESTMENT_TYPES)
            debt_total = sum(-a.balance for a in accounts if a.type in LONG_TERM_TYPES and a.balance < 0)
        finally:
            d.close()
        data = r.to_dict()
        upcoming = [x for x in data["days"] if x["events"]][:25]
        recently_paid = d_paid_list(r.as_of)
        pool = [a for a in accounts if a.in_pool]
        setup_hints = []
        if not accounts:
            setup_hints.append(("Connect SimpleFIN or drop files in the inbox, then sync.", "/settings"))
        elif not pool:
            setup_hints.append(("No account is in the cash pool yet — tick 'pool' on the accounts you spend from.", "/accounts"))
        if accounts and not c.projection.default_payment_account:
            setup_hints.append(("Choose which account pays your cards (default payment account).", "/settings"))
        return templates.TemplateResponse(request, "dashboard.html", {
            "p": data, "chart": svg_chart(data["days"], data["safety_buffer"]), "accounts": accounts,
            "pending": pending, "upcoming": upcoming, "syncs": syncs, "cfg": c, "setup_hints": setup_hints,
            "connections": connections, "invest_total": invest_total, "debt_total": debt_total,
            "recently_paid": recently_paid,
        })

    @app.post("/paid")
    def mark_paid(kind: str = Form(...), ref_id: int = Form(...), day: str = Form(...), undo: bool = Form(False)):
        """Mark a projected outflow occurrence as paid by hand (or undo that).
        Items also roll to their next date so the Scheduled page agrees."""
        from ..projection.engine import mark_occurrence_paid

        d = db()
        try:
            mark_occurrence_paid(d, kind, ref_id, date.fromisoformat(day), undo=undo)
        finally:
            d.close()
        return RedirectResponse("/", status_code=303)

    @app.post("/sync")
    def sync_now():
        d = db()
        try:
            run_sync(effective(d), d)
        finally:
            d.close()
        return RedirectResponse("/", status_code=303)

    # ---- settings ----
    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request, msg: str = "", err: str = ""):
        d = db()
        try:
            stored = d.get_settings()
            c = effective(d)
            keys = [a.key for a in d.list_accounts() if a.key]
        finally:
            d.close()
        # Values as config.toml (or defaults) would have them, for the hint column.
        base = {
            "projection.horizon_days": cfg.projection.horizon_days,
            "projection.safety_buffer": cfg.projection.safety_buffer,
            "projection.default_payment_account": cfg.projection.default_payment_account or "",
            "projection.card_payment_mode": cfg.projection.card_payment_mode,
            "projection.estimate_future_card_payments": cfg.projection.estimate_future_card_payments,
            "recurring.min_occurrences": cfg.recurring.min_occurrences,
            "recurring.amount_tolerance": cfg.recurring.amount_tolerance,
            "recurring.income_merge_tolerance": cfg.recurring.income_merge_tolerance,
            "scheduler.daily_sync_time": cfg.scheduler.daily_sync_time,
            "general.timezone": cfg.general.timezone,
            "simplefin.lookback_days": next((pc.lookback_days for pc in cfg.provider if pc.kind == "simplefin"), 60),
            "projection.use_available_balance": cfg.projection.use_available_balance,
            "projection.stale_after_days": cfg.projection.stale_after_days,
            "general.keep_raw_responses": cfg.general.keep_raw_responses,
            "projection.assume_card_payoff": cfg.projection.assume_card_payoff,
            "projection.default_card_pay_day": cfg.projection.default_card_pay_day,
            "projection.assume_inflows_landed": cfg.projection.assume_inflows_landed,
            "projection.max_assumed_payoff": cfg.projection.max_assumed_payoff,
        }
        current = {
            "projection.horizon_days": c.projection.horizon_days,
            "projection.safety_buffer": c.projection.safety_buffer,
            "projection.default_payment_account": c.projection.default_payment_account or "",
            "projection.card_payment_mode": c.projection.card_payment_mode,
            "projection.estimate_future_card_payments": c.projection.estimate_future_card_payments,
            "recurring.min_occurrences": c.recurring.min_occurrences,
            "recurring.amount_tolerance": c.recurring.amount_tolerance,
            "recurring.income_merge_tolerance": c.recurring.income_merge_tolerance,
            "scheduler.daily_sync_time": c.scheduler.daily_sync_time,
            "general.timezone": c.general.timezone,
            "simplefin.lookback_days": next((pc.lookback_days for pc in c.provider if pc.kind == "simplefin"), 60),
            "projection.use_available_balance": c.projection.use_available_balance,
            "projection.stale_after_days": c.projection.stale_after_days,
            "general.keep_raw_responses": c.general.keep_raw_responses,
            "projection.assume_card_payoff": c.projection.assume_card_payoff,
            "projection.default_card_pay_day": c.projection.default_card_pay_day,
            "projection.assume_inflows_landed": c.projection.assume_inflows_landed,
            "projection.max_assumed_payoff": c.projection.max_assumed_payoff,
        }
        raw_dir = cfg.general.data_dir / "raw"
        raw_files = sorted(raw_dir.glob("*.json"), reverse=True)[:10] if raw_dir.exists() else []
        return templates.TemplateResponse(request, "settings.html", {
            "raw_files": [f.name for f in raw_files], "raw_dir": str(raw_dir),
            "help": GUI_SETTINGS, "base": base, "current": current, "stored": stored, "account_keys": keys,
            "simplefin": simplefin_connected(), "config_path": str(cfg.config_path or "(no config.toml — using defaults)"),
            "msg": msg, "err": err,
        })

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        d = db()
        try:
            checkboxes = {"projection.estimate_future_card_payments", "projection.use_available_balance",
                          "general.keep_raw_responses", "projection.assume_card_payoff",
                          "projection.assume_inflows_landed"}
            for key in GUI_SETTINGS:
                if key in checkboxes:
                    val = "true" if form.get(key) else "false"
                else:
                    val = str(form.get(key, "")).strip()
                d.set_setting(key, val)
            # validate by applying; roll back on error
            try:
                effective(d)
            except Exception as e:  # noqa: BLE001
                return RedirectResponse(f"/settings?err=Invalid+value:+{str(e)[:120]}", status_code=303)
        finally:
            d.close()
        return RedirectResponse("/settings?msg=Saved", status_code=303)

    @app.post("/settings/reset")
    def settings_reset():
        d = db()
        try:
            for key in GUI_SETTINGS:
                d.set_setting(key, None)
        finally:
            d.close()
        return RedirectResponse("/settings?msg=Reset+to+config.toml+values", status_code=303)

    @app.post("/settings/simplefin")
    def settings_simplefin(setup_token: str = Form(...)):
        from ..providers.simplefin import claim_access_url

        token = setup_token.strip().strip("<>")
        try:
            url = claim_access_url(token)
            secrets.set_secret(simplefin_secret_name(), url)
        except Exception as e:  # noqa: BLE001
            return RedirectResponse(f"/settings?err=SimpleFIN:+{str(e)[:160]}", status_code=303)
        return RedirectResponse("/settings?msg=SimpleFIN+connected.+Press+Sync+Now.", status_code=303)

    @app.post("/settings/simplefin/disconnect")
    def settings_simplefin_disconnect():
        secrets.delete_secret(simplefin_secret_name())
        return RedirectResponse("/settings?msg=SimpleFIN+disconnected", status_code=303)

    @app.get("/accounts", response_class=HTMLResponse)
    def accounts(request: Request):
        d = db()
        try:
            rows = d.list_accounts(active_only=False)
        finally:
            d.close()
        return templates.TemplateResponse(request, "accounts.html", {"accounts": rows, "types": list(AccountType)})

    @app.post("/accounts/add")
    def account_add(name: str = Form(...), institution: str = Form(...), type: str = Form("utility"),
                    key: str = Form(""), last4: str = Form(""), balance: str = Form("0"), in_pool: bool = Form(False),
                    due_date: str = Form(""), statement_balance: str = Form(""), minimum_payment: str = Form(""),
                    pay_day: str = Form("")):
        d = db()
        try:
            d.add_manual_account(
                name=name, institution=institution, type=AccountType(type), key=key or None, last4=last4 or None,
                balance=to_cents(balance) if balance.strip() else 0, in_pool=in_pool,
                statement_balance=to_cents(statement_balance) if statement_balance.strip() else None,
                minimum_payment=to_cents(minimum_payment) if minimum_payment.strip() else None,
                due_date=date.fromisoformat(due_date) if due_date else None,
                pay_day=int(pay_day) if pay_day.strip() else None,
            )
        finally:
            d.close()
        return RedirectResponse("/accounts", status_code=303)

    @app.post("/accounts/{account_id}")
    def account_update(account_id: int, key: str = Form(""), nickname: str = Form(""), type: str = Form("other"),
                       in_pool: bool = Form(False), active: bool = Form(False), last4: str = Form(""),
                       pay_day: str = Form(""), balance: str = Form(""),
                       due_date: str = Form(""), statement_balance: str = Form(""), minimum_payment: str = Form("")):
        d = db()
        try:
            d.set_account_user_fields(account_id, key=key, nickname=nickname, type=AccountType(type),
                                      in_pool=in_pool, active=active, last4=last4,
                                      pay_day=int(pay_day) if pay_day.strip() else None)
            if due_date:
                d.set_account_due(account_id, due_date=date.fromisoformat(due_date),
                                  statement_balance=to_cents(statement_balance) if statement_balance else None,
                                  minimum_payment=to_cents(minimum_payment) if minimum_payment else None)
            a = d.get_account(account_id)
            if a and a.provider == "manual" and balance.strip():
                d.set_manual_balance(account_id, to_cents(balance))
        finally:
            d.close()
        return RedirectResponse("/accounts", status_code=303)

    @app.get("/items", response_class=HTMLResponse)
    def items(request: Request):
        d = db()
        try:
            rows = d.list_items(active_only=False)
            accts = {a.id: a for a in d.list_accounts(active_only=False)}
        finally:
            d.close()
        from ..projection.links import suggest_counter_account

        suggestions = {}
        for it in rows:
            if it.counter_account_id is None and it.amount < 0:
                sug = suggest_counter_account(it, list(accts.values()))
                if sug:
                    suggestions[it.id] = sug.id
        return templates.TemplateResponse(request, "items.html", {"items": rows, "accts": accts,
                                                                  "freqs": list(Frequency), "suggestions": suggestions})

    @app.post("/items/{item_id}/pays")
    def item_pays(item_id: int, counter_account_id: str = Form("")):
        d = db()
        try:
            d.set_item_counter_account(item_id, int(counter_account_id) if counter_account_id else None)
        finally:
            d.close()
        return RedirectResponse("/items", status_code=303)

    @app.post("/items/add")
    def item_add(name: str = Form(...), account_id: int = Form(...), amount: str = Form(...), frequency: str = Form(...),
                 next_date: str = Form(...), counter_account_id: str = Form(""), weekend_rule: str = Form("none")):
        d = db()
        try:
            d.save_item(ScheduledItem(
                id=None, name=name, account_id=account_id, amount=to_cents(amount), frequency=Frequency(frequency),
                next_date=date.fromisoformat(next_date),
                source=ItemSource.TRANSFER if counter_account_id else ItemSource.MANUAL,
                counter_account_id=int(counter_account_id) if counter_account_id else None, weekend_rule=weekend_rule,
            ))
        finally:
            d.close()
        return RedirectResponse("/items", status_code=303)

    @app.post("/items/{item_id}/{action}")
    def item_action(item_id: int, action: str):
        d = db()
        try:
            it = d.get_item(item_id)
            if it:
                if action == "confirm":
                    it.confirmed = True
                elif action == "dismiss":
                    it.active, it.confirmed = False, True
                elif action == "activate":
                    it.active = True
                elif action == "delete":
                    d.delete_item(item_id)
                    it = None
                if it:
                    d.save_item(it)
        finally:
            d.close()
        return RedirectResponse("/items", status_code=303)

    @app.get("/statements", response_class=HTMLResponse)
    def statements(request: Request, msg: str = ""):
        d = db()
        try:
            rows = d.list_statements()
            accts = {a.id: a for a in d.list_accounts(active_only=False)}
        finally:
            d.close()
        return templates.TemplateResponse(request, "statements.html", {"statements": rows, "accts": accts, "msg": msg})

    @app.post("/statements/backfill")
    def statements_backfill():
        from ..projection.recurring import detect_recurring
        from ..statements.organizer import import_statement_transactions

        d = db()
        try:
            accts = {a.id: a for a in d.list_accounts(active_only=False)}
            total = 0
            for st in d.list_statements():
                a = accts.get(st.account_id)
                if a and st.archived_path and Path(st.archived_path).exists():
                    total += import_statement_transactions(d, st, a)
            c = effective(d)
            today = date.today()
            for p_ in detect_recurring(c, d.list_transactions(since=today.replace(year=today.year - 3)), today,
                                       d.get_payee_aliases()):
                if d.find_item_by_pattern(p_.account_id, p_.payee_pattern or "") is None:
                    d.save_item(p_)
        finally:
            d.close()
        return RedirectResponse(f"/statements?msg={total}+transactions+imported+from+statements", status_code=303)

    # ---- raw provider data (what came back, verbatim) ----
    @app.get("/data/raw", response_class=HTMLResponse)
    @app.get("/data/raw/{name}", response_class=HTMLResponse)
    def raw_view(request: Request, name: str = ""):
        raw_dir = cfg.general.data_dir / "raw"
        files = sorted(raw_dir.glob("*.json"), reverse=True) if raw_dir.exists() else []
        chosen = None
        if name:
            cand = raw_dir / Path(name).name  # no traversal
            chosen = cand if cand in files else None
        elif files:
            chosen = files[0]
        body = chosen.read_text() if chosen else ""
        return templates.TemplateResponse(request, "raw.html", {
            "files": [f.name for f in files[:30]], "chosen": chosen.name if chosen else "", "body": body,
        })

    @app.get("/liabilities", response_class=HTMLResponse)
    def liabilities(request: Request):
        from ..models import LONG_TERM_TYPES
        from ..projection.engine import installment

        d = db()
        try:
            c = effective(d)
            accounts = [a for a in d.list_accounts(active_only=False) if a.active]
            items = d.list_items()
        finally:
            d.close()
        linked = {i.counter_account_id: i for i in items if i.counter_account_id and i.confirmed and i.active}
        loans, cards, unknown = [], [], 0
        for a in accounts:
            if a.type in LONG_TERM_TYPES:
                if a.id in linked:
                    it = linked[a.id]
                    loans.append({"account": a, "payment": -it.amount, "source": f"scheduled item '{it.name}'",
                                  "next": it.next_date})
                    continue
                inst = installment(a)
                if inst is None:
                    unknown += 1
                    loans.append({"account": a, "payment": None, "source": "—", "next": a.due_date})
                    continue
                amt, how = inst
                nxt = a.due_date or (a.effective_pay_day and f"day {a.effective_pay_day} monthly")
                loans.append({"account": a, "payment": amt, "source": how, "next": nxt})
            elif a.type == AccountType.CREDIT:
                if a.id in linked:
                    src = f"scheduled item '{linked[a.id].name}'"
                elif a.due_date:
                    src = "statement balance on due date" if c.projection.card_payment_mode != "minimum" else "minimum on due date"
                elif c.projection.assume_card_payoff and a.balance < 0:
                    src = "assumed payoff on pay day"
                else:
                    src = "not projected"
                cards.append({"account": a, "source": src})
        return templates.TemplateResponse(request, "liabilities.html", {
            "loans": loans, "cards": cards, "unknown": unknown,
            "loans_total": sum(-r["account"].balance for r in loans if r["account"].balance < 0),
            "loans_payment": sum(r["payment"] or 0 for r in loans),
            "cards_total": sum(-r["account"].balance for r in cards if r["account"].balance < 0),
        })

    @app.get("/holdings", include_in_schema=False)
    def holdings_redirect():
        return RedirectResponse("/investments", status_code=301)

    @app.get("/investments", response_class=HTMLResponse)
    def investments(request: Request):
        from collections import namedtuple

        from ..models import HEALTHCARE_TYPES
        from ..projection.engine import fsa_year_end

        d = db()
        try:
            c = effective(d)
            accounts = [a for a in d.list_accounts(active_only=False) if a.active]
            holdings = d.list_holdings()
        finally:
            d.close()
        HV = namedtuple("HV", "value gain")
        hv: dict[int, HV] = {}
        for h in holdings:
            cur = hv.get(h.account_id, HV(0, 0))
            gain = cur.gain
            if gain is not None and h.market_value is not None and h.cost_basis is not None:
                gain += h.market_value - h.cost_basis
            else:
                gain = None
            hv[h.account_id] = HV(cur.value + (h.market_value or 0), gain)
        retirement = [a for a in accounts if a.type == AccountType.INVESTMENT]
        healthcare = [a for a in accounts if a.type in HEALTHCARE_TYPES]
        today = date.today()
        year_end = fsa_year_end(today)
        accts = {a.id: a for a in accounts}
        return templates.TemplateResponse(request, "investments.html", {
            "retirement": retirement, "healthcare": healthcare, "holdings": holdings, "accts": accts, "hv": hv,
            "retirement_total": sum(a.balance for a in retirement),
            "retirement_hold": sum(hv[a.id].value for a in retirement if a.id in hv),
            "healthcare_total": sum(a.balance for a in healthcare),
            "healthcare_hold": sum(hv[a.id].value for a in healthcare if a.id in hv),
            "fsa_forfeit": sum(a.balance for a in healthcare if a.type == AccountType.FSA and a.balance > 0),
            "hold_cost": sum(h.cost_basis or 0 for h in holdings),
            "hold_value": sum(h.market_value or 0 for h in holdings),
            "hold_gain": sum((h.market_value or 0) - (h.cost_basis or 0) for h in holdings),
            "year_end": year_end, "days_left": (year_end - today).days, "warn_days": c.projection.fsa_warn_days,
            "warn_from": year_end - timedelta(days=c.projection.fsa_warn_days),
        })

    # ---- JSON API ----
    @app.get("/api/projection")
    def api_projection():
        d = db()
        try:
            return JSONResponse(project(d, effective(d)).to_dict())
        finally:
            d.close()

    @app.get("/api/discretionary")
    def api_discretionary():
        d = db()
        try:
            r = project(d, effective(d))
        finally:
            d.close()
        return {"discretionary": from_cents(r.discretionary), "floor": from_cents(r.floor),
                "floor_date": r.floor_date.isoformat(), "as_of": r.as_of.isoformat(),
                "unconfirmed_count": r.unconfirmed_count, "warnings": r.warnings}

    return app
