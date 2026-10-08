"""Rendert einen Screenshot der App mit Beispieldaten (für README/Webseite).

Aufruf:  QT_QPA_PLATFORM=offscreen python tools/make_screenshot.py [ziel.png]
Alle Hosts/VMs sind erfundene Beispielwerte (*.example.lan); die echte
Konfiguration des Benutzers wird nicht angefasst (temporäres Config-Verzeichnis).
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()
os.environ["APPDATA"] = os.environ["XDG_CONFIG_HOME"]

from PySide6.QtWidgets import QApplication  # noqa: E402

from esxi_backupper.core.config import (  # noqa: E402
    AppConfig, BackupJob, HostConfig, Schedule, save_config,
)
from esxi_backupper.core.esxi_client import DatastoreInfo, VmInfo  # noqa: E402
from esxi_backupper.core.transfer import TransferProgress  # noqa: E402
from esxi_backupper.ui.main_window import MainWindow  # noqa: E402
from esxi_backupper.ui.resources import app_icon  # noqa: E402

GB = 1024 ** 3


def vm(name, state, os_name, cpu=2, mem=4096):
    return VmInfo(name=name, power_state=state, guest_os=os_name, vmx_path="",
                  tools_running=state == "poweredOn", num_cpu=cpu, memory_mb=mem,
                  has_snapshots=False)


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "screenshot.png"
    src = HostConfig(address="esxi-prod.example.lan")
    dst = HostConfig(address="esxi-backup.example.lan")
    now = datetime.now()
    cfg = AppConfig(jobs=[
        BackupJob(name="Fileserver nachts", source_host=src, source_vm="srv-files01",
                  target_host=dst, target_datastore="backup-ssd",
                  schedule=Schedule(mode="daily", time_of_day="22:00"),
                  last_run=(now - timedelta(hours=11)).isoformat(timespec="seconds"),
                  last_status="ok"),
        BackupJob(name="Webserver wöchentlich", source_host=src, source_vm="srv-web02",
                  target_host=dst, target_datastore="backup-hdd",
                  schedule=Schedule(mode="weekly", weekday=6, time_of_day="03:00"),
                  last_run=(now - timedelta(days=3)).isoformat(timespec="seconds"),
                  last_status="ok"),
    ])
    save_config(cfg)

    app = QApplication(sys.argv)
    app.setWindowIcon(app_icon())
    win = MainWindow()
    win.resize(1180, 800)

    s, t = win.panel_source, win.panel_target
    s.edit_host.setText(src.address)
    s.lbl_status.setText(f"Verbunden mit {src.address}")
    s.lbl_status.setStyleSheet("color: green;")
    s.btn_connect.setText("Neu verbinden")
    s.vms = [
        vm("srv-dc01", "poweredOn", "Microsoft Windows Server 2022", 4, 8192),
        vm("srv-files01", "poweredOn", "Microsoft Windows Server 2019", 4, 16384),
        vm("srv-web02", "poweredOn", "Ubuntu Linux (64-bit)"),
        vm("srv-test", "poweredOff", "Debian GNU/Linux 12 (64-bit)"),
    ]
    s._fill_tree()
    s.tree.topLevelItem(1).setSelected(True)

    t.edit_host.setText(dst.address)
    t.lbl_status.setText(f"Verbunden mit {dst.address}")
    t.lbl_status.setStyleSheet("color: green;")
    t.btn_connect.setText("Neu verbinden")
    t.datastores = [
        DatastoreInfo("backup-ssd", 2000 * GB, 1430 * GB, "VMFS"),
        DatastoreInfo("backup-hdd", 8000 * GB, 5120 * GB, "VMFS"),
    ]
    t._fill_tree()
    t.tree.topLevelItem(0).setSelected(True)

    win.log.append("=== Starte Backup-Job 'Fileserver nachts' ===")
    win.log.append("Quell-VM 'srv-files01' gefunden: [datastore1] srv-files01/srv-files01.vmx")
    win.log.append("Erstelle Snapshot 'AutoBackup_3f9a1c' (mit Quiesce) ...")
    win.log.append("Lege Zielordner an: [backup-ssd] srv-files01_backup_20261008_220000")
    win.log.append("3 Dateien zu kopieren, gesamt 120.0 GB (Thin-Disks werden in voller Größe übertragen)")
    win.log.append("[2/3] srv-files01-flat.vmdk (120.0 GB) ...")
    win.log.update_progress(TransferProgress(
        file_name="srv-files01-flat.vmdk", file_index=2, file_count=3,
        bytes_done=int(71.5 * GB), bytes_total=120 * GB, rate_bps=112 * 1024 * 1024))

    win.show()
    app.processEvents()
    win.grab().save(str(out))
    print(f"Screenshot geschrieben: {out}")


if __name__ == "__main__":
    main()
