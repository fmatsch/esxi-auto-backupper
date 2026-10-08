"""Log-Bereich mit Fortschrittsbalken für laufende Backups."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar, QVBoxLayout, QWidget,
)

from ..core.transfer import TransferProgress, format_bytes


class LogWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.lbl_rate = QLabel("")
        row.addWidget(self.progress, 1)
        row.addWidget(self.lbl_rate)
        layout.addLayout(row)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(5000)
        layout.addWidget(self.text, 1)

    def append(self, message: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.text.appendPlainText(f"[{stamp}] {message}")

    def update_progress(self, p: TransferProgress):
        self.progress.setValue(int(p.percent))
        self.lbl_rate.setText(
            f"Datei {p.file_index}/{p.file_count}: {p.file_name}  "
            f"{format_bytes(p.bytes_done)} / {format_bytes(p.bytes_total)}  "
            f"({format_bytes(p.rate_bps)}/s)"
        )

    def reset_progress(self):
        self.progress.setValue(0)
        self.lbl_rate.setText("")
