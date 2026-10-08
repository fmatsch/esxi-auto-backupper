"""Pipeline-Tests mit gemockten ESXi-Clients (kein echter Host nötig).

Der FakeEsxiClient bildet zwei Hosts mit In-Memory-"Datastores" ab und
simuliert das ESXi-Verhalten beim Snapshot (VMX zeigt danach auf die Delta-Disk).
"""

from __future__ import annotations

import threading

import pytest

from esxi_backupper.core.config import BackupJob, HardwareProfile, HostConfig
from esxi_backupper.core.esxi_client import ApiReadOnlyError, DatastoreInfo, EsxiError
from esxi_backupper.core.job import BackupPipeline, sanitize_name


class FakeVm:
    """Nachbildung der pyVmomi-VM-Attribute, die die Pipeline nutzt."""

    def __init__(self, name, vmx_path, tools_running=True):
        self.config = type("Cfg", (), {})()
        self.config.name = name
        self.config.files = type("F", (), {})()
        self.config.files.vmPathName = vmx_path
        self.guest = type("G", (), {})()
        self.guest.toolsRunningStatus = (
            "guestToolsRunning" if tools_running else "guestToolsNotRunning"
        )
        self.runtime = type("R", (), {})()
        self.runtime.powerState = "poweredOn"


class FakeVmInfo:
    def __init__(self, name, power_state, vmx_path):
        self.name = name
        self.power_state = power_state
        self.vmx_path = vmx_path


VMX_BEFORE_SNAPSHOT = '''\
.encoding = "UTF-8"
displayName = "srv01"
numvcpus = "2"
memSize = "4096"
scsi0:0.present = "TRUE"
scsi0:0.fileName = "srv01.vmdk"
scsi0:0.deviceType = "scsi-hardDisk"
ethernet0.present = "TRUE"
ethernet0.networkName = "VM Network"
nvram = "srv01.nvram"
'''

BASE_DESCRIPTOR = '''# Disk DescriptorFile
version=1
createType="vmfs"
# Extent description
RW 2048 VMFS "srv01-flat.vmdk"
'''

DELTA_DESCRIPTOR = '''# Disk DescriptorFile
version=1
createType="seSparse"
parentFileNameHint="srv01.vmdk"
# Extent description
RW 2048 SESPARSE "srv01-000001-sesparse.vmdk"
'''


class FakeEsxiClient:
    def __init__(self, address, datastores=None, api_writable=True):
        self.address = address
        self.api_writable = api_writable
        # {ds: {rel_path: bytes}}
        self.files: dict[str, dict[str, bytes]] = datastores or {}
        self.vms: dict[str, FakeVm] = {}
        self.registered: list[tuple[str, str]] = []  # (vmx_path, name)
        self.snapshots: dict[str, list[str]] = {}    # vm_name -> [snap_names]
        self.destroyed: list[str] = []
        self.vm_infos: list[FakeVmInfo] = []
        self.free_space = 10 ** 13

    def _check_writable(self):
        if not self.api_writable:
            raise ApiReadOnlyError("read-only")

    # Inventar
    def list_datastores(self):
        return [DatastoreInfo(ds, 10 ** 13, self.free_space, "VMFS") for ds in self.files]

    def get_vm(self, name):
        if name not in self.vms:
            raise EsxiError(f"VM '{name}' nicht gefunden.")
        return self.vms[name]

    def vm_path(self, vm):
        return vm.config.files.vmPathName

    def vm_tools_running(self, vm):
        return vm.guest.toolsRunningStatus == "guestToolsRunning"

    def list_vms(self):
        return self.vm_infos

    # Snapshots
    def find_snapshot(self, vm, name):
        return name if name in self.snapshots.get(vm.config.name, []) else None

    def create_snapshot(self, vm, name, quiesce):
        self._check_writable()
        vm_name = vm.config.name
        self.snapshots.setdefault(vm_name, []).append(name)
        # ESXi-Verhalten simulieren: VMX zeigt jetzt auf die Delta-Disk
        ds, rel = vm.config.files.vmPathName[1:].split("] ")
        folder = rel.rsplit("/", 1)[0]
        vmx = self.files[ds][rel].decode()
        vmx = vmx.replace('scsi0:0.fileName = "srv01.vmdk"',
                          'scsi0:0.fileName = "srv01-000001.vmdk"')
        self.files[ds][rel] = vmx.encode()
        self.files[ds][f"{folder}/srv01-000001.vmdk"] = DELTA_DESCRIPTOR.encode()
        self.files[ds][f"{folder}/srv01-000001-sesparse.vmdk"] = b"D" * 512

    def remove_snapshot(self, vm, name):
        self._check_writable()
        snaps = self.snapshots.get(vm.config.name, [])
        if name in snaps:
            snaps.remove(name)
            return True
        return False

    # VM-Verwaltung
    def register_vm(self, vmx_datastore_path, name):
        self._check_writable()
        self.registered.append((vmx_datastore_path, name))
        self.vm_infos.append(FakeVmInfo(name, "poweredOff", vmx_datastore_path))

    def destroy_vm(self, vm):
        self._check_writable()
        self.destroyed.append(vm.config.name)
        self.vm_infos = [i for i in self.vm_infos if i.name != vm.config.name]

    def make_directory(self, ds, folder):
        self._check_writable()
        self.files.setdefault(ds, {})

    def delete_datastore_path(self, ds, folder):
        self._check_writable()
        prefix = folder + "/"
        for k in list(self.files.get(ds, {})):
            if k.startswith(prefix):
                del self.files[ds][k]

    # Datastore-HTTP (funktioniert auch "read-only")
    def file_size(self, ds, rel):
        try:
            return len(self.files[ds][rel])
        except KeyError:
            raise EsxiError(f"Datei nicht gefunden: [{ds}] {rel}")

    def download_text(self, ds, rel):
        return self.files[ds][rel].decode()

    def open_download(self, ds, rel):
        data = self.files[ds][rel]

        class R:
            def iter_content(self, chunk_size):
                yield data

            def close(self):
                pass

        return R()

    def upload_stream(self, ds, rel, data, content_length=None):
        self.files.setdefault(ds, {})[rel] = b"".join(data)

    def upload_text(self, ds, rel, text):
        self.files.setdefault(ds, {})[rel] = text.encode()


@pytest.fixture
def env():
    source = FakeEsxiClient("src.local", {
        "ds1": {
            "srv01/srv01.vmx": VMX_BEFORE_SNAPSHOT.encode(),
            "srv01/srv01.vmdk": BASE_DESCRIPTOR.encode(),
            "srv01/srv01-flat.vmdk": b"X" * 4096,
            "srv01/srv01.nvram": b"N" * 128,
        }
    })
    source.vms["srv01"] = FakeVm("srv01", "[ds1] srv01/srv01.vmx")
    target = FakeEsxiClient("dst.local", {"backup_ds": {}})
    job = BackupJob(name="Job1", source_vm="srv01", target_datastore="backup_ds",
                    retention_count=2)
    return source, target, job


def make_pipeline(source, target, job, **kw):
    logs = []
    p = BackupPipeline(job, source, target, log=logs.append, **kw)
    return p, logs


def test_successful_backup(env):
    source, target, job = env
    p, logs = make_pipeline(source, target, job)
    name = p.run()

    assert name.startswith("srv01_backup_")
    # Zieldateien: Basis-Disk + flat + nvram + vmx (Delta wird NICHT kopiert)
    tfiles = target.files["backup_ds"]
    folder = name
    assert f"{folder}/srv01.vmdk" in tfiles
    assert f"{folder}/srv01-flat.vmdk" in tfiles
    assert tfiles[f"{folder}/srv01-flat.vmdk"] == b"X" * 4096
    assert f"{folder}/srv01.nvram" in tfiles
    assert f"{folder}/{name}.vmx" in tfiles
    assert not any("sesparse" in f for f in tfiles)
    # VMX zeigt auf die Basis-Disk, nicht auf das Delta
    vmx = tfiles[f"{folder}/{name}.vmx"].decode()
    assert 'scsi0:0.fileName = "srv01.vmdk"' in vmx
    assert f'displayName = "{name}"' in vmx
    # registriert, Snapshot wieder weg
    assert target.registered == [(f"[backup_ds] {folder}/{name}.vmx", name)]
    assert source.snapshots["srv01"] == []


def test_hardware_overrides_applied(env):
    source, target, job = env
    job.hardware = HardwareProfile(cpu_count=8, memory_mb=16384,
                                   network_mapping={"VM Network": "Backup-LAN"})
    p, _ = make_pipeline(source, target, job)
    name = p.run()
    vmx = target.files["backup_ds"][f"{name}/{name}.vmx"].decode()
    assert 'numvcpus = "8"' in vmx
    assert 'memSize = "16384"' in vmx
    assert 'ethernet0.networkName = "Backup-LAN"' in vmx
    assert 'uuid.action = "create"' in vmx


def test_cleanup_on_transfer_failure(env):
    source, target, job = env

    def broken_upload(ds, rel, data, content_length=None):
        raise EsxiError("Netzwerkfehler beim Upload")

    target.upload_stream = broken_upload
    p, logs = make_pipeline(source, target, job)
    with pytest.raises(EsxiError):
        p.run()
    # Snapshot wurde aufgeräumt, nichts registriert
    assert source.snapshots["srv01"] == []
    assert target.registered == []


def test_stale_snapshot_removed_before_run(env):
    source, target, job = env
    p, logs = make_pipeline(source, target, job)
    source.snapshots["srv01"] = [p.snapshot_name]  # Rest vom letzten Absturz
    p.run()
    assert source.snapshots["srv01"] == []


def test_retention_deletes_oldest(env):
    source, target, job = env
    job.retention_count = 2
    target.vm_infos = [
        FakeVmInfo("srv01_backup_20260101_000000", "poweredOff",
                   "[backup_ds] srv01_backup_20260101_000000/x.vmx"),
        FakeVmInfo("srv01_backup_20260201_000000", "poweredOff",
                   "[backup_ds] srv01_backup_20260201_000000/x.vmx"),
        FakeVmInfo("srv01_backup_20260301_000000", "poweredOff",
                   "[backup_ds] srv01_backup_20260301_000000/x.vmx"),
    ]
    for info in target.vm_infos:
        target.vms[info.name] = FakeVm(info.name, info.vmx_path)
    p, _ = make_pipeline(source, target, job)
    p.run()
    # 3 alte + 1 neues = 4, behalten werden 2 -> die 2 ältesten fliegen raus
    assert target.destroyed == ["srv01_backup_20260101_000000",
                                "srv01_backup_20260201_000000"]


def test_retention_never_deletes_running_vm(env):
    source, target, job = env
    job.retention_count = 1
    target.vm_infos = [
        FakeVmInfo("srv01_backup_20260101_000000", "poweredOn",
                   "[backup_ds] a/x.vmx"),
    ]
    p, _ = make_pipeline(source, target, job)
    p.run()
    assert target.destroyed == []


def test_free_license_requires_ssh_fallback(env):
    source, target, job = env
    source.api_writable = False
    p, _ = make_pipeline(source, target, job)
    with pytest.raises(EsxiError, match="SSH"):
        p.run()
    # kein halbfertiger Zustand auf dem Ziel
    assert target.registered == []


def test_sanitize_name():
    assert sanitize_name("Mein Server 1") == "Mein_Server_1"
    assert sanitize_name('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
