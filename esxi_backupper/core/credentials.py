"""Passwort-Speicherung im Betriebssystem-Schlüsselbund.

Windows: Credential Manager, macOS: Keychain (via keyring-Paket).
Schlüssel: Service "EsxiAutoBackupper", Konto "<user>@<host>[:ssh]".
"""

from __future__ import annotations

from typing import Optional

import keyring

_SERVICE = "EsxiAutoBackupper"


def _account(host: str, username: str, ssh: bool = False) -> str:
    suffix = ":ssh" if ssh else ""
    return f"{username}@{host}{suffix}"


def get_password(host: str, username: str, ssh: bool = False) -> Optional[str]:
    try:
        return keyring.get_password(_SERVICE, _account(host, username, ssh))
    except keyring.errors.KeyringError:
        return None


def set_password(host: str, username: str, password: str, ssh: bool = False) -> None:
    keyring.set_password(_SERVICE, _account(host, username, ssh), password)


def delete_password(host: str, username: str, ssh: bool = False) -> None:
    try:
        keyring.delete_password(_SERVICE, _account(host, username, ssh))
    except keyring.errors.KeyringError:
        pass
