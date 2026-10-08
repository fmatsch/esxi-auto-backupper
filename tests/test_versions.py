from datetime import datetime

import pytest
from test_job import FakeVm, FakeVmInfo, env, make_pipeline  # noqa: F401

from esxi_backupper.core import versions
from esxi_backupper.core.config import BackupJob
from esxi_backupper.core.esxi_client import EsxiError


def make_job(**kw):
    return BackupJob(name="J", source_vm="srv01", **kw)


def test_version_time_requires_exact_timestamp():
    prefix = "srv01_backup_"
    assert versions.version_time("srv01_backup_20261008_220000", prefix) == datetime(2026, 10, 8, 22, 0, 0)
    assert versions.version_time("srv01_backup_manual", prefix) is None
    assert versions.version_time("srv01_backup_20261008_220000_old", prefix) is None
    assert versions.version_time("other_backup_20261008_220000", prefix) is None
    assert versions.version_time("srv01_backup_20269999_999999", prefix) is None


def test_prefix_uses_folder_override_and_sanitizes():
    assert versions.backup_prefix(make_job()) == "srv01_backup_"
    assert versions.backup_prefix(make_job(target_vm_folder="Mein Server")) == "Mein_Server_backup_"


def test_list_versions_newest_first_and_ignores_foreign_vms(env):
    _, target, job = env
    names = ["srv01_backup_20260101_000000", "srv01_backup_20260301_000000",
             "srv01_backup_20260201_000000", "srv01_backup_notes", "srv02_backup_20260301_000000"]
    target.vm_infos = [FakeVmInfo(n, "poweredOff", f"[backup_ds] {n}/x.vmx") for n in names]
    found = versions.list_versions(target, job)
    assert [i.name for _, i in found] == [
        "srv01_backup_20260301_000000", "srv01_backup_20260201_000000",
        "srv01_backup_20260101_000000"]


def test_delete_version_refuses_running_vm(env):
    _, target, _ = env
    info = FakeVmInfo("srv01_backup_20260101_000000", "poweredOn", "[backup_ds] a/x.vmx")
    with pytest.raises(EsxiError, match="läuft"):
        versions.delete_version(target, None, info)
    assert target.destroyed == []


def test_delete_version_falls_back_to_ssh_on_free_license(env):
    _, target, _ = env
    target.api_writable = False
    info = FakeVmInfo("srv01_backup_20260101_000000", "poweredOff",
                      "[backup_ds] srv01_backup_20260101_000000/x.vmx")
    target.vms[info.name] = FakeVm(info.name, info.vmx_path)

    class Ssh:
        calls = []
        def unregister_vm(self, name): self.calls.append(("unregister", name))
        def delete_path(self, ds, folder): self.calls.append(("rm", ds, folder))

    ssh = Ssh()
    versions.delete_version(target, ssh, info)
    assert ssh.calls == [("unregister", info.name),
                         ("rm", "backup_ds", "srv01_backup_20260101_000000")]


def test_four_versions_are_kept(env):
    """'Von dieser VM 4 Versionen aufheben': 5 alte + 1 neue -> die 2 ältesten gehen."""
    source, target, job = env
    job.retention_count = 4
    old = [f"srv01_backup_2026010{d}_000000" for d in range(1, 6)]
    target.vm_infos = [FakeVmInfo(n, "poweredOff", f"[backup_ds] {n}/x.vmx") for n in old]
    for i in target.vm_infos:
        target.vms[i.name] = FakeVm(i.name, i.vmx_path)
    p, _ = make_pipeline(source, target, job)
    new = p.run()
    assert target.destroyed == ["srv01_backup_20260101_000000", "srv01_backup_20260102_000000"]
    remaining = [i.name for _, i in versions.list_versions(target, job)]
    assert len(remaining) == 4 and remaining[0] == new


def test_retention_ignores_vms_that_only_look_similar(env):
    source, target, job = env
    job.retention_count = 1
    target.vm_infos = [FakeVmInfo("srv01_backup_manual", "poweredOff", "[backup_ds] m/x.vmx")]
    target.vms["srv01_backup_manual"] = FakeVm("srv01_backup_manual", "[backup_ds] m/x.vmx")
    p, _ = make_pipeline(source, target, job)
    p.run()
    assert "srv01_backup_manual" not in target.destroyed


def test_retention_delete_failure_is_only_a_warning(env):
    source, target, job = env
    job.retention_count = 1
    old = "srv01_backup_20260101_000000"
    target.vm_infos = [FakeVmInfo(old, "poweredOff", f"[backup_ds] {old}/x.vmx")]
    target.vms[old] = FakeVm(old, f"[backup_ds] {old}/x.vmx")

    def boom(vm):
        raise EsxiError("Datei gesperrt")
    target.destroy_vm = boom
    p, logs = make_pipeline(source, target, job)
    name = p.run()                          # darf nicht scheitern: Backup ist fertig
    assert name.startswith("srv01_backup_")
    assert any("konnte nicht entfernt werden" in m for m in logs)
