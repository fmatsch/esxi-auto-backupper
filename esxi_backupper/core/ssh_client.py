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
import socket
import threading
import time
from typing import Callable, Optional

import paramiko

from .esxi_client import Cancelled, EsxiError


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

    def ensure_connected(self) -> None:
        if not self._client:
            self.connect()

    def _exec(
        self,
        command: str,
        timeout: Optional[float] = 600,
        on_output: Optional[Callable[[str], None]] = None,
        cancel: Optional[threading.Event] = None,
        pty: bool = False,
    ) -> tuple[int, str]:
        """Führt einen Befehl aus und liest die Ausgabe laufend (kein Puffer-Stau).

        stderr wird mit stdout zusammengeführt. Mit pty=True bekommt der Prozess
        beim Schließen des Kanals ein SIGHUP - so lässt sich ein langer
        vmkfstools-Lauf per Abbruch tatsächlich beenden.
        """
        self.ensure_connected()
        chan = self._client.get_transport().open_session()
        try:
            if pty:
                chan.get_pty()
            chan.set_combine_stderr(True)
            chan.settimeout(1.0)
            chan.exec_command(command)
            deadline = None if timeout is None else time.monotonic() + timeout
            parts: list[str] = []
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled("Backup abgebrochen.")
                if deadline is not None and time.monotonic() > deadline:
                    raise SshError(f"Zeitüberschreitung nach {int(timeout)} s: {command}")
                try:
                    data = chan.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                parts.append(text)
                if on_output:
                    on_output(text)
            return chan.recv_exit_status(), "".join(parts)
        finally:
            chan.close()

    def run(self, command: str, timeout: Optional[float] = 600) -> str:
        rc, out = self._exec(command, timeout)
        if rc != 0:
            raise SshError(f"SSH-Befehl fehlgeschlagen (rc={rc}): {command}\n{out}")
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
        self.run(f"vim-cmd vmsvc/snapshot.removeall {vmid}", timeout=None)

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
        self.run(f"vim-cmd vmsvc/snapshot.remove {vmid} {snap_id}", timeout=None)
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

    # --- Thin-Export / -Import (vmkfstools) -----------------------------------

    def has_command(self, name: str) -> bool:
        q = shlex.quote(name)
        rc, _ = self._exec(
            f"command -v {q} >/dev/null 2>&1 || test -x /sbin/{q} || test -x /bin/{q}",
            timeout=30)
        return rc == 0

    def allocated_bytes(self, paths: list[str]) -> int:
        """Tatsächlich belegter Platz der Dateien (bei Thin-Disks << logische Größe)."""
        if not paths:
            return 0
        out = self.run("du -k " + " ".join(shlex.quote(p) for p in paths), timeout=120)
        sizes = [int(m.group(1)) for m in re.finditer(r"^(\d+)\s", out, re.MULTILINE)]
        if len(sizes) != len(paths):
            raise SshError(f"Unerwartete 'du'-Ausgabe: {out.strip()[:200]}")
        return sum(sizes) * 1024

    def list_dir(self, path: str) -> list[str]:
        out = self.run(f"ls -1 {shlex.quote(path)}", timeout=60)
        return [line.strip() for line in out.splitlines() if line.strip()]

    def clone_disk(
        self,
        src_abs: str,
        dst_abs: str,
        disk_format: str,
        progress: Optional[Callable[[int], None]] = None,
        cancel: Optional[threading.Event] = None,
    ) -> None:
        """vmkfstools -i <src> -d <format> <dst>; meldet Prozent-Fortschritt."""
        if disk_format not in ("thin", "2gbsparse"):
            raise SshError(f"Nicht erlaubtes Disk-Format: {disk_format}")
        last = {"pct": -1, "tail": ""}

        def on_output(text: str) -> None:
            last["tail"] = (last["tail"] + text)[-400:]
            for m in re.finditer(r"(\d+)% done", text):
                pct = int(m.group(1))
                if pct > last["pct"]:
                    last["pct"] = pct
                    if progress:
                        progress(pct)

        cmd = (f"vmkfstools -i {shlex.quote(src_abs)} -d {disk_format} "
               f"{shlex.quote(dst_abs)}")
        rc, out = self._exec(cmd, timeout=None, on_output=on_output,
                             cancel=cancel, pty=True)
        if rc != 0:
            raise SshError(f"vmkfstools fehlgeschlagen (rc={rc}): {last['tail'].strip()}")

    def delete_file(self, absolute_path: str) -> None:
        """Löscht eine einzelne Datei unterhalb von /vmfs/volumes (keine Platzhalter)."""
        if (not absolute_path.startswith("/vmfs/volumes/") or ".." in absolute_path
                or any(c in absolute_path for c in "*?[")):
            raise SshError(f"Verweigert: unsicherer Dateipfad '{absolute_path}'")
        self.run(f"rm -f {shlex.quote(absolute_path)}", timeout=60)
