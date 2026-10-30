"""USAA — statement/document downloader.

Statement-only plugin: balances and transactions for USAA come through
SimpleFIN; this fetches every PDF under "My Documents" (bank statements,
insurance statements, tax forms, notices).

How USAA serves documents (observed 2026-10, web app "ent-my-documents-web-ui"):

* Opening https://www.usaa.com/my/documents makes the page call
      GET https://api.usaa.com/v1/enterprise/my-documents/experience/
          individuals/{customerGuid}/documents?limit=100
  once; filtering and paging in the UI are client-side.  Default window is
  the last 18 months.
* Each document has its own content GUID.  The PDF is
      GET https://api.usaa.com/v1/enterprise/content-viewer/render/content/
          {contentGuid}/display-dates/{YYYY-MM-DD}
  with request header `Transform-To-Pdf: true`; the body is raw %PDF.
* Every api.usaa.com call needs two headers the page adds itself:
  `Authorization` and `api-key`.  Cookies alone -> 401.
* Viewing a document in the UI also PATCHes `.../open` (marks it read).
  We deliberately do not, so your unread markers are untouched.

Strategy: let the page do the login and the list call; capture the list
JSON and the two auth headers from the page's own traffic; then fetch each
PDF directly with the browser context's request client (cookies included).
No menu clicking, so UI redesigns don't matter; only the API shape does.

Config:

    [[provider]]
    name = "usaa"
    kind = "playwright"
    module = "usaa"
    headless = false            # USAA's bot detection: keep headful, use real Chrome
    statements_since = "2025-01-01"
    [provider.options]
    channel = "chrome"          # installed Google Chrome / "msedge"; or instead:
    # executable_path = "C:/Users/you/AppData/Local/Vivaldi/Application/vivaldi.exe"
    # include = ["STATEMENT"]   # only titles containing one of these (case-insensitive); default: all
    # login_timeout_s = 300     # how long to wait for you to finish logging in

No credentials are stored for USAA: you log in (and pass MFA) in the
plugin's own browser window the first time; the persistent profile keeps
the session afterwards.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ...models import Account
from ..playwright_base import InstitutionScraper

try:
    from playwright.sync_api import Page, Request, Response
except ImportError:  # pragma: no cover
    Page = Request = Response = object  # type: ignore[misc,assignment]

API = "https://api.usaa.com"
DOCS_LIST_RE = re.compile(r"/v1/enterprise/my-documents/experience/individuals/[^/]+/documents")
CONTENT_URL = API + "/v1/enterprise/content-viewer/render/content/{cid}/display-dates/{day}"
GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
ISO_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
US_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
AUTH_HEADERS = ("authorization", "api-key")

# Field-name hints.  The list JSON's exact schema wasn't captured (it would
# have meant exporting a live session token), so extraction is heuristic
# and the raw JSON is saved under data/raw/ for refinement.
ID_KEYS = ("contentId", "content_id", "contentGuid", "documentId", "document_id", "id")
DATE_KEYS = ("displayDate", "display_date", "deliveryDate", "delivery_date", "dateDelivered",
             "documentDate", "statementDate", "date")
TITLE_KEYS = ("title", "documentTitle", "name", "description", "documentType", "type")
ACCOUNT_KEYS = ("accountLabel", "accountName", "account", "maskedAccountNumber", "accountNumber",
                "productName", "displayAccount")


def _parse_day(v: Any) -> date | None:
    if isinstance(v, int | float) and v > 10_000_000:  # epoch seconds or millis
        v = v / 1000 if v > 10_000_000_000 else v
        return datetime.fromtimestamp(v, UTC).date()
    if not isinstance(v, str):
        return None
    m = ISO_DATE_RE.match(v)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            return None
    m = US_DATE_RE.match(v)
    if m:
        mo, d, y = (int(x) for x in m.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    return None


def _first(d: dict, keys: tuple[str, ...], pred=lambda v: True) -> Any:
    for k in keys:
        if k in d and d[k] is not None and pred(d[k]):
            return d[k]
    return None


def extract_documents(payload: Any) -> list[dict]:
    """Walk any JSON shape and return [{content_id, day, title, account}, ...]
    for every object that looks like a document (a GUID + a date)."""
    found: list[dict] = []
    seen: set[tuple[str, date]] = set()

    def walk(o: Any) -> None:
        if isinstance(o, list):
            for x in o:
                walk(x)
            return
        if not isinstance(o, dict):
            return
        cid = _first(o, ID_KEYS, lambda v: isinstance(v, str) and GUID_RE.match(v))
        if cid is None:  # any GUID-valued field as a fallback
            cid = next((v for k, v in o.items() if isinstance(v, str) and GUID_RE.match(v) and "guid" in k.lower() or
                        (isinstance(v, str) and GUID_RE.match(v) and k.lower().endswith("id"))), None)
        day = None
        for k in DATE_KEYS:
            if k in o:
                day = _parse_day(o[k])
                if day:
                    break
        if day is None:
            for k, v in o.items():
                if "date" in k.lower():
                    day = _parse_day(v)
                    if day:
                        break
        if cid and day and (cid, day) not in seen:
            seen.add((cid, day))
            title = _first(o, TITLE_KEYS, lambda v: isinstance(v, str)) or ""
            acct = _first(o, ACCOUNT_KEYS, lambda v: isinstance(v, str | int))
            if isinstance(acct, dict):
                acct = _first(acct, ACCOUNT_KEYS + ("name", "label"), lambda v: isinstance(v, str))
            found.append({"content_id": cid, "day": day, "title": str(title), "account": str(acct or "")})
            return  # don't descend into a document's own children
        for v in o.values():
            if isinstance(v, dict | list):
                walk(v)

    walk(payload)
    return found


def content_url(cid: str, day: date) -> str:
    return CONTENT_URL.format(cid=cid, day=day.isoformat())


def safe_name(doc: dict) -> str:
    bits = [doc["day"].isoformat(), "USAA", doc.get("account", ""), doc.get("title", "")]
    segs = [re.sub(r"-{2,}", "-", re.sub(r"[^\w\-]+", "-", b.strip())).strip("-") for b in bits if b]
    return "_".join(x for x in segs if x)[:140] + ".pdf"


class Scraper(InstitutionScraper):
    institution = "USAA"
    login_url = "https://www.usaa.com/my/documents"

    def __init__(self, credentials: dict[str, Any], options: dict[str, Any]):
        super().__init__(credentials, options)
        self.auth: dict[str, str] = {}
        self.list_payloads: list[Any] = []
        self.list_urls: list[str] = []
        self._wired = False

    # -- traffic capture ---------------------------------------------------
    def _wire(self, page: Page) -> None:
        if self._wired:
            return
        self._wired = True

        def on_request(req: Request) -> None:
            if "api.usaa.com" in req.url:
                for k, v in req.headers.items():
                    if k.lower() in AUTH_HEADERS:
                        self.auth[k.lower()] = v

        def on_response(resp: Response) -> None:
            if DOCS_LIST_RE.search(resp.url) and resp.ok:
                try:
                    self.list_payloads.append(resp.json())
                    self.list_urls.append(resp.url)
                except Exception:  # noqa: BLE001 — non-JSON body; ignore
                    pass

        page.on("request", on_request)
        page.on("response", on_response)

    # -- contract ----------------------------------------------------------
    def is_logged_in(self, page: Page) -> bool:
        self._wire(page)
        return "/my/documents" in page.url and page.get_by_text("Log Off").count() > 0

    def login(self, page: Page) -> None:
        """Human-in-the-loop: USAA's logon has MFA and bot detection, so we
        wait for you to sign in inside the plugin's browser window."""
        self._wire(page)
        timeout = int(self.options.get("login_timeout_s", 300)) * 1000
        if page.get_by_text("Log Off").count() == 0:
            page.wait_for_function(
                "() => document.body && /Log Off/i.test(document.body.innerText)", timeout=timeout
            )
        if "/my/documents" not in page.url:
            page.goto(self.login_url, wait_until="domcontentloaded")

    def fetch_accounts(self, page: Page) -> list[Account]:
        return []  # balances come from SimpleFIN

    def download_all_statements(self, page: Page, since: date, dest: Path) -> list[Path]:
        self._wire(page)
        if not self.list_payloads:
            page.goto(self.login_url, wait_until="networkidle")
            page.wait_for_timeout(1500)
        if not self.list_payloads:
            raise RuntimeError("USAA: document list was not observed; is the session logged in?")
        if not all(k in self.auth for k in AUTH_HEADERS):
            raise RuntimeError("USAA: could not capture API auth headers from the page")

        docs = []
        for p in self.list_payloads:
            docs.extend(extract_documents(p))
        self.raw = {"list_urls": [re.sub(r"individuals/[^/]+", "individuals/{guid}", u) for u in self.list_urls],
                    "list_payloads": self.list_payloads, "extracted": len(docs)}
        include = [s.lower() for s in self.options.get("include", [])]
        if include:
            docs = [d for d in docs if any(s in d["title"].lower() for s in include)]
        docs = [d for d in docs if d["day"] >= since]

        manifest_path = dest / ".usaa_downloaded.json"
        done: set[str] = set(json.loads(manifest_path.read_text())) if manifest_path.exists() else set()
        headers = {
            "Authorization": self.auth["authorization"],
            "api-key": self.auth["api-key"],
            "Transform-To-Pdf": "true",
            "Accept": "application/pdf, application/json, */*",
        }
        saved: list[Path] = []
        for d in sorted(docs, key=lambda x: x["day"]):
            key = f"{d['content_id']}:{d['day'].isoformat()}"
            if key in done:
                continue
            r = page.request.get(content_url(d["content_id"], d["day"]), headers=headers, timeout=60_000)
            body = r.body() if r.ok else b""
            if not body.startswith(b"%PDF"):
                continue  # not a PDF (e.g. non-renderable notice); leave for next run
            target = dest / safe_name(d)
            target.write_bytes(body)
            saved.append(target)
            done.add(key)
            manifest_path.write_text(json.dumps(sorted(done)))
            page.wait_for_timeout(int(self.options.get("delay_ms", 800)))  # be polite
        return saved
