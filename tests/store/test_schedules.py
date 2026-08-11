import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from aicom.domain.enums import RunStatus, TaskStatus
from aicom.store.models import Run, Schedule, Task
from aicom.store.schedules import (
    claim_firing,
    disable,
    due_schedules,
    has_unfinished_cycle,
    record_skip,
)
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _schedule(session: Session, *, due: datetime, enabled: bool = True) -> Schedule:
    agent = make_agent(session)
    schedule = Schedule(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name=f"s-{uuid.uuid4().hex[:6]}",
        cron="0 * * * *",
        timezone="UTC",
        title_template="t",
        goal_template="g",
        next_due_at=due,
        enabled=enabled,
    )
    session.add(schedule)
    session.commit()
    return schedule


def test_due_schedules_returns_only_enabled_and_due(session: Session) -> None:
    due = _schedule(session, due=NOW - timedelta(minutes=1))
    _schedule(session, due=NOW + timedelta(hours=1))
    _schedule(session, due=NOW - timedelta(hours=1), enabled=False)

    assert [s.id for s in due_schedules(session, now=NOW)] == [due.id]


def test_claim_firing_succeeds_once_for_a_given_clock_value(session: Session) -> None:
    schedule = _schedule(session, due=NOW - timedelta(minutes=1))
    observed = schedule.next_due_at
    later = NOW + timedelta(hours=1)

    assert claim_firing(
        session, schedule.id, observed, later, last_fired_at=NOW
    ) is True
    session.commit()

    # A second scheduler that observed the same value must lose.
    assert claim_firing(session, schedule.id, observed, later, last_fired_at=NOW) is False


def test_unfinished_cycle_detected_for_queued_run(session: Session) -> None:
    schedule = _schedule(session, due=NOW)
    task = Task(
        id=uuid.uuid4(),
        agent_id=schedule.agent_id,
        title="t",
        goal="g",
        schedule_id=schedule.id,
    )
    session.add(task)
    session.flush()
    session.add(Run(id=uuid.uuid4(), task_id=task.id, attempt=1))
    session.commit()

    assert has_unfinished_cycle(session, schedule.id) is True


def test_finished_cycle_is_not_unfinished(session: Session) -> None:
    schedule = _schedule(session, due=NOW)
    task = Task(
        id=uuid.uuid4(),
        agent_id=schedule.agent_id,
        title="t",
        goal="g",
        schedule_id=schedule.id,
        status=TaskStatus.DONE,
    )
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1, status=RunStatus.SUCCEEDED)
    session.add(run)
    session.commit()

    assert has_unfinished_cycle(session, schedule.id) is False


def test_a_schedule_with_no_tasks_has_no_unfinished_cycle(session: Session) -> None:
    schedule = _schedule(session, due=NOW)
    assert has_unfinished_cycle(session, schedule.id) is False


def test_record_skip_advances_the_clock_and_stores_the_reason(session: Session) -> None:
    schedule = _schedule(session, due=NOW - timedelta(minutes=1))
    observed = schedule.next_due_at
    later = NOW + timedelta(hours=1)

    assert record_skip(
        session,
        schedule.id,
        observed,
        now=NOW,
        reason="previous cycle unfinished",
        next_due_at=later,
    ) is True
    session.commit()
    session.refresh(schedule)

    assert schedule.next_due_at == later
    assert schedule.last_skipped_at == NOW
    assert schedule.last_skip_reason == "previous cycle unfinished"
    assert schedule.last_fired_at is None
    # The observed value is gone, so a racing caller cannot also claim it.
    assert claim_firing(session, schedule.id, observed, later) is False


def test_record_skip_with_a_stale_observed_value_changes_nothing(session: Session) -> None:
    schedule = _schedule(session, due=NOW - timedelta(minutes=1))
    stale = schedule.next_due_at
    later = NOW + timedelta(hours=1)

    # Another instance already claimed (or skipped) this slot, moving the
    # clock on. Our observed value is now stale.
    assert claim_firing(session, schedule.id, stale, later, last_fired_at=NOW) is True
    session.commit()

    even_later = later + timedelta(hours=1)
    assert record_skip(
        session,
        schedule.id,
        stale,
        now=NOW,
        reason="previous cycle unfinished",
        next_due_at=even_later,
    ) is False
    session.commit()
    session.refresh(schedule)

    # Nothing changed: the earlier claim_firing's writes stand untouched.
    assert schedule.next_due_at == later
    assert schedule.last_skipped_at is None
    assert schedule.last_skip_reason is None
    assert schedule.last_fired_at == NOW


def test_disable_records_the_reason_and_stops_it_being_due(session: Session) -> None:
    schedule = _schedule(session, due=NOW - timedelta(minutes=1))

    assert disable(session, schedule.id, reason="invalid cron", now=NOW) is True
    session.commit()

    assert due_schedules(session, now=NOW) == []
    session.refresh(schedule)
    assert schedule.enabled is False
    assert schedule.last_skip_reason == "invalid cron"
