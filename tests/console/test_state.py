import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from aicom.console.state import build_snapshot
from aicom.domain.enums import ApprovalKind, RunStatus
from aicom.store.models import Approval, Event, Run, SystemState, Task
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _task_with_run(session: Session, agent_id: uuid.UUID, status: RunStatus) -> Run:
    task = Task(id=uuid.uuid4(), agent_id=agent_id, title="t", goal="g")
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1, status=status)
    session.add(run)
    session.flush()
    return run


def _task_with_run_at(
    session: Session,
    agent_id: uuid.UUID,
    status: RunStatus,
    *,
    started_at: datetime | None,
    ended_at: datetime | None,
) -> Run:
    task = Task(id=uuid.uuid4(), agent_id=agent_id, title="t", goal="g")
    session.add(task)
    session.flush()
    run = Run(
        id=uuid.uuid4(),
        task_id=task.id,
        attempt=1,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
    )
    session.add(run)
    session.flush()
    return run


def test_agent_with_no_runs_is_idle(session: Session) -> None:
    agent = make_agent(session)
    session.commit()

    snapshot = build_snapshot(session, now=NOW)

    view = next(a for a in snapshot.agents if a.agent_id == str(agent.id))
    assert view.status == "idle"
    assert view.run_id is None
    assert view.activity is None
    assert view.name == agent.name


def test_running_agent_is_working(session: Session) -> None:
    agent = make_agent(session)
    run = _task_with_run(session, agent.id, RunStatus.RUNNING)
    session.commit()

    view = next(
        a for a in build_snapshot(session, now=NOW).agents if a.agent_id == str(agent.id)
    )
    assert view.status == "working"
    assert view.run_id == str(run.id)


def test_awaiting_approval_outranks_everything(session: Session) -> None:
    agent = make_agent(session)
    _task_with_run(session, agent.id, RunStatus.RUNNING)
    _task_with_run(session, agent.id, RunStatus.AWAITING_APPROVAL)
    session.commit()

    view = next(
        a for a in build_snapshot(session, now=NOW).agents if a.agent_id == str(agent.id)
    )
    assert view.status == "waiting"


def test_queued_run_while_globally_paused_shows_paused(session: Session) -> None:
    agent = make_agent(session)
    _task_with_run(session, agent.id, RunStatus.QUEUED)
    session.add(SystemState(id=1, llm_paused_until=NOW + timedelta(hours=1)))
    session.commit()

    snapshot = build_snapshot(session, now=NOW)
    view = next(a for a in snapshot.agents if a.agent_id == str(agent.id))
    assert view.status == "paused"
    assert snapshot.paused_until is not None


def test_expired_pause_is_not_paused(session: Session) -> None:
    agent = make_agent(session)
    _task_with_run(session, agent.id, RunStatus.QUEUED)
    session.add(SystemState(id=1, llm_paused_until=NOW - timedelta(hours=1)))
    session.commit()

    snapshot = build_snapshot(session, now=NOW)
    view = next(a for a in snapshot.agents if a.agent_id == str(agent.id))
    assert view.status != "paused"
    assert snapshot.paused_until is None


def test_latest_terminal_run_failed_shows_failed(session: Session) -> None:
    agent = make_agent(session)
    _task_with_run(session, agent.id, RunStatus.FAILED)
    session.commit()

    view = next(
        a for a in build_snapshot(session, now=NOW).agents if a.agent_id == str(agent.id)
    )
    assert view.status == "failed"


def test_activity_line_comes_from_the_running_runs_latest_event(
    session: Session,
) -> None:
    agent = make_agent(session)
    run = _task_with_run(session, agent.id, RunStatus.RUNNING)
    session.add(
        Event(
            id=uuid.uuid4(),
            run_id=run.id,
            seq=0,
            type="assistant",
            payload={"message": {"content": []}},
        )
    )
    session.add(
        Event(
            id=uuid.uuid4(),
            run_id=run.id,
            seq=1,
            type="tool_use",
            payload={"name": "WebSearch"},
        )
    )
    session.commit()

    view = next(
        a for a in build_snapshot(session, now=NOW).agents if a.agent_id == str(agent.id)
    )
    assert view.activity is not None
    assert "WebSearch" in view.activity


def test_pending_approvals_are_counted(session: Session) -> None:
    agent = make_agent(session)
    run = _task_with_run(session, agent.id, RunStatus.AWAITING_APPROVAL)
    session.add(
        Approval(
            id=uuid.uuid4(),
            run_id=run.id,
            kind=ApprovalKind.SPEND,
            proposal="p",
            payload={},
            nonce=uuid.uuid4().hex,
        )
    )
    session.commit()

    assert build_snapshot(session, now=NOW).pending_approvals == 1


def test_disabled_agents_are_excluded(session: Session) -> None:
    agent = make_agent(session)
    agent.enabled = False
    session.commit()

    assert build_snapshot(session, now=NOW).agents == []


def test_newer_success_with_null_ended_at_is_not_reported_failed(
    session: Session,
) -> None:
    # Older FAILED run has a stamped ended_at; the newer SUCCEEDED run is a
    # crash-free finish whose ended_at hasn't been written yet. Ordering by
    # ended_at would rank the stamped FAILED row above the un-stamped, truly
    # newer SUCCEEDED row and misreport "failed". started_at distinguishes them.
    agent = make_agent(session)
    _task_with_run_at(
        session,
        agent.id,
        RunStatus.FAILED,
        started_at=NOW - timedelta(hours=2),
        ended_at=NOW - timedelta(hours=1, minutes=50),
    )
    _task_with_run_at(
        session,
        agent.id,
        RunStatus.SUCCEEDED,
        started_at=NOW - timedelta(minutes=10),
        ended_at=None,
    )
    session.commit()

    view = next(
        a for a in build_snapshot(session, now=NOW).agents if a.agent_id == str(agent.id)
    )
    assert view.status != "failed"


def test_newer_crash_failure_with_null_ended_at_is_reported_failed(
    session: Session,
) -> None:
    # Older SUCCEEDED run has a stamped ended_at; the newer FAILED run is a
    # crash whose ended_at never got written. Ordering by ended_at would rank
    # the stamped SUCCEEDED row above the un-stamped, truly newer FAILED row
    # and mask the real failure. started_at distinguishes them.
    agent = make_agent(session)
    _task_with_run_at(
        session,
        agent.id,
        RunStatus.SUCCEEDED,
        started_at=NOW - timedelta(hours=2),
        ended_at=NOW - timedelta(hours=1, minutes=50),
    )
    _task_with_run_at(
        session,
        agent.id,
        RunStatus.FAILED,
        started_at=NOW - timedelta(minutes=10),
        ended_at=None,
    )
    session.commit()

    view = next(
        a for a in build_snapshot(session, now=NOW).agents if a.agent_id == str(agent.id)
    )
    assert view.status == "failed"


def test_snapshot_is_stable_for_unchanged_data(session: Session) -> None:
    make_agent(session)
    session.commit()

    first = build_snapshot(session, now=NOW)
    second = build_snapshot(session, now=NOW)
    assert first == second
