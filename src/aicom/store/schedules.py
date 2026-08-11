import uuid
from datetime import datetime

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from aicom.domain.enums import RunStatus, TaskStatus
from aicom.store.models import Run, Schedule, Task

# A cycle is still in flight while its task is open or blocked, or while any
# of its runs has not reached a terminal state.
_LIVE_TASK_STATUSES = (TaskStatus.OPEN, TaskStatus.BLOCKED)
_LIVE_RUN_STATUSES = (
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.AWAITING_APPROVAL,
)


def due_schedules(session: Session, *, now: datetime) -> list[Schedule]:
    stmt = (
        select(Schedule)
        .where(Schedule.enabled.is_(True), Schedule.next_due_at <= now)
        .order_by(Schedule.next_due_at)
    )
    return list(session.scalars(stmt))


def claim_firing(
    session: Session,
    schedule_id: uuid.UUID,
    observed_due_at: datetime,
    next_due_at: datetime,
    **fields: object,
) -> bool:
    """Take ownership of one firing.

    The clock value the caller observed is part of the WHERE clause, so only
    one scheduler can win a given slot. Callers MUST check the result before
    creating a task; treating an unclaimed firing as claimed double-fires.
    """
    result = session.execute(
        update(Schedule)
        .where(Schedule.id == schedule_id, Schedule.next_due_at == observed_due_at)
        .values(next_due_at=next_due_at, **fields)
    )
    return bool(result.rowcount)


def has_unfinished_cycle(session: Session, schedule_id: uuid.UUID) -> bool:
    live_task = exists().where(
        Task.schedule_id == schedule_id, Task.status.in_(_LIVE_TASK_STATUSES)
    )
    live_run = exists().where(
        Task.schedule_id == schedule_id,
        Run.task_id == Task.id,
        Run.status.in_(_LIVE_RUN_STATUSES),
    )
    return bool(session.scalar(select(live_task | live_run)))


def record_skip(
    session: Session,
    schedule_id: uuid.UUID,
    observed_due_at: datetime,
    *,
    now: datetime,
    reason: str,
    next_due_at: datetime,
) -> bool:
    """Advance the clock without firing. The clock moves on a skip too, so a
    blocked schedule does not accumulate overdue slots to work through.

    Like `claim_firing`, the clock value the caller observed is part of the
    WHERE clause: without it, a skip could overwrite
    `last_skipped_at`/`last_skip_reason` on a schedule another instance just
    fired, or move `next_due_at` backwards if commits interleave. Callers
    MUST check the result before treating the skip as recorded.
    """
    result = session.execute(
        update(Schedule)
        .where(Schedule.id == schedule_id, Schedule.next_due_at == observed_due_at)
        .values(next_due_at=next_due_at, last_skipped_at=now, last_skip_reason=reason)
    )
    return bool(result.rowcount)


def disable(
    session: Session, schedule_id: uuid.UUID, *, reason: str, now: datetime
) -> bool:
    result = session.execute(
        update(Schedule)
        .where(Schedule.id == schedule_id)
        .values(enabled=False, last_skipped_at=now, last_skip_reason=reason)
    )
    return bool(result.rowcount)
