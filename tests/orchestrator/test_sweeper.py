import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from aicom.domain.enums import RunStatus, TaskStatus
from aicom.domain.views import ApprovalView
from aicom.gate.service import request_approval
from aicom.notify.fake import FakeNotifier
from aicom.orchestrator.sweeper import Sweeper
from aicom.orchestrator.worker import MAX_ATTEMPTS
from aicom.store.approvals import attach_slack_ref
from aicom.store.models import Approval, Run, Task
from aicom.store.runs import claim_next_queued, touch_heartbeat
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


@dataclass
class _NotifierThatFailsFor(FakeNotifier):
    """FakeNotifier whose send_reminder raises for one chosen approval id."""

    failing_approval_id: uuid.UUID | None = field(default=None)

    def send_reminder(self, view: ApprovalView, stage: int) -> None:
        if view.approval_id == self.failing_approval_id:
            raise RuntimeError("notifier blew up for this approval")
        super().send_reminder(view, stage)


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
    assert refreshed.attempt == 2  # recovery consumed an attempt


def test_a_failing_notifier_does_not_starve_other_pending_reminders(
    sessions: sessionmaker[Session], session: Session
) -> None:
    ok_approval_1 = _parked(session)
    bad_approval = _parked(session)
    ok_approval_2 = _parked(session)

    notifier = _NotifierThatFailsFor(failing_approval_id=bad_approval.id)
    sweeper = Sweeper(sessions, notifier)

    sent = sweeper.sweep_reminders(NOW + timedelta(minutes=31))

    assert sent == 2  # only the two that actually succeeded are counted
    reminded_ids = {view.approval_id for view, _stage in notifier.reminders}
    assert reminded_ids == {ok_approval_1.id, ok_approval_2.id}

    session.expire_all()
    ok_1 = session.get(Approval, ok_approval_1.id)
    ok_2 = session.get(Approval, ok_approval_2.id)
    bad = session.get(Approval, bad_approval.id)
    assert ok_1 is not None and ok_1.remind_count == 1
    assert ok_2 is not None and ok_2.remind_count == 1
    assert bad is not None and bad.remind_count == 0  # untouched, will retry next sweep
    assert bad.status.value == "pending"

    # a later sweep, with the bad approval no longer failing, picks it back up
    notifier.failing_approval_id = None
    sent_again = sweeper.sweep_reminders(NOW + timedelta(hours=5))
    assert sent_again == 3
    assert {view.approval_id for view, _stage in notifier.reminders[2:]} == {
        ok_approval_1.id,
        ok_approval_2.id,
        bad_approval.id,
    }


def _running_run_with_attempt(session: Session, *, attempt: int) -> Run:
    agent = make_agent(session)
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="t", goal="g")
    session.add(task)
    session.flush()
    session.add(Run(id=uuid.uuid4(), task_id=task.id, attempt=attempt))
    session.commit()
    run = claim_next_queued(session, worker_id="dead-worker", now=NOW)
    assert run is not None
    touch_heartbeat(session, run.id, NOW)
    session.commit()
    return run


def test_recovery_below_attempt_cap_requeues_with_incremented_attempt(
    sessions: sessionmaker[Session], session: Session
) -> None:
    run = _running_run_with_attempt(session, attempt=MAX_ATTEMPTS - 2)

    recovered = Sweeper(sessions, FakeNotifier()).recover_stale_runs(
        NOW + timedelta(minutes=10), stale_after=timedelta(minutes=5)
    )
    assert recovered == 1

    session.expire_all()
    refreshed = session.get(Run, run.id)
    assert refreshed is not None
    assert refreshed.status is RunStatus.QUEUED
    assert refreshed.attempt == MAX_ATTEMPTS - 1
    assert refreshed.task.status is TaskStatus.OPEN


def test_recovery_at_attempt_cap_fails_the_run_and_reports_once(
    sessions: sessionmaker[Session], session: Session
) -> None:
    run = _running_run_with_attempt(session, attempt=MAX_ATTEMPTS - 1)

    notifier = FakeNotifier()
    recovered = Sweeper(sessions, notifier).recover_stale_runs(
        NOW + timedelta(minutes=10), stale_after=timedelta(minutes=5)
    )
    assert recovered == 0  # not requeued — it was given up on, not recovered

    session.expire_all()
    refreshed = session.get(Run, run.id)
    assert refreshed is not None
    assert refreshed.status is RunStatus.FAILED
    assert refreshed.attempt == MAX_ATTEMPTS
    assert refreshed.task.status is TaskStatus.FAILED

    assert len(notifier.reports) == 1
    report = notifier.reports[0]
    assert report.run_id == run.id
    assert report.status is RunStatus.FAILED
