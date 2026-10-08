"""Dialog für das globale Hardware-Standardprofil (gilt für neue Jobs)."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from ..core.config import HardwareProfile


class HardwareDialog(QDialog):
    """Bearbeitet ein HardwareProfile. 0 bzw. leer = Wert der Quell-VM übernehmen."""

    def __init__(self, profile: HardwareProfile, networks: list[str] | None = None,
                 title: str = "Hardware-Standardprofil", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(480)
        self._build_ui(profile, networks or [])

    def _build_ui(self, p: HardwareProfile, networks: list[str]):
        layout = QVBoxLayout(self)
        info = QLabel("Diese Werte überschreiben die Hardware der Quell-VM.\n"
                      "0 bzw. leer bedeutet: Wert unverändert übernehmen.")
        info.setStyleSheet("color: gray;")
        layout.addWidget(info)

        form = QFormLayout()
        self.spin_cpu = QSpinBox()
        self.spin_cpu.setRange(0, 128)
        self.spin_cpu.setSpecialValueText("wie Quelle")
        self.spin_cpu.setValue(p.cpu_count or 0)
        form.addRow("CPU-Kerne:", self.spin_cpu)

        self.spin_ram = QSpinBox()
        self.spin_ram.setRange(0, 1024 * 1024)
        self.spin_ram.setSingleStep(1024)
        self.spin_ram.setSuffix(" MB")
        self.spin_ram.setSpecialValueText("wie Quelle")
        self.spin_ram.setValue(p.memory_mb or 0)
        form.addRow("Arbeitsspeicher:", self.spin_ram)

        self.combo_default_net = QComboBox()
        self.combo_default_net.setEditable(True)
        self.combo_default_net.addItem("")  # leer = unverändert
        for n in networks:
            self.combo_default_net.addItem(n)
        self.combo_default_net.setCurrentText(p.default_network)
        form.addRow("Standard-Zielnetzwerk:", self.combo_default_net)
        layout.addLayout(form)

        grp = QGroupBox("Netzwerk-Zuordnung (Quell-Portgruppe → Ziel-Portgruppe)")
        gl = QVBoxLayout(grp)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Quelle", "Ziel"])
        self.table.horizontalHeader().setStretchLastSection(True)
        for src, dst in p.network_mapping.items():
            self._add_row(src, dst)
        gl.addWidget(self.table)
        btns = QHBoxLayout()
        btn_add = QPushButton("Zeile hinzufügen")
        btn_add.clicked.connect(lambda: self._add_row("", ""))
        btn_del = QPushButton("Zeile entfernen")
        btn_del.clicked.connect(self._remove_row)
        btns.addWidget(btn_add)
        btns.addWidget(btn_del)
        btns.addStretch(1)
        gl.addLayout(btns)
        layout.addWidget(grp)

        self.chk_mac = QCheckBox("MAC-Adressen neu generieren (empfohlen, "
                                 "verhindert Konflikte mit der Original-VM)")
        self.chk_mac.setChecked(p.generate_new_mac)
        layout.addWidget(self.chk_mac)

        self.chk_uuid = QCheckBox("BIOS-UUID der Quell-VM beibehalten "
                                  "(nur für Lizenz-Sonderfälle)")
        self.chk_uuid.setChecked(p.keep_uuid)
        layout.addWidget(self.chk_uuid)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def _add_row(self, src: str, dst: str):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(src))
        self.table.setItem(r, 1, QTableWidgetItem(dst))

    def _remove_row(self):
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)

    def result_profile(self) -> HardwareProfile:
        mapping = {}
        for r in range(self.table.rowCount()):
            src = (self.table.item(r, 0).text() if self.table.item(r, 0) else "").strip()
            dst = (self.table.item(r, 1).text() if self.table.item(r, 1) else "").strip()
            if src and dst:
                mapping[src] = dst
        return HardwareProfile(
            cpu_count=self.spin_cpu.value() or None,
            memory_mb=self.spin_ram.value() or None,
            network_mapping=mapping,
            default_network=self.combo_default_net.currentText().strip(),
            generate_new_mac=self.chk_mac.isChecked(),
            keep_uuid=self.chk_uuid.isChecked(),
        )
