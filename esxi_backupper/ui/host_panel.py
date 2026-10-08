"""Wiederverwendbares Host-Panel: links Quelle (VM-Liste), rechts Ziel (Datastores)."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..core import credentials
from ..core.config import HostConfig
from ..core.esxi_client import DatastoreInfo, EsxiClient, VmInfo
from ..core.transfer import format_bytes
from .workers import FuncWorker


class HostPanel(QGroupBox):
    """mode='source' zeigt VMs, mode='target' zeigt Datastores."""

    connected_changed = Signal(bool)
    selection_changed = Signal()

    def __init__(self, title: str, mode: str, parent=None):
        super().__init__(title, parent)
        self.mode = mode
        self.client: EsxiClient | None = None
        self.vms: list[VmInfo] = []
        self.datastores: list[DatastoreInfo] = []
        self.networks: list[str] = []
        self._worker: FuncWorker | None = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.edit_host = QLineEdit()
        self.edit_host.setPlaceholderText("z.B. 192.168.1.10 oder esxi01.local")
        self.edit_user = QLineEdit("root")
        self.edit_pass = QLineEdit()
        self.edit_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.chk_save = QCheckBox("Passwort speichern (für geplante Jobs nötig)")
        self.chk_save.setChecked(True)
        form.addRow("Host:", self.edit_host)
        form.addRow("Benutzer:", self.edit_user)
        form.addRow("Passwort:", self.edit_pass)
        layout.addLayout(form)
        layout.addWidget(self.chk_save)

        row = QHBoxLayout()
        self.btn_connect = QPushButton("Verbinden")
        self.btn_connect.clicked.connect(self._on_connect)
        self.lbl_status = QLabel("Nicht verbunden")
        self.lbl_status.setStyleSheet("color: gray;")
        row.addWidget(self.btn_connect)
        row.addWidget(self.lbl_status, 1)
        layout.addLayout(row)

        self.tree = QTreeWidget()
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        if self.mode == "source":
            self.tree.setHeaderLabels(["VM", "Status", "Gast-OS"])
        else:
            self.tree.setHeaderLabels(["Datastore", "Frei", "Gesamt"])
        self.tree.itemSelectionChanged.connect(self.selection_changed)
        layout.addWidget(self.tree, 1)

        self.edit_host.editingFinished.connect(self._prefill_password)

    # --- Verbindung -----------------------------------------------------------

    def _prefill_password(self):
        host, user = self.edit_host.text().strip(), self.edit_user.text().strip()
        if host and user and not self.edit_pass.text():
            pw = credentials.get_password(host, user)
            if pw:
                self.edit_pass.setText(pw)

    def _on_connect(self):
        host = self.edit_host.text().strip()
        user = self.edit_user.text().strip()
        password = self.edit_pass.text()
        if not host or not user or not password:
            self.lbl_status.setText("Bitte Host, Benutzer und Passwort angeben.")
            self.lbl_status.setStyleSheet("color: red;")
            return
        self.btn_connect.setEnabled(False)
        self.lbl_status.setText("Verbinde ...")
        self.lbl_status.setStyleSheet("color: gray;")

        def connect_and_fetch():
            client = EsxiClient(host, user, password)
            client.connect()
            vms = client.list_vms() if self.mode == "source" else []
            datastores = client.list_datastores()
            networks = client.list_networks()
            return client, vms, datastores, networks

        self._worker = FuncWorker(connect_and_fetch, self)
        self._worker.finished_ok.connect(self._on_connected)
        self._worker.failed.connect(self._on_connect_failed)
        self._worker.start()

    def _on_connected(self, result):
        if self.client:
            self.client.disconnect()
        self.client, self.vms, self.datastores, self.networks = result
        if self.chk_save.isChecked():
            credentials.set_password(self.edit_host.text().strip(),
                                     self.edit_user.text().strip(),
                                     self.edit_pass.text())
        self.btn_connect.setEnabled(True)
        self.btn_connect.setText("Neu verbinden")
        self.lbl_status.setText(f"Verbunden mit {self.client.address}")
        self.lbl_status.setStyleSheet("color: green;")
        self._fill_tree()
        self.connected_changed.emit(True)

    def _on_connect_failed(self, message: str):
        self.btn_connect.setEnabled(True)
        self.lbl_status.setText(message)
        self.lbl_status.setStyleSheet("color: red;")
        self.connected_changed.emit(False)

    def refresh(self):
        if self.client:
            self._on_connect()

    def _fill_tree(self):
        self.tree.clear()
        if self.mode == "source":
            for vm in self.vms:
                state = {"poweredOn": "läuft", "poweredOff": "aus",
                         "suspended": "pausiert"}.get(vm.power_state, vm.power_state)
                item = QTreeWidgetItem([vm.name, state, vm.guest_os])
                if vm.power_state == "poweredOn":
                    item.setForeground(1, Qt.GlobalColor.darkGreen)
                self.tree.addTopLevelItem(item)
        else:
            for ds in self.datastores:
                self.tree.addTopLevelItem(QTreeWidgetItem([
                    ds.name, format_bytes(ds.free_space), format_bytes(ds.capacity),
                ]))
        for i in range(self.tree.columnCount()):
            self.tree.resizeColumnToContents(i)

    # --- Auswahl --------------------------------------------------------------

    def selected_vm(self) -> VmInfo | None:
        items = self.tree.selectedItems()
        if self.mode != "source" or not items:
            return None
        name = items[0].text(0)
        return next((v for v in self.vms if v.name == name), None)

    def selected_datastore(self) -> DatastoreInfo | None:
        items = self.tree.selectedItems()
        if self.mode != "target" or not items:
            return None
        name = items[0].text(0)
        return next((d for d in self.datastores if d.name == name), None)

    def host_config(self) -> HostConfig:
        return HostConfig(address=self.edit_host.text().strip(),
                          username=self.edit_user.text().strip())
