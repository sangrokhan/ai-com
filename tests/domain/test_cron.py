from datetime import UTC, datetime

import pytest

from aicom.domain.cron import InvalidCron, next_fire, validate_cron


def test_next_fire_returns_utc_for_a_local_daily_schedule() -> None:
    # 09:00 in Seoul (UTC+9, no DST) is 00:00 UTC.
    after = datetime(2026, 8, 11, 3, 0, tzinfo=UTC)
    assert next_fire("0 9 * * *", "Asia/Seoul", after) == datetime(
        2026, 8, 12, 0, 0, tzinfo=UTC
    )


def test_next_fire_is_strictly_after_the_given_moment() -> None:
    exactly_on_the_hour = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
    result = next_fire("0 9 * * *", "Asia/Seoul", exactly_on_the_hour)
    assert result > exactly_on_the_hour


def test_next_fire_accepts_a_naive_datetime_as_utc() -> None:
    naive = datetime(2026, 8, 11, 3, 0)  # noqa: DTZ001 -- deliberately naive; that's what this test covers
    assert next_fire("0 9 * * *", "Asia/Seoul", naive) == datetime(
        2026, 8, 12, 0, 0, tzinfo=UTC
    )


def test_dst_spring_forward_keeps_local_wall_clock_time() -> None:
    # New York moves to EDT on 2026-03-08. 09:00 local is 14:00 UTC before
    # the change and 13:00 UTC after it.
    before = next_fire("0 9 * * *", "America/New_York", datetime(2026, 3, 6, 20, 0, tzinfo=UTC))
    after = next_fire("0 9 * * *", "America/New_York", datetime(2026, 3, 8, 20, 0, tzinfo=UTC))
    assert before == datetime(2026, 3, 7, 14, 0, tzinfo=UTC)
    assert after == datetime(2026, 3, 9, 13, 0, tzinfo=UTC)


def test_dst_fall_back_keeps_local_wall_clock_time() -> None:
    # New York returns to EST on 2026-11-01.
    after = next_fire("0 9 * * *", "America/New_York", datetime(2026, 11, 1, 20, 0, tzinfo=UTC))
    assert after == datetime(2026, 11, 2, 14, 0, tzinfo=UTC)


def test_weekday_only_schedule_skips_the_weekend() -> None:
    # 2026-08-14 is a Friday; the next weekday firing is Monday the 17th.
    friday_evening = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    assert next_fire("0 9 * * 1-5", "Asia/Seoul", friday_evening) == datetime(
        2026, 8, 17, 0, 0, tzinfo=UTC
    )


@pytest.mark.parametrize("expression", ["", "not a cron", "* * * *", "99 * * * *"])
def test_invalid_expression_raises(expression: str) -> None:
    with pytest.raises(InvalidCron):
        validate_cron(expression, "Asia/Seoul")
    with pytest.raises(InvalidCron):
        next_fire(expression, "Asia/Seoul", datetime(2026, 8, 11, tzinfo=UTC))


def test_invalid_timezone_raises() -> None:
    with pytest.raises(InvalidCron):
        validate_cron("0 9 * * *", "Mars/Olympus_Mons")


def test_validate_accepts_a_good_expression() -> None:
    assert validate_cron("*/15 * * * *", "UTC") is None
