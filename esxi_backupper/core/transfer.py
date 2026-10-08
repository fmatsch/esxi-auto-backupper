"""Streaming-Transfer von Datastore-Dateien: Quelle -> (dieser PC) -> Ziel.

Die Daten werden nicht zwischengespeichert, sondern chunkweise vom
Quell-Host gelesen und direkt zum Ziel-Host hochgeladen. Fortschritt und
Übertragungsrate werden per Callback gemeldet, Abbruch über ein Event.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .esxi_client import CHUNK_SIZE, EsxiClient, EsxiError


class TransferCancelled(EsxiError):
    pass


@dataclass
class TransferProgress:
    file_name: str = ""
    file_index: int = 0
    file_count: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    rate_bps: float = 0.0

    @property
    def percent(self) -> float:
        if self.bytes_total <= 0:
            return 0.0
        return 100.0 * self.bytes_done / self.bytes_total


ProgressCallback = Callable[[TransferProgress], None]


def copy_datastore_file(
    source: EsxiClient,
    source_ds: str,
    source_path: str,
    target: EsxiClient,
    target_ds: str,
    target_path: str,
    progress: Optional[ProgressCallback] = None,
    cancel: Optional[threading.Event] = None,
    file_index: int = 0,
    file_count: int = 0,
) -> int:
    """Kopiert eine Datei streamend, gibt die übertragenen Bytes zurück."""
    total = source.file_size(source_ds, source_path)
    state = TransferProgress(
        file_name=source_path.rsplit("/", 1)[-1],
        file_index=file_index, file_count=file_count, bytes_total=total,
    )
    response = source.open_download(source_ds, source_path)
    start = time.monotonic()
    last_report = 0.0

    def chunks():
        nonlocal last_report
        try:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if cancel is not None and cancel.is_set():
                    raise TransferCancelled("Übertragung abgebrochen.")
                if not chunk:
                    continue
                state.bytes_done += len(chunk)
                now = time.monotonic()
                if progress and (now - last_report >= 0.5 or state.bytes_done >= total):
                    elapsed = max(now - start, 0.001)
                    state.rate_bps = state.bytes_done / elapsed
                    progress(state)
                    last_report = now
                yield chunk
        finally:
            response.close()

    target.upload_stream(target_ds, target_path, chunks(), content_length=total)
    if state.bytes_done != total:
        raise EsxiError(
            f"Unvollständige Übertragung von {source_path}: "
            f"{state.bytes_done} von {total} Bytes."
        )
    stored = target.file_size(target_ds, target_path)
    if stored != total:
        raise EsxiError(
            f"Zieldatei {target_path} hat {stored} statt {total} Bytes - "
            "Übertragung fehlerhaft."
        )
    return state.bytes_done


def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"
