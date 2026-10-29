from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config, ProviderConfig
from ..models import Account, Transaction


@dataclass
class SyncResult:
    provider: str
    accounts: list[Account] = field(default_factory=list)
    transactions: list[Transaction] = field(default_factory=list)
    statement_files: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class Provider(ABC):
    """Contract every data source implements.

    `fetch()` must be idempotent: calling it twice yields the same records
    (the DB upsert handles de-duplication).  Accounts come back with
    `id=None`; the sync service assigns DB ids and rewrites transaction
    account_ids by (provider, external_id).
    """

    kind: str = "base"

    def __init__(self, cfg: Config, pc: ProviderConfig):
        self.cfg = cfg
        self.pc = pc

    @property
    def name(self) -> str:
        return self.pc.name

    @abstractmethod
    def fetch(self) -> SyncResult: ...

    def supports_statements(self) -> bool:
        return False
