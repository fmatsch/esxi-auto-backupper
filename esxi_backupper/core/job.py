"""Backup-Pipeline: Snapshot -> Dateien auflösen -> Transfer -> Registrieren.

Ablauf pro Lauf (BackupPipeline.run):
 1. Quell-VM finden, evtl. übrig gebliebenen Backup-Snapshot vom letzten
    (abgebrochenen) Lauf entfernen
 2. Snapshot erstellen (friert die Basis-Disks ein)
 3. VMX laden, pro Disk die Snapshot-Kette per parentFileNameHint auflösen:
    kopiert wird alles UNTER dem gerade erstellten Backup-Delta
 4. Zielordner anlegen, Dateien streamend kopieren (Quelle -> PC -> Ziel)
 5. VMX umschreiben (Name, Disk-Refs, Hardware-Profil) und hochladen
 6. VM auf dem Ziel registrieren (bleibt ausgeschaltet)
 7. Snapshot auf der Quelle entfernen
 8. Retention: alte Backup-VMs auf dem Ziel abräumen

Schreiboperationen versuchen zuerst die API; bei ApiReadOnlyError
(kostenlose Lizenz) wird auf SSH ausgewichen, sofern konfiguriert.
Bei Fehlern wird aufgeräumt: Snapshot weg, unvollständiger Zielordner weg.
"""

from __future__ import annotations

import posixpath
import re
import threading
from datetime import datetime
from typing import Callable, Optional

from . import vmx_editor
from .config import BackupJob, HardwareProfile
from .esxi_client import ApiReadOnlyError, EsxiClient, EsxiError, split_datastore_path
from .ssh_client import EsxiSshClient
from .transfer import ProgressCallback, copy_datastore_file, format_bytes

LogCallback = Callable[[str], None]

_TIMESTAMP_FMT = "%Y%m%d_%H%M%S"


def sanitize_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_")


class BackupPipeline:
    def __init__(
        self,
        job: BackupJob,
        source: EsxiClient,
        target: EsxiClient,
        source_ssh: Optional[EsxiSshClient] = None,
        target_ssh: Optional[EsxiSshClient] = None,
        log: Optional[LogCallback] = None,
        progress: Optional[ProgressCallback] = None,
        cancel: Optional[threading.Event] = None,
    ):
        self.job = job
        self.source = source
        self.target = target
        self.source_ssh = source_ssh
        self.target_ssh = target_ssh
        self._log = log or (lambda msg: None)
        self.progress = progress
        self.cancel = cancel or threading.Event()
        self.snapshot_name = f"AutoBackup_{job.id}"
        self._target_folder_created: Optional[tuple[str, str]] = None  # (ds, folder)
        self._registered = False
        self._descriptor_paths: set[str] = set()
        self._vmx_target_rel = ""

    def log(self, msg: str) -> None:
        self._log(msg)

    # --- Fallback-Helfer ------------------------------------------------------

    def _source_fallback(self) -> EsxiSshClient:
        if not self.source_ssh:
            raise EsxiError(
                "Quell-Host: API ist read-only (Free-Lizenz) und kein SSH-Fallback "
                "konfiguriert. Bitte SSH auf dem Host aktivieren."
            )
        return self.source_ssh

    def _target_fallback(self) -> EsxiSshClient:
        if not self.target_ssh:
            raise EsxiError(
                "Ziel-Host: API ist read-only (Free-Lizenz) und kein SSH-Fallback "
                "konfiguriert. Bitte SSH auf dem Host aktivieren."
            )
        return self.target_ssh

    # --- Pipeline-Schritte ----------------------------------------------------

    def run(self) -> str:
        """Führt das Backup aus, gibt den Namen der erstellten Ziel-VM zurück."""
        job = self.job
        vm = self.source.get_vm(job.source_vm)
        vmx_ds, vmx_rel = split_datastore_path(self.source.vm_path(vm))
        src_folder = posixpath.dirname(vmx_rel)
        self.log(f"Quell-VM '{job.source_vm}' gefunden: [{vmx_ds}] {vmx_rel}")

        self._cleanup_stale_snapshot(vm)

        # target_vm_folder dient als Namenspräfix; der Zeitstempel gehört immer
        # dazu, damit sich Läufe nie gegenseitig überschreiben (Retention räumt auf).
        timestamp = datetime.now().strftime(_TIMESTAMP_FMT)
        base_name = sanitize_name(job.target_vm_folder or job.source_vm)
        target_name = f"{base_name}_backup_{timestamp}"
        target_folder = target_name

        try:
            self._create_snapshot(vm)
            vmx_text = self.source.download_text(vmx_ds, vmx_rel)
            entries = vmx_editor.parse_vmx(vmx_text)
            files, disk_map = self._resolve_disk_chains(entries, vmx_ds, src_folder)
            self._add_nvram(entries, vmx_ds, src_folder, files)
            self._prepare_target_folder(job.target_datastore, target_folder)
            self._copy_files(vmx_ds, files, job.target_datastore, target_folder)
            self._upload_vmx(entries, disk_map, target_name,
                             job.target_datastore, target_folder)
            self._register(job.target_datastore, target_folder, target_name)
        except BaseException:
            self._cleanup_after_failure(vm)
            raise
        # Snapshot-Entfernung gehört zum Erfolgspfad; schlägt sie fehl, meldet
        # der Lauf einen Fehler, das Backup selbst ist aber schon vollständig.
        self._remove_snapshot(vm)
        self._apply_retention(base_name, target_name)
        self.log(f"Backup fertig: '{target_name}' auf {self.target.address} "
                 f"[{job.target_datastore}] (ausgeschaltet registriert)")
        return target_name

    def _cleanup_stale_snapshot(self, vm) -> None:
        if self.source.find_snapshot(vm, self.snapshot_name):
            self.log("Übrig gebliebener Backup-Snapshot vom letzten Lauf wird entfernt ...")
            self._remove_snapshot(vm)

    def _create_snapshot(self, vm) -> None:
        quiesce = self.job.quiesce and self.source.vm_tools_running(vm)
        self.log(f"Erstelle Snapshot '{self.snapshot_name}' "
                 f"({'mit' if quiesce else 'ohne'} Quiesce) ...")
        try:
            self.source.create_snapshot(vm, self.snapshot_name, quiesce)
        except ApiReadOnlyError:
            self.log("API read-only - erstelle Snapshot per SSH ...")
            self._source_fallback().create_snapshot(
                self.job.source_vm, self.snapshot_name, quiesce)
        except EsxiError as e:
            if quiesce:
                self.log(f"Quiesce-Snapshot fehlgeschlagen ({e}) - versuche ohne Quiesce ...")
                self.source.create_snapshot(vm, self.snapshot_name, False)
            else:
                raise

    def _resolve_disk_chains(
        self, entries: dict[str, str], ds: str, src_folder: str
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Ermittelt alle zu kopierenden Dateien und das VMX-Disk-Mapping.

        Rückgabe: (files {quell_rel_pfad: ziel_dateiname}, disk_map für rewrite_vmx)
        Deskriptor-Dateien werden zusätzlich in self._descriptor_paths vermerkt,
        damit der Kopierschritt sie als Text behandelt (Parent-Hint kürzen) und
        Binärdateien niemals durch die Textverarbeitung laufen.
        """
        files: dict[str, str] = {}
        disk_map: dict[str, str] = {}
        disks = vmx_editor.list_disk_files(entries)
        if not disks:
            raise EsxiError("Keine virtuellen Disks in der VMX gefunden.")

        for vmx_key, ref in disks.items():
            if ref.startswith("/") or ref.startswith("["):
                raise EsxiError(
                    f"Disk '{ref}' liegt außerhalb des VM-Ordners (absoluter Pfad). "
                    "Das wird aktuell nicht unterstützt - bitte die Disk in den "
                    "VM-Ordner verschieben (Storage vMotion/Kopie) und erneut versuchen."
                )
            top_rel = posixpath.normpath(posixpath.join(src_folder, ref))
            top_text = self.source.download_text(ds, top_rel)
            parent = vmx_editor.descriptor_parent(top_text)

            if parent is None:
                # Independent-Disk o.ä.: kein Delta durch den Snapshot -> Disk wird
                # im laufenden Zustand kopiert (nicht crash-konsistent!)
                self.log(f"WARNUNG: Disk '{ref}' ist nicht im Snapshot enthalten "
                         "(independent?) - wird live kopiert, Konsistenz nicht garantiert.")
                chain_start = ref
            else:
                chain_start = posixpath.basename(parent)
                disk_map[ref] = chain_start

            # Kette ab chain_start bis zur Basis einsammeln
            current = chain_start
            seen = set()
            while current:
                if current in seen:
                    raise EsxiError(f"Zirkuläre Snapshot-Kette bei '{current}'.")
                seen.add(current)
                cur_rel = posixpath.normpath(posixpath.join(src_folder, current))
                cur_text = self.source.download_text(ds, cur_rel)
                files[cur_rel] = posixpath.basename(current)
                self._descriptor_paths.add(cur_rel)
                for extent in vmx_editor.descriptor_extents(cur_text):
                    ext_rel = posixpath.normpath(posixpath.join(src_folder, extent))
                    files[ext_rel] = posixpath.basename(extent)
                nxt = vmx_editor.descriptor_parent(cur_text)
                current = posixpath.basename(nxt) if nxt else None

        return files, disk_map

    def _add_nvram(self, entries: dict[str, str], ds: str,
                   src_folder: str, files: dict[str, str]) -> None:
        nvram = None
        for k, v in entries.items():
            if k.lower() == "nvram" and v:
                nvram = v
                break
        if not nvram:
            return
        rel = posixpath.normpath(posixpath.join(src_folder, nvram))
        try:
            self.source.file_size(ds, rel)
            files[rel] = posixpath.basename(nvram)
        except EsxiError:
            pass  # keine NVRAM-Datei vorhanden - unkritisch

    def _prepare_target_folder(self, ds: str, folder: str) -> None:
        self.log(f"Lege Zielordner an: [{ds}] {folder}")
        try:
            self.target.make_directory(ds, folder)
        except ApiReadOnlyError:
            self._target_fallback().make_directory(ds, folder)
        self._target_folder_created = (ds, folder)

    def _copy_files(self, src_ds: str, files: dict[str, str],
                    dst_ds: str, dst_folder: str) -> None:
        # Deskriptoren (klein, Text) zuletzt hochladen wäre egal - wir kopieren
        # in stabiler Reihenfolge und melden Fortschritt über die großen Extents.
        items = sorted(files.items())
        total = 0
        sizes: dict[str, int] = {}
        for src_rel, _ in items:
            sizes[src_rel] = self.source.file_size(src_ds, src_rel)
            total += sizes[src_rel]
        self.log(f"{len(items)} Dateien zu kopieren, gesamt {format_bytes(total)} "
                 "(Thin-Disks werden in voller Größe übertragen)")

        for idx, (src_rel, dst_name) in enumerate(items, start=1):
            if self.cancel.is_set():
                raise EsxiError("Backup abgebrochen.")
            dst_rel = f"{dst_folder}/{dst_name}"
            size = sizes[src_rel]
            self.log(f"[{idx}/{len(items)}] {dst_name} ({format_bytes(size)}) ...")
            if src_rel in self._descriptor_paths:
                # Deskriptor: als Text kopieren und parentFileNameHint auf
                # reinen Dateinamen kürzen (alles liegt im selben Zielordner)
                text = self.source.download_text(src_ds, src_rel)
                parent = vmx_editor.descriptor_parent(text)
                if parent and parent != posixpath.basename(parent):
                    text = vmx_editor.set_descriptor_parent(
                        text, posixpath.basename(parent))
                self.target.upload_text(dst_ds, dst_rel, text)
            else:
                copy_datastore_file(
                    self.source, src_ds, src_rel,
                    self.target, dst_ds, dst_rel,
                    progress=self.progress, cancel=self.cancel,
                    file_index=idx, file_count=len(items),
                )

    def _upload_vmx(self, entries: dict[str, str], disk_map: dict[str, str],
                    target_name: str, dst_ds: str, dst_folder: str) -> None:
        hardware = self._effective_hardware()
        out = vmx_editor.rewrite_vmx(entries, target_name, disk_map, hardware)
        vmx_name = f"{sanitize_name(target_name)}.vmx"
        self.log(f"Schreibe angepasste VMX: {vmx_name}")
        self.target.upload_text(dst_ds, f"{dst_folder}/{vmx_name}",
                                vmx_editor.serialize_vmx(out))
        self._vmx_target_rel = f"{dst_folder}/{vmx_name}"

    def _effective_hardware(self) -> HardwareProfile:
        return self.job.hardware

    def _register(self, ds: str, folder: str, name: str) -> None:
        self.log(f"Registriere VM '{name}' auf {self.target.address} ...")
        vmx_rel = self._vmx_target_rel
        try:
            self.target.register_vm(f"[{ds}] {vmx_rel}", name)
        except ApiReadOnlyError:
            self._target_fallback().register_vm(f"/vmfs/volumes/{ds}/{vmx_rel}", name)
        self._registered = True

    def _remove_snapshot(self, vm) -> None:
        self.log("Entferne Backup-Snapshot auf der Quelle ...")
        try:
            self.source.remove_snapshot(vm, self.snapshot_name)
        except ApiReadOnlyError:
            self._source_fallback().remove_snapshot_by_name(
                self.job.source_vm, self.snapshot_name)

    def _apply_retention(self, base_name: str, keep_name: str) -> None:
        n = max(1, self.job.retention_count)
        prefix = f"{base_name}_backup_"
        backups = [v for v in self.target.list_vms()
                   if v.name.startswith(prefix) and v.power_state != "poweredOn"]
        backups.sort(key=lambda v: v.name)  # Timestamp im Namen ist sortierbar
        to_delete = backups[:-n] if len(backups) > n else []
        for old in to_delete:
            if old.name == keep_name:
                continue
            self.log(f"Retention: entferne altes Backup '{old.name}' ...")
            try:
                vm = self.target.get_vm(old.name)
                self.target.destroy_vm(vm)
            except ApiReadOnlyError:
                ssh = self._target_fallback()
                ds, rel = split_datastore_path(old.vmx_path)
                ssh.unregister_vm(old.name)
                ssh.delete_path(ds, posixpath.dirname(rel))
            except EsxiError as e:
                self.log(f"WARNUNG: Altes Backup '{old.name}' konnte nicht "
                         f"entfernt werden: {e}")

    def _cleanup_after_failure(self, vm) -> None:
        self.log("Fehler - räume auf ...")
        try:
            self._remove_snapshot(vm)
        except Exception as e:
            self.log(f"WARNUNG: Snapshot konnte nicht entfernt werden: {e}")
        if self._target_folder_created and not self._registered:
            ds, folder = self._target_folder_created
            try:
                self.log(f"Entferne unvollständigen Zielordner [{ds}] {folder} ...")
                try:
                    self.target.delete_datastore_path(ds, folder)
                except ApiReadOnlyError:
                    self._target_fallback().delete_path(ds, folder)
            except Exception as e:
                self.log(f"WARNUNG: Zielordner konnte nicht entfernt werden: {e}")
