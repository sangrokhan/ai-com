import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from aicom.domain.enums import RunStatus
from aicom.store.models import Run, Task
from aicom.store.runs import (
    claim_next_queued,
    stale_running_runs,
    touch_heartbeat,
    transition,
)
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _queued_run(session: Session) -> Run:
    agent = make_agent(session)
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="t", goal="g")
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1)
    session.add(run)
    session.commit()
    return run


def test_claim_marks_running_and_is_exclusive(session: Session) -> None:
    run = _queued_run(session)

    claimed = claim_next_queued(session, worker_id="w1", now=NOW)
    session.commit()
    assert claimed is not None and claimed.id == run.id
    assert claimed.status is RunStatus.RUNNING
    assert claimed.worker_id == "w1"
    assert claimed.started_at == NOW

    again = claim_next_queued(session, worker_id="w2", now=NOW)
    assert again is None


def test_transition_is_conditional_on_current_status(session: Session) -> None:
    run = _queued_run(session)
    assert transition(session, run.id, RunStatus.QUEUED, RunStatus.RUNNING) is True
    session.commit()
    # second attempt from the same expected state must not apply
    assert transition(session, run.id, RunStatus.QUEUED, RunStatus.RUNNING) is False


def test_transition_rejects_illegal_pairs(session: Session) -> None:
    import pytest

    from aicom.domain.transitions import IllegalTransition

    run = _queued_run(session)
    with pytest.raises(IllegalTransition):
        transition(session, run.id, RunStatus.QUEUED, RunStatus.SUCCEEDED)


def test_stale_running_runs_detected_by_heartbeat(session: Session) -> None:
    run = _queued_run(session)
    claim_next_queued(session, worker_id="w1", now=NOW)
    touch_heartbeat(session, run.id, NOW)
    session.commit()

    assert stale_running_runs(session, older_than=NOW - timedelta(minutes=1)) == []
    stale = stale_running_runs(session, older_than=NOW + timedelta(minutes=5))
    assert [r.id for r in stale] == [run.id]
