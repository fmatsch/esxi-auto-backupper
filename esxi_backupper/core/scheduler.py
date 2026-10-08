"""Zeitplan-Logik (Qt-frei) + Registrierung im Windows-Taskplaner.

Der interne Scheduler der GUI fragt periodisch is_due() ab. Zusätzlich kann
ein Job im Windows-Taskplaner registriert werden, damit er auch ohne offene
App läuft (startet die exe headless mit --run-job <id>).
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta
from typing import Optional

from .config import BackupJob, Schedule


def _parse_time(hhmm: str) -> tuple[int, int]:
    try:
        h, m = hhmm.strip().split(":")
        return max(0, min(23, int(h))), max(0, min(59, int(m)))
    except (ValueError, AttributeError):
        return 22, 0


def _parse_last_run(iso: str) -> Optional[datetime]:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def _last_occurrence(schedule: Schedule, now: datetime) -> Optional[datetime]:
    """Jüngster geplanter Zeitpunkt <= now (für daily/weekly)."""
    h, m = _parse_time(schedule.time_of_day)
    candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if schedule.mode == "daily":
        if candidate > now:
            candidate -= timedelta(days=1)
        return candidate
    if schedule.mode == "weekly":
        diff = (candidate.weekday() - schedule.weekday) % 7
        candidate -= timedelta(days=diff)
        if candidate > now:
            candidate -= timedelta(days=7)
        return candidate
    return None


def is_due(schedule: Schedule, last_run_iso: str, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    last = _parse_last_run(last_run_iso)
    if schedule.mode == "manual":
        return False
    if schedule.mode == "hourly":
        if last is None:
            return True
        return now - last >= timedelta(hours=max(1, schedule.interval_hours))
    occ = _last_occurrence(schedule, now)
    if occ is None:
        return False
    return last is None or last < occ


def next_run(schedule: Schedule, last_run_iso: str,
             now: Optional[datetime] = None) -> Optional[datetime]:
    """Nächster geplanter Lauf (nur zur Anzeige)."""
    now = now or datetime.now()
    last = _parse_last_run(last_run_iso)
    if schedule.mode == "manual":
        return None
    if schedule.mode == "hourly":
        if last is None:
            return now
        return last + timedelta(hours=max(1, schedule.interval_hours))
    if is_due(schedule, last_run_iso, now):
        return now
    occ = _last_occurrence(schedule, now)
    step = timedelta(days=1 if schedule.mode == "daily" else 7)
    return occ + step


def describe(schedule: Schedule) -> str:
    if schedule.mode == "manual":
        return "Manuell"
    if schedule.mode == "hourly":
        return f"Alle {max(1, schedule.interval_hours)} Std."
    days = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    if schedule.mode == "daily":
        return f"Täglich {schedule.time_of_day}"
    if schedule.mode == "weekly":
        return f"Wöchentl. {days[schedule.weekday % 7]} {schedule.time_of_day}"
    return schedule.mode


# --- Windows-Taskplaner -------------------------------------------------------

_TASK_PREFIX = "EsxiAutoBackupper"


def _task_name(job: BackupJob) -> str:
    return f"{_TASK_PREFIX}_{job.id}"


def headless_exe_path() -> str:
    """Pfad der Konsolen-Exe für den Taskplaner (bevorzugt EsxiBackupperCli.exe)."""
    import os
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        cli = os.path.join(exe_dir, "EsxiBackupperCli.exe")
        return cli if os.path.exists(cli) else sys.executable
    return sys.executable  # Entwicklung: python-Interpreter


def register_windows_task(job: BackupJob, exe_path: Optional[str] = None) -> None:
    """Legt einen Windows-Taskplaner-Task an, der den Job headless ausführt."""
    if sys.platform != "win32":
        raise RuntimeError("Taskplaner-Registrierung ist nur unter Windows möglich.")
    exe = exe_path or headless_exe_path()
    module_arg = "" if getattr(sys, "frozen", False) else "-m esxi_backupper "
    s = job.schedule
    h, m = _parse_time(s.time_of_day)
    st = f"{h:02d}:{m:02d}"
    cmd = ["schtasks", "/Create", "/F", "/TN", _task_name(job),
           "/TR", f'"{exe}" {module_arg}--run-job {job.id}']
    if s.mode == "hourly":
        cmd += ["/SC", "HOURLY", "/MO", str(max(1, s.interval_hours))]
    elif s.mode == "daily":
        cmd += ["/SC", "DAILY", "/ST", st]
    elif s.mode == "weekly":
        days = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
        cmd += ["/SC", "WEEKLY", "/D", days[s.weekday % 7], "/ST", st]
    else:
        raise RuntimeError("Für manuelle Jobs wird kein Taskplaner-Task angelegt.")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"schtasks fehlgeschlagen: {result.stderr or result.stdout}")


def unregister_windows_task(job: BackupJob) -> None:
    if sys.platform != "win32":
        return
    subprocess.run(["schtasks", "/Delete", "/F", "/TN", _task_name(job)],
                   capture_output=True, text=True)
