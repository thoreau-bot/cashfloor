"""Playwright-based institution scrapers.

Design goals, given 10+ institutions with some odd portals:

* One small plugin file per institution under `providers/institutions/`
  (or any installed package exposing a `cashfloor.institutions` entry point).
* A persistent browser profile per institution (cookies, device trust) so
  MFA is normally a one-time event.  First login runs headful; once the
  saved session survives, flip `headless = true` in config.
* The plugin only knows how to drive the site.  Everything else — file
  hashing, renaming, archiving, DB writes — happens in the shared pipeline.
* A plugin that fails raises; the sync service records the error and moves
  on to the next provider.  One broken portal never blocks the others.

Plugin contract: subclass `InstitutionScraper` and implement `login`,
`fetch_accounts`, and optionally `download_statements`.
"""

from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import secrets
from ..models import Account, Transaction
from .base import Provider, SyncResult

log = logging.getLogger("cashfloor.playwright")

if TYPE_CHECKING:  # playwright is an optional dependency
    from playwright.sync_api import BrowserContext, Page


class InstitutionScraper(ABC):
    """Base class for one institution's portal."""

    #: Human-readable institution name used for archive folders.
    institution: str = "Unknown"
    #: Login page URL.
    login_url: str = ""

    def __init__(self, credentials: dict[str, Any], options: dict[str, Any]):
        self.credentials = credentials
        self.options = options
        #: Anything the plugin wants saved verbatim under data/raw/ (e.g. the
        #: portal's document-list JSON) so parsers can be refined offline.
        self.raw: dict[str, Any] = {}

    @abstractmethod
    def login(self, page: Page) -> None:
        """Drive the login form.  Leave MFA to the human when headful; raise
        `MFARequired` if headless and a challenge appears."""

    @abstractmethod
    def fetch_accounts(self, page: Page) -> list[Account]:
        """Return accounts with current balance (cents) and, where the portal
        shows them, statement_balance / minimum_payment / due_date."""

    def fetch_transactions(self, page: Page, account: Account, since: date) -> list[Transaction]:
        return []

    def download_statements(self, page: Page, account: Account, since: date, dest: Path) -> list[Path]:
        """Download statement PDFs newer than `since` into `dest`.  Return paths.
        Naming is irrelevant here; the statement pipeline renames."""
        return []

    def download_all_statements(self, page: Page, since: date, dest: Path) -> list[Path]:
        """For statement-only plugins (balances come from elsewhere): download
        every statement newer than `since` regardless of account.  Called after
        the per-account loop."""
        return []

    def is_logged_in(self, page: Page) -> bool:
        """Cheap check so a saved session can skip login()."""
        return False


class MFARequired(RuntimeError):
    pass


def load_scraper_class(module_name: str) -> type[InstitutionScraper]:
    """Resolve `module_name` to a scraper class.

    Order: built-in `cashfloor.providers.institutions.<name>`, then a dotted
    path, then `cashfloor.institutions` entry points.  The module must expose
    `Scraper` (a subclass of InstitutionScraper).
    """
    candidates = [f"cashfloor.providers.institutions.{module_name}", module_name]
    for cand in candidates:
        try:
            mod = importlib.import_module(cand)
        except ModuleNotFoundError:
            continue
        cls = getattr(mod, "Scraper", None)
        if cls and issubclass(cls, InstitutionScraper):
            return cls
    from importlib.metadata import entry_points

    for ep in entry_points(group="cashfloor.institutions"):
        if ep.name == module_name:
            return ep.load()
    raise ImportError(f"No institution scraper named {module_name!r}")


class PlaywrightProvider(Provider):
    kind = "playwright"

    def supports_statements(self) -> bool:
        return True

    def _profile_dir(self) -> Path:
        d = self.cfg.browser_profiles_dir / self.pc.name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _downloads_dir(self) -> Path:
        d = self.cfg.general.data_dir / "downloads" / self.pc.name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def fetch(self) -> SyncResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "Install scraping support: pip install 'cashfloor[scrape]' && playwright install chromium"
            ) from e

        if not self.pc.module:
            raise ValueError(f"provider {self.pc.name!r}: `module` is required for kind=playwright")
        creds = secrets.get_json_secret(self.pc.secret) if self.pc.secret else {}
        scraper = load_scraper_class(self.pc.module)(creds or {}, self.pc.options)
        res = SyncResult(provider=self.name)
        since = self.pc.statements_since or date(date.today().year - 1, 1, 1)
        now = datetime.now(UTC)

        with sync_playwright() as p:
            browser = None
            cdp_url = self.pc.options.get("cdp_url")
            if cdp_url:
                # Attach to a browser the user started themselves with
                # --remote-debugging-port=NNNN (Vivaldi, Brave, Chrome ...).
                # Runs in the user's own browser with their existing login;
                # nothing is launched, disguised, or closed by us.
                log.info("%s: connecting to your browser at %s", self.pc.name, cdp_url)
                browser = p.chromium.connect_over_cdp(cdp_url)
                ctx: BrowserContext = browser.contexts[0] if browser.contexts else browser.new_context()
            else:
                launch_kwargs: dict[str, Any] = dict(
                    user_data_dir=str(self._profile_dir()),
                    headless=self.pc.headless,
                    accept_downloads=True,
                    viewport={"width": 1280, "height": 900},
                )
                # "chrome" / "msedge" uses the real installed browser.
                if self.pc.options.get("executable_path"):
                    launch_kwargs["executable_path"] = str(Path(self.pc.options["executable_path"]).expanduser())
                elif self.pc.options.get("channel"):
                    launch_kwargs["channel"] = self.pc.options["channel"]
                log.info("%s: launching %s (profile %s)", self.pc.name,
                         launch_kwargs.get("channel") or launch_kwargs.get("executable_path") or "bundled Chromium",
                         self._profile_dir())
                ctx = p.chromium.launch_persistent_context(**launch_kwargs)
            try:
                page = ctx.new_page()
                log.info("%s: opening %s", self.pc.name, scraper.login_url)
                page.goto(scraper.login_url, wait_until="domcontentloaded")
                if not scraper.is_logged_in(page):
                    log.info("%s: not logged in — please sign in in the browser window (waiting up to %ss)",
                             self.pc.name, scraper.options.get("login_timeout_s", 300))
                    scraper.login(page)
                log.info("%s: logged in; fetching", self.pc.name)
                for acct in scraper.fetch_accounts(page):
                    acct.provider = self.name
                    acct.institution = acct.institution or scraper.institution
                    acct.last_synced = now
                    acct.balance_as_of = acct.balance_as_of or now
                    res.accounts.append(acct)
                    for t in scraper.fetch_transactions(page, acct, since):
                        t.__dict__["_account_external_id"] = acct.external_id
                        res.transactions.append(t)
                    try:
                        res.statement_files.extend(
                            scraper.download_statements(page, acct, since, self._downloads_dir())
                        )
                    except Exception as e:  # noqa: BLE001
                        res.errors.append(f"{acct.name}: statements failed: {e}")
                try:
                    res.statement_files.extend(scraper.download_all_statements(page, since, self._downloads_dir()))
                except Exception as e:  # noqa: BLE001
                    res.errors.append(f"statements failed: {e}")
            finally:
                if browser is not None:
                    page.close()  # leave the user's browser running; just drop our tab
                    browser.close()  # disconnects CDP only
                else:
                    ctx.close()
        log.info("%s: %d statement file(s) downloaded", self.pc.name, len(res.statement_files))
        if scraper.raw:
            res.raw = scraper.raw
        return res
