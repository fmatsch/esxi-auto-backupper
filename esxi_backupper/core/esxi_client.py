"""Verbindung zu einem einzelnen ESXi-Host (ohne vCenter) über die vSphere-API.

Schreiboperationen (Snapshot, Register, Löschen) schlagen auf Hosts mit
kostenloser Lizenz mit vim.fault.RestrictedVersion fehl -> ApiReadOnlyError.
Der Aufrufer (job.py) fällt dann auf den SSH-Weg zurück (ssh_client.py).

Datei-Transfers laufen über den HTTP-Datastore-Zugriff des Hosts
(https://host/folder/...), der auch mit Free-Lizenz funktioniert.
"""

from __future__ import annotations

import ssl
import urllib.parse
from dataclasses import dataclass
from typing import Iterator, Optional

import requests
import urllib3
from pyVim.connect import Disconnect, SmartConnect
from pyVim.task import WaitForTask
from pyVmomi import vim, vmodl

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DC_PATH = "ha-datacenter"  # fester Datacenter-Name auf Standalone-ESXi
CHUNK_SIZE = 4 * 1024 * 1024


class EsxiError(Exception):
    pass


class ApiReadOnlyError(EsxiError):
    """Write-API vom Host abgelehnt (kostenlose Lizenz)."""


@dataclass
class VmInfo:
    name: str
    power_state: str          # poweredOn | poweredOff | suspended
    guest_os: str
    vmx_path: str             # "[datastore] ordner/name.vmx"
    tools_running: bool
    num_cpu: int
    memory_mb: int
    has_snapshots: bool


@dataclass
class DatastoreInfo:
    name: str
    capacity: int
    free_space: int
    ds_type: str


def _wrap_restricted(exc: Exception) -> Exception:
    if isinstance(exc, vim.fault.RestrictedVersion):
        return ApiReadOnlyError(
            "Der Host lehnt Schreibzugriffe über die API ab (kostenlose ESXi-Lizenz). "
            "SSH-Fallback erforderlich."
        )
    return exc


def split_datastore_path(path: str) -> tuple[str, str]:
    """'[ds1] ordner/datei.vmx' -> ('ds1', 'ordner/datei.vmx')"""
    if not path.startswith("["):
        raise EsxiError(f"Ungültiger Datastore-Pfad: {path}")
    ds, _, rel = path[1:].partition("] ")
    return ds, rel


class _IterReader:
    """File-like-Ansicht über einen Byte-Iterator mit bekannter Gesamtlänge."""

    def __init__(self, iterator: Iterator[bytes], length: int):
        self._it = iterator
        self._len = length
        self._chunk = memoryview(b"")
        self._pos = 0

    def __len__(self) -> int:
        return self._len

    def read(self, size: int = -1) -> bytes:
        # Liefert höchstens einen Chunk-Rest pro Aufruf (erlaubt für read());
        # so wird nie umkopiert, egal wie klein die Leseblöcke von http.client sind.
        if size == 0:
            return b""
        if self._pos >= len(self._chunk):
            try:
                self._chunk = memoryview(next(self._it))
            except StopIteration:
                return b""
            self._pos = 0
        end = len(self._chunk) if size < 0 else min(self._pos + size, len(self._chunk))
        out = bytes(self._chunk[self._pos:end])
        self._pos = end
        return out


class EsxiClient:
    def __init__(self, address: str, username: str, password: str, port: int = 443):
        self.address = address
        self.username = username
        self.password = password
        self.port = port
        self._si = None
        self._http = requests.Session()
        self._http.auth = (username, password)
        self._http.verify = False

    # --- Verbindung -----------------------------------------------------------

    def connect(self) -> None:
        ctx = ssl._create_unverified_context()
        try:
            self._si = SmartConnect(
                host=self.address, user=self.username, pwd=self.password,
                port=self.port, sslContext=ctx,
            )
        except vim.fault.InvalidLogin:
            raise EsxiError(f"Anmeldung an {self.address} fehlgeschlagen (Benutzer/Passwort).")
        except Exception as e:
            raise EsxiError(f"Verbindung zu {self.address}:{self.port} fehlgeschlagen: {e}")

    def disconnect(self) -> None:
        if self._si:
            try:
                Disconnect(self._si)
            except Exception:
                pass
            self._si = None

    @property
    def content(self):
        if not self._si:
            raise EsxiError("Nicht verbunden.")
        return self._si.RetrieveContent()

    def _datacenter(self) -> vim.Datacenter:
        for child in self.content.rootFolder.childEntity:
            if isinstance(child, vim.Datacenter):
                return child
        raise EsxiError("Kein Datacenter auf dem Host gefunden.")

    def _host_system(self) -> vim.HostSystem:
        dc = self._datacenter()
        for cr in dc.hostFolder.childEntity:
            if isinstance(cr, vim.ComputeResource) and cr.host:
                return cr.host[0]
        raise EsxiError("Kein HostSystem gefunden.")

    def _resource_pool(self) -> vim.ResourcePool:
        dc = self._datacenter()
        for cr in dc.hostFolder.childEntity:
            if isinstance(cr, vim.ComputeResource):
                return cr.resourcePool
        raise EsxiError("Kein ResourcePool gefunden.")

    # --- Inventar -------------------------------------------------------------

    # Gezielter Property-Abruf statt kompletter Objekte: eine SOAP-Runde für
    # alle VMs statt N+1, und robust gegen einzelne kaputte/verwaiste Properties.
    _VM_PROPS = [
        "name", "runtime.powerState", "config.guestFullName",
        "config.files.vmPathName", "config.hardware.numCPU",
        "config.hardware.memoryMB", "guest.toolsRunningStatus", "snapshot",
    ]

    def _retrieve_vm_properties(
        self, props: list[str]
    ) -> list[tuple[vim.VirtualMachine, dict]]:
        view = self.content.viewManager.CreateContainerView(
            self.content.rootFolder, [vim.VirtualMachine], True
        )
        try:
            q = vmodl.query.PropertyCollector
            traversal = q.TraversalSpec(name="v", path="view", skip=False,
                                        type=type(view))
            filter_spec = q.FilterSpec(
                objectSet=[q.ObjectSpec(obj=view, skip=True, selectSet=[traversal])],
                propSet=[q.PropertySpec(type=vim.VirtualMachine, pathSet=props)],
            )
            result = []
            for oc in self.content.propertyCollector.RetrieveContents([filter_spec]):
                result.append((oc.obj, {p.name: p.val for p in oc.propSet}))
            return result
        finally:
            view.Destroy()

    def _vm_prop(self, vm: vim.VirtualMachine, path: str):
        q = vmodl.query.PropertyCollector
        filter_spec = q.FilterSpec(
            objectSet=[q.ObjectSpec(obj=vm)],
            propSet=[q.PropertySpec(type=vim.VirtualMachine, pathSet=[path])],
        )
        for oc in self.content.propertyCollector.RetrieveContents([filter_spec]):
            for p in oc.propSet:
                if p.name == path:
                    return p.val
        return None

    def list_vms(self) -> list[VmInfo]:
        result = []
        for _, props in self._retrieve_vm_properties(self._VM_PROPS):
            if "config.files.vmPathName" not in props:
                continue  # verwaiste/unzugreifbare VM überspringen
            snap = props.get("snapshot")
            result.append(VmInfo(
                name=props.get("name", "?"),
                power_state=str(props.get("runtime.powerState", "")),
                guest_os=props.get("config.guestFullName", "") or "",
                vmx_path=props["config.files.vmPathName"],
                tools_running=(props.get("guest.toolsRunningStatus")
                               == "guestToolsRunning"),
                num_cpu=int(props.get("config.hardware.numCPU", 0) or 0),
                memory_mb=int(props.get("config.hardware.memoryMB", 0) or 0),
                has_snapshots=bool(snap and snap.rootSnapshotList),
            ))
        return sorted(result, key=lambda v: v.name.lower())

    def get_vm(self, name: str) -> vim.VirtualMachine:
        for vm, props in self._retrieve_vm_properties(["name"]):
            if props.get("name") == name:
                return vm
        raise EsxiError(f"VM '{name}' auf {self.address} nicht gefunden.")

    def vm_power_state(self, vm: vim.VirtualMachine) -> str:
        return str(self._vm_prop(vm, "runtime.powerState") or "")

    def vm_tools_running(self, vm: vim.VirtualMachine) -> bool:
        return self._vm_prop(vm, "guest.toolsRunningStatus") == "guestToolsRunning"

    def vm_path(self, vm: vim.VirtualMachine) -> str:
        return self._vm_prop(vm, "config.files.vmPathName") or ""

    def list_datastores(self) -> list[DatastoreInfo]:
        result = []
        for ds in self._datacenter().datastore:
            s = ds.summary
            result.append(DatastoreInfo(
                name=s.name, capacity=s.capacity, free_space=s.freeSpace, ds_type=s.type,
            ))
        return sorted(result, key=lambda d: d.name.lower())

    def list_networks(self) -> list[str]:
        return sorted(n.name for n in self._datacenter().network)

    # --- Snapshots ------------------------------------------------------------

    def create_snapshot(self, vm: vim.VirtualMachine, name: str, quiesce: bool) -> None:
        try:
            task = vm.CreateSnapshot_Task(
                name=name, description="ESXi Auto Backupper (temporär)",
                memory=False, quiesce=quiesce,
            )
            WaitForTask(task)
        except Exception as e:
            raise _wrap_restricted(e)

    def find_snapshot(self, vm: vim.VirtualMachine, name: str):
        def walk(nodes):
            for n in nodes:
                if n.name == name:
                    return n.snapshot
                found = walk(n.childSnapshotList)
                if found:
                    return found
            return None
        snap_info = self._vm_prop(vm, "snapshot")
        if not snap_info:
            return None
        return walk(snap_info.rootSnapshotList)

    def remove_snapshot(self, vm: vim.VirtualMachine, name: str) -> bool:
        snap = self.find_snapshot(vm, name)
        if not snap:
            return False
        try:
            WaitForTask(snap.RemoveSnapshot_Task(removeChildren=True, consolidate=True))
            return True
        except Exception as e:
            raise _wrap_restricted(e)

    # --- VM-Verwaltung --------------------------------------------------------

    def register_vm(self, vmx_datastore_path: str, name: str) -> None:
        dc = self._datacenter()
        try:
            task = dc.vmFolder.RegisterVM_Task(
                path=vmx_datastore_path, name=name, asTemplate=False,
                pool=self._resource_pool(), host=self._host_system(),
            )
            WaitForTask(task)
        except Exception as e:
            raise _wrap_restricted(e)

    def destroy_vm(self, vm: vim.VirtualMachine) -> None:
        """VM abmelden und Dateien löschen (für Retention alter Backups)."""
        try:
            if self.vm_power_state(vm) == "poweredOn":
                raise EsxiError("VM läuft - wird nicht gelöscht.")
            WaitForTask(vm.Destroy_Task())
        except Exception as e:
            raise _wrap_restricted(e)

    def make_directory(self, datastore: str, folder: str) -> None:
        fm = self.content.fileManager
        path = f"[{datastore}] {folder}"
        try:
            fm.MakeDirectory(name=path, datacenter=self._datacenter(),
                             createParentDirectories=True)
        except vim.fault.FileAlreadyExists:
            pass
        except Exception as e:
            raise _wrap_restricted(e)

    def delete_datastore_path(self, datastore: str, folder: str) -> None:
        fm = self.content.fileManager
        path = f"[{datastore}] {folder}"
        try:
            task = fm.DeleteDatastoreFile_Task(name=path, datacenter=self._datacenter())
            WaitForTask(task)
        except vim.fault.FileNotFound:
            pass
        except Exception as e:
            raise _wrap_restricted(e)

    def list_datastore_folder(self, datastore: str, folder: str = "") -> list[str]:
        """Dateinamen in einem Datastore-Ordner (read-only, geht auch mit Free-Lizenz)."""
        ds_obj = None
        for ds in self._datacenter().datastore:
            if ds.summary.name == datastore:
                ds_obj = ds
                break
        if ds_obj is None:
            raise EsxiError(f"Datastore '{datastore}' nicht gefunden.")
        browser = ds_obj.browser
        spec = vim.host.DatastoreBrowser.SearchSpec(
            details=vim.host.DatastoreBrowser.FileInfo.Details(
                fileType=True, fileSize=True, modification=False, fileOwner=False
            )
        )
        path = f"[{datastore}] {folder}".rstrip()
        task = browser.SearchDatastore_Task(datastorePath=path, searchSpec=spec)
        WaitForTask(task)
        info = task.info.result
        return [f.path for f in (info.file or [])]

    # --- Datastore-HTTP (Dateien lesen/schreiben, auch mit Free-Lizenz) -------

    def _file_url(self, datastore: str, rel_path: str) -> str:
        quoted = urllib.parse.quote(rel_path)
        return (
            f"https://{self.address}:{self.port}/folder/{quoted}"
            f"?dcPath={urllib.parse.quote(DC_PATH)}&dsName={urllib.parse.quote(datastore)}"
        )

    def file_size(self, datastore: str, rel_path: str) -> int:
        r = self._http.head(self._file_url(datastore, rel_path), timeout=30)
        if r.status_code == 404:
            raise EsxiError(f"Datei nicht gefunden: [{datastore}] {rel_path}")
        if r.status_code in (405, 501) or "Content-Length" not in r.headers:
            # Fallback für Hosts ohne HEAD-Unterstützung: GET öffnen, sofort schließen
            g = self._http.get(self._file_url(datastore, rel_path), stream=True, timeout=30)
            try:
                if g.status_code == 404:
                    raise EsxiError(f"Datei nicht gefunden: [{datastore}] {rel_path}")
                g.raise_for_status()
                return int(g.headers.get("Content-Length", 0))
            finally:
                g.close()
        r.raise_for_status()
        return int(r.headers.get("Content-Length", 0))

    def download_text(self, datastore: str, rel_path: str) -> str:
        r = self._http.get(self._file_url(datastore, rel_path), timeout=60)
        if r.status_code == 404:
            raise EsxiError(f"Datei nicht gefunden: [{datastore}] {rel_path}")
        r.raise_for_status()
        return r.content.decode("utf-8", errors="replace")

    def open_download(self, datastore: str, rel_path: str) -> requests.Response:
        """Streaming-GET; Aufrufer iteriert über iter_content()."""
        r = self._http.get(self._file_url(datastore, rel_path), stream=True, timeout=60)
        if r.status_code == 404:
            raise EsxiError(f"Datei nicht gefunden: [{datastore}] {rel_path}")
        r.raise_for_status()
        return r

    def upload_stream(
        self,
        datastore: str,
        rel_path: str,
        data: Iterator[bytes],
        content_length: Optional[int] = None,
    ) -> None:
        # Kein chunked encoding: ESXi/Go-HTTP erwarten einen Body mit bekannter
        # Länge. Der Wrapper meldet die Länge über __len__, requests setzt dann
        # selbst den korrekten Content-Length-Header und streamt via read().
        if content_length is None:
            body: object = b"".join(data)
        else:
            body = _IterReader(data, content_length)
        headers = {"Content-Type": "application/octet-stream"}
        r = self._http.put(
            self._file_url(datastore, rel_path), data=body, headers=headers, timeout=60,
        )
        if r.status_code not in (200, 201):
            raise EsxiError(
                f"Upload nach [{datastore}] {rel_path} fehlgeschlagen: "
                f"HTTP {r.status_code} {r.reason}"
            )

    def upload_text(self, datastore: str, rel_path: str, text: str) -> None:
        payload = text.encode("utf-8")
        self.upload_stream(datastore, rel_path, iter([payload]), len(payload))

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.disconnect()
