"""Data providers.  Each provider turns an external source into Account /
Transaction records and (optionally) statement files."""

from __future__ import annotations

from ..config import Config, ProviderConfig
from .base import Provider, SyncResult


def build_provider(cfg: Config, pc: ProviderConfig) -> Provider:
    if pc.kind == "simplefin":
        from .simplefin import SimpleFINProvider

        return SimpleFINProvider(cfg, pc)
    if pc.kind == "filedrop":
        from .filedrop import FileDropProvider

        return FileDropProvider(cfg, pc)
    if pc.kind == "playwright":
        from .playwright_base import PlaywrightProvider

        return PlaywrightProvider(cfg, pc)
    raise ValueError(f"Unknown provider kind: {pc.kind!r}")


__all__ = ["Provider", "SyncResult", "build_provider"]
