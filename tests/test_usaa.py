import json
from datetime import date
from pathlib import Path

from cashfloor.providers.institutions.usaa import (
    Scraper,
    clean_title,
    content_url,
    extract_documents,
    last4_from,
    safe_name,
)

CID = "3c1f0a52-9b7e-4d21-8e4a-5f6a7b8c9d01"
CID2 = "9e8d7c6b-5a4f-4e3d-9c2b-1a0f9e8d7c6b"


def test_extract_handles_several_plausible_shapes():
    shapes = [
        {"documents": [{"contentId": CID, "displayDate": "2026-10-08", "title": "CHECKING STATEMENT",
                        "account": "USAA CLASSIC CHECKING *4471"}]},
        {"data": {"items": [{"id": CID, "deliveryDate": "10/08/2026", "documentTitle": "CHECKING STATEMENT",
                             "accountInfo": {"maskedAccountNumber": "*4471"}}]}},
        [{"documentGuid": CID, "dateDelivered": 1791417600, "name": "CHECKING STATEMENT"}],
        {"documents": [{"contentId": CID, "displayDate": "2026-10-08T00:00:00Z", "title": "X",
                        "nested": {"contentId": CID2, "displayDate": "2026-05-08"}}]},
    ]
    for payload in shapes:
        docs = extract_documents(payload)
        assert docs and docs[0]["content_id"] == CID and docs[0]["day"] == date(2026, 10, 8), payload
        assert "STATEMENT" in docs[0]["title"] or docs[0]["title"] == "X"
    # nested document inside a document is not double-counted
    assert len(extract_documents(shapes[3])) == 1


def test_real_usaa_shape_is_pinned():
    payload = {"documents": [
        {"documentId": CID, "documentDate": "2026-10-08", "displayDate": "10/08/2026",
         "title": "CHECKING ******4471 STATEMENT ", "accountName": "", "category": "Banking",
         "subCategory": "Deposits", "filterGroup": "All banking accounts", "unread": True},
        {"documentId": CID2, "documentDate": "2026-10-27", "displayDate": "10/27/2026",
         "title": "Auto and Property Insurance Statement", "accountName": "", "category": "Insurance",
         "subCategory": "Accounting/Billing", "filterGroup": "All insurance policies", "unread": False},
    ], "lastItem": "abc"}
    docs = extract_documents(payload)
    assert [d["title"] for d in docs] == ["CHECKING STATEMENT", "Auto and Property Insurance Statement"]
    assert docs[0]["last4"] == "4471" and docs[0]["day"] == date(2026, 10, 8)
    assert docs[1]["last4"] is None and docs[1]["category"] == "Insurance"


def test_clean_title_and_last4():
    assert clean_title("CHECKING ******4471 STATEMENT ") == "CHECKING STATEMENT"
    assert clean_title("Automobile Policy *********-2208 Renewal  / Auto ID Cards") == "Automobile Policy Renewal / Auto ID Cards"
    assert last4_from("", "CHECKING ******4471 STATEMENT") == "4471"
    assert last4_from("USAA SAVINGS *5530", "SAVINGS STATEMENT") == "5530"
    assert last4_from("*********-2208", "") == "2208"
    assert last4_from("", "AutoIDCards") is None


def test_content_url_and_name():
    assert content_url(CID, date(2026, 10, 8)) == (
        "https://api.usaa.com/v1/enterprise/content-viewer/render/content/" + CID + "/display-dates/2026-10-08"
    )
    n = safe_name({"day": date(2026, 10, 8), "title": "CHECKING STATEMENT", "account": "USAA CLASSIC CHECKING *4471",
                   "content_id": CID})
    assert n == "2026-10-08_USAA_USAA-CLASSIC-CHECKING-4471_CHECKING-STATEMENT_3c1f0a52.pdf"
    n2 = safe_name({"day": date(2026, 10, 8), "title": "CHECKING STATEMENT", "account": "USAA CLASSIC CHECKING *4471",
                    "content_id": CID2})
    assert n != n2


class FakeResponse:
    def __init__(self, body: bytes, ok=True):
        self._b, self.ok, self.status = body, ok, 200 if ok else 500

    def body(self):
        return self._b


class FakeRequest:
    def __init__(self, pages=None):
        self.calls = []
        self.pages = pages or {}  # cursor -> payload, served when ?lastItem=<cursor>

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        if "/documents?" in url:
            cur = url.split("lastItem=")[-1] if "lastItem=" in url else None
            if cur in self.pages:
                r = FakeResponse(json.dumps(self.pages[cur]).encode())
                r.json = lambda: self.pages[cur]
                return r
            return FakeResponse(b"", ok=False)
        return FakeResponse(b"%PDF-1.7 fake" if CID in url else b'{"error":"no"}')


class FakePage:
    url = "https://www.usaa.com/my/documents"

    def __init__(self):
        self.request = FakeRequest()
        self.handlers = {}

    def on(self, evt, fn):
        self.handlers[evt] = fn

    def goto(self, *a, **k):
        pass

    def wait_for_timeout(self, ms):
        pass

    def get_by_text(self, t):
        class L:
            def count(self_inner):
                return 1
        return L()


def test_cursor_pagination_via_query_param(tmp_path: Path):
    s = Scraper({}, {"delay_ms": 0})
    page = FakePage()
    page.request = FakeRequest(pages={"c1": {"documents": [
        {"documentId": CID2, "documentDate": "2024-03-08", "title": "CHECKING ******4471 STATEMENT", "accountName": ""}]}})
    s._wire(page)
    class Req:
        url = "https://api.usaa.com/v1/x"
        headers = {"authorization": "Bearer abc", "api-key": "k123"}
    page.handlers["request"](Req())
    class Resp:
        url = "https://api.usaa.com/v1/enterprise/my-documents/experience/individuals/GUID/documents?limit=100"
        ok = True
        def json(self):
            return {"documents": [{"documentId": CID, "documentDate": "2026-10-08",
                                   "title": "CHECKING ******4471 STATEMENT", "accountName": ""}], "lastItem": "c1"}
    page.handlers["response"](Resp())
    saved = s.download_all_statements(page, since=None, dest=tmp_path)
    assert len(s.list_payloads) == 2 and s.raw["extracted"] == 2
    assert any("lastItem=c1" in u for u, _ in page.request.calls)
    assert len(saved) == 1  # CID2's fake GET returns non-PDF; CID downloads


def test_download_flow_with_captured_auth(tmp_path: Path):
    s = Scraper({}, {"include": ["statement"], "delay_ms": 0})
    page = FakePage()
    s._wire(page)
    # simulate the page's own traffic
    class Req:
        url = "https://api.usaa.com/v1/x"
        headers = {"authorization": "Bearer abc", "api-key": "k123", "accept": "*/*"}
    page.handlers["request"](Req())
    class Resp:
        url = "https://api.usaa.com/v1/enterprise/my-documents/experience/individuals/GUID/documents?limit=100"
        ok = True
        def json(self):
            return {"documents": [
                {"contentId": CID, "displayDate": "2026-10-08", "title": "CHECKING STATEMENT", "account": "*4471"},
                {"contentId": CID2, "displayDate": "2026-05-08", "title": "CHECKING STATEMENT", "account": "*4471"},
                {"contentId": CID2, "displayDate": "2026-09-01", "title": "Privacy notice", "account": ""},
            ]}
    page.handlers["response"](Resp())

    saved = s.download_all_statements(page, since=date(2026, 6, 1), dest=tmp_path)
    assert [p.name for p in saved] == ["2026-10-08_USAA_4471_CHECKING-STATEMENT_3c1f0a52.pdf"]
    assert saved[0].read_bytes().startswith(b"%PDF")
    meta = json.loads(saved[0].with_name(saved[0].name + ".meta.json").read_text())
    assert {k: meta[k] for k in ("institution", "title", "last4", "date", "content_id")} == {
        "institution": "USAA", "title": "CHECKING STATEMENT", "last4": "4471", "date": "2026-10-08", "content_id": CID}
    url, headers = page.request.calls[0]
    assert headers["Authorization"] == "Bearer abc" and headers["api-key"] == "k123"
    assert headers["Transform-To-Pdf"] == "true"
    assert s.raw["list_urls"] == ["https://api.usaa.com/v1/enterprise/my-documents/experience/individuals/{guid}/documents?limit=100"]
    # second run: manifest prevents re-download
    assert s.download_all_statements(page, since=date(2026, 6, 1), dest=tmp_path) == []
    assert json.loads((tmp_path / ".usaa_downloaded.json").read_text()) == [f"{CID}:2026-10-08"]
