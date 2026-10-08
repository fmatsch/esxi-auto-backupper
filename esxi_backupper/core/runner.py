"""Führt einen Backup-Job aus (verwendet von GUI-Thread und CLI-Modus).

Baut die Verbindungen aus der Job-Konfiguration + Credential-Store auf,
startet die Pipeline und aktualisiert die Job-Statusfelder.
"""

from __future__ import annotations

import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Iterator, Optional

from . import credentials, versions
from .config import BackupJob, config_dir, find_job, load_config, save_config
from .esxi_client import EsxiClient, EsxiError, VmInfo
from .job import BackupPipeline
from .ssh_client import EsxiSshClient
from .transfer import ProgressCallback


class JobAlreadyRunning(EsxiError):
    pass


@contextmanager
def job_lock(job_id: str) -> Iterator[None]:
    """Verhindert, dass derselbe Job zweimal gleichzeitig läuft (App + Taskplaner).

    Nutzt eine Dateisperre statt einer Marker-Datei: das Betriebssystem gibt sie
    beim Prozessende frei, nach einem Absturz bleibt also nichts hängen.
    """
    path = config_dir() / f"job-{job_id}.lock"
    handle = open(path, "a+b")
    try:
        try:
            if sys.platform == "win32":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise JobAlreadyRunning(
                "Dieser Job läuft bereits (in einer anderen App-Instanz oder "
                "über den Taskplaner)."
            )
        yield
    finally:
        try:
            if sys.platform == "win32":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        handle.close()


def _password_for(host_cfg) -> str:
    pw = credentials.get_password(host_cfg.address, host_cfg.username)
    if not pw:
        raise EsxiError(
            f"Kein gespeichertes Passwort für {host_cfg.username}@{host_cfg.address}. "
            "Bitte in der App einmal mit 'Passwort speichern' verbinden."
        )
    return pw


def list_job_versions(job: BackupJob) -> list[tuple[datetime, VmInfo]]:
    """Alle Backup-Versionen des Jobs auf dem Ziel-Host (neueste zuerst)."""
    client = EsxiClient(job.target_host.address, job.target_host.username,
                        _password_for(job.target_host), job.target_host.port)
    client.connect()
    try:
        return versions.list_versions(client, job)
    finally:
        client.disconnect()


def delete_job_versions(job: BackupJob, vm_names: list[str],
                        log: Optional[Callable[[str], None]] = None) -> list[str]:
    """Löscht die genannten Versionen. Gibt Fehlermeldungen zurück (leer = alles gelöscht).

    Hält die Job-Sperre: während ein Backup läuft, wird nichts gelöscht.
    """
    with job_lock(job.id):
        pw = _password_for(job.target_host)
        client = EsxiClient(job.target_host.address, job.target_host.username,
                            pw, job.target_host.port)
        ssh = None
        if job.target_host.use_ssh_fallback:
            ssh_pw = credentials.get_password(job.target_host.address,
                                              job.target_host.username, ssh=True) or pw
            ssh = EsxiSshClient(job.target_host.address, job.target_host.username,
                                ssh_pw, job.target_host.ssh_port)
        errors: list[str] = []
        client.connect()
        try:
            known = {info.name: info for _, info in versions.list_versions(client, job)}
            for name in vm_names:
                info = known.get(name)
                if info is None:
                    errors.append(f"'{name}' gehört nicht (mehr) zu diesem Job.")
                    continue
                try:
                    versions.delete_version(client, ssh, info, log)
                except EsxiError as e:
                    errors.append(f"{name}: {e}")
        finally:
            client.disconnect()
            if ssh:
                ssh.disconnect()
        return errors


def _store_status(job: BackupJob) -> None:
    """Schreibt nur die Statusfelder dieses Jobs in die frisch geladene Config,
    damit parallele Änderungen (GUI/anderer Lauf) nicht überschrieben werden."""
    cfg = load_config()
    stored = find_job(cfg, job.id)
    if stored is None:
        return
    stored.last_run = job.last_run
    stored.last_status = job.last_status
    stored.last_error = job.last_error
    save_config(cfg)


def run_job(
    job: BackupJob,
    log: Optional[Callable[[str], None]] = None,
    progress: Optional[ProgressCallback] = None,
    cancel: Optional[threading.Event] = None,
) -> str:
    """Führt den Job aus und aktualisiert last_run/last_status/last_error
    (im Objekt und in der Config-Datei). Wirft EsxiError bei Fehlern."""
    with job_lock(job.id):
        return _run_locked(job, log, progress, cancel)


def _run_locked(job, log, progress, cancel) -> str:
    src_pw = _password_for(job.source_host)
    dst_pw = _password_for(job.target_host)

    source = EsxiClient(job.source_host.address, job.source_host.username,
                        src_pw, job.source_host.port)
    target = EsxiClient(job.target_host.address, job.target_host.username,
                        dst_pw, job.target_host.port)

    source_ssh = target_ssh = None
    if job.source_host.use_ssh_fallback:
        ssh_pw = credentials.get_password(job.source_host.address,
                                          job.source_host.username, ssh=True) or src_pw
        source_ssh = EsxiSshClient(job.source_host.address, job.source_host.username,
                                   ssh_pw, job.source_host.ssh_port)
    if job.target_host.use_ssh_fallback:
        ssh_pw = credentials.get_password(job.target_host.address,
                                          job.target_host.username, ssh=True) or dst_pw
        target_ssh = EsxiSshClient(job.target_host.address, job.target_host.username,
                                   ssh_pw, job.target_host.ssh_port)

    job.last_run = datetime.now().isoformat(timespec="seconds")
    job.last_status = "running"
    job.last_error = ""
    _store_status(job)

    try:
        source.connect()
        target.connect()
        pipeline = BackupPipeline(
            job, source, target, source_ssh=source_ssh, target_ssh=target_ssh,
            log=log, progress=progress, cancel=cancel,
        )
        result = pipeline.run()
        job.last_status = "ok"
        return result
    except BaseException as e:
        job.last_status = "error"
        job.last_error = str(e)
        raise
    finally:
        source.disconnect()
        target.disconnect()
        for ssh in (source_ssh, target_ssh):
            if ssh:
                ssh.disconnect()
        _store_status(job)
