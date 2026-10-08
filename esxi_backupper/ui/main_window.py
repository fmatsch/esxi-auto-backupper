"""Hauptfenster: Quelle links, Ziel rechts, Jobliste und Log unten."""

from __future__ import annotations

import copy
import sys
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QAbstractItemView,
)

from .. import APP_NAME, __version__
from ..core import scheduler as sched
from ..core.config import AppConfig, BackupJob, find_job, load_config, save_config
from .hardware_dialog import HardwareDialog
from .host_panel import HostPanel
from .job_dialog import JobDialog
from .versions_dialog import VersionsDialog
from .log_widget import LogWidget
from .workers import JobWorker

_JOB_COLUMNS = ["Name", "Quelle", "Ziel", "Zeitplan", "Versionen", "Letzter Lauf",
                "Status", "Nächster Lauf"]


_STATUS_COL = _JOB_COLUMNS.index("Status")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"ESXi Auto Backupper {__version__}")
        self.resize(1150, 780)
        self.config: AppConfig = load_config()
        self.worker: JobWorker | None = None
        self._build_ui()
        self._build_menu()
        self._refresh_job_table()

        # Interner Scheduler: prüft alle 30 s, ob ein Job fällig ist
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._check_schedule)
        self._timer.start(30_000)

    # --- UI-Aufbau ------------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)

        # Oben: Quelle | Pfeil/Aktion | Ziel
        top = QHBoxLayout()
        self.panel_source = HostPanel("Quellsystem", "source")
        self.panel_target = HostPanel("Zielsystem", "target")
        self.panel_source.selection_changed.connect(self._update_create_button)
        self.panel_target.selection_changed.connect(self._update_create_button)
        self.panel_source.connected_changed.connect(self._update_create_button)
        self.panel_target.connected_changed.connect(self._update_create_button)

        mid = QVBoxLayout()
        mid.addStretch(1)
        arrow = QLabel("➜")
        arrow.setStyleSheet("font-size: 28px;")
        arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mid.addWidget(arrow)
        self.btn_create = QPushButton("Backup-Job\nerstellen")
        self.btn_create.setEnabled(False)
        self.btn_create.setMinimumHeight(56)
        self.btn_create.clicked.connect(self._create_job)
        mid.addWidget(self.btn_create)
        mid.addStretch(1)

        top.addWidget(self.panel_source, 1)
        top.addLayout(mid)
        top.addWidget(self.panel_target, 1)

        top_widget = QWidget()
        top_widget.setLayout(top)

        # Mitte: Jobliste + Buttons
        jobs_widget = QWidget()
        jl = QVBoxLayout(jobs_widget)
        jl.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, len(_JOB_COLUMNS))
        self.table.setHorizontalHeaderLabels(_JOB_COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._update_job_buttons)
        jl.addWidget(self.table)

        btn_row = QHBoxLayout()
        self.btn_run = QPushButton("Jetzt ausführen")
        self.btn_run.clicked.connect(self._run_selected)
        self.btn_edit = QPushButton("Bearbeiten")
        self.btn_edit.clicked.connect(self._edit_selected)
        self.btn_versions = QPushButton("Versionen ...")
        self.btn_versions.clicked.connect(self._show_versions)
        self.btn_delete = QPushButton("Löschen")
        self.btn_delete.clicked.connect(self._delete_selected)
        self.btn_cancel = QPushButton("Abbrechen")
        self.btn_cancel.clicked.connect(self._cancel_running)
        self.btn_cancel.setEnabled(False)
        for b in (self.btn_run, self.btn_edit, self.btn_versions, self.btn_delete):
            b.setEnabled(False)
            btn_row.addWidget(b)
        btn_row.addWidget(self.btn_cancel)
        btn_row.addStretch(1)
        jl.addLayout(btn_row)

        # Unten: Log
        self.log = LogWidget()

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(top_widget)
        splitter.addWidget(jobs_widget)
        splitter.addWidget(self.log)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 3)
        root.addWidget(splitter)
        self.setCentralWidget(central)
        self.statusBar().showMessage("Bereit")

    def _build_menu(self):
        m_file = self.menuBar().addMenu("&Datei")
        m_file.addAction("Beenden", self.close)
        m_settings = self.menuBar().addMenu("&Einstellungen")
        m_settings.addAction("Hardware-Standardprofil ...", self._edit_default_hardware)
        m_help = self.menuBar().addMenu("&Hilfe")
        m_help.addAction("Über", self._show_about)

    # --- Job-Verwaltung -------------------------------------------------------

    def _update_create_button(self):
        ok = (self.panel_source.selected_vm() is not None
              and self.panel_target.selected_datastore() is not None)
        self.btn_create.setEnabled(ok)

    def _update_job_buttons(self):
        has_sel = bool(self.table.selectedItems())
        running = self.worker is not None
        self.btn_run.setEnabled(has_sel and not running)
        self.btn_edit.setEnabled(has_sel)
        self.btn_versions.setEnabled(has_sel)
        self.btn_delete.setEnabled(has_sel and not running)

    def _selected_job(self) -> BackupJob | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        job_id = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return find_job(self.config, job_id)

    def _create_job(self):
        vm = self.panel_source.selected_vm()
        ds = self.panel_target.selected_datastore()
        if not vm or not ds:
            return
        job = BackupJob(
            source_host=self.panel_source.host_config(),
            source_vm=vm.name,
            target_host=self.panel_target.host_config(),
            target_datastore=ds.name,
            hardware=copy.deepcopy(self.config.default_hardware),
        )
        dlg = JobDialog(job, self.panel_target.networks, is_new=True, parent=self)
        if not dlg.exec():
            return
        dlg.apply_to_job()
        self.config.jobs.append(job)
        save_config(self.config)
        self._sync_windows_task(dlg, job)
        self._refresh_job_table()
        self.log.append(f"Job '{job.name}' angelegt.")

    def _edit_selected(self):
        job = self._selected_job()
        if not job:
            return
        dlg = JobDialog(job, self.panel_target.networks, is_new=False, parent=self)
        if not dlg.exec():
            return
        dlg.apply_to_job()
        save_config(self.config)
        self._sync_windows_task(dlg, job)
        self._refresh_job_table()

    def _sync_windows_task(self, dlg: JobDialog, job: BackupJob):
        """Gleicht den Taskplaner-Eintrag mit dem Dialog ab (anlegen/aktualisieren/entfernen)."""
        wanted = dlg.wants_windows_task() and job.schedule.mode != "manual"
        if wanted:
            try:
                sched.register_windows_task(job)
                job.use_windows_task = True
                self.log.append(f"Windows-Taskplaner-Task für '{job.name}' angelegt.")
            except Exception as e:
                job.use_windows_task = False
                QMessageBox.warning(self, APP_NAME,
                                    f"Taskplaner-Registrierung fehlgeschlagen:\n{e}")
        elif job.use_windows_task:
            sched.unregister_windows_task(job)
            job.use_windows_task = False
            self.log.append(f"Windows-Taskplaner-Task für '{job.name}' entfernt.")
        save_config(self.config)

    def _show_versions(self):
        job = self._selected_job()
        if job:
            VersionsDialog(job, self).exec()

    def _delete_selected(self):
        job = self._selected_job()
        if not job:
            return
        answer = QMessageBox.question(
            self, APP_NAME,
            f"Job '{job.name}' löschen?\n(Bereits erstellte Backups bleiben erhalten.)")
        if answer != QMessageBox.StandardButton.Yes:
            return
        sched.unregister_windows_task(job)
        self.config.jobs.remove(job)
        save_config(self.config)
        self._refresh_job_table()
        self.log.append(f"Job '{job.name}' gelöscht.")

    def _refresh_job_table(self):
        self.table.setRowCount(0)
        now = datetime.now()
        for job in self.config.jobs:
            r = self.table.rowCount()
            self.table.insertRow(r)
            nxt = sched.next_run(job.schedule, job.last_run, now)
            status = {"ok": "OK", "error": "FEHLER", "running": "läuft ..."}.get(
                job.last_status, "-")
            if job.last_status == "error" and job.last_error:
                status = f"FEHLER: {job.last_error[:60]}"
            values = [
                job.name,
                f"{job.source_vm} @ {job.source_host.address}",
                f"[{job.target_datastore}] @ {job.target_host.address}",
                sched.describe(job.schedule),
                f"{job.retention_count} aufbewahren",
                job.last_run.replace("T", " ") if job.last_run else "-",
                status,
                nxt.strftime("%d.%m.%Y %H:%M") if nxt else "-",
            ]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, job.id)
                if c == _STATUS_COL and job.last_status == "error":
                    item.setForeground(Qt.GlobalColor.red)
                if c == _STATUS_COL and job.last_status == "ok":
                    item.setForeground(Qt.GlobalColor.darkGreen)
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        self._update_job_buttons()

    # --- Job-Ausführung -------------------------------------------------------

    def _run_selected(self):
        job = self._selected_job()
        if job:
            self._start_job(job)

    def _start_job(self, job: BackupJob):
        if self.worker is not None:
            self.log.append(f"Job '{job.name}' übersprungen - es läuft bereits ein Backup.")
            return
        self.log.reset_progress()
        self.log.append(f"=== Starte Backup-Job '{job.name}' ===")
        self.statusBar().showMessage(f"Backup läuft: {job.name}")
        self.worker = JobWorker(job, self)
        self.worker.log.connect(self.log.append)
        self.worker.progress.connect(self.log.update_progress)
        self.worker.done.connect(lambda name, j=job: self._job_finished(j, name, None))
        self.worker.failed.connect(lambda msg, j=job: self._job_finished(j, None, msg))
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()
        self.btn_cancel.setEnabled(True)
        self._refresh_job_table()

    def _job_finished(self, job: BackupJob, vm_name: str | None, error: str | None):
        if error:
            self.log.append(f"FEHLER bei Job '{job.name}': {error}")
        else:
            self.log.append(f"=== Job '{job.name}' erfolgreich: {vm_name} ===")
        self.worker = None
        self.btn_cancel.setEnabled(False)
        self.statusBar().showMessage("Bereit")
        self.log.reset_progress()
        self._refresh_job_table()

    def _cancel_running(self):
        if self.worker:
            self.log.append("Abbruch angefordert ...")
            self.worker.cancel()

    def _check_schedule(self):
        if self.worker is not None:
            return
        for job in self.config.jobs:
            if job.use_windows_task:
                continue  # läuft über den Windows-Taskplaner
            if sched.is_due(job.schedule, job.last_run):
                self.log.append(f"Zeitplan: Job '{job.name}' ist fällig.")
                self._start_job(job)
                return  # ein Job nach dem anderen

    # --- Dialoge --------------------------------------------------------------

    def _edit_default_hardware(self):
        dlg = HardwareDialog(self.config.default_hardware,
                             self.panel_target.networks, parent=self)
        if dlg.exec():
            self.config.default_hardware = dlg.result_profile()
            save_config(self.config)
            self.log.append("Hardware-Standardprofil gespeichert "
                            "(gilt für neue Jobs).")

    def _show_about(self):
        QMessageBox.about(
            self, APP_NAME,
            f"<b>ESXi Auto Backupper {__version__}</b><br><br>"
            "Sichert laufende VMs von einem ESXi-Host auf einen anderen<br>"
            "(Snapshot → Kopie → Registrierung), manuell oder zeitgesteuert.<br><br>"
            "Hinweis: Thin-Disks werden bei der Übertragung in voller<br>"
            "provisionierter Größe gelesen (wie beim vCenter Converter).")

    def closeEvent(self, event):
        if self.worker is not None:
            answer = QMessageBox.question(
                self, APP_NAME,
                "Ein Backup läuft noch. Wirklich beenden?\n"
                "Das Backup wird abgebrochen und aufgeräumt.")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.worker.wait(30_000)
        event.accept()
