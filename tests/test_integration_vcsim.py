"""Integrationstest gegen vcsim (govmomi-Simulator) im ESXi-Modus.

Wird automatisch übersprungen, wenn vcsim nicht installiert ist.
Prüft die echte pyVmomi-Kommunikation der EsxiClient-Klasse:
Verbindung, Inventar, Snapshots und Datastore-Datei-Zugriff.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time

import pytest

from esxi_backupper.core.esxi_client import EsxiClient

VCSIM = shutil.which("vcsim")

pytestmark = pytest.mark.skipif(VCSIM is None, reason="vcsim nicht installiert")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def vcsim_client():
    port = _free_port()
    proc = subprocess.Popen(
        [VCSIM, "-esx", "-l", f"127.0.0.1:{port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 15
        client = None
        last_err = None
        while time.time() < deadline:
            try:
                client = EsxiClient("127.0.0.1", "user", "pass", port=port)
                client.connect()
                break
            except Exception as e:
                last_err = e
                client = None
                time.sleep(0.3)
        if client is None:
            raise RuntimeError(f"vcsim nicht erreichbar: {last_err}")
        yield client
        client.disconnect()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_inventory(vcsim_client):
    vms = vcsim_client.list_vms()
    assert vms, "vcsim -esx sollte mindestens eine VM simulieren"
    assert vms[0].vmx_path.startswith("[")
    datastores = vcsim_client.list_datastores()
    assert datastores
    assert datastores[0].capacity > 0
    assert vcsim_client.list_networks()


def test_snapshot_roundtrip(vcsim_client):
    vms = vcsim_client.list_vms()
    vm = vcsim_client.get_vm(vms[0].name)
    vcsim_client.create_snapshot(vm, "AutoBackup_test", quiesce=False)
    vm = vcsim_client.get_vm(vms[0].name)
    assert vcsim_client.find_snapshot(vm, "AutoBackup_test") is not None
    assert vcsim_client.remove_snapshot(vm, "AutoBackup_test")
    vm = vcsim_client.get_vm(vms[0].name)
    assert vcsim_client.find_snapshot(vm, "AutoBackup_test") is None


def test_large_file_streaming_copy(vcsim_client):
    """24 MB über copy_datastore_file (echter HTTP-GET -> PUT-Stream, Größenprüfung)."""
    from esxi_backupper.core.transfer import copy_datastore_file

    ds = vcsim_client.list_datastores()[0].name
    payload = bytes(range(256)) * (24 * 1024 * 4)  # 24 MiB
    vcsim_client.upload_stream(ds, "backupper_test/big.bin", iter([payload]), len(payload))

    seen = []
    copied = copy_datastore_file(
        vcsim_client, ds, "backupper_test/big.bin",
        vcsim_client, ds, "backupper_test/big_copy.bin",
        progress=seen.append,
    )
    assert copied == len(payload)
    assert vcsim_client.file_size(ds, "backupper_test/big_copy.bin") == len(payload)
    assert seen and seen[-1].bytes_done == len(payload)


def test_datastore_file_roundtrip(vcsim_client):
    ds = vcsim_client.list_datastores()[0].name
    vcsim_client.upload_text(ds, "backupper_test/probe.txt", "hallo welt")
    assert vcsim_client.download_text(ds, "backupper_test/probe.txt") == "hallo welt"
    assert vcsim_client.file_size(ds, "backupper_test/probe.txt") == len(b"hallo welt")
