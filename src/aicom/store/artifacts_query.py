"""Finds the reports a schedule has already produced.

Used to give a monitoring agent memory of what it has already said.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from aicom.store.models import Artifact, Run, Task

REPORT_FILENAME = "report.md"


def recent_schedule_reports(
    session: Session, schedule_id: uuid.UUID, *, limit: int
) -> list[tuple[uuid.UUID, str]]:
    """`(run_id, path)` for this schedule's most recent reports, newest first.

    Ordered by `Run.started_at`: `Artifact` carries no timestamp, and
    `ended_at` is null whenever a worker crashed, so it cannot order runs.
    """
    stmt = (
        select(Artifact.run_id, Artifact.path)
        .join(Run, Artifact.run_id == Run.id)
        .join(Task, Run.task_id == Task.id)
        .where(Task.schedule_id == schedule_id, Artifact.path == REPORT_FILENAME)
        .order_by(Run.started_at.desc().nulls_last())
        .limit(limit)
    )
    return [(run_id, path) for run_id, path in session.execute(stmt)]
