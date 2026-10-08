import pytest

from esxi_backupper.core.config import HardwareProfile
from esxi_backupper.core.vmx_editor import (
    descriptor_extents,
    descriptor_parent,
    is_descriptor,
    list_disk_files,
    parse_vmx,
    rewrite_vmx,
    serialize_vmx,
)

SAMPLE_VMX = '''\
.encoding = "UTF-8"
config.version = "8"
virtualHW.version = "19"
displayName = "Testserver"
numvcpus = "4"
cpuid.coresPerSocket = "4"
memSize = "8192"
scsi0.present = "TRUE"
scsi0:0.present = "TRUE"
scsi0:0.fileName = "Testserver-000001.vmdk"
scsi0:0.deviceType = "scsi-hardDisk"
scsi0:0.ctkEnabled = "TRUE"
ctkEnabled = "TRUE"
ide1:0.present = "TRUE"
ide1:0.deviceType = "cdrom-image"
ide1:0.fileName = "/vmfs/volumes/ds1/iso/debian.iso"
ide1:0.startConnected = "TRUE"
ethernet0.present = "TRUE"
ethernet0.networkName = "VM Network"
ethernet0.addressType = "generated"
ethernet0.generatedAddress = "00:0c:29:aa:bb:cc"
ethernet0.generatedAddressOffset = "0"
uuid.bios = "56 4d 11 22 33 44 55 66-77 88 99 aa bb cc dd ee"
uuid.location = "56 4d 11 22 33 44 55 66-77 88 99 aa bb cc dd ee"
sched.swap.derivedName = "/vmfs/volumes/abc/Testserver/Testserver-hash.vswp"
checkpoint.vmState = ""
vc.uuid = "52 aa bb cc dd ee ff 00-11 22 33 44 55 66 77 88"
'''


def test_parse_and_serialize_roundtrip():
    entries = parse_vmx(SAMPLE_VMX)
    assert entries["displayName"] == "Testserver"
    text = serialize_vmx(entries)
    assert parse_vmx(text) == entries


def test_list_disk_files_skips_cdrom():
    entries = parse_vmx(SAMPLE_VMX)
    disks = list_disk_files(entries)
    assert disks == {"scsi0:0.fileName": "Testserver-000001.vmdk"}


def test_rewrite_basic():
    entries = parse_vmx(SAMPLE_VMX)
    hw = HardwareProfile(cpu_count=2, memory_mb=4096,
                         network_mapping={"VM Network": "Backup Network"})
    out = rewrite_vmx(entries, "Testserver_backup_20260818",
                      {"Testserver-000001.vmdk": "Testserver.vmdk"}, hw)

    assert out["displayName"] == "Testserver_backup_20260818"
    assert out["scsi0:0.fileName"] == "Testserver.vmdk"
    assert out["numvcpus"] == "2"
    assert out["cpuid.coresPerSocket"] == "2"  # darf CPU-Zahl nicht überschreiten
    assert out["memSize"] == "4096"
    assert out["ethernet0.networkName"] == "Backup Network"
    # MAC neu generieren: alte generatedAddress weg
    assert "ethernet0.generatedAddress" not in out
    assert out["ethernet0.addressType"] == "generated"
    # neue Identität
    assert out["uuid.action"] == "create"
    assert "uuid.bios" not in out
    # Host-/Laufzeitzustand entfernt
    assert "sched.swap.derivedName" not in out
    assert "vc.uuid" not in out
    assert "checkpoint.vmState" not in out
    # CTK aus
    assert out["ctkEnabled"] == "FALSE"
    assert out["scsi0:0.ctkEnabled"] == "FALSE"
    # CD-ROM nicht verbunden
    assert out["ide1:0.startConnected"] == "FALSE"
    # Original unverändert
    assert entries["displayName"] == "Testserver"


def test_rewrite_keeps_source_values_without_overrides():
    entries = parse_vmx(SAMPLE_VMX)
    hw = HardwareProfile(generate_new_mac=False)
    out = rewrite_vmx(entries, "Kopie", {}, hw)
    assert out["numvcpus"] == "4"
    assert out["memSize"] == "8192"
    assert out["ethernet0.networkName"] == "VM Network"
    assert out["ethernet0.generatedAddress"] == "00:0c:29:aa:bb:cc"


def test_rewrite_default_network_fallback():
    entries = parse_vmx(SAMPLE_VMX)
    hw = HardwareProfile(default_network="LAN2")
    out = rewrite_vmx(entries, "Kopie", {}, hw)
    assert out["ethernet0.networkName"] == "LAN2"


def test_keep_uuid():
    entries = parse_vmx(SAMPLE_VMX)
    hw = HardwareProfile(keep_uuid=True)
    out = rewrite_vmx(entries, "Kopie", {}, hw)
    assert out["uuid.action"] == "keep"
    assert "uuid.bios" in out


DESCRIPTOR_DELTA = '''# Disk DescriptorFile
version=1
CID=fffffffe
parentCID=fffffffe
createType="vmfsSparse"
parentFileNameHint="Testserver.vmdk"
# Extent description
RW 41943040 VMFSSPARSE "Testserver-000001-delta.vmdk"
'''

DESCRIPTOR_BASE = '''# Disk DescriptorFile
version=1
CID=fffffffe
parentCID=ffffffff
createType="vmfs"
# Extent description
RW 41943040 VMFS "Testserver-flat.vmdk"
'''


def test_descriptor_parent_and_extents():
    assert descriptor_parent(DESCRIPTOR_DELTA) == "Testserver.vmdk"
    assert descriptor_parent(DESCRIPTOR_BASE) is None
    assert descriptor_extents(DESCRIPTOR_DELTA) == ["Testserver-000001-delta.vmdk"]
    assert descriptor_extents(DESCRIPTOR_BASE) == ["Testserver-flat.vmdk"]


def test_is_descriptor():
    assert is_descriptor(DESCRIPTOR_BASE.encode())
    assert not is_descriptor(b"\x4b\x44\x4d" + b"\x00" * 100)
