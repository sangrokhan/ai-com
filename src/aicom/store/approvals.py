import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from aicom.domain.enums import ApprovalKind, ApprovalStatus
from aicom.store.models import Approval

# first reminder delay per kind; later stages come from sweeper escalation
FIRST_REMINDER: dict[ApprovalKind, timedelta] = {
    ApprovalKind.SPEND: timedelta(minutes=30),
    ApprovalKind.EXECUTE_ORDER: timedelta(minutes=30),
    ApprovalKind.PUBLISH: timedelta(hours=2),
    ApprovalKind.CONTACT: timedelta(hours=2),
}


def create_approval(
    session: Session,
    *,
    run_id: uuid.UUID,
    kind: ApprovalKind,
    proposal: str,
    payload: dict,
    now: datetime,
) -> Approval:
    approval = Approval(
        id=uuid.uuid4(),
        run_id=run_id,
        kind=kind,
        proposal=proposal,
        payload=payload,
        nonce=secrets.token_urlsafe(32),
        remind_after=now + FIRST_REMINDER[kind],
    )
    session.add(approval)
    session.flush()
    return approval


def consume_nonce(
    session: Session, *, nonce: str, decided_by: str, approved: bool, now: datetime
) -> Approval | None:
    """Atomically claim an unconsumed approval. Duplicate clicks return None."""
    status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
    result = session.execute(
        update(Approval)
        .where(Approval.nonce == nonce, Approval.consumed_at.is_(None))
        .values(
            status=status,
            consumed_at=now,
            decided_by=decided_by,
            decided_at=now,
            response={"approved": approved},
        )
        .returning(Approval.id)
    )
    approval_id = result.scalar_one_or_none()
    if approval_id is None:
        return None
    session.expire_all()
    return session.get(Approval, approval_id)


def pending_approvals(session: Session, *, due_before: datetime) -> list[Approval]:
    stmt = select(Approval).where(
        Approval.status == ApprovalStatus.PENDING,
        Approval.remind_after.is_not(None),
        Approval.remind_after < due_before,
    )
    return list(session.scalars(stmt))


def record_reminder(
    session: Session, approval: Approval, *, now: datetime, next_after: datetime
) -> None:
    approval.remind_count += 1
    approval.last_reminded_at = now
    approval.remind_after = next_after


def attach_slack_ref(session: Session, approval_id: uuid.UUID, channel: str, ts: str) -> None:
    session.execute(
        update(Approval)
        .where(Approval.id == approval_id)
        .values(slack_channel=channel, slack_ts=ts)
    )
