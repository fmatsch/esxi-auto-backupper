"""Einstiegspunkt: GUI (Standard) oder headless CLI (--run-job, für Taskplaner)."""

from __future__ import annotations

import argparse
import sys


def run_headless(job_id: str) -> int:
    from .core.config import find_job, load_config
    from .core.runner import run_job

    cfg = load_config()
    job = find_job(cfg, job_id)
    if job is None:
        print(f"FEHLER: Job '{job_id}' nicht gefunden. Vorhandene Jobs:")
        for j in cfg.jobs:
            print(f"  {j.id}  {j.name}")
        return 2
    print(f"Starte Backup-Job '{job.name}' ({job.source_vm} -> "
          f"{job.target_host.address})")

    def progress(p):
        print(f"\r  {p.file_name}: {p.percent:5.1f}%", end="", flush=True)

    try:
        name = run_job(job, log=lambda m: print(f"\n{m}"), progress=progress)
        print(f"\nErfolg: {name}")
        return 0
    except Exception as e:
        print(f"\nFEHLER: {e}", file=sys.stderr)
        return 1


def list_jobs() -> int:
    from .core.config import load_config

    cfg = load_config()
    if not cfg.jobs:
        print("Keine Jobs konfiguriert.")
        return 0
    for j in cfg.jobs:
        print(f"{j.id}  {j.name:30s}  {j.source_vm} -> {j.target_host.address}"
              f"  [{j.last_status or 'nie gelaufen'}]")
    return 0


def selftest() -> int:
    """Prüft, ob die (gebaute) Anwendung vollständig ist: Module, Qt, Icon, Schlüsselbund."""
    import keyring

    from . import __version__
    from .core import esxi_client, job, runner, scheduler, ssh_client, transfer  # noqa: F401
    from .ui.resources import asset_path

    ok = True
    backend = keyring.get_keyring()
    backend_name = f"{type(backend).__module__}.{type(backend).__name__}"
    print(f"ESXi Auto Backupper {__version__}")
    print(f"Schlüsselbund-Backend: {backend_name}")
    if type(backend).__module__.startswith("keyring.backends.fail"):
        print("FEHLER: kein nutzbarer Schlüsselbund")
        ok = False
    icon = asset_path("icon.png")
    print(f"Icon: {icon} ({'vorhanden' if icon.exists() else 'FEHLT'})")
    ok = ok and icon.exists()
    import PySide6.QtWidgets  # noqa: F401
    import pyVmomi  # noqa: F401
    print("Selbsttest:", "OK" if ok else "FEHLGESCHLAGEN")
    return 0 if ok else 1


def run_gui() -> int:
    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow
    from .ui.resources import app_icon

    if sys.platform == "win32":
        # Eigene AppUserModelID, damit die Taskleiste das App-Icon statt des
        # Python-Icons zeigt und die App nicht mit anderen gruppiert wird.
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "fmatsch.EsxiAutoBackupper")

    app = QApplication(sys.argv)
    app.setApplicationName("ESXi Auto Backupper")
    app.setWindowIcon(app_icon())
    win = MainWindow()
    win.show()
    return app.exec()


def main() -> int:
    # In der fensterlosen Windows-Exe (console=False) sind stdout/stderr None -
    # print() würde dann crashen. Ins Leere schreiben statt abstürzen.
    if sys.stdout is None:
        sys.stdout = open(__import__("os").devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(__import__("os").devnull, "w")

    parser = argparse.ArgumentParser(
        prog="EsxiBackupper",
        description="Backup laufender ESXi-VMs auf einen anderen Host.")
    parser.add_argument("--run-job", metavar="JOB_ID",
                        help="Job headless ausführen (für Windows-Taskplaner)")
    parser.add_argument("--list-jobs", action="store_true",
                        help="konfigurierte Jobs auflisten")
    parser.add_argument("--selftest", action="store_true",
                        help="Installation prüfen (Module, Qt, Icon, Schlüsselbund)")
    args = parser.parse_args()

    if args.selftest:
        return selftest()
    if args.list_jobs:
        return list_jobs()
    if args.run_job:
        return run_headless(args.run_job)
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
