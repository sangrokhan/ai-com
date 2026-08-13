import logging
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aicom.domain.enums import RunStatus
from aicom.gate import server as gate_server
from aicom.store.models import Approval, Run, Task
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _queued_run(session: Session) -> Run:
    """A QUEUED (not RUNNING) run: request_approval's transition() step
    fails for it, exactly like a run that raced out from under the gate
    call -- the cleanest reproducible way to make approval recording fail
    without actually breaking the DB connection."""
    agent = make_agent(session)
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="t", goal="g")
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1)
    session.add(run)
    session.commit()
    assert run.status is RunStatus.QUEUED
    return run


def test_failed_approval_raises_instead_of_reporting_success(
    sessions: sessionmaker[Session],
    session: Session,  # pulls in truncation teardown
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A gate tool call whose approval cannot be recorded must report an
    error to the model, not the terminate-and-wait success message -- and
    must leave no run parked in a half-state (no dangling approval row, no
    run stuck outside its original status)."""
    run = _queued_run(session)
    monkeypatch.setattr(gate_server, "_sessions", sessions)
    monkeypatch.setenv("AICOM_RUN_ID", str(run.id))

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(gate_server.ApprovalRecordingFailed) as exc_info,
    ):
        gate_server.request_approval_tool(kind="spend", proposal="p", payload={})

    # The model must be able to tell "no sign-off" from "sign-off obtained"
    # from the exception text alone.
    message = str(exc_info.value)
    assert "not" in message.lower() or "NOT" in message
    assert "sign-off" in message.lower() or "approval" in message.lower()

    # Server-side diagnosability: enough detail to find this run's failure.
    assert any(str(run.id) in record.message for record in caplog.records)

    # No half-state in the approval flow: no approval row leaked, and the
    # run's status is untouched.
    with sessions() as check:
        leftover = check.scalars(select(Approval).where(Approval.run_id == run.id)).all()
        assert leftover == []
        fresh = check.get(Run, run.id)
        assert fresh is not None
        assert fresh.status is RunStatus.QUEUED
        # Operator-side visibility (Finding 2): the failure must be
        # discoverable from the orchestrator's own DB, not only from the
        # model's exception or this subprocess's own (discarded) stderr.
        assert fresh.gate_error is not None
        assert "spend" in fresh.gate_error


def test_missing_run_id_raises_cleanly_instead_of_a_bare_keyerror(
    sessions: sessionmaker[Session],
    session: Session,  # pulls in truncation teardown
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AICOM_RUN_ID missing or malformed must still produce the model-facing
    ApprovalRecordingFailed contract, not an unhandled KeyError/ValueError
    with no "you did not get a sign-off" message."""
    monkeypatch.setattr(gate_server, "_sessions", sessions)
    monkeypatch.delenv("AICOM_RUN_ID", raising=False)

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(gate_server.ApprovalRecordingFailed) as exc_info,
    ):
        gate_server.request_approval_tool(kind="spend", proposal="p", payload={})

    assert "sign-off" in str(exc_info.value).lower()
    assert len(caplog.records) >= 1
