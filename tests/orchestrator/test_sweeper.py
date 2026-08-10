import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from aicom.domain.enums import RunStatus
from aicom.gate.service import request_approval
from aicom.notify.fake import FakeNotifier
from aicom.orchestrator.sweeper import Sweeper
from aicom.store.approvals import attach_slack_ref
from aicom.store.models import Approval, Run, Task
from aicom.store.runs import claim_next_queued, touch_heartbeat
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _parked(session: Session) -> Approval:
    agent = make_agent(session)
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="t", goal="g")
    session.add(task)
    session.flush()
    session.add(Run(id=uuid.uuid4(), task_id=task.id, attempt=1))
    session.commit()
    run = claim_next_queued(session, worker_id="w1", now=NOW)
    assert run is not None
    result = request_approval(
        session, run_id=run.id, kind="spend", proposal="p", payload={}, now=NOW
    )
    session.commit()
    approval = session.get(Approval, result.approval_id)
    assert approval is not None
    return approval


def test_reminders_escalate_through_stages_and_never_deny(
    sessions: sessionmaker[Session], session: Session
) -> None:
    approval = _parked(session)
    notifier = FakeNotifier()
    sweeper = Sweeper(sessions, notifier)

    assert sweeper.sweep_reminders(NOW) == 0  # first reminder not due yet
    assert sweeper.sweep_reminders(NOW + timedelta(minutes=31)) == 1
    assert sweeper.sweep_reminders(NOW + timedelta(minutes=40)) == 0  # backed off
    assert sweeper.sweep_reminders(NOW + timedelta(hours=5)) == 1
    assert sweeper.sweep_reminders(NOW + timedelta(days=2)) == 1

    assert [stage for _v, stage in notifier.reminders] == [1, 2, 3]

    session.expire_all()
    refreshed = session.get(Approval, approval.id)
    assert refreshed is not None
    assert refreshed.status.value == "pending"  # never auto-denied
    assert refreshed.remind_count == 3


def test_reminder_view_carries_slack_thread_ref_for_stage_1_threading(
    sessions: sessionmaker[Session], session: Session
) -> None:
    approval = _parked(session)
    attach_slack_ref(session, approval.id, "C123", "1700000000.000100")
    session.commit()

    notifier = FakeNotifier()
    sweeper = Sweeper(sessions, notifier)

    assert sweeper.sweep_reminders(NOW + timedelta(minutes=31)) == 1

    assert len(notifier.reminders) == 1
    view, stage = notifier.reminders[0]
    assert stage == 1
    assert view.slack_channel == "C123"
    assert view.slack_ts == "1700000000.000100"


def test_stale_running_run_returns_to_queued(
    sessions: sessionmaker[Session], session: Session
) -> None:
    agent = make_agent(session)
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="t", goal="g")
    session.add(task)
    session.flush()
    session.add(Run(id=uuid.uuid4(), task_id=task.id, attempt=1))
    session.commit()
    run = claim_next_queued(session, worker_id="dead-worker", now=NOW)
    assert run is not None
    touch_heartbeat(session, run.id, NOW)
    session.commit()

    recovered = Sweeper(sessions, FakeNotifier()).recover_stale_runs(
        NOW + timedelta(minutes=10), stale_after=timedelta(minutes=5)
    )
    assert recovered == 1

    session.expire_all()
    refreshed = session.get(Run, run.id)
    assert refreshed is not None
    assert refreshed.status is RunStatus.QUEUED
    assert refreshed.worker_id is None
