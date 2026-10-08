"""Dialog zum Anlegen/Bearbeiten eines Backup-Jobs."""

from __future__ import annotations

import copy
import sys

from PySide6.QtCore import QTime
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QLabel, QLineEdit, QPushButton, QSpinBox, QTimeEdit, QVBoxLayout, QHBoxLayout,
)

from ..core.config import BackupJob, HardwareProfile, Schedule
from .hardware_dialog import HardwareDialog

_WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag",
             "Freitag", "Samstag", "Sonntag"]
_MODES = [("manual", "Manuell (nur per Klick)"),
          ("hourly", "Alle N Stunden"),
          ("daily", "Täglich"),
          ("weekly", "Wöchentlich")]


class JobDialog(QDialog):
    def __init__(self, job: BackupJob, target_networks: list[str],
                 is_new: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Backup-Job anlegen" if is_new else "Backup-Job bearbeiten")
        self.setMinimumWidth(460)
        self.job = job
        self._hardware = copy.deepcopy(job.hardware)
        self._networks = target_networks
        self._build_ui()

    def _build_ui(self):
        j = self.job
        layout = QVBoxLayout(self)

        src = f"{j.source_vm}  @  {j.source_host.address}"
        dst = f"[{j.target_datastore}]  @  {j.target_host.address}"
        info = QLabel(f"Quelle: {src}\nZiel: {dst}")
        info.setStyleSheet("font-weight: bold;")
        layout.addWidget(info)

        form = QFormLayout()
        self.edit_name = QLineEdit(j.name or j.source_vm)
        form.addRow("Job-Name:", self.edit_name)

        self.edit_folder = QLineEdit(j.target_vm_folder)
        self.edit_folder.setPlaceholderText(
            "leer = VM-Name  (Ergebnis: <Präfix>_backup_<Zeitstempel>)")
        form.addRow("Namenspräfix:", self.edit_folder)

        self.spin_retention = QSpinBox()
        self.spin_retention.setRange(1, 20)
        self.spin_retention.setValue(j.retention_count)
        self.spin_retention.setSuffix(" Backup(s)")
        form.addRow("Aufbewahren:", self.spin_retention)

        self.chk_quiesce = QCheckBox(
            "Dateisystem einfrieren (Quiesce, braucht VMware Tools)")
        self.chk_quiesce.setChecked(j.quiesce)
        form.addRow("", self.chk_quiesce)
        layout.addLayout(form)

        grp = QGroupBox("Zeitplan")
        gf = QFormLayout(grp)
        self.combo_mode = QComboBox()
        for _, label in _MODES:
            self.combo_mode.addItem(label)
        self.combo_mode.setCurrentIndex(
            next((i for i, (m, _) in enumerate(_MODES) if m == j.schedule.mode), 0))
        self.combo_mode.currentIndexChanged.connect(self._update_schedule_fields)
        gf.addRow("Modus:", self.combo_mode)

        self.spin_hours = QSpinBox()
        self.spin_hours.setRange(1, 168)
        self.spin_hours.setValue(j.schedule.interval_hours)
        self.spin_hours.setSuffix(" Stunden")
        gf.addRow("Intervall:", self.spin_hours)

        h, m = 22, 0
        try:
            h, m = (int(x) for x in j.schedule.time_of_day.split(":"))
        except ValueError:
            pass
        self.time_edit = QTimeEdit(QTime(h, m))
        self.time_edit.setDisplayFormat("HH:mm")
        gf.addRow("Uhrzeit:", self.time_edit)

        self.combo_weekday = QComboBox()
        self.combo_weekday.addItems(_WEEKDAYS)
        self.combo_weekday.setCurrentIndex(j.schedule.weekday % 7)
        gf.addRow("Wochentag:", self.combo_weekday)

        self.chk_taskplaner = QCheckBox(
            "Im Windows-Taskplaner registrieren (läuft auch ohne offene App)")
        self.chk_taskplaner.setChecked(j.use_windows_task)
        self.chk_taskplaner.setEnabled(sys.platform == "win32")
        if sys.platform != "win32":
            self.chk_taskplaner.setToolTip("Nur unter Windows verfügbar.")
        gf.addRow("", self.chk_taskplaner)
        layout.addWidget(grp)

        hw_row = QHBoxLayout()
        self.lbl_hw = QLabel()
        self._update_hw_label()
        btn_hw = QPushButton("Hardware anpassen ...")
        btn_hw.clicked.connect(self._edit_hardware)
        hw_row.addWidget(self.lbl_hw, 1)
        hw_row.addWidget(btn_hw)
        layout.addLayout(hw_row)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)
        self._update_schedule_fields()

    def _update_schedule_fields(self):
        mode = _MODES[self.combo_mode.currentIndex()][0]
        self.spin_hours.setEnabled(mode == "hourly")
        self.time_edit.setEnabled(mode in ("daily", "weekly"))
        self.combo_weekday.setEnabled(mode == "weekly")
        self.chk_taskplaner.setEnabled(sys.platform == "win32" and mode != "manual")

    def _update_hw_label(self):
        p = self._hardware
        parts = []
        parts.append(f"CPU: {p.cpu_count or 'wie Quelle'}")
        parts.append(f"RAM: {str(p.memory_mb) + ' MB' if p.memory_mb else 'wie Quelle'}")
        parts.append(f"MAC neu: {'ja' if p.generate_new_mac else 'nein'}")
        self.lbl_hw.setText("Hardware: " + ", ".join(parts))

    def _edit_hardware(self):
        dlg = HardwareDialog(self._hardware, self._networks,
                             title="Hardware für diesen Job", parent=self)
        if dlg.exec():
            self._hardware = dlg.result_profile()
            self._update_hw_label()

    def apply_to_job(self) -> BackupJob:
        j = self.job
        j.name = self.edit_name.text().strip() or j.source_vm
        j.target_vm_folder = self.edit_folder.text().strip()
        j.retention_count = self.spin_retention.value()
        j.quiesce = self.chk_quiesce.isChecked()
        j.hardware = self._hardware
        t = self.time_edit.time()
        j.schedule = Schedule(
            mode=_MODES[self.combo_mode.currentIndex()][0],
            interval_hours=self.spin_hours.value(),
            time_of_day=f"{t.hour():02d}:{t.minute():02d}",
            weekday=self.combo_weekday.currentIndex(),
        )
        return j

    def wants_windows_task(self) -> bool:
        return self.chk_taskplaner.isEnabled() and self.chk_taskplaner.isChecked()
