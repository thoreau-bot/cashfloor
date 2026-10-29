"""Template institution scraper.  Copy this file, rename, fill in selectors.

Reference it from config.toml:

    [[provider]]
    name = "mybank"
    kind = "playwright"
    module = "mybank"            # -> providers/institutions/mybank.py
    secret = "mybank_login"      # cashfloor secret set-json mybank_login '{"username":"u","password":"p"}'
    headless = false

Tips for odd portals:
* Record a session first: `playwright codegen https://portal.example.com`
  writes the selectors for you.
* Prefer `page.get_by_role(...)` / `get_by_label(...)` over CSS — they
  survive redesigns better.
* For statement lists rendered as a table, iterate rows and use
  `with page.expect_download() as dl:` around the click.
* If a portal exposes a JSON API behind the login (most do), call it with
  `page.request.get(...)` instead of scraping the DOM — far more robust.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from ...models import Account, AccountType, to_cents
from ..playwright_base import InstitutionScraper

try:
    from playwright.sync_api import Page
except ImportError:  # pragma: no cover
    Page = object  # type: ignore[misc,assignment]


class Scraper(InstitutionScraper):
    institution = "Example Bank"
    login_url = "https://online.example-bank.test/login"

    def is_logged_in(self, page: Page) -> bool:
        return page.locator("text=Log out").count() > 0

    def login(self, page: Page) -> None:
        page.get_by_label("Username").fill(self.credentials["username"])
        page.get_by_label("Password").fill(self.credentials["password"])
        page.get_by_role("button", name=re.compile("sign in|log in", re.I)).click()
        # Headful run: the human completes MFA here.  Wait for the dashboard.
        page.wait_for_url(re.compile(r"/dashboard"), timeout=180_000)

    def fetch_accounts(self, page: Page) -> list[Account]:
        page.goto("https://online.example-bank.test/accounts")
        accounts: list[Account] = []
        for row in page.locator("table.accounts tbody tr").all():
            name = row.locator("td.name").inner_text().strip()
            number = row.locator("td.number").inner_text().strip()
            balance = row.locator("td.balance").inner_text().strip()
            accounts.append(
                Account(
                    id=None,
                    provider="",  # filled by PlaywrightProvider
                    external_id=f"example:{number[-4:]}",
                    name=name,
                    institution=self.institution,
                    type=AccountType.CHECKING,
                    last4=number[-4:],
                    balance=to_cents(balance.replace("$", "").replace(",", "")),
                )
            )
        return accounts

    def download_statements(self, page: Page, account: Account, since: date, dest: Path) -> list[Path]:
        page.goto(f"https://online.example-bank.test/accounts/{account.last4}/statements")
        saved: list[Path] = []
        for link in page.get_by_role("link", name=re.compile(r"statement", re.I)).all():
            with page.expect_download() as dl_info:
                link.click()
            dl = dl_info.value
            target = dest / dl.suggested_filename
            dl.save_as(target)
            saved.append(target)
        return saved
