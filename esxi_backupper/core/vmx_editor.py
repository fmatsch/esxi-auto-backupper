"""Parsen und Umschreiben von VMX-Dateien für die Ziel-VM.

Eine VMX-Datei ist eine Liste von `key = "value"`-Zeilen. Beim Umschreiben:
- displayName -> neuer Backup-Name
- Disk-Referenzen: Snapshot-Deltas (xxx-000001.vmdk) -> Basis-Disks (laut disk_map)
- Hardware-Overrides aus dem HardwareProfile (CPU, RAM, Netzwerk-Mapping)
- MAC/UUID-Behandlung, damit die Kopie nicht mit dem Original kollidiert
- hostspezifische/statusbezogene Keys entfernen
"""

from __future__ import annotations

import re

from .config import HardwareProfile

# Keys, die in der Kopie nichts verloren haben (Host-/Laufzeitzustand)
_DROP_KEYS = {
    "sched.swap.derivedname",
    "checkpoint.vmstate",
    "checkpoint.vmstate.readonly",
    "vc.uuid",
    "migrate.hostlog",
}

_LINE_RE = re.compile(r'^\s*([^=#\s]+)\s*=\s*"(.*)"\s*$')

# Disk-Controller-Prefixe, deren .fileName eine VMDK referenziert
_DISK_BUS_RE = re.compile(r"^(scsi|sata|ide|nvme)\d+:\d+\.filename$", re.IGNORECASE)


def parse_vmx(text: str) -> dict[str, str]:
    """VMX-Text -> geordnetes dict. Original-Schreibweise der Keys bleibt erhalten."""
    entries: dict[str, str] = {}
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if m:
            entries[m.group(1)] = m.group(2)
    return entries


def serialize_vmx(entries: dict[str, str]) -> str:
    return "\n".join(f'{k} = "{v}"' for k, v in entries.items()) + "\n"


def _get_ci(entries: dict[str, str], key_lower: str) -> str | None:
    for k, v in entries.items():
        if k.lower() == key_lower:
            return v
    return None


def _set_ci(entries: dict[str, str], key: str, value: str) -> None:
    """Setzt einen Key case-insensitiv (behält vorhandene Schreibweise bei)."""
    for k in entries:
        if k.lower() == key.lower():
            entries[k] = value
            return
    entries[key] = value


def _del_ci(entries: dict[str, str], key_lower: str) -> None:
    for k in list(entries):
        if k.lower() == key_lower:
            del entries[k]


def list_disk_files(entries: dict[str, str]) -> dict[str, str]:
    """Alle Disk-Referenzen der VMX: {vmx_key: vmdk_dateiname}.

    Nur echte VMDKs (CD-ROM-Images etc. werden übersprungen): das zugehörige
    deviceType-Feld darf kein cdrom sein.
    """
    disks: dict[str, str] = {}
    for k, v in entries.items():
        if not _DISK_BUS_RE.match(k.lower()):
            continue
        if not v.lower().endswith(".vmdk"):
            continue
        dev_key = k.lower().rsplit(".", 1)[0] + ".devicetype"
        dev_type = (_get_ci(entries, dev_key) or "").lower()
        if "cdrom" in dev_type:
            continue
        present_key = k.lower().rsplit(".", 1)[0] + ".present"
        present = (_get_ci(entries, present_key) or "true").lower()
        if present == "false":
            continue
        disks[k] = v
    return disks


def rewrite_vmx(
    entries: dict[str, str],
    new_name: str,
    disk_map: dict[str, str],
    hardware: HardwareProfile,
) -> dict[str, str]:
    """Erzeugt die VMX für die Ziel-VM (verändert das übergebene dict nicht)."""
    out = dict(entries)

    _set_ci(out, "displayName", new_name)

    # Laufzeit-/hostspezifische Keys raus
    for k in list(out):
        if k.lower() in _DROP_KEYS:
            del out[k]

    # Changed Block Tracking deaktivieren (-ctk.vmdk-Dateien werden nicht mitkopiert)
    for k in list(out):
        if k.lower() == "ctkenabled" or k.lower().endswith(".ctkenabled"):
            out[k] = "FALSE"

    # Disk-Referenzen: Delta -> Basis (bzw. neuer Dateiname laut Map)
    for k, v in list_disk_files(out).items():
        if v in disk_map:
            out[k] = disk_map[v]

    # Hardware-Overrides
    if hardware.cpu_count:
        _set_ci(out, "numvcpus", str(hardware.cpu_count))
        # coresPerSocket darf die neue Kernanzahl nicht überschreiten
        cps = _get_ci(out, "cpuid.corespersocket")
        if cps and int(cps) > hardware.cpu_count:
            _set_ci(out, "cpuid.coresPerSocket", str(hardware.cpu_count))
    if hardware.memory_mb:
        _set_ci(out, "memSize", str(hardware.memory_mb))

    # Netzwerk: Portgruppen mappen, MACs neu generieren lassen
    for k in list(out):
        m = re.match(r"^(ethernet\d+)\.networkname$", k.lower())
        if m:
            eth = m.group(1)
            src_net = out[k]
            if src_net in hardware.network_mapping:
                out[k] = hardware.network_mapping[src_net]
            elif hardware.default_network:
                out[k] = hardware.default_network
            if hardware.generate_new_mac:
                _set_ci(out, f"{eth}.addressType", "generated")
                _del_ci(out, f"{eth}.generatedaddress")
                _del_ci(out, f"{eth}.generatedaddressoffset")
                _del_ci(out, f"{eth}.address")

    # UUID/Identität: "create" beantwortet die "moved or copied?"-Frage automatisch
    if hardware.keep_uuid:
        _set_ci(out, "uuid.action", "keep")
    else:
        _set_ci(out, "uuid.action", "create")
        _del_ci(out, "uuid.bios")
        _del_ci(out, "uuid.location")

    # CD-ROM-Images: nicht mitverbunden starten (ISO existiert auf dem Ziel meist nicht)
    for k in list(out):
        if k.lower().endswith(".devicetype") and "cdrom-image" in out[k].lower():
            prefix = k.rsplit(".", 1)[0]
            _set_ci(out, f"{prefix}.startConnected", "FALSE")

    # Swap-Datei neu ableiten lassen
    _del_ci(out, "sched.swap.derivedname")

    return out


# --- Disk-Deskriptor (VMDK) ---------------------------------------------------

_PARENT_HINT_RE = re.compile(r'^\s*parentFileNameHint\s*=\s*"(.*)"\s*$', re.IGNORECASE | re.MULTILINE)
_EXTENT_RE = re.compile(
    r'^\s*(RW|RDONLY|NOACCESS)\s+\d+\s+(\w+)\s+"([^"]+)"', re.MULTILINE
)


def descriptor_parent(descriptor_text: str) -> str | None:
    """parentFileNameHint eines Delta-Deskriptors (None bei Basis-Disk)."""
    m = _PARENT_HINT_RE.search(descriptor_text)
    return m.group(1) if m else None


def descriptor_extents(descriptor_text: str) -> list[str]:
    """Dateinamen der Extents (z.B. xxx-flat.vmdk, xxx-delta.vmdk)."""
    return [m.group(3) for m in _EXTENT_RE.finditer(descriptor_text)]


def set_descriptor_parent(descriptor_text: str, new_parent: str) -> str:
    """Ersetzt den parentFileNameHint (z.B. um Pfade auf Dateinamen zu kürzen)."""
    return _PARENT_HINT_RE.sub(f'parentFileNameHint="{new_parent}"', descriptor_text)


def is_descriptor(first_bytes: bytes) -> bool:
    """True, wenn die Datei ein Text-Deskriptor ist (und kein Binär-Extent)."""
    head = first_bytes[:1024]
    return b"# Disk DescriptorFile" in head or b"version=" in head[:64]
