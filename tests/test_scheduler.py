from datetime import datetime

from esxi_backupper.core.config import Schedule
from esxi_backupper.core.scheduler import describe, is_due, next_run

NOW = datetime(2026, 8, 18, 12, 0)  # Dienstag


def test_manual_never_due():
    s = Schedule(mode="manual")
    assert not is_due(s, "", NOW)
    assert next_run(s, "", NOW) is None


def test_hourly():
    s = Schedule(mode="hourly", interval_hours=6)
    assert is_due(s, "", NOW)  # noch nie gelaufen -> sofort
    assert not is_due(s, "2026-08-18T08:00:00", NOW)   # vor 4h gelaufen
    assert is_due(s, "2026-08-18T05:59:00", NOW)       # vor >6h gelaufen
    assert next_run(s, "2026-08-18T08:00:00", NOW) == datetime(2026, 8, 18, 14, 0)


def test_daily():
    s = Schedule(mode="daily", time_of_day="10:30")
    # heute 10:30 war schon, letzter Lauf gestern -> fällig
    assert is_due(s, "2026-08-17T10:31:00", NOW)
    # heute schon gelaufen -> nicht fällig
    assert not is_due(s, "2026-08-18T10:31:00", NOW)
    assert next_run(s, "2026-08-18T10:31:00", NOW) == datetime(2026, 8, 19, 10, 30)


def test_daily_before_time_of_day():
    s = Schedule(mode="daily", time_of_day="22:00")
    # 22:00 ist heute noch nicht erreicht, gestern gelaufen -> nicht fällig
    assert not is_due(s, "2026-08-17T22:01:00", NOW)
    assert next_run(s, "2026-08-17T22:01:00", NOW) == datetime(2026, 8, 18, 22, 0)


def test_weekly():
    s = Schedule(mode="weekly", weekday=6, time_of_day="03:00")  # Sonntag
    # letzter Sonntag 03:00 war der 16.8.; Lauf davor -> fällig
    assert is_due(s, "2026-08-15T03:00:00", NOW)
    assert not is_due(s, "2026-08-16T03:05:00", NOW)
    assert next_run(s, "2026-08-16T03:05:00", NOW) == datetime(2026, 8, 23, 3, 0)


def test_never_run_daily_is_due():
    s = Schedule(mode="daily", time_of_day="10:00")
    assert is_due(s, "", NOW)


def test_describe():
    assert describe(Schedule(mode="manual")) == "Manuell"
    assert describe(Schedule(mode="hourly", interval_hours=4)) == "Alle 4 Std."
    assert describe(Schedule(mode="daily", time_of_day="22:00")) == "Täglich 22:00"
    assert "So" in describe(Schedule(mode="weekly", weekday=6, time_of_day="03:00"))
