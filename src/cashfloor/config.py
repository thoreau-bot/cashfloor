"""Configuration loading.

Non-secret settings live in a TOML file (default ./config.toml, override with
CASHFLOOR_CONFIG).  Secrets live in the OS keyring (see secrets.py).
"""

from __future__ import annotations

import os
import tomllib
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field


class GeneralConfig(BaseModel):
    data_dir: Path = Path("./data")
    archive_dir: Path = Path("./statements")
    inbox_dir: Path = Path("./inbox")
    currency: str = "USD"
    timezone: str = "UTC"


class ProjectionConfig(BaseModel):
    horizon_days: int = 90
    safety_buffer: float = 0.0
    pool_account_types: list[str] = ["checking", "savings"]
    default_payment_account: str | None = None
    card_payment_mode: str = "statement"  # "statement" | "minimum"
    # Repeat the latest card/loan/utility payment monthly through the horizon.
    estimate_future_card_payments: bool = True


class RecurringConfig(BaseModel):
    min_occurrences: int = 3
    date_tolerance_days: int = 3
    amount_tolerance: float = 0.15


class SchedulerConfig(BaseModel):
    daily_sync_time: str = "06:30"


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8477


class ProviderConfig(BaseModel):
    name: str
    kind: str  # simplefin | filedrop | playwright
    enabled: bool = True
    secret: str | None = None
    module: str | None = None
    headless: bool = False
    lookback_days: int = 60
    statements_since: date | None = None
    options: dict = Field(default_factory=dict)


class AccountOverride(BaseModel):
    key: str
    provider: str
    external_id: str
    type: str = "checking"
    nickname: str | None = None
    in_pool: bool | None = None


class Config(BaseModel):
    general: GeneralConfig = GeneralConfig()
    projection: ProjectionConfig = ProjectionConfig()
    recurring: RecurringConfig = RecurringConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    web: WebConfig = WebConfig()
    provider: list[ProviderConfig] = Field(default_factory=list)
    account: list[AccountOverride] = Field(default_factory=list)

    # Resolved at load time
    config_path: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.general.data_dir / "cashfloor.sqlite"

    @property
    def browser_profiles_dir(self) -> Path:
        return self.general.data_dir / "browser-profiles"

    def ensure_dirs(self) -> None:
        for p in (
            self.general.data_dir,
            self.general.archive_dir,
            self.general.inbox_dir,
            self.browser_profiles_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)


def find_config_path(explicit: str | os.PathLike | None = None) -> Path | None:
    if explicit:
        return Path(explicit)
    env = os.environ.get("CASHFLOOR_CONFIG")
    if env:
        return Path(env)
    for candidate in (Path("config.toml"), Path.home() / ".config" / "cashfloor" / "config.toml"):
        if candidate.exists():
            return candidate
    return None


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load config from TOML.  Missing file -> defaults (useful for tests/first run)."""
    resolved = find_config_path(path)
    if resolved is None or not resolved.exists():
        cfg = Config()
    else:
        with open(resolved, "rb") as f:
            raw = tomllib.load(f)
        cfg = Config.model_validate(raw)
        cfg.config_path = resolved
        # Make relative paths relative to the config file, not the CWD.
        base = resolved.parent
        g = cfg.general
        for attr in ("data_dir", "archive_dir", "inbox_dir"):
            p = getattr(g, attr)
            if not p.is_absolute():
                setattr(g, attr, (base / p).resolve())
    return cfg
