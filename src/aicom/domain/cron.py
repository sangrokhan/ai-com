"""Pure cron arithmetic.

Kept free of I/O and of clock reads so the calendar logic — the part most
likely to be subtly wrong — is testable without a database. The caller
always supplies the moment to compute from.
"""

from datetime import UTC, datetime
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, croniter


class InvalidCron(Exception):
    """The expression or the timezone name cannot be used."""


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidCron(f"unknown timezone: {timezone!r}") from exc


def validate_cron(cron: str, timezone: str) -> None:
    """Raise InvalidCron if this pair could not be scheduled."""
    _zone(timezone)
    if not croniter.is_valid(cron):
        raise InvalidCron(f"invalid cron expression: {cron!r}")


def next_fire(cron: str, timezone: str, after: datetime) -> datetime:
    """The first occurrence strictly after `after`, returned in UTC.

    The expression is evaluated in the schedule's own timezone, so "09:00"
    stays 09:00 local across a DST change rather than drifting by an hour.
    """
    validate_cron(cron, timezone)
    moment = after if after.tzinfo is not None else after.replace(tzinfo=UTC)
    local = moment.astimezone(_zone(timezone))
    try:
        upcoming = cast(datetime, croniter(cron, local).get_next(datetime))
    except CroniterBadCronError as exc:
        raise InvalidCron(f"invalid cron expression: {cron!r}") from exc
    return upcoming.astimezone(UTC)
