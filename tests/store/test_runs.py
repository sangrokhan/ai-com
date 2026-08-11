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


def test_heartbeat_from_a_non_owning_worker_does_not_update_the_row(session: Session) -> None:
    """After the sweeper recovers a run and a NEW worker claims it, the old
    worker's zombie heartbeat ticker must not keep stamping heartbeat_at --
    that would corrupt the new attempt's staleness signal."""
    run = _queued_run(session)
    claim_next_queued(session, worker_id="w2", now=NOW)
    session.commit()

    assert touch_heartbeat(session, run.id, NOW + timedelta(minutes=9), worker_id="w1") is False
    session.commit()

    session.expire_all()
    refreshed = session.get(Run, run.id)
    assert refreshed is not None
    assert refreshed.heartbeat_at == NOW

    # ...and the owning worker still can.
    assert touch_heartbeat(session, run.id, NOW + timedelta(minutes=9), worker_id="w2") is True
    session.commit()
    session.expire_all()
    refreshed = session.get(Run, run.id)
    assert refreshed is not None
    assert refreshed.heartbeat_at == NOW + timedelta(minutes=9)


def test_heartbeat_does_not_touch_a_run_that_left_running(session: Session) -> None:
    """The sign-off race can return a run to QUEUED while its executor is
    still exiting; the ticker must not stamp it."""
    run = _queued_run(session)
    claim_next_queued(session, worker_id="w1", now=NOW)
    session.commit()
    transition(session, run.id, RunStatus.RUNNING, RunStatus.QUEUED)
    session.commit()

    assert touch_heartbeat(session, run.id, NOW + timedelta(minutes=9), worker_id="w1") is False


def test_stale_running_runs_detected_by_heartbeat(session: Session) -> None:
    run = _queued_run(session)
    claim_next_queued(session, worker_id="w1", now=NOW)
    touch_heartbeat(session, run.id, NOW, worker_id="w1")
    session.commit()

    assert stale_running_runs(session, older_than=NOW - timedelta(minutes=1)) == []
    stale = stale_running_runs(session, older_than=NOW + timedelta(minutes=5))
    assert [r.id for r in stale] == [run.id]
