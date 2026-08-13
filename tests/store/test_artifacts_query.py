import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from aicom.store.artifacts_query import REPORT_FILENAME, recent_schedule_reports
from aicom.store.models import Artifact, Run, Schedule, Task
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def _schedule(session: Session, name: str = "beat") -> Schedule:
    agent = make_agent(session)
    schedule = Schedule(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name=f"{name}-{uuid.uuid4().hex[:6]}",
        cron="0 9 * * *",
        timezone="UTC",
        title_template="t",
        goal_template="g",
        next_due_at=NOW,
    )
    session.add(schedule)
    session.flush()
    return schedule


def _run_with_report(
    session: Session,
    schedule: Schedule,
    *,
    started_at: datetime | None,
    path: str = REPORT_FILENAME,
) -> Run:
    task = Task(
        id=uuid.uuid4(),
        agent_id=schedule.agent_id,
        title="t",
        goal="g",
        schedule_id=schedule.id,
    )
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1, started_at=started_at)
    session.add(run)
    session.flush()
    session.add(
        Artifact(id=uuid.uuid4(), run_id=run.id, kind="md", path=path, git_ref="abc")
    )
    session.flush()
    return run


def test_returns_reports_newest_first(session: Session) -> None:
    schedule = _schedule(session)
    older = _run_with_report(session, schedule, started_at=NOW - timedelta(days=2))
    newer = _run_with_report(session, schedule, started_at=NOW - timedelta(days=1))
    session.commit()

    found = recent_schedule_reports(session, schedule.id, limit=10)

    assert [run_id for run_id, _ in found] == [newer.id, older.id]
    assert all(path == REPORT_FILENAME for _, path in found)


def test_limit_is_respected(session: Session) -> None:
    schedule = _schedule(session)
    for day in range(5):
        _run_with_report(session, schedule, started_at=NOW - timedelta(days=day))
    session.commit()

    assert len(recent_schedule_reports(session, schedule.id, limit=2)) == 2


def test_another_schedules_reports_are_never_returned(session: Session) -> None:
    mine = _schedule(session, "mine")
    theirs = _schedule(session, "theirs")
    _run_with_report(session, theirs, started_at=NOW)
    session.commit()

    assert recent_schedule_reports(session, mine.id, limit=10) == []


def test_non_report_artifacts_are_ignored(session: Session) -> None:
    schedule = _schedule(session)
    _run_with_report(session, schedule, started_at=NOW, path="data.csv")
    session.commit()

    assert recent_schedule_reports(session, schedule.id, limit=10) == []


def test_a_run_that_never_started_sorts_last(session: Session) -> None:
    # ended_at would be null for a crashed worker; started_at is written on
    # claim, so only a run that never ran at all has none.
    schedule = _schedule(session)
    started = _run_with_report(session, schedule, started_at=NOW - timedelta(days=1))
    _run_with_report(session, schedule, started_at=None)
    session.commit()

    assert recent_schedule_reports(session, schedule.id, limit=1)[0][0] == started.id


def test_a_schedule_with_no_reports_returns_nothing(session: Session) -> None:
    schedule = _schedule(session)
    session.commit()

    assert recent_schedule_reports(session, schedule.id, limit=10) == []


def test_ties_in_started_at_break_deterministically_by_run_id(session: Session) -> None:
    # Two runs sharing the same started_at (e.g. claimed in the same batch) must
    # not depend on the database's arbitrary tie-break order, or the report
    # shown to a monitoring agent at the limit boundary could flip between
    # calls with no underlying change.
    schedule = _schedule(session)
    first = _run_with_report(session, schedule, started_at=NOW)
    second = _run_with_report(session, schedule, started_at=NOW)
    session.commit()

    expected = sorted([first.id, second.id], reverse=True)

    result_a = [run_id for run_id, _ in recent_schedule_reports(session, schedule.id, limit=10)]
    result_b = [run_id for run_id, _ in recent_schedule_reports(session, schedule.id, limit=10)]

    assert result_a == expected
    assert result_b == expected
