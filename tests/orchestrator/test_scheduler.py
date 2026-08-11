import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aicom.domain.enums import RunStatus
from aicom.notify.fake import FakeNotifier
from aicom.orchestrator.scheduler import Scheduler
from aicom.store.models import Run, Schedule, Task
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _schedule(
    session: Session,
    *,
    due: datetime,
    cron: str = "0 * * * *",
    enabled: bool = True,
) -> Schedule:
    agent = make_agent(session)
    schedule = Schedule(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name=f"s-{uuid.uuid4().hex[:6]}",
        cron=cron,
        timezone="UTC",
        title_template="Market scan",
        goal_template="Scan the market.",
        next_due_at=due,
        enabled=enabled,
    )
    session.add(schedule)
    session.commit()
    return schedule


def test_due_schedule_creates_one_task_and_one_queued_run(
    sessions: sessionmaker[Session], session: Session
) -> None:
    schedule = _schedule(session, due=NOW - timedelta(minutes=1))

    created = Scheduler(sessions, FakeNotifier()).tick(NOW)
    assert created == 1

    session.expire_all()
    tasks = list(session.scalars(select(Task).where(Task.schedule_id == schedule.id)))
    assert len(tasks) == 1
    assert tasks[0].title == "Market scan"
    assert tasks[0].goal == "Scan the market."
    assert tasks[0].agent_id == schedule.agent_id

    runs = list(session.scalars(select(Run).where(Run.task_id == tasks[0].id)))
    assert len(runs) == 1
    assert runs[0].status is RunStatus.QUEUED

    refreshed = session.get(Schedule, schedule.id)
    assert refreshed is not None
    assert refreshed.last_fired_at == NOW
    assert refreshed.next_due_at > NOW


def test_a_schedule_not_yet_due_does_not_fire(
    sessions: sessionmaker[Session], session: Session
) -> None:
    _schedule(session, due=NOW + timedelta(hours=1))
    assert Scheduler(sessions, FakeNotifier()).tick(NOW) == 0


def test_unfinished_previous_cycle_is_skipped_with_a_reason(
    sessions: sessionmaker[Session], session: Session
) -> None:
    schedule = _schedule(session, due=NOW - timedelta(minutes=1))
    scheduler = Scheduler(sessions, FakeNotifier())
    assert scheduler.tick(NOW) == 1

    # The first cycle's run is still queued, so the next firing must skip.
    session.expire_all()
    refreshed = session.get(Schedule, schedule.id)
    assert refreshed is not None
    later = refreshed.next_due_at + timedelta(seconds=1)

    assert scheduler.tick(later) == 0

    session.expire_all()
    refreshed = session.get(Schedule, schedule.id)
    assert refreshed is not None
    assert refreshed.last_skip_reason == "previous cycle unfinished"
    assert refreshed.last_skipped_at == later
    assert refreshed.next_due_at > later
    assert len(list(session.scalars(select(Task).where(Task.schedule_id == schedule.id)))) == 1


def test_a_long_overdue_schedule_fires_once_and_rejoins_the_normal_cadence(
    sessions: sessionmaker[Session], session: Session
) -> None:
    # Six hours overdue on an hourly schedule: one firing, not six.
    schedule = _schedule(session, due=NOW - timedelta(hours=6))

    assert Scheduler(sessions, FakeNotifier()).tick(NOW) == 1

    session.expire_all()
    tasks = list(session.scalars(select(Task).where(Task.schedule_id == schedule.id)))
    assert len(tasks) == 1

    refreshed = session.get(Schedule, schedule.id)
    assert refreshed is not None
    assert refreshed.next_due_at == datetime(2026, 8, 11, 13, 0, tzinfo=UTC)


def test_broken_cron_disables_the_schedule_and_notifies(
    sessions: sessionmaker[Session], session: Session
) -> None:
    schedule = _schedule(session, due=NOW - timedelta(minutes=1), cron="not a cron")
    notifier = FakeNotifier()

    assert Scheduler(sessions, notifier).tick(NOW) == 0

    session.expire_all()
    refreshed = session.get(Schedule, schedule.id)
    assert refreshed is not None
    assert refreshed.enabled is False
    assert len(notifier.notices) == 1
    assert schedule.name in notifier.notices[0]
    assert len(list(session.scalars(select(Task).where(Task.schedule_id == schedule.id)))) == 0
    assert (
        len(
            list(
                session.scalars(
                    select(Run).join(Task, Run.task_id == Task.id).where(
                        Task.schedule_id == schedule.id
                    )
                )
            )
        )
        == 0
    )


def test_a_disabled_agent_skips_without_disabling_the_schedule(
    sessions: sessionmaker[Session], session: Session
) -> None:
    schedule = _schedule(session, due=NOW - timedelta(minutes=1))
    schedule.agent.enabled = False
    session.commit()

    assert Scheduler(sessions, FakeNotifier()).tick(NOW) == 0

    session.expire_all()
    refreshed = session.get(Schedule, schedule.id)
    assert refreshed is not None
    assert refreshed.enabled is True
    assert refreshed.last_skip_reason == "agent disabled"
    # The clock advances on a skip too: this schedule must not accumulate an
    # overdue slot behind a disabled agent.
    assert refreshed.next_due_at > NOW


def test_losing_the_claim_race_does_not_double_fire(
    sessions: sessionmaker[Session],
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A due schedule whose slot is won by a competing scheduler instance:
    # claim_firing's conditional UPDATE observes a mismatch and reports
    # failure. Simulated deterministically (a genuine two-connection race is
    # not reproducible in a single-threaded test) by making the real
    # claim_firing behave exactly as it would on a lost race: it always
    # returns False. If the "if not claim_firing(...): return False" check
    # in scheduler.py were ever dropped, this schedule would still get
    # processed and this test would fail on the task-row assertion below —
    # that's what makes it a real regression test for the check, not just
    # for claim_firing's own return value.
    _schedule(session, due=NOW - timedelta(minutes=1))
    monkeypatch.setattr(
        "aicom.orchestrator.scheduler.claim_firing", lambda *args, **kwargs: False
    )

    assert Scheduler(sessions, FakeNotifier()).tick(NOW) == 0

    session.expire_all()
    assert len(list(session.scalars(select(Task)))) == 0


def test_one_broken_schedule_does_not_stop_the_others(
    sessions: sessionmaker[Session], session: Session
) -> None:
    _schedule(session, due=NOW - timedelta(minutes=1), cron="not a cron")
    good = _schedule(session, due=NOW - timedelta(minutes=1))

    assert Scheduler(sessions, FakeNotifier()).tick(NOW) == 1

    session.expire_all()
    assert len(list(session.scalars(select(Task).where(Task.schedule_id == good.id)))) == 1
