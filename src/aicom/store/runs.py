import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from aicom.domain.enums import RunStatus
from aicom.domain.transitions import assert_transition
from aicom.store.models import Run


def claim_next_queued(session: Session, *, worker_id: str, now: datetime) -> Run | None:
    """Atomically grab one queued run. SKIP LOCKED lets many workers poll safely."""
    stmt = (
        select(Run.id)
        .where(Run.status == RunStatus.QUEUED)
        .order_by(Run.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    run_id = session.scalar(stmt)
    if run_id is None:
        return None
    applied = transition(
        session,
        run_id,
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        worker_id=worker_id,
        started_at=now,
        heartbeat_at=now,
    )
    if not applied:
        return None
    # populate_existing forces a refresh: the bulk UPDATE above only synchronizes
    # a subset of attributes on any already-loaded identity-map instance, so a
    # plain session.get() here could return a stale worker_id/started_at.
    return session.get(Run, run_id, populate_existing=True)


def transition(
    session: Session,
    run_id: uuid.UUID,
    frm: RunStatus,
    to: RunStatus,
    **fields: object,
) -> bool:
    assert_transition(frm, to)
    result = session.execute(
        update(Run)
        .where(Run.id == run_id, Run.status == frm)
        .values(status=to, **fields)
        .execution_options(synchronize_session="fetch")
    )
    return bool(result.rowcount)


def touch_heartbeat(session: Session, run_id: uuid.UUID, now: datetime) -> None:
    session.execute(update(Run).where(Run.id == run_id).values(heartbeat_at=now))


def stale_running_runs(session: Session, *, older_than: datetime) -> list[Run]:
    stmt = select(Run).where(Run.status == RunStatus.RUNNING, Run.heartbeat_at < older_than)
    return list(session.scalars(stmt))
