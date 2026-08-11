import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from aicom.store.models import Schedule, Task
from tests.store.test_models import make_agent


def test_schedule_roundtrip_with_defaults(session: Session) -> None:
    agent = make_agent(session)
    schedule = Schedule(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name="daily market scan",
        cron="0 9 * * 1-5",
        timezone="Asia/Seoul",
        title_template="Market scan",
        goal_template="Scan the market and report anything unusual.",
        next_due_at=datetime(2026, 8, 12, 0, 0, tzinfo=UTC),
    )
    session.add(schedule)
    session.flush()

    loaded = session.scalar(select(Schedule).where(Schedule.id == schedule.id))
    assert loaded is not None
    assert loaded.enabled is True
    assert loaded.last_fired_at is None
    assert loaded.last_skipped_at is None
    assert loaded.last_skip_reason is None
    assert loaded.agent.id == agent.id


def test_task_links_back_to_its_schedule(session: Session) -> None:
    agent = make_agent(session)
    schedule = Schedule(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name="hourly",
        cron="0 * * * *",
        timezone="UTC",
        title_template="t",
        goal_template="g",
        next_due_at=datetime(2026, 8, 11, tzinfo=UTC),
    )
    session.add(schedule)
    session.flush()

    task = Task(
        id=uuid.uuid4(), agent_id=agent.id, title="t", goal="g", schedule_id=schedule.id
    )
    session.add(task)
    session.flush()

    assert task.schedule_id == schedule.id


def test_manual_task_has_no_schedule_link(session: Session) -> None:
    agent = make_agent(session)
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="t", goal="g")
    session.add(task)
    session.flush()

    assert task.schedule_id is None


def test_old_schedule_string_column_is_gone(session: Session) -> None:
    # Recurrence now lives in its own entity; the reserved column must not
    # linger, or two places would claim to define the same thing.
    assert "schedule" not in Task.__table__.columns
