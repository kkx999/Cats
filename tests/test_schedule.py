from datetime import UTC, datetime

from app.models import ScheduledTask
from app.schedule import local_to_utc, next_future_occurrence, next_occurrence


def task(kind: str, start: datetime, config: dict | None = None) -> ScheduledTask:
    return ScheduledTask(
        owner_id=1,
        chat_id=-1001,
        title="test",
        schedule_kind=kind,
        schedule_config=config or {},
        timezone="Asia/Shanghai",
        start_at=start,
        next_run_at=start,
    )


def test_local_time_converts_to_utc() -> None:
    result = local_to_utc("2026-08-26T20:00", "Asia/Shanghai")
    assert result == datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def test_once_has_no_next_occurrence() -> None:
    start = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    assert next_occurrence(task("once", start)) is None


def test_daily_preserves_local_wall_time() -> None:
    start = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    assert next_occurrence(task("daily", start)) == datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def test_interval_minutes() -> None:
    start = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    result = next_occurrence(task("interval", start, {"interval": 15, "unit": "minutes"}))
    assert result == datetime(2026, 8, 26, 12, 15, tzinfo=UTC)


def test_monthly_clamps_last_day() -> None:
    start = datetime(2027, 1, 31, 12, 0, tzinfo=UTC)
    item = task("monthly", start, {"day_of_month": 31})
    february = next_occurrence(item)
    assert february == datetime(2027, 2, 28, 12, 0, tzinfo=UTC)
    assert next_occurrence(item, february) == datetime(2027, 3, 31, 12, 0, tzinfo=UTC)


def test_end_date_stops_recurrence() -> None:
    start = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    item = task("daily", start)
    item.end_at = datetime(2026, 8, 26, 20, 0, tzinfo=UTC)
    assert next_occurrence(item) is None


def test_missed_daily_runs_are_coalesced() -> None:
    start = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 8, 26, 0, 0, tzinfo=UTC)
    item = task("daily", start)
    assert next_future_occurrence(item, start, now) == datetime(2026, 8, 27, 0, 0, tzinfo=UTC)


def test_missed_once_task_has_no_second_run() -> None:
    start = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 8, 26, 0, 0, tzinfo=UTC)
    assert next_future_occurrence(task("once", start), start, now) is None
