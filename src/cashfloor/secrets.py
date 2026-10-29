"""Secret storage via the OS keyring.

Windows Credential Manager, macOS Keychain, or Secret Service on Linux.
Nothing secret is ever written to config.toml or the database.
"""

from __future__ import annotations

import json
from typing import Any

import keyring

SERVICE = "cashfloor"


def get_secret(name: str) -> str | None:
    return keyring.get_password(SERVICE, name)


def set_secret(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)


def delete_secret(name: str) -> None:
    try:
        keyring.delete_password(SERVICE, name)
    except keyring.errors.PasswordDeleteError:
        pass


def get_json_secret(name: str) -> dict[str, Any] | None:
    raw = get_secret(name)
    if raw is None:
        return None
    return json.loads(raw)


def set_json_secret(name: str, value: dict[str, Any]) -> None:
    set_secret(name, json.dumps(value))
