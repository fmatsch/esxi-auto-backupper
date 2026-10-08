"""Zugriff auf mitgelieferte Dateien (Icon) - im Quellbaum und in der PyInstaller-Exe."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon


def asset_path(name: str) -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "esxi_backupper" / "assets" / name
    return Path(__file__).resolve().parent.parent / "assets" / name


def app_icon() -> QIcon:
    return QIcon(str(asset_path("icon.png")))
