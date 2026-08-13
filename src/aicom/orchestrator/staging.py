"""Gives a scheduled agent memory of what it already reported.

The agent is never granted access to the artifact repository — that would let
every agent read everything every agent has produced. Instead the reports it
needs are copied into its own workspace before it starts.
"""

import logging
import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from aicom.store.artifacts_query import recent_schedule_reports
from aicom.store.models import Run

logger = logging.getLogger(__name__)

PREVIOUS_DIR = "previous"
PREVIOUS_REPORT_LIMIT = 5


def stage_previous_reports(
    session: Session,
    run: Run,
    workspace: Path,
    artifact_repo: Path,
    *,
    limit: int = PREVIOUS_REPORT_LIMIT,
) -> int:
    """Copy this schedule's recent reports into `workspace/previous/`.

    Returns how many were staged. Creates no directory when there is nothing
    to stage, so the agent cannot mistake an empty directory for a failure.
    """
    schedule_id = run.task.schedule_id
    if schedule_id is None:
        return 0

    target = workspace / PREVIOUS_DIR
    staged = 0
    for index, (run_id, path) in enumerate(
        recent_schedule_reports(session, schedule_id, limit=limit)
    ):
        source = Path(artifact_repo) / str(run_id) / path
        if not source.is_file():
            logger.warning("previous report missing from disk: %s", source)
            continue
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target / f"{index:02d}-{run_id}.md")
        staged += 1
    return staged
