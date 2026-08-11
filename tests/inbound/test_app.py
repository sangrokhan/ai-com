import hashlib
import hmac
import json
import urllib.parse
import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from aicom.config import Settings
from aicom.domain.enums import ApprovalStatus, RunStatus
from aicom.gate.service import request_approval
from aicom.inbound.app import ResolveOutcome, create_app, resolve_approval
from aicom.store.models import Approval, Run, Task
from aicom.store.runs import claim_next_queued, transition
from tests.store.test_models import make_agent

SECRET = "shhh"
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(
        slack_signing_secret=SECRET,
        slack_approver_ids=("U_OWNER",),
        database_url="postgresql+psycopg://unused/unused",
    )


def _post(client: TestClient, payload: dict) -> object:
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
    ts = str(int(datetime.now(UTC).timestamp()))
    base = b"v0:" + ts.encode() + b":" + body
    sig = "v0=" + hmac.new(SECRET.encode(), base, hashlib.sha256).hexdigest()
    return client.post(
        "/slack/interactions",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
        },
    )


def _parked_approval(session: Session) -> Approval:
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


def _action(nonce: str, action_id: str = "approve", user: str = "U_OWNER") -> dict:
    return {
        "type": "block_actions",
        "user": {"id": user},
        "actions": [{"action_id": action_id, "value": json.dumps({"nonce": nonce})}],
    }


def test_approve_requeues_the_parked_run(
    sessions: sessionmaker[Session], session: Session
) -> None:
    approval = _parked_approval(session)
    client = TestClient(create_app(sessions, _settings()))

    response = _post(client, _action(approval.nonce))
    assert response.status_code == 200

    session.expire_all()
    refreshed = session.get(Approval, approval.id)
    assert refreshed is not None
    assert refreshed.status is ApprovalStatus.APPROVED
    assert refreshed.run.status is RunStatus.QUEUED
    assert refreshed.run.resume_pending is True


def test_non_allowlisted_user_cannot_approve(
    sessions: sessionmaker[Session], session: Session
) -> None:
    approval = _parked_approval(session)
    client = TestClient(create_app(sessions, _settings()))

    response = _post(client, _action(approval.nonce, user="U_STRANGER"))
    assert response.status_code == 403

    session.expire_all()
    refreshed = session.get(Approval, approval.id)
    assert refreshed is not None
    assert refreshed.status is ApprovalStatus.PENDING


def test_bad_signature_rejected(sessions: sessionmaker[Session], session: Session) -> None:
    approval = _parked_approval(session)
    client = TestClient(create_app(sessions, _settings()))

    body = urllib.parse.urlencode({"payload": json.dumps(_action(approval.nonce))}).encode()
    response = client.post(
        "/slack/interactions",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": str(int(datetime.now(UTC).timestamp())),
            "X-Slack-Signature": "v0=deadbeef",
        },
    )
    assert response.status_code == 401


def test_duplicate_click_is_a_no_op(
    sessions: sessionmaker[Session], session: Session
) -> None:
    approval = _parked_approval(session)
    client = TestClient(create_app(sessions, _settings()))

    assert _post(client, _action(approval.nonce)).status_code == 200
    second = _post(client, _action(approval.nonce, action_id="reject"))
    assert second.status_code == 200

    session.expire_all()
    refreshed = session.get(Approval, approval.id)
    assert refreshed is not None
    assert refreshed.status is ApprovalStatus.APPROVED  # unchanged by the second click


def test_requeue_failure_is_reported_to_the_caller_of_resolve_approval(
    session: Session,
) -> None:
    """If the run backing a nonce is no longer AWAITING_APPROVAL (e.g. it was
    cancelled or timed out out-of-band between the approval request and the
    click), consume_nonce still atomically burns the nonce and marks the
    approval decided -- but the requeue must be reported as failed to the
    caller rather than silently swallowed, since the run is now parked
    forever without that signal."""
    approval = _parked_approval(session)
    moved = transition(session, approval.run_id, RunStatus.AWAITING_APPROVAL, RunStatus.CANCELLED)
    assert moved
    session.commit()

    outcome = resolve_approval(
        session, nonce=approval.nonce, decided_by="U_OWNER", approved=True, now=NOW
    )
    session.commit()

    assert outcome is ResolveOutcome.REQUEUE_FAILED

    session.expire_all()
    refreshed = session.get(Approval, approval.id)
    assert refreshed is not None
    assert refreshed.status is ApprovalStatus.APPROVED  # nonce still burned
    assert refreshed.run.status is RunStatus.CANCELLED  # but the run was left untouched


def test_mixed_batch_resolves_valid_actions_and_skips_malformed_ones(
    sessions: sessionmaker[Session], session: Session
) -> None:
    """A single click can carry several actions. One malformed action must
    not block the others in the same batch from being resolved."""
    approval = _parked_approval(session)
    client = TestClient(create_app(sessions, _settings()))

    payload = {
        "type": "block_actions",
        "user": {"id": "U_OWNER"},
        "actions": [
            {"action_id": "approve", "value": json.dumps({"nonce": approval.nonce})},
            {"action_id": "approve", "value": json.dumps({"not_a_nonce_field": "x"})},
        ],
    }
    response = _post(client, payload)
    assert response.status_code == 200

    session.expire_all()
    refreshed = session.get(Approval, approval.id)
    assert refreshed is not None
    assert refreshed.status is ApprovalStatus.APPROVED
    assert refreshed.run.status is RunStatus.QUEUED
