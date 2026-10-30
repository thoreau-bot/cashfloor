import json
from datetime import date
from pathlib import Path

from cashfloor.providers.institutions.usaa import Scraper, content_url, extract_documents, safe_name

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
    def __init__(self):
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
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
    assert meta == {"institution": "USAA", "title": "CHECKING STATEMENT", "account": "*4471",
                    "date": "2026-10-08", "content_id": CID, "source": "usaa"}
    url, headers = page.request.calls[0]
    assert headers["Authorization"] == "Bearer abc" and headers["api-key"] == "k123"
    assert headers["Transform-To-Pdf"] == "true"
    assert s.raw["list_urls"] == ["https://api.usaa.com/v1/enterprise/my-documents/experience/individuals/{guid}/documents?limit=100"]
    # second run: manifest prevents re-download
    assert s.download_all_statements(page, since=date(2026, 6, 1), dest=tmp_path) == []
    assert json.loads((tmp_path / ".usaa_downloaded.json").read_text()) == [f"{CID}:2026-10-08"]
