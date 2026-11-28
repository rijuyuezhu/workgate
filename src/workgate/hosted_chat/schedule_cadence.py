"""Pinned v6.0.3 schedule cadence, calendar and timezone semantics."""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIN_INTERVAL_MINUTES = 5
_DAY_MS = 86_400_000


def _zone(value):
    if not isinstance(value, str) or not value or len(value) > 100:
        raise ValueError("Invalid timezone")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Unknown timezone") from exc


def _cron_field(source, lower, upper, *, weekday=False):
    if not re.fullmatch(r"[0-9*,/-]+", source):
        raise ValueError("Invalid cron field")
    items = set()
    for section in source.split(","):
        parts = section.split("/")
        if len(parts) > 2 or not parts[0]:
            raise ValueError("Invalid cron segment")
        if len(parts) == 2 and not parts[1].isascii():
            raise ValueError("Invalid cron step")
        if len(parts) == 2 and not parts[1].isdigit():
            raise ValueError("Invalid cron step")
        step = int(parts[1]) if len(parts) == 2 else 1
        if step <= 0 or step > upper:
            raise ValueError("Invalid cron step")
        expression = parts[0]
        if expression == "*":
            start, end = lower, upper
        elif "-" in expression:
            start, end = (int(v) for v in expression.split("-", 1))
        else:
            start = int(expression)
            end = upper if len(parts) == 2 else start
        if start < lower or end > upper or end < start:
            raise ValueError("Invalid cron range")
        for value in range(start, end + 1, step):
            items.add(0 if weekday and value == 7 else value)
    return items


def _parse_cadence(value):
    if not isinstance(value, dict):
        raise ValueError("Invalid schedule cadence")
    frequency = value.get("frequency")
    if frequency == "cron":
        if set(value) != {"frequency", "expression"}:
            raise ValueError("Invalid cron cadence fields")
        expression = value.get("expression")
        if not isinstance(expression, str) or len(expression) > 256:
            raise ValueError("Invalid cron expression")
        fields = expression.split()
        if len(fields) != 5:
            raise ValueError("Cron must have five fields")
        try:
            minute = _cron_field(fields[0], 0, 59)
            hour = _cron_field(fields[1], 0, 23)
            dom = _cron_field(fields[2], 1, 31)
            month = _cron_field(fields[3], 1, 12)
            dow = _cron_field(fields[4], 0, 7, weekday=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid cron expression") from exc
        return frequency, (
            minute,
            hour,
            dom,
            month,
            dow,
            fields[2] == "*",
            fields[4] == "*",
        )
    if not isinstance(frequency, str) or frequency not in {
        "hourly",
        "daily",
        "weekdays",
        "weekly",
    }:
        raise ValueError("Unsupported schedule frequency")
    if set(value) - {"frequency", "hour", "minute", "daysOfWeek"}:
        raise ValueError("Invalid cadence fields")
    hour, minute = value.get("hour"), value.get("minute")
    if (
        type(hour) is not int
        or not 0 <= hour <= 23
        or type(minute) is not int
        or not 0 <= minute <= 59
    ):
        raise ValueError("Invalid schedule time")
    days = value.get("daysOfWeek")
    if days is not None and (
        not isinstance(days, list)
        or not days
        or any(type(day) is not int or day not in range(7) for day in days)
    ):
        raise ValueError("Invalid weekdays")
    return frequency, (hour, minute, set(days or []))


def _matches(local, parsed):
    frequency, settings = parsed
    dow = (local.weekday() + 1) % 7
    if frequency == "hourly":
        return local.minute == settings[1]
    if frequency == "daily":
        return (local.hour, local.minute) == settings[:2]
    if frequency == "weekdays":
        return (
            dow in {1, 2, 3, 4, 5}
            and (local.hour, local.minute) == settings[:2]
        )
    if frequency == "weekly":
        return (
            dow in (settings[2] or {1})
            and (local.hour, local.minute) == settings[:2]
        )
    minutes, hours, dom, months, weekdays, dom_any, dow_any = settings
    day_match = local.day in dom
    weekday_match = dow in weekdays
    if dom_any:
        day_ok = weekday_match
    elif dow_any:
        day_ok = day_match
    else:
        day_ok = day_match or weekday_match
    return (
        local.minute in minutes
        and local.hour in hours
        and local.month in months
        and day_ok
    )


def next_run(cadence, timezone, *, after_ms, min_interval_minutes=5):
    """Find the next real instant, skipping DST gaps and keeping repeated folds."""
    parsed = _parse_cadence(cadence)
    zone = _zone(timezone)
    earliest = after_ms + min_interval_minutes * 60_000
    if parsed[0] == "cron":
        found = _next_sparse_cron(parsed, zone, after_ms, earliest)
        if found is not None:
            return found
    else:
        initial = (after_ms // 60_000 + 1) * 60_000
        for timestamp in range(initial, initial + 15 * _DAY_MS, 60_000):
            if timestamp < earliest:
                continue
            local = datetime.fromtimestamp(timestamp / 1000, tz=UTC).astimezone(
                zone
            )
            if _matches(local, parsed):
                return timestamp
    raise ValueError("Schedule has no occurrence within one Gregorian cycle")


def _next_sparse_cron(parsed, zone, after_ms, earliest):
    minutes, hours, days, months, weekdays, dom_any, dow_any = parsed[1]
    start = (
        datetime.fromtimestamp(after_ms / 1000, tz=UTC).astimezone(zone).date()
    )
    for offset in range(146_098):
        try:
            day = start + timedelta(days=offset)
        except OverflowError:
            break
        if day.month not in months:
            continue
        weekday = (day.weekday() + 1) % 7
        if dom_any:
            day_ok = weekday in weekdays
        elif dow_any:
            day_ok = day.day in days
        else:
            day_ok = day.day in days or weekday in weekdays
        if not day_ok:
            continue
        candidates = []
        for hour in hours:
            for minute in minutes:
                for fold in (0, 1):
                    local = datetime(
                        day.year,
                        day.month,
                        day.day,
                        hour,
                        minute,
                        tzinfo=zone,
                        fold=fold,
                    )
                    timestamp = int(local.timestamp() * 1000)
                    if timestamp <= after_ms or timestamp < earliest:
                        continue
                    # Reject nonexistent local times in spring-forward gaps.
                    actual = datetime.fromtimestamp(
                        timestamp / 1000, tz=UTC
                    ).astimezone(zone)
                    if (
                        actual.year,
                        actual.month,
                        actual.day,
                        actual.hour,
                        actual.minute,
                    ) != (day.year, day.month, day.day, hour, minute):
                        continue
                    candidates.append(timestamp)
        if candidates:
            return min(candidates)
    return None


def validate_cadence_interval(cadence):
    """Reject cron expressions with occurrences closer than the 5-minute limit.

    This validates the cadence itself; next_run can then advance from a due
    time without padding each occurrence by the scheduler's polling latency.
    """
    parsed = _parse_cadence(cadence)
    if parsed[0] != "cron":
        return
    minutes, hours, *_ = parsed[1]
    assert isinstance(minutes, set) and isinstance(hours, set)
    times = sorted(hour * 60 + minute for hour in hours for minute in minutes)
    minimum = MIN_INTERVAL_MINUTES
    if any(
        right - left < minimum
        for left, right in zip(times, times[1:], strict=False)
    ):
        raise ValueError("Schedule interval must be at least five minutes")
    if 1440 + times[0] - times[-1] >= minimum:
        return
    # Inter-day gaps matter only when the cron day predicate matches both
    # adjacent dates. A Monday-only 23:59/00:00 cadence is not too frequent.
    day = datetime(2000, 1, 1, min(hours), min(minutes), tzinfo=UTC)
    for offset in range(146_097):
        current = day + timedelta(days=offset)
        if _matches(current, parsed) and _matches(
            current + timedelta(days=1), parsed
        ):
            raise ValueError("Schedule interval must be at least five minutes")
