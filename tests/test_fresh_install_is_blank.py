"""A fresh install ships no data: empty database, nothing on the dashboard
until the user connects a source and labels accounts."""

from fastapi.testclient import TestClient

from cashfloor.config import Config
from cashfloor.db import Database
from cashfloor.web.app import create_app


def test_fresh_install_has_no_data(tmp_path):
    cfg = Config()
    cfg.general.data_dir = tmp_path / "data"
    cfg.general.archive_dir = tmp_path / "statements"
    cfg.general.inbox_dir = tmp_path / "inbox"
    cfg.ensure_dirs()

    db = Database(cfg.db_path)
    assert db.list_accounts(active_only=False) == []
    assert db.list_transactions() == []
    assert db.list_items(active_only=False) == []
    assert db.list_statements() == []
    assert db.list_connections() == []
    assert db.get_settings() == {}
    db.close()

    client = TestClient(create_app(cfg))
    page = client.get("/").text
    assert "Getting started" in page
    assert "No accounts yet" in page
    assert client.get("/api/discretionary").json()["discretionary"] == 0.0
    assert list((tmp_path / "statements").iterdir()) == []
