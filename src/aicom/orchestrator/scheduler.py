import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from aicom.domain.cron import InvalidCron, next_fire
from aicom.notify.base import Notifier
from aicom.store.models import Run, Schedule, Task
from aicom.store.schedules import (
    claim_firing,
    disable,
    due_schedules,
    has_unfinished_cycle,
    record_skip,
)

logger = logging.getLogger(__name__)


class Scheduler:
    """Creates tasks from recurring schedules.

    Runs alongside the worker and the sweeper in the same loop.
    """

    def __init__(self, sessions: sessionmaker[Session], notifier: Notifier) -> None:
        self._sessions = sessions
        self._notifier = notifier

    def tick(self, now: datetime) -> int:
        """Fire every schedule that is due. Returns the number of tasks created."""
        created = 0
        with self._sessions() as session:
            for schedule in due_schedules(session, now=now):
                try:
                    fired = self._process(session, schedule, now)
                    session.commit()
                except Exception:
                    # One schedule must never starve the rest.
                    session.rollback()
                    logger.exception("schedule %s failed to process", schedule.id)
                    continue
                # Only count a firing once the transaction that created it has
                # actually landed. If the commit above had failed, the task
                # and run rows would have rolled back with it, and reporting
                # them as created would lie to the caller.
                if fired:
                    created += 1
        return created

    def _process(self, session: Session, schedule: Schedule, now: datetime) -> bool:
        observed = schedule.next_due_at
        try:
            upcoming = next_fire(schedule.cron, schedule.timezone, now)
        except InvalidCron as exc:
            disable(session, schedule.id, reason=str(exc), now=now)
            # Commit the disable before notifying: a notifier failure must
            # not undo the disable, or the same broken schedule gets retried
            # (and fails to notify) on every subsequent tick forever.
            session.commit()
            try:
                self._notifier.send_system_notice(
                    f"Schedule '{schedule.name}' disabled: {exc}"
                )
            except Exception:
                logger.exception(
                    "schedule %s: disabled but failed to notify", schedule.id
                )
            return False

        if not schedule.agent.enabled:
            record_skip(
                session,
                schedule.id,
                now=now,
                reason="agent disabled",
                next_due_at=upcoming,
            )
            return False

        if has_unfinished_cycle(session, schedule.id):
            record_skip(
                session,
                schedule.id,
                now=now,
                reason="previous cycle unfinished",
                next_due_at=upcoming,
            )
            return False

        if not claim_firing(
            session, schedule.id, observed, upcoming, last_fired_at=now
        ):
            # Another scheduler took this slot.
            return False

        task = Task(
            id=uuid.uuid4(),
            agent_id=schedule.agent_id,
            title=schedule.title_template,
            goal=schedule.goal_template,
            schedule_id=schedule.id,
        )
        session.add(task)
        session.flush()
        session.add(Run(id=uuid.uuid4(), task_id=task.id, attempt=1))
        return True
