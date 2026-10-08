"""Konfiguration: Hosts, Hardware-Profil und Backup-Jobs als JSON.

Speicherort: %APPDATA%\\EsxiAutoBackupper\\config.json (Windows)
bzw. ~/.config/EsxiAutoBackupper/config.json (macOS/Linux, Entwicklung).
Passwörter liegen NIE in dieser Datei, nur im Credential Manager (siehe credentials.py).
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / "EsxiAutoBackupper"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return config_dir() / "config.json"


@dataclass
class HostConfig:
    """Ein ESXi-Host (Quelle oder Ziel)."""
    address: str = ""
    username: str = "root"
    port: int = 443
    ssh_port: int = 22
    use_ssh_fallback: bool = True  # SSH nutzen, wenn API read-only (Free-Lizenz)


@dataclass
class HardwareProfile:
    """Hardware-Overrides für die Ziel-VM. None/leer = Wert der Quell-VM übernehmen."""
    cpu_count: Optional[int] = None
    memory_mb: Optional[int] = None
    network_mapping: dict[str, str] = field(default_factory=dict)  # Quell-Portgruppe -> Ziel-Portgruppe
    default_network: str = ""       # Ziel-Portgruppe für nicht gemappte NICs ("" = unverändert)
    generate_new_mac: bool = True   # MAC-Adressen neu generieren (vermeidet Konflikte im LAN)
    keep_uuid: bool = False         # BIOS-UUID beibehalten (False = ESXi vergibt neue)


@dataclass
class Schedule:
    """Zeitplan eines Jobs."""
    mode: str = "manual"            # manual | hourly | daily | weekly
    interval_hours: int = 6         # bei mode=hourly: alle N Stunden
    time_of_day: str = "22:00"      # bei daily/weekly: Uhrzeit HH:MM
    weekday: int = 6                # bei weekly: 0=Montag .. 6=Sonntag


@dataclass
class BackupJob:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    source_host: HostConfig = field(default_factory=HostConfig)
    source_vm: str = ""             # VM-Name auf dem Quell-Host
    target_host: HostConfig = field(default_factory=HostConfig)
    target_datastore: str = ""
    target_vm_folder: str = ""      # leer = "<vm>_backup_<timestamp>"
    hardware: HardwareProfile = field(default_factory=HardwareProfile)
    schedule: Schedule = field(default_factory=Schedule)
    retention_count: int = 2        # so viele Backup-VMs auf dem Ziel behalten
    quiesce: bool = True            # Snapshot mit Quiesce versuchen (braucht VMware Tools)
    use_windows_task: bool = False  # läuft über den Taskplaner -> interner Scheduler überspringt den Job
    last_run: str = ""              # ISO-Timestamp des letzten Laufs
    last_status: str = ""           # ok | error | running
    last_error: str = ""


@dataclass
class AppConfig:
    hosts: list[HostConfig] = field(default_factory=list)          # gemerkte Hosts für die Panels
    default_hardware: HardwareProfile = field(default_factory=HardwareProfile)
    jobs: list[BackupJob] = field(default_factory=list)


def _host_from_dict(d: dict) -> HostConfig:
    return HostConfig(**{k: v for k, v in d.items() if k in HostConfig.__dataclass_fields__})


def _job_from_dict(d: dict) -> BackupJob:
    job = BackupJob(**{
        k: v for k, v in d.items()
        if k in BackupJob.__dataclass_fields__
        and k not in ("source_host", "target_host", "hardware", "schedule")
    })
    job.source_host = _host_from_dict(d.get("source_host", {}))
    job.target_host = _host_from_dict(d.get("target_host", {}))
    hw = d.get("hardware", {})
    job.hardware = HardwareProfile(**{k: v for k, v in hw.items() if k in HardwareProfile.__dataclass_fields__})
    sc = d.get("schedule", {})
    job.schedule = Schedule(**{k: v for k, v in sc.items() if k in Schedule.__dataclass_fields__})
    return job


def load_config(path: Optional[Path] = None) -> AppConfig:
    p = path or config_path()
    if not p.exists():
        return AppConfig()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppConfig()
    cfg = AppConfig()
    cfg.hosts = [_host_from_dict(h) for h in data.get("hosts", [])]
    hw = data.get("default_hardware", {})
    cfg.default_hardware = HardwareProfile(
        **{k: v for k, v in hw.items() if k in HardwareProfile.__dataclass_fields__}
    )
    cfg.jobs = [_job_from_dict(j) for j in data.get("jobs", [])]
    return cfg


def save_config(cfg: AppConfig, path: Optional[Path] = None) -> None:
    p = path or config_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(cfg), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def find_job(cfg: AppConfig, job_id: str) -> Optional[BackupJob]:
    for job in cfg.jobs:
        if job.id == job_id:
            return job
    return None
