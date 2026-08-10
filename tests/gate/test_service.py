import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from aicom.domain.enums import ApprovalKind, ApprovalStatus, RunStatus
from aicom.gate.service import UnknownApprovalKind, request_approval
from aicom.store.approvals import consume_nonce, pending_approvals
from aicom.store.models import Approval, Run, Task
from aicom.store.runs import claim_next_queued
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _running_run(session: Session) -> Run:
    agent = make_agent(session)
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="t", goal="g")
    session.add(task)
    session.flush()
    session.add(Run(id=uuid.uuid4(), task_id=task.id, attempt=1))
    session.commit()
    run = claim_next_queued(session, worker_id="w1", now=NOW)
    session.commit()
    assert run is not None
    return run


def test_gate_request_parks_run_and_creates_pending_approval(session: Session) -> None:
    run = _running_run(session)

    result = request_approval(
        session,
        run_id=run.id,
        kind="spend",
        proposal="Buy feed X, $20/mo, alternative free tier, cancellable.",
        payload={"amount_usd": 20},
        now=NOW,
    )
    session.commit()
    session.refresh(run)

    approval = session.get(Approval, result.approval_id)
    assert approval is not None
    assert approval.status is ApprovalStatus.PENDING
    assert approval.kind is ApprovalKind.SPEND
    assert approval.nonce
    assert run.status is RunStatus.AWAITING_APPROVAL
    assert "terminate" in result.message.lower()


def test_gate_rejects_unknown_kind(session: Session) -> None:
    run = _running_run(session)
    with pytest.raises(UnknownApprovalKind):
        request_approval(
            session, run_id=run.id, kind="delete_prod", proposal="p", payload={}, now=NOW
        )


def test_nonce_is_single_use(session: Session) -> None:
    run = _running_run(session)
    result = request_approval(
        session, run_id=run.id, kind="spend", proposal="p", payload={}, now=NOW
    )
    session.commit()
    approval = session.get(Approval, result.approval_id)
    assert approval is not None

    first = consume_nonce(
        session, nonce=approval.nonce, decided_by="U123", approved=True, now=NOW
    )
    session.commit()
    assert first is not None and first.status is ApprovalStatus.APPROVED
    assert first.decided_by == "U123"

    second = consume_nonce(
        session, nonce=approval.nonce, decided_by="U999", approved=False, now=NOW
    )
    assert second is None


def test_pending_approvals_returns_items_due_for_reminder(session: Session) -> None:
    run = _running_run(session)
    request_approval(
        session, run_id=run.id, kind="publish", proposal="p", payload={}, now=NOW
    )
    session.commit()

    assert pending_approvals(session, due_before=NOW) == []
    due = pending_approvals(session, due_before=NOW + timedelta(hours=3))
    assert len(due) == 1
