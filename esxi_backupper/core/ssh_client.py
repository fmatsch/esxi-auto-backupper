"""SSH-Fallback für ESXi-Hosts mit kostenloser Lizenz (API read-only).

Nutzt die auf jedem ESXi vorhandenen Bordmittel:
- vim-cmd vmsvc/...   Snapshots, Registrierung, VM-Verwaltung
- mkdir / rm          Datastore-Ordner anlegen/löschen

Voraussetzung: SSH ist auf dem Host aktiviert (Host-UI: Aktionen ->
Dienste -> Secure Shell (SSH) aktivieren).
"""

from __future__ import annotations

import re
import shlex

import paramiko

from .esxi_client import EsxiError


class SshError(EsxiError):
    pass


class EsxiSshClient:
    def __init__(self, address: str, username: str, password: str, port: int = 22):
        self.address = address
        self.username = username
        self.password = password
        self.port = port
        self._client: paramiko.SSHClient | None = None

    def connect(self) -> None:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect(
                self.address, port=self.port, username=self.username,
                password=self.password, timeout=15, allow_agent=False,
                look_for_keys=False,
            )
        except paramiko.AuthenticationException:
            raise SshError(f"SSH-Anmeldung an {self.address} fehlgeschlagen.")
        except Exception as e:
            raise SshError(
                f"SSH-Verbindung zu {self.address}:{self.port} fehlgeschlagen: {e}. "
                "Ist SSH auf dem Host aktiviert?"
            )
        self._client = c

    def disconnect(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def run(self, command: str, timeout: int = 600) -> str:
        if not self._client:
            self.connect()
        _, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        rc = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if rc != 0:
            raise SshError(f"SSH-Befehl fehlgeschlagen (rc={rc}): {command}\n{err or out}")
        return out

    # --- vim-cmd Helfer -------------------------------------------------------

    def vm_id_by_name(self, name: str) -> str:
        """VM-ID (vmid) aus 'vim-cmd vmsvc/getallvms' ermitteln."""
        out = self.run("vim-cmd vmsvc/getallvms")
        # Format: Vmid  Name  File  Guest OS  Version  Annotation
        for line in out.splitlines()[1:]:
            m = re.match(r"^(\d+)\s+(.+?)\s+\[", line)
            if m and m.group(2).strip() == name:
                return m.group(1)
        raise SshError(f"VM '{name}' per vim-cmd nicht gefunden.")

    def create_snapshot(self, vm_name: str, snap_name: str, quiesce: bool) -> None:
        vmid = self.vm_id_by_name(vm_name)
        q = "1" if quiesce else "0"
        # snapshot.create <vmid> <name> <desc> <includeMemory> <quiesced>
        self.run(
            f"vim-cmd vmsvc/snapshot.create {vmid} "
            f"{shlex.quote(snap_name)} {shlex.quote('ESXi Auto Backupper (temporär)')} 0 {q}"
        )

    def remove_all_snapshots(self, vm_name: str) -> None:
        vmid = self.vm_id_by_name(vm_name)
        self.run(f"vim-cmd vmsvc/snapshot.removeall {vmid}", timeout=3600)

    def remove_snapshot_by_name(self, vm_name: str, snap_name: str) -> bool:
        """Entfernt gezielt einen Snapshot (lässt fremde Snapshots unangetastet)."""
        vmid = self.vm_id_by_name(vm_name)
        out = self.run(f"vim-cmd vmsvc/snapshot.get {vmid}")
        # Ausgabe enthält Blöcke mit "--Snapshot Name : xxx" und "--Snapshot Id : N"
        current_name = None
        snap_id = None
        for line in out.splitlines():
            m = re.search(r"Snapshot Name\s*:\s*(.+)$", line)
            if m:
                current_name = m.group(1).strip()
                continue
            m = re.search(r"Snapshot Id\s*:\s*(\d+)", line)
            if m and current_name == snap_name:
                snap_id = m.group(1)
                break
        if snap_id is None:
            return False
        # snapshot.remove <vmid> <snapshotId> - konsolidiert automatisch
        self.run(f"vim-cmd vmsvc/snapshot.remove {vmid} {snap_id}", timeout=3600)
        return True

    def register_vm(self, vmx_absolute_path: str, name: str) -> None:
        # vmx_absolute_path: /vmfs/volumes/<ds>/<ordner>/<datei>.vmx
        self.run(
            f"vim-cmd solo/registervm {shlex.quote(vmx_absolute_path)} {shlex.quote(name)}"
        )

    def unregister_vm(self, vm_name: str) -> None:
        vmid = self.vm_id_by_name(vm_name)
        self.run(f"vim-cmd vmsvc/unregister {vmid}")

    def vm_power_state(self, vm_name: str) -> str:
        vmid = self.vm_id_by_name(vm_name)
        out = self.run(f"vim-cmd vmsvc/power.getstate {vmid}")
        return "poweredOn" if "Powered on" in out else "poweredOff"

    # --- Datastore-Dateisystem ------------------------------------------------

    def make_directory(self, datastore: str, folder: str) -> None:
        path = f"/vmfs/volumes/{datastore}/{folder}"
        self.run(f"mkdir -p {shlex.quote(path)}")

    def delete_path(self, datastore: str, folder: str) -> None:
        path = f"/vmfs/volumes/{datastore}/{folder}"
        # Sicherheitsnetz: niemals den Datastore-Root oder darüber löschen
        parts = [p for p in folder.split("/") if p]
        if not parts or any(p in (".", "..") for p in parts) or "/" in datastore:
            raise SshError(f"Verweigert: unsicherer Löschpfad '{path}'")
        self.run(f"rm -rf {shlex.quote(path)}")

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.disconnect()
