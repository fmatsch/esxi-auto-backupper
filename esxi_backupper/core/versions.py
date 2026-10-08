"""Backup-Versionen eines Jobs: erkennen, auflisten, löschen.

Eine Version ist eine auf dem Ziel-Host registrierte VM mit dem Namen
`<Präfix>_backup_<JJJJMMTT_HHMMSS>`. Der Zeitstempel muss exakt passen -
so fasst die Verwaltung nie fremde VMs an, die nur zufällig ähnlich heißen.
"""

from __future__ import annotations

import posixpath
import re
from datetime import datetime
from typing import Callable, Optional

from .config import BackupJob
from .esxi_client import ApiReadOnlyError, EsxiClient, EsxiError, VmInfo, split_datastore_path
from .ssh_client import EsxiSshClient

TIMESTAMP_FMT = "%Y%m%d_%H%M%S"
_TS_RE = re.compile(r"^(\d{8}_\d{6})$")


def sanitize_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_")


def backup_base_name(job: BackupJob) -> str:
    return sanitize_name(job.target_vm_folder or job.source_vm)


def backup_prefix(job: BackupJob) -> str:
    return f"{backup_base_name(job)}_backup_"


def version_time(vm_name: str, prefix: str) -> Optional[datetime]:
    """Zeitpunkt einer Version aus dem VM-Namen; None, wenn der Name nicht passt."""
    if not vm_name.startswith(prefix):
        return None
    m = _TS_RE.match(vm_name[len(prefix):])
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), TIMESTAMP_FMT)
    except ValueError:
        return None


def list_versions(target: EsxiClient, job: BackupJob) -> list[tuple[datetime, VmInfo]]:
    """Alle Versionen des Jobs auf dem Ziel-Host, neueste zuerst."""
    prefix = backup_prefix(job)
    found = []
    for info in target.list_vms():
        ts = version_time(info.name, prefix)
        if ts is not None:
            found.append((ts, info))
    found.sort(key=lambda item: item[0], reverse=True)
    return found


def delete_version(
    target: EsxiClient,
    target_ssh: Optional[EsxiSshClient],
    info: VmInfo,
    log: Optional[Callable[[str], None]] = None,
) -> None:
    """Meldet die Backup-VM ab und löscht ihre Dateien. Läuft die VM, passiert nichts."""
    if info.power_state == "poweredOn":
        raise EsxiError(f"'{info.name}' läuft und wird nicht gelöscht.")
    try:
        vm = target.get_vm(info.name)
        target.destroy_vm(vm)
    except ApiReadOnlyError:
        if target_ssh is None:
            raise EsxiError(
                "Ziel-Host: API ist read-only (Free-Lizenz) und kein SSH-Fallback "
                "konfiguriert. Bitte SSH auf dem Host aktivieren."
            )
        if log:
            log(f"API read-only - lösche '{info.name}' per SSH ...")
        ds, rel = split_datastore_path(info.vmx_path)
        target_ssh.unregister_vm(info.name)
        target_ssh.delete_path(ds, posixpath.dirname(rel))
