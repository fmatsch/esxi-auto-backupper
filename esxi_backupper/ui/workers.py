"""QThread-Helfer: Verbindungsaufbau und Job-Ausführung ohne UI-Blockade."""

from __future__ import annotations

import threading
import traceback
from typing import Callable

from PySide6.QtCore import QThread, Signal

from ..core.config import BackupJob
from ..core.runner import run_job
from ..core.transfer import TransferProgress


class FuncWorker(QThread):
    """Führt eine Funktion im Hintergrund aus und liefert Ergebnis/Fehler."""

    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[], object], parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            self.finished_ok.emit(self._fn())
        except Exception as e:
            self.failed.emit(str(e) or traceback.format_exc())


class JobWorker(QThread):
    """Führt einen Backup-Job aus (run_job) und meldet Log/Fortschritt."""

    log = Signal(str)
    progress = Signal(object)   # TransferProgress
    done = Signal(str)          # Name der erstellten Ziel-VM
    failed = Signal(str)

    def __init__(self, job: BackupJob, parent=None):
        super().__init__(parent)
        self.job = job
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            name = run_job(
                self.job,
                log=self.log.emit,
                progress=lambda p: self.progress.emit(TransferProgress(**vars(p))),
                cancel=self.cancel_event,
            )
            self.done.emit(name)
        except Exception as e:
            self.failed.emit(str(e) or traceback.format_exc())
