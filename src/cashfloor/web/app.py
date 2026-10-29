"""Local web dashboard.  Server-rendered, zero CDN dependencies, binds to
127.0.0.1 by default.  JSON endpoints under /api for scripts and widgets."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import Config
from ..db import Database
from ..models import AccountType, Frequency, ItemSource, ScheduledItem, from_cents, to_cents
from ..projection.engine import project
from ..sync import run_sync

TEMPLATES = Path(__file__).parent / "templates"


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
    templates.env.filters["money"] = lambda c: f"{from_cents(c):,.2f}" if c is not None else ""

    if with_scheduler:
        from ..scheduler import start_scheduler

        app.state.scheduler = start_scheduler(cfg)

    def db() -> Database:
        return Database(cfg.db_path)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        d = db()
        try:
            r = project(d, cfg)
            accounts = d.list_accounts()
            pending = [i for i in d.list_items() if not i.confirmed]
            syncs = d.last_syncs(8)
        finally:
            d.close()
        data = r.to_dict()
        upcoming = [x for x in data["days"] if x["events"]][:25]
        return templates.TemplateResponse(request, "dashboard.html", {
            "p": data, "chart": svg_chart(data["days"], data["safety_buffer"]), "accounts": accounts,
            "pending": pending, "upcoming": upcoming, "syncs": syncs, "cfg": cfg,
        })

    @app.post("/sync")
    def sync_now():
        d = db()
        try:
            run_sync(cfg, d)
        finally:
            d.close()
        return RedirectResponse("/", status_code=303)

    @app.get("/accounts", response_class=HTMLResponse)
    def accounts(request: Request):
        d = db()
        try:
            rows = d.list_accounts(active_only=False)
        finally:
            d.close()
        return templates.TemplateResponse(request, "accounts.html", {"accounts": rows, "types": list(AccountType)})

    @app.post("/accounts/{account_id}")
    def account_update(account_id: int, key: str = Form(""), nickname: str = Form(""), type: str = Form("other"),
                       in_pool: bool = Form(False), active: bool = Form(False),
                       due_date: str = Form(""), statement_balance: str = Form(""), minimum_payment: str = Form("")):
        d = db()
        try:
            d.set_account_user_fields(account_id, key=key, nickname=nickname, type=AccountType(type),
                                      in_pool=in_pool, active=active)
            if due_date:
                d.set_account_due(account_id, due_date=date.fromisoformat(due_date),
                                  statement_balance=to_cents(statement_balance) if statement_balance else None,
                                  minimum_payment=to_cents(minimum_payment) if minimum_payment else None)
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
        return templates.TemplateResponse(request, "items.html", {"items": rows, "accts": accts, "freqs": list(Frequency)})

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
    def statements(request: Request):
        d = db()
        try:
            rows = d.list_statements()
            accts = {a.id: a for a in d.list_accounts(active_only=False)}
        finally:
            d.close()
        return templates.TemplateResponse(request, "statements.html", {"statements": rows, "accts": accts})

    # ---- JSON API ----
    @app.get("/api/projection")
    def api_projection():
        d = db()
        try:
            return JSONResponse(project(d, cfg).to_dict())
        finally:
            d.close()

    @app.get("/api/discretionary")
    def api_discretionary():
        d = db()
        try:
            r = project(d, cfg)
        finally:
            d.close()
        return {"discretionary": from_cents(r.discretionary), "floor": from_cents(r.floor),
                "floor_date": r.floor_date.isoformat(), "as_of": r.as_of.isoformat(),
                "unconfirmed_count": r.unconfirmed_count}

    return app
