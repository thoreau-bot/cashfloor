from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cashfloor.config import Config, apply_overrides, load_config
from cashfloor.models import Frequency, ScheduledItem
from cashfloor.web import app as webapp


def test_apply_overrides_coerces_and_validates():
    cfg = load_config(None)  # defaults incl. default providers
    c = apply_overrides(cfg, {"projection.safety_buffer": "750", "projection.horizon_days": "30",
                              "projection.estimate_future_card_payments": "false",
                              "simplefin.lookback_days": "90", "bogus.key": "x"})
    assert c.projection.safety_buffer == 750.0
    assert c.projection.horizon_days == 30
    assert c.projection.estimate_future_card_payments is False
    assert next(p for p in c.provider if p.kind == "simplefin").lookback_days == 90
    assert cfg.projection.safety_buffer == 0.0  # original untouched
    with pytest.raises(ValidationError):
        apply_overrides(cfg, {"projection.horizon_days": "ninety"})


def test_settings_persist_in_db(db):
    assert db.get_settings() == {}
    db.set_setting("projection.safety_buffer", "123")
    assert db.get_settings() == {"projection.safety_buffer": "123"}
    db.set_setting("projection.safety_buffer", None)
    assert db.get_settings() == {}


@pytest.fixture
def client(cfg: Config, db, accounts, monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr(webapp.secrets, "get_secret", lambda n: store.get(n))
    monkeypatch.setattr(webapp.secrets, "set_secret", lambda n, v: store.__setitem__(n, v))
    monkeypatch.setattr(webapp.secrets, "delete_secret", lambda n: store.pop(n, None))
    cfg.provider = load_config(None).provider
    db.save_item(ScheduledItem(id=None, name="Rent", account_id=accounts["chk"].id, amount=-220_000,
                               frequency=Frequency.MONTHLY, next_date=date(2026, 11, 1)))
    c = TestClient(webapp.create_app(cfg))
    c.store = store  # type: ignore[attr-defined]
    return c


def test_settings_page_and_save_changes_projection(client):
    assert client.get("/settings").status_code == 200
    before = client.get("/api/discretionary").json()["discretionary"]
    r = client.post("/settings", data={"projection.safety_buffer": "1000", "projection.horizon_days": "90",
                                       "projection.card_payment_mode": "statement",
                                       "projection.estimate_future_card_payments": "true",
                                       "projection.default_payment_account": "chk",
                                       "recurring.min_occurrences": "3", "recurring.amount_tolerance": "0.15",
                                       "scheduler.daily_sync_time": "06:30", "general.timezone": "UTC",
                                       "simplefin.lookback_days": "60"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=Saved" in r.headers["location"]
    after = client.get("/api/discretionary").json()["discretionary"]
    assert after == before - 500  # buffer went 500 -> 1000
    r = client.post("/settings/reset", follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/api/discretionary").json()["discretionary"] == before


def test_settings_rejects_bad_value(client):
    r = client.post("/settings", data={"projection.horizon_days": "ninety"}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_simplefin_connect_via_gui(client, monkeypatch):
    monkeypatch.setattr("cashfloor.providers.simplefin.claim_access_url", lambda t: f"https://u:p@bridge.test/simplefin#{t}")
    page = client.get("/settings").text
    assert "Not connected" in page
    r = client.post("/settings/simplefin", data={"setup_token": " <abc123> "}, follow_redirects=False)
    assert "connected" in r.headers["location"].lower()
    assert client.store["simplefin_access_url"].endswith("#abc123")
    assert "Connected." in client.get("/settings").text
    client.post("/settings/simplefin/disconnect")
    assert "simplefin_access_url" not in client.store


def test_dashboard_setup_hints(client):
    assert "Getting started" not in client.get("/").text or "default payment account" in client.get("/").text
