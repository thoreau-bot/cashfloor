# cashfloor

**Local-only cash-flow floor.** Pulls balances, due dates and transactions from
every bank, card and utility account you have; archives every statement with a
sane name; projects your daily balance up to three months out; and answers one
question at any moment:

> *How much extra money do I have for discretionary spending?*

The answer is `floor − buffer`, where **floor** is the lowest end-of-day
balance across your spendable accounts over the next N days (default 90),
given every paycheck, bill, transfer and card payment the app knows about.
That number is the most you could spend *today* without the pool ever dipping
below your safety buffer.

Everything runs on your machine: SQLite database, OS keyring for secrets,
server-rendered dashboard bound to `127.0.0.1`. No cloud, no telemetry, MIT.

---

## How data gets in

| Source | What it gives | Notes |
|---|---|---|
| **SimpleFIN Bridge** | balances + transactions for most US/CA banks & cards | Read-only aggregator, ~$15/yr, you hold the credentials. Recommended daily feed. |
| **Playwright scrapers** | balances, due dates, **statement PDFs**, utilities | One small plugin file per institution. Persistent browser profile so MFA is a one-time event. |
| **Inbox folder** | PDF / OFX / QFX / CSV you drop in by hand | Universal fallback. Any portal you can't automate still gets archived and projected. |

SimpleFIN does not carry statement PDFs or due dates, so cards/loans/utilities
get those from parsed statements (scraped or dropped) or from a manual entry.

## Quick start

**Windows:** double-click `scripts\setup.bat` once (creates `.venv`, installs
everything, runs the tests, writes `config.toml`). From then on, double-click
`scripts\shell.bat` to get a Command Prompt with the environment active.

**Linux / macOS:** `scripts/setup.sh` once, then `scripts/shell.sh`.

Or by hand:

```bash
git clone <this repo> && cd cashfloor
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"                             # add  ,scrape  for Playwright (then: playwright install chromium)
cashfloor init                                      # writes config.toml
```

Then, in the activated environment:

```bash
cashfloor simplefin setup SETUP_TOKEN               # token from bridge.simplefin.org (no brackets)
cashfloor sync
cashfloor accounts list                             # then label them:
cashfloor accounts set 1 --key main-checking --type checking --pool
cashfloor floor                                     # the projection, in words
cashfloor serve                                     # dashboard at http://127.0.0.1:8477
```

Requires Python 3.11+. Every dependency ships prebuilt wheels, so no C
compiler is needed on any platform.

`cashfloor serve` also runs the daily sync at `scheduler.daily_sync_time`.
To run it headless on a box that's always on, use a systemd unit / Task
Scheduler entry that launches `cashfloor serve`.

## The model

```
accounts ──┐
           ├─► cash pool (checking + savings flagged in_pool) ──► starting balance
scheduled items (paycheck, rent, subscriptions, transfers)  ──► daily events
card/loan/utility due dates (from statements or manual)     ──► payment events
                                                            ↓
            day-by-day walk, N days ──► floor, floor date, discretionary
```

* **Scheduled items** are signed from the account's point of view
  (`+3000 biweekly` paycheck, `-2200 monthly` rent). Frequencies: once,
  weekly, biweekly, semimonthly, monthly, quarterly, yearly; optional
  weekend rule and end date.
* **Transfers** between two pool accounts net to zero. A transfer *to* a card
  account replaces the automatic due-date payment for that card.
* **Recurring detection** proposes items from transaction history
  (≥3 hits, stable interval, amounts within tolerance). Proposals are
  *unconfirmed* and excluded from the projection until you confirm them —
  the dashboard shows how many are waiting.
* **Card payments** default to the statement balance on the due date and
  repeat monthly at the same amount through the horizon (`(est.)`), so the
  floor isn't falsely rosy after the first known due date. Switch to
  `card_payment_mode = "minimum"` or turn off estimation in config.

## Statement archive

```
statements/
  Chase/visa/2026/2026-10-18_Chase_visa_statement.pdf
  PGE/x7890/2026/2026-10-21_PGE_x7890_statement.pdf
  _review/mystery__3fa9c2d1.pdf          # couldn't identify → look at it
```

Parsing is regex-based on the first pages (institution, last 4, period,
statement date, due date, amount due, minimum, closing balance). Known
institutions are listed in `statements/parser.py::INSTITUTION_HINTS`; add
yours. Confidence < 0.6 or no matching account → `_review`. Duplicates (by
SHA-256) are discarded.

## Adding an institution scraper

See `docs/ADDING_AN_INSTITUTION.md`. Short version: copy
`providers/institutions/example_bank.py`, implement `login`,
`fetch_accounts`, and `download_statements`, add a `[[provider]]` block with
`kind = "playwright"`, store credentials with `cashfloor secret set-json`.
First run headful; once the saved profile survives MFA, set `headless = true`.

## CLI reference

```
cashfloor sync [--only NAME]        fetch all providers, archive statements, refresh proposals
cashfloor ingest PATH...            file PDFs without a sync (--copy to keep originals)
cashfloor floor [--json]            projection summary
cashfloor discretionary             just the number (for scripts / widgets)
cashfloor serve                     web dashboard + daily scheduler
cashfloor accounts list|set|due     label accounts, set due dates by hand
cashfloor items list|add|confirm|dismiss|delete
cashfloor secret set|set-json|delete
cashfloor simplefin setup TOKEN
```

JSON endpoints when serving: `GET /api/projection`, `GET /api/discretionary`,
docs at `/api/docs`.

## Security posture

* Secrets only in the OS keyring (`keyring` library). Never in config, DB or logs.
* `config.toml`, `data/`, `statements/`, `inbox/`, browser profiles and every
  PDF/OFX/CSV are git-ignored by default.
* Web UI binds to loopback. Put it behind a reverse proxy + auth yourself if
  you want LAN access; it ships with none.
* Scrapers run in a persistent Chromium profile under `data/browser-profiles/`
  — treat that folder like a password.

## Project layout

```
src/cashfloor/
  config.py        TOML config → pydantic models
  secrets.py       keyring wrapper
  models.py        dataclasses; money as integer cents
  db.py            sqlite3 schema + queries (no ORM)
  sync.py          one sync pass across providers
  scheduler.py     APScheduler daily job
  cli.py           typer CLI
  providers/       base, simplefin, filedrop, playwright_base, institutions/*
  statements/      parser (regex), organizer (naming, archiving, due-date feedback)
  projection/      recurrence rules, engine (floor/discretionary), recurring detector
  web/             FastAPI app + Jinja templates (no JS build, no CDN)
tests/             pytest; run `pytest`
```

## Roadmap

- [ ] More institution plugins (contributions welcome — one file each)
- [ ] Per-institution statement parsers where the generic regexes fall short
- [ ] "What if" sliders: add a hypothetical purchase, see the new floor
- [ ] Balance-history and variance charts
- [ ] Optional LAN mode with auth
- [ ] Notifications (floor below buffer, unconfirmed proposals, statement needs review)

## License

MIT — see `LICENSE`.
