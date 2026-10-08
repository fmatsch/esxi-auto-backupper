"""Schneller Transfer von Thin-Disks per SSH (vmkfstools-Export/-Import).

Warum nicht einfach "dünn klonen und per HTTP kopieren"? Der HTTP-Zugriff
liefert auch bei Thin-Disks die komplette logische Größe aus (Nullblöcke
inklusive). Schneller wird es nur, wenn die Nullblöcke gar nicht erst über
das Netzwerk gehen. Deshalb:

  Quell-Host:  vmkfstools -i <eingefrorene Basis-Disk> -d 2gbsparse  -> kleine Dateien
  dieser PC:   kopiert nur diese Dateien (Größe ~ belegte Daten) zum Ziel
  Ziel-Host:   vmkfstools -i <sparse> -d thin                        -> fertige Thin-Disk

Voraussetzung: SSH auf beiden Hosts. Die Pipeline fällt bei jedem Problem
(fehlende Voraussetzung, Platzmangel, Fehler, falsche Größe) automatisch auf
den vollständigen HTTP-Transfer zurück; nur ein Abbruch durch den Benutzer
wird nicht abgefangen.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from .esxi_client import Cancelled, EsxiClient, EsxiError
from .ssh_client import EsxiSshClient
from .transfer import ProgressCallback, copy_datastore_file, format_bytes

GIB = 1024 ** 3

# Thin-Export lohnt nur, wenn die Disks deutlich leerer als ihre logische Größe sind
MAX_FILL_RATIO = 0.70
# Sicherheitszuschläge: Quell-Datastore trägt laufende VMs und muss immer Luft behalten
SRC_SPACE_FACTOR, SRC_SPACE_RESERVE = 1.15, 2 * GIB
DST_SPACE_FACTOR, DST_SPACE_RESERVE = 2.3, 2 * GIB


@dataclass
class DiskChain:
    """Eine Disk mit Snapshot-Kette, die ab `descriptor_rel` (eingefroren) kopiert wird."""
    name: str                       # Dateiname der Disk auf dem Ziel, z.B. "srv01.vmdk"
    descriptor_rel: str             # Quell-Deskriptor, relativ zum Datastore
    files: dict[str, str] = field(default_factory=dict)   # {quell_rel: ziel_name}, ganze Kette
    extents: list[str] = field(default_factory=list)      # alle Extent-Dateien der Kette (rel)
    base_extents: list[str] = field(default_factory=list) # Extents der Basis-Disk (rel)

    @property
    def flat_name(self) -> str:
        stem = self.name[:-5] if self.name.lower().endswith(".vmdk") else self.name
        return f"{stem}-flat.vmdk"


def _abs(ds: str, rel: str) -> str:
    return f"/vmfs/volumes/{ds}/{rel}"


class ThinTransfer:
    def __init__(
        self,
        job_id: str,
        source: EsxiClient,
        target: EsxiClient,
        source_ssh: Optional[EsxiSshClient],
        target_ssh: Optional[EsxiSshClient],
        log: Callable[[str], None],
        progress: Optional[ProgressCallback] = None,
        cancel: Optional[threading.Event] = None,
    ):
        self.job_id = job_id
        self.source = source
        self.target = target
        self.source_ssh = source_ssh
        self.target_ssh = target_ssh
        self.log = log
        self.progress = progress
        self.cancel = cancel or threading.Event()

    # --- Vorab-Prüfung --------------------------------------------------------

    def check(self, chains: list[DiskChain], src_ds: str, dst_ds: str) -> Optional[str]:
        """None = Thin-Export möglich und sinnvoll, sonst der Grund für den Verzicht."""
        if not chains:
            return "keine Disk mit Snapshot-Kette"
        if self.source_ssh is None or self.target_ssh is None:
            return "SSH ist nicht für beide Hosts konfiguriert"
        try:
            for ssh in (self.source_ssh, self.target_ssh):
                ssh.ensure_connected()
                if not ssh.has_command("vmkfstools"):
                    return f"vmkfstools nicht gefunden auf {ssh.address}"
            extents = [rel for c in chains for rel in c.extents]
            allocated = self.source_ssh.allocated_bytes([_abs(src_ds, r) for r in extents])
            provisioned = sum(self.source.file_size(src_ds, r)
                              for c in chains for r in c.base_extents)
        except Cancelled:
            raise
        except EsxiError as e:
            return str(e)

        if provisioned <= 0:
            return "Disk-Größe konnte nicht ermittelt werden"
        ratio = allocated / provisioned
        if ratio > MAX_FILL_RATIO:
            return (f"Disks sind zu {ratio:.0%} belegt "
                    f"({format_bytes(allocated)} von {format_bytes(provisioned)}) - "
                    "Thin-Export brächte keinen Vorteil")

        free = {d.name: d.free_space for d in self.source.list_datastores()}
        free_dst = {d.name: d.free_space for d in self.target.list_datastores()}
        need_src = allocated * SRC_SPACE_FACTOR + SRC_SPACE_RESERVE
        need_dst = allocated * DST_SPACE_FACTOR + DST_SPACE_RESERVE
        if src_ds not in free or free[src_ds] < need_src:
            return (f"zu wenig freier Platz auf Quell-Datastore '{src_ds}' für die "
                    f"Zwischendateien (benötigt ~{format_bytes(need_src)})")
        if dst_ds not in free_dst or free_dst[dst_ds] < need_dst:
            return (f"zu wenig freier Platz auf Ziel-Datastore '{dst_ds}' "
                    f"(benötigt ~{format_bytes(need_dst)})")

        self.log(f"Thin-Export: {format_bytes(allocated)} belegt von "
                 f"{format_bytes(provisioned)} - übertragen wird nur der belegte Teil.")
        return None

    # --- Ausführung -----------------------------------------------------------

    def run(self, chains: list[DiskChain], src_ds: str, dst_ds: str, dst_folder: str) -> None:
        """Exportiert, überträgt und importiert alle Ketten. Räumt immer auf.

        Wirft bei jedem Fehler (außer Abbruch: dann Cancelled); die bereits
        erzeugten Ziel-Disks werden in diesem Fall wieder entfernt.
        """
        src_ssh, dst_ssh = self.source_ssh, self.target_ssh
        assert src_ssh is not None and dst_ssh is not None
        tmp_src = f"_backupper_tmp_{self.job_id}"
        tmp_dst = f"{dst_folder}/_import"
        created: list[str] = []
        try:
            src_ssh.delete_path(src_ds, tmp_src)       # Rest eines abgestürzten Laufs
            src_ssh.make_directory(src_ds, tmp_src)
            dst_ssh.make_directory(dst_ds, tmp_dst)

            for i, chain in enumerate(chains, start=1):
                self._one_disk(i, len(chains), chain, src_ds, dst_ds, dst_folder,
                               tmp_src, tmp_dst, created)
        except BaseException:
            for name in created:
                self._remove_target_disk(dst_ssh, dst_ds, dst_folder, name)
            raise
        finally:
            self._cleanup_tmp(src_ssh, src_ds, tmp_src, "Quell-Host")
            self._cleanup_tmp(dst_ssh, dst_ds, tmp_dst, "Ziel-Host")

    def _one_disk(self, i, n, chain, src_ds, dst_ds, dst_folder, tmp_src, tmp_dst, created):
        src_ssh, dst_ssh = self.source_ssh, self.target_ssh
        tmp_name = f"d{i}.vmdk"
        label = f"Disk {i}/{n} ({chain.name})"

        self.log(f"Thin-Export {label}: exportiere auf dem Quell-Host ...")
        src_ssh.clone_disk(_abs(src_ds, chain.descriptor_rel),
                           _abs(src_ds, f"{tmp_src}/{tmp_name}"), "2gbsparse",
                           progress=self._step_logger(f"Export {chain.name}"),
                           cancel=self.cancel)

        names = [f for f in src_ssh.list_dir(_abs(src_ds, tmp_src))
                 if f == tmp_name or f.startswith(f"d{i}-")]
        if tmp_name not in names:
            raise EsxiError(f"Export von {chain.name} hat keinen Deskriptor erzeugt.")
        names.sort(key=lambda f: (f != tmp_name, f))   # Deskriptor zuerst

        self.log(f"Thin-Export {label}: übertrage {len(names)} Dateien ...")
        for k, fname in enumerate(names, start=1):
            if self.cancel.is_set():
                raise Cancelled("Backup abgebrochen.")
            copy_datastore_file(self.source, src_ds, f"{tmp_src}/{fname}",
                                self.target, dst_ds, f"{tmp_dst}/{fname}",
                                progress=self.progress, cancel=self.cancel,
                                file_index=k, file_count=len(names))

        self.log(f"Thin-Export {label}: importiere auf dem Ziel-Host als Thin-Disk ...")
        created.append(chain.name)
        dst_ssh.clone_disk(_abs(dst_ds, f"{tmp_dst}/{tmp_name}"),
                           _abs(dst_ds, f"{dst_folder}/{chain.name}"), "thin",
                           progress=self._step_logger(f"Import {chain.name}"),
                           cancel=self.cancel)

        expected = sum(self.source.file_size(src_ds, r) for r in chain.base_extents)
        actual = self.target.file_size(dst_ds, f"{dst_folder}/{chain.flat_name}")
        if actual != expected:
            raise EsxiError(
                f"Größenprüfung fehlgeschlagen für {chain.name}: Ziel hat {actual} statt "
                f"{expected} Bytes.")
        self.log(f"Thin-Export {label}: fertig und geprüft.")

    def _step_logger(self, label: str) -> Callable[[int], None]:
        last = {"step": -1}

        def report(pct: int) -> None:
            step = pct // 10
            if step > last["step"]:
                last["step"] = step
                self.log(f"  {label}: {step * 10}%")
        return report

    # --- Aufräumen ------------------------------------------------------------

    def _remove_target_disk(self, ssh: EsxiSshClient, ds: str, folder: str, name: str) -> None:
        flat = DiskChain(name=name, descriptor_rel="").flat_name
        for fname in (name, flat):
            try:
                ssh.delete_file(_abs(ds, f"{folder}/{fname}"))
            except EsxiError as e:
                self.log(f"WARNUNG: Teildatei {fname} konnte nicht entfernt werden: {e}")

    def _cleanup_tmp(self, ssh: EsxiSshClient, ds: str, folder: str, where: str) -> None:
        try:
            ssh.delete_path(ds, folder)
        except EsxiError as e:
            self.log(f"WARNUNG: Temporäre Dateien auf dem {where} "
                     f"([{ds}] {folder}) konnten nicht entfernt werden: {e}")
