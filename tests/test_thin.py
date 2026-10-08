"""Thin-Export per SSH: Pipeline mit gemockten Hosts und einem Fake-vmkfstools."""

from __future__ import annotations

import threading

import pytest
from test_job import BASE_DESCRIPTOR, env, make_pipeline  # noqa: F401  (env = Fixture)

from esxi_backupper.core.esxi_client import Cancelled, EsxiError
from esxi_backupper.core.ssh_client import EsxiSshClient, SshError


class FakeSsh:
    """Bildet ensure_connected/vmkfstools/ls/du/rm auf In-Memory-Datastores ab."""

    def __init__(self, address, stores, allocated=1000, thin_flat_size=4096):
        self.address = address
        self.stores = stores
        self.allocated = allocated
        self.thin_flat_size = thin_flat_size
        self.fail_format = None
        self.cancel_on_format = None
        self.clones: list[tuple[str, str, str]] = []
        self.calls: list[str] = []

    @staticmethod
    def _split(abs_path):
        parts = abs_path.split("/", 4)          # '', vmfs, volumes, ds, rel
        return parts[3], parts[4]

    def ensure_connected(self):
        self.calls.append("connect")

    def has_command(self, name):
        return True

    def allocated_bytes(self, paths):
        return self.allocated

    def list_dir(self, path):
        ds, rel = self._split(path)
        prefix = rel.rstrip("/") + "/"
        return sorted(k[len(prefix):] for k in self.stores[ds] if k.startswith(prefix))

    def make_directory(self, ds, folder):
        self.calls.append(f"mkdir {ds}/{folder}")
        self.stores.setdefault(ds, {})

    def delete_path(self, ds, folder):
        self.calls.append(f"rmdir {ds}/{folder}")
        for k in [k for k in self.stores.get(ds, {}) if k.startswith(folder + "/")]:
            del self.stores[ds][k]

    def delete_file(self, abs_path):
        ds, rel = self._split(abs_path)
        self.stores[ds].pop(rel, None)

    def clone_disk(self, src_abs, dst_abs, disk_format, progress=None, cancel=None):
        self.clones.append((src_abs, dst_abs, disk_format))
        if self.fail_format == disk_format:
            raise SshError("vmkfstools fehlgeschlagen (rc=1): no space")
        if self.cancel_on_format == disk_format:
            cancel.set()
            raise Cancelled("Backup abgebrochen.")
        dds, drel = self._split(dst_abs)
        stem = drel[:-5]
        if disk_format == "2gbsparse":
            self.stores[dds][drel] = b"# Disk DescriptorFile\ncreateType=\"twoGbMaxExtentSparse\"\n"
            self.stores[dds][f"{stem}-s001.vmdk"] = b"S" * 64
        else:
            self.stores[dds][drel] = BASE_DESCRIPTOR.encode()
            self.stores[dds][f"{stem}-flat.vmdk"] = b"T" * self.thin_flat_size
        if progress:
            progress(100)


def pipeline_with_ssh(env, mode="auto", **ssh_kw):
    source, target, job = env
    job.transfer_mode = mode
    ssh_s = FakeSsh("src.local", source.files, **{k: v for k, v in ssh_kw.items() if k == "allocated"})
    ssh_t = FakeSsh("dst.local", target.files, **{k: v for k, v in ssh_kw.items() if k == "thin_flat_size"})
    p, logs = make_pipeline(source, target, job, source_ssh=ssh_s, target_ssh=ssh_t)
    return p, logs, ssh_s, ssh_t, source, target


def test_thin_export_transfers_only_sparse_files(env):
    p, logs, ssh_s, ssh_t, source, target = pipeline_with_ssh(env)
    name = p.run()
    tfiles = target.files["backup_ds"]

    # Disk kommt aus dem Thin-Import (T), nicht aus der vollen Kopie (X)
    assert tfiles[f"{name}/srv01-flat.vmdk"] == b"T" * 4096
    assert f"{name}/srv01.vmdk" in tfiles
    assert [c[2] for c in ssh_s.clones] == ["2gbsparse"]
    assert [c[2] for c in ssh_t.clones] == ["thin"]
    # NVRAM und VMX laufen weiter den normalen Weg
    assert f"{name}/srv01.nvram" in tfiles
    assert f'scsi0:0.fileName = "srv01.vmdk"' in tfiles[f"{name}/{name}.vmx"].decode()
    # nichts Temporäres bleibt zurück
    assert not any("_backupper_tmp" in k for k in source.files["ds1"])
    assert not any("_import" in k for k in tfiles)
    assert target.registered and source.snapshots["srv01"] == []
    assert any("Thin-Export" in m for m in logs)


@pytest.mark.parametrize("kw, expect_log", [
    ({"allocated": 4000}, "keinen Vorteil"),           # Disk fast voll
])
def test_full_transfer_when_disks_are_full(env, kw, expect_log):
    p, logs, ssh_s, ssh_t, source, target = pipeline_with_ssh(env, **kw)
    name = p.run()
    assert target.files["backup_ds"][f"{name}/srv01-flat.vmdk"] == b"X" * 4096
    assert ssh_s.clones == [] and ssh_t.clones == []
    assert any(expect_log in m for m in logs)


def test_full_transfer_when_target_has_too_little_space(env):
    source, target, job = env
    target.free_space = 10
    p, logs, ssh_s, ssh_t, source, target = pipeline_with_ssh(env)
    name = p.run()
    assert target.files["backup_ds"][f"{name}/srv01-flat.vmdk"] == b"X" * 4096
    assert ssh_s.clones == []
    assert any("zu wenig freier Platz" in m for m in logs)


def test_mode_full_never_touches_ssh(env):
    p, logs, ssh_s, ssh_t, source, target = pipeline_with_ssh(env, mode="full")
    name = p.run()
    assert target.files["backup_ds"][f"{name}/srv01-flat.vmdk"] == b"X" * 4096
    assert ssh_s.calls == [] and ssh_t.calls == []


def test_no_ssh_configured_falls_back(env):
    source, target, job = env
    p, logs = make_pipeline(source, target, job)    # ohne SSH
    name = p.run()
    assert target.files["backup_ds"][f"{name}/srv01-flat.vmdk"] == b"X" * 4096
    assert any("SSH ist nicht für beide Hosts konfiguriert" in m for m in logs)


def test_import_failure_falls_back_and_cleans_up(env):
    p, logs, ssh_s, ssh_t, source, target = pipeline_with_ssh(env)
    ssh_t.fail_format = "thin"
    name = p.run()
    tfiles = target.files["backup_ds"]
    assert tfiles[f"{name}/srv01-flat.vmdk"] == b"X" * 4096     # volle Kopie
    assert not any("_import" in k for k in tfiles)
    assert not any("_backupper_tmp" in k for k in source.files["ds1"])
    assert any("Thin-Export fehlgeschlagen" in m for m in logs)
    assert target.registered


def test_size_mismatch_falls_back(env):
    p, logs, ssh_s, ssh_t, source, target = pipeline_with_ssh(env, thin_flat_size=100)
    name = p.run()
    assert target.files["backup_ds"][f"{name}/srv01-flat.vmdk"] == b"X" * 4096
    assert any("Größenprüfung fehlgeschlagen" in m for m in logs)


def test_cancel_during_export_aborts_and_cleans(env):
    source, target, job = env
    job.transfer_mode = "auto"
    cancel = threading.Event()
    ssh_s = FakeSsh("src.local", source.files)
    ssh_t = FakeSsh("dst.local", target.files)
    ssh_s.cancel_on_format = "2gbsparse"
    from esxi_backupper.core.job import BackupPipeline
    p = BackupPipeline(job, source, target, source_ssh=ssh_s, target_ssh=ssh_t,
                       log=lambda m: None, cancel=cancel)
    with pytest.raises(Cancelled):
        p.run()
    assert source.snapshots["srv01"] == []
    assert not any("_backupper_tmp" in k for k in source.files["ds1"])
    assert target.registered == []
    assert target.files["backup_ds"] == {}      # Zielordner entfernt


# --- SSH-Helfer (ohne echte Verbindung) ---------------------------------------

class ScriptedSsh(EsxiSshClient):
    def __init__(self, outputs):
        super().__init__("h", "root", "pw")
        self.outputs = outputs
        self.commands = []

    def ensure_connected(self):
        pass

    def _exec(self, command, timeout=600, on_output=None, cancel=None, pty=False):
        self.commands.append(command)
        rc, out = self.outputs(command)
        if on_output:
            on_output(out)
        return rc, out


def test_allocated_bytes_sums_du_output():
    ssh = ScriptedSsh(lambda c: (0, "1024\t/vmfs/volumes/a/x-flat.vmdk\n2048\t/vmfs/volumes/a/y-flat.vmdk\n"))
    assert ssh.allocated_bytes(["/vmfs/volumes/a/x-flat.vmdk", "/vmfs/volumes/a/y-flat.vmdk"]) == 3072 * 1024


def test_allocated_bytes_rejects_unexpected_output():
    ssh = ScriptedSsh(lambda c: (0, "du: no such file\n"))
    with pytest.raises(SshError):
        ssh.allocated_bytes(["/vmfs/volumes/a/x"])


def test_clone_disk_reports_progress_and_quotes_paths():
    out = "Clone: 10% done.\rClone: 55% done.\rClone: 100% done.\n"
    ssh = ScriptedSsh(lambda c: (0, out))
    seen = []
    ssh.clone_disk("/vmfs/volumes/my ds/a b.vmdk", "/vmfs/volumes/ds/t/d1.vmdk",
                   "2gbsparse", progress=seen.append)
    assert seen == [10, 55, 100]
    assert "'/vmfs/volumes/my ds/a b.vmdk'" in ssh.commands[0]
    assert "-d 2gbsparse" in ssh.commands[0]


def test_clone_disk_failure_raises_with_output_tail():
    ssh = ScriptedSsh(lambda c: (1, "Failed to clone disk: No space left on device (1).\n"))
    with pytest.raises(SshError, match="No space left"):
        ssh.clone_disk("/vmfs/volumes/a/x.vmdk", "/vmfs/volumes/a/y.vmdk", "thin")


def test_clone_disk_rejects_unknown_format():
    ssh = ScriptedSsh(lambda c: (0, ""))
    with pytest.raises(SshError):
        ssh.clone_disk("/a", "/b", "zeroedthick; rm -rf /")
    assert ssh.commands == []


@pytest.mark.parametrize("path", [
    "/etc/passwd", "/vmfs/volumes/ds/../../etc/x", "/vmfs/volumes/ds/*.vmdk",
    "/vmfs/volumes/ds/a?b",
])
def test_delete_file_refuses_unsafe_paths(path):
    ssh = ScriptedSsh(lambda c: (0, ""))
    with pytest.raises(SshError, match="unsicher"):
        ssh.delete_file(path)
    assert ssh.commands == []


def test_has_command_checks_path_and_fixed_locations():
    ssh = ScriptedSsh(lambda c: (0, ""))
    assert ssh.has_command("vmkfstools")
    assert "command -v vmkfstools" in ssh.commands[0] and "/sbin/vmkfstools" in ssh.commands[0]
    assert not ScriptedSsh(lambda c: (1, "")).has_command("vmkfstools")
