"""Versionsverwaltung: vorhandene Backups eines Jobs ansehen und einzelne löschen."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..core.config import BackupJob
from ..core.runner import delete_job_versions, list_job_versions
from .workers import FuncWorker

_STATES = {"poweredOn": "läuft", "poweredOff": "aus", "suspended": "pausiert"}


class VersionsDialog(QDialog):
    def __init__(self, job: BackupJob, parent=None):
        super().__init__(parent)
        self.job = job
        self._worker: FuncWorker | None = None
        self.setWindowTitle(f"Versionen - {job.name}")
        self.setMinimumSize(620, 380)

        layout = QVBoxLayout(self)
        self.lbl_info = QLabel()
        self.lbl_info.setWordWrap(True)
        layout.addWidget(self.lbl_info)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Version (Zeitpunkt)", "VM auf dem Ziel", "Zustand"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table, 1)

        self.lbl_status = QLabel("")
        layout.addWidget(self.lbl_status)

        row = QHBoxLayout()
        self.btn_refresh = QPushButton("Aktualisieren")
        self.btn_refresh.clicked.connect(self.reload)
        self.btn_delete = QPushButton("Ausgewählte löschen")
        self.btn_delete.clicked.connect(self._delete_selected)
        self.btn_delete.setEnabled(False)
        btn_close = QPushButton("Schließen")
        btn_close.clicked.connect(self.accept)
        row.addWidget(self.btn_refresh)
        row.addWidget(self.btn_delete)
        row.addStretch(1)
        row.addWidget(btn_close)
        layout.addLayout(row)

        self._update_info(None)
        self.reload()

    def _update_info(self, count: int | None):
        j = self.job
        have = "" if count is None else f"Vorhanden: {count}. "
        self.lbl_info.setText(
            f"{have}Es werden die letzten {j.retention_count} Versionen aufbewahrt "
            f"(Ziel: {j.target_host.address}). Nach einem erfolgreichen Backup wird die "
            "jeweils älteste überzählige Version automatisch gelöscht. Zum Zurückspielen "
            "die gewünschte VM auf dem Ziel-Host einschalten.")

    def _busy(self, text: str):
        self.lbl_status.setText(text)
        self.btn_refresh.setEnabled(False)
        self.btn_delete.setEnabled(False)

    def _idle(self):
        self.btn_refresh.setEnabled(True)
        self._update_buttons()

    def reload(self):
        self._busy("Lade Versionen ...")
        self._worker = FuncWorker(lambda: list_job_versions(self.job), self)
        self._worker.finished_ok.connect(self._loaded)
        self._worker.failed.connect(self._failed)
        self._worker.start()

    def _loaded(self, found):
        self.table.setRowCount(0)
        for ts, info in found:
            r = self.table.rowCount()
            self.table.insertRow(r)
            cells = [ts.strftime("%d.%m.%Y %H:%M:%S"), info.name,
                     _STATES.get(info.power_state, info.power_state)]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, (info.name, info.power_state))
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        self._update_info(len(found))
        self.lbl_status.setText("" if found else "Noch keine Versionen vorhanden.")
        self._idle()

    def _failed(self, message: str):
        self.lbl_status.setText(f"Fehler: {message}")
        self._idle()

    def _selected(self) -> list[tuple[str, str]]:
        rows = {i.row() for i in self.table.selectedItems()}
        return [self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in sorted(rows)]

    def _update_buttons(self):
        self.btn_delete.setEnabled(bool(self._selected()) and self.btn_refresh.isEnabled())

    def _delete_selected(self):
        selected = self._selected()
        running = [n for n, state in selected if state == "poweredOn"]
        if running:
            QMessageBox.information(
                self, "Versionen",
                "Laufende VMs werden nicht gelöscht:\n" + "\n".join(running))
            return
        names = [n for n, _ in selected]
        answer = QMessageBox.question(
            self, "Versionen löschen",
            f"{len(names)} Version(en) unwiderruflich vom Ziel-Host löschen?\n\n"
            + "\n".join(names))
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._busy("Lösche ...")
        self._worker = FuncWorker(lambda: delete_job_versions(self.job, names), self)
        self._worker.finished_ok.connect(self._deleted)
        self._worker.failed.connect(self._failed)
        self._worker.start()

    def _deleted(self, errors):
        if errors:
            QMessageBox.warning(self, "Versionen", "Nicht alles konnte gelöscht werden:\n"
                                + "\n".join(errors))
        self.reload()

    def done(self, result):
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(60_000)
        super().done(result)
