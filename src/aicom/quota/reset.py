import re
from datetime import UTC, datetime, timedelta

_EPOCH = re.compile(r"\|(\d{9,11})\b")
_ISO = re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)(Z|[+-]\d{2}:?\d{2})?")
_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)

_BACKOFF_STEPS = (timedelta(minutes=15), timedelta(minutes=30), timedelta(hours=1))


def parse_reset_at(text: str, *, now: datetime) -> datetime | None:
    """Extract when the account limit resets. Returns None if unknown or already past."""
    for candidate in (_from_epoch(text), _from_iso(text), _from_clock(text, now)):
        if candidate is not None and candidate > now:
            return candidate
    return None


def _from_epoch(text: str) -> datetime | None:
    match = _EPOCH.search(text)
    if not match:
        return None
    return datetime.fromtimestamp(int(match.group(1)), tz=UTC)


def _from_iso(text: str) -> datetime | None:
    match = _ISO.search(text)
    if not match:
        return None
    raw = match.group(1).replace(" ", "T")
    suffix = match.group(2) or "Z"
    try:
        return datetime.fromisoformat(raw + suffix.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _from_clock(text: str, now: datetime) -> datetime | None:
    match = _CLOCK.search(text)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    minute = int(match.group(2) or 0)
    if match.group(3).lower() == "pm":
        hour += 12
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def fallback_backoff(consecutive: int, *, now: datetime) -> datetime:
    """Used when the reset time cannot be parsed. Never hammer the account."""
    index = min(max(consecutive, 0), len(_BACKOFF_STEPS) - 1)
    return now + _BACKOFF_STEPS[index]
