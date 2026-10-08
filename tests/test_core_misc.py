import pytest

from esxi_backupper.core import config as cfgmod
from esxi_backupper.core.esxi_client import EsxiError, _IterReader
from esxi_backupper.core.runner import JobAlreadyRunning, _store_status, job_lock
from esxi_backupper.core.ssh_client import EsxiSshClient, SshError


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return tmp_path


def test_iter_reader_streams_all_bytes_in_small_reads():
    chunks = [b"a" * 10, b"b" * 7, b"c" * 20]
    r = _IterReader(iter(chunks), 37)
    assert len(r) == 37
    out = b""
    while True:
        part = r.read(4)
        if not part:
            break
        assert len(part) <= 4
        out += part
    assert out == b"".join(chunks)


def test_iter_reader_read_all():
    r = _IterReader(iter([b"xy", b"z"]), 3)
    assert r.read() + r.read() == b"xyz"
    assert r.read(1) == b""


def test_job_lock_blocks_second_holder():
    with job_lock("abc"):
        with pytest.raises(JobAlreadyRunning):
            with job_lock("abc"):
                pass
        # anderer Job ist davon unberührt
        with job_lock("other"):
            pass
    # nach Freigabe wieder möglich
    with job_lock("abc"):
        pass


@pytest.mark.parametrize("folder", ["", "/", "..", "a/../..", "./", "x/./y/.."])
def test_ssh_delete_refuses_unsafe_paths(folder):
    ssh = EsxiSshClient("h", "root", "pw")
    with pytest.raises(SshError, match="unsicher"):
        ssh.delete_path("ds1", folder)


def test_ssh_delete_refuses_datastore_with_slash():
    ssh = EsxiSshClient("h", "root", "pw")
    with pytest.raises(SshError, match="unsicher"):
        ssh.delete_path("ds1/../..", "folder")


def test_store_status_only_touches_status_fields():
    cfg = cfgmod.AppConfig()
    job = cfgmod.BackupJob(name="J", source_vm="vm1")
    cfg.jobs.append(job)
    cfgmod.save_config(cfg)

    # Der Nutzer ändert den Job parallel in der GUI (neuer Name, neue Retention)
    gui_cfg = cfgmod.load_config()
    gui_cfg.jobs[0].name = "Umbenannt"
    gui_cfg.jobs[0].retention_count = 7
    cfgmod.save_config(gui_cfg)

    # Der (veraltete) Lauf meldet seinen Status
    job.last_status = "ok"
    job.last_run = "2026-10-08T10:00:00"
    _store_status(job)

    final = cfgmod.load_config().jobs[0]
    assert final.name == "Umbenannt"
    assert final.retention_count == 7
    assert final.last_status == "ok"
    assert final.last_run == "2026-10-08T10:00:00"


def test_config_roundtrip_ignores_unknown_keys(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"jobs": [{"id": "x", "name": "N", "disk_provisioning": "thin"}]}')
    cfg = cfgmod.load_config(p)
    assert cfg.jobs[0].name == "N"
