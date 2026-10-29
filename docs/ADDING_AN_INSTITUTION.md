# Adding an institution scraper

Each portal is one Python file exposing a `Scraper` class. The shared
`PlaywrightProvider` handles the browser, the persistent profile, error
isolation, and hands downloaded PDFs to the statement pipeline. Your file
only knows how to drive that one website.

## 1. Record the flow

```bash
pip install -e ".[scrape]" && playwright install chromium
playwright codegen https://portal.example.com
```

Log in by hand and click through to the balance page and the statements
list. Codegen writes the selectors; copy the useful ones.

## 2. Create the plugin

Copy `src/cashfloor/providers/institutions/example_bank.py` to
`<slug>.py` and implement:

| Method | Required | Returns |
|---|---|---|
| `login(page)` | yes | nothing; leave the page logged in |
| `is_logged_in(page)` | recommended | bool — lets a saved session skip `login()` |
| `fetch_accounts(page)` | yes | `list[Account]` with `external_id`, `name`, `type`, `balance` (cents) and, if shown, `statement_balance`, `minimum_payment`, `due_date` |
| `fetch_transactions(page, account, since)` | optional | `list[Transaction]` |
| `download_statements(page, account, since, dest)` | optional | `list[Path]` of PDFs saved under `dest` |

Rules of thumb for odd portals:

* **Look for the JSON first.** Open DevTools → Network while clicking around.
  Most portals load balances and statement lists from an internal API; calling
  it with `page.request.get(url)` (which reuses the session cookies) is far
  more robust than DOM scraping.
* Prefer `page.get_by_role(...)` / `get_by_label(...)` / `get_by_text(...)`
  over CSS class selectors; they survive redesigns.
* Wrap each statement click in `with page.expect_download() as dl:`.
* Raise `MFARequired` if you detect a challenge while headless so the sync
  log says exactly what happened.
* Don't rename files; the pipeline names them from the PDF contents.

## 3. Configure

```toml
[[provider]]
name = "mycu"
kind = "playwright"
module = "mycu"            # providers/institutions/mycu.py
secret = "mycu_login"
headless = false           # first run: you may need to complete MFA
statements_since = "2024-01-01"
```

```bash
cashfloor secret set-json mycu_login '{"username":"you","password":"…"}'
cashfloor sync --only mycu
```

Once the persistent profile in `data/browser-profiles/mycu/` keeps you logged
in across runs, set `headless = true`.

## 4. Teach the parser (if needed)

If the archive puts this institution's PDFs in `_review/`, add a hint to
`INSTITUTION_HINTS` in `statements/parser.py` (a lowercase string that
appears in the first page of every statement → folder name). If the generic
regexes miss the due date or amount, add a pattern to `FIELD_PATTERNS`, and
add a short fixture to `tests/test_statements.py`.

## 5. Share it

Plugins are self-contained and contain no secrets, so they're easy to PR.
Please include: the institution name, which methods are implemented, whether
the JSON API or DOM is used, and the date you last verified it.
