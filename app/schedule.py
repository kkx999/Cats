from calendar import monthrange
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.models import ScheduledTask, ScheduleKind


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def local_to_utc(value: str, timezone_name: str) -> datetime:
    local = datetime.fromisoformat(value)
    return local.replace(tzinfo=ZoneInfo(timezone_name)).astimezone(UTC)


def utc_to_local_input(value: datetime | None, timezone_name: str) -> str:
    if not value:
        return ""
    return ensure_utc(value).astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%dT%H:%M")


def next_occurrence(task: ScheduledTask, after: datetime | None = None) -> datetime | None:
    current = ensure_utc(after or task.next_run_at or task.start_at)
    kind = ScheduleKind(task.schedule_kind)
    config = task.schedule_config or {}

    if kind is ScheduleKind.ONCE:
        return None
    if kind is ScheduleKind.INTERVAL:
        amount = max(1, int(config.get("interval", 1)))
        unit = config.get("unit", "hours")
        seconds = amount * {"minutes": 60, "hours": 3600, "days": 86400}.get(unit, 3600)
        candidate = current + timedelta(seconds=seconds)
    else:
        zone = ZoneInfo(task.timezone)
        local = current.astimezone(zone)
        if kind is ScheduleKind.DAILY:
            candidate_local = local + timedelta(days=1)
        elif kind is ScheduleKind.WEEKLY:
            candidate_local = local + timedelta(days=7)
        else:
            year = local.year + (1 if local.month == 12 else 0)
            month = 1 if local.month == 12 else local.month + 1
            configured_day = int(
                config.get("day_of_month", ensure_utc(task.start_at).astimezone(zone).day)
            )
            day = min(configured_day, monthrange(year, month)[1])
            candidate_local = local.replace(year=year, month=month, day=day)
        candidate = candidate_local.astimezone(UTC)

    if task.end_at and candidate > ensure_utc(task.end_at):
        return None
    return candidate


def next_future_occurrence(
    task: ScheduledTask,
    scheduled_for: datetime,
    now: datetime | None = None,
) -> datetime | None:
    """Return the first future run while coalescing all missed occurrences.

    A task that was offline for a month is sent once when the worker recovers,
    then advances directly to its next future slot instead of flooding the chat.
    """

    now = ensure_utc(now or datetime.now(UTC))
    candidate = next_occurrence(task, scheduled_for)
    for _ in range(100_000):
        if candidate is None or candidate > now:
            return candidate
        candidate = next_occurrence(task, candidate)
    raise RuntimeError("无法在合理范围内计算下一次发送时间")


def describe_schedule(task: ScheduledTask) -> str:
    labels = {
        ScheduleKind.ONCE.value: "仅一次",
        ScheduleKind.DAILY.value: "每天",
        ScheduleKind.WEEKLY.value: "每周",
        ScheduleKind.MONTHLY.value: "每月",
        ScheduleKind.INTERVAL.value: "自定义间隔",
    }
    if task.schedule_kind != ScheduleKind.INTERVAL.value:
        return labels.get(task.schedule_kind, task.schedule_kind)
    config = task.schedule_config or {}
    units = {"minutes": "分钟", "hours": "小时", "days": "天"}
    return f"每 {config.get('interval', 1)} {units.get(config.get('unit'), '小时')}"
