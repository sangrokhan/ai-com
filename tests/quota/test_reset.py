from datetime import UTC, datetime, timedelta

from aicom.quota.reset import fallback_backoff, parse_reset_at

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def test_parses_pipe_delimited_epoch_seconds() -> None:
    epoch = int((NOW + timedelta(hours=3)).timestamp())
    assert parse_reset_at(f"Claude AI usage limit reached|{epoch}", now=NOW) == NOW + timedelta(
        hours=3
    )


def test_parses_iso8601_timestamp() -> None:
    assert parse_reset_at("limit reached, resets at 2026-08-10T15:30:00Z", now=NOW) == datetime(
        2026, 8, 10, 15, 30, tzinfo=UTC
    )


def test_parses_clock_time_and_rolls_to_tomorrow_when_already_past() -> None:
    assert parse_reset_at("resets at 3pm", now=NOW) == datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
    later = datetime(2026, 8, 10, 20, 0, tzinfo=UTC)
    assert parse_reset_at("resets at 3pm", now=later) == datetime(
        2026, 8, 11, 15, 0, tzinfo=UTC
    )


def test_returns_none_when_no_time_present() -> None:
    assert parse_reset_at("usage limit reached", now=NOW) is None


def test_past_epoch_is_rejected() -> None:
    epoch = int((NOW - timedelta(hours=1)).timestamp())
    assert parse_reset_at(f"limit|{epoch}", now=NOW) is None


def test_fallback_backoff_steps_then_caps_at_one_hour() -> None:
    assert fallback_backoff(0, now=NOW) == NOW + timedelta(minutes=15)
    assert fallback_backoff(1, now=NOW) == NOW + timedelta(minutes=30)
    assert fallback_backoff(2, now=NOW) == NOW + timedelta(hours=1)
    assert fallback_backoff(9, now=NOW) == NOW + timedelta(hours=1)
