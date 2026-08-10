import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from aicom.domain.enums import RunStatus, TaskStatus
from aicom.domain.views import ApprovalView, RunReport
from aicom.notify.base import Notifier
from aicom.orchestrator.worker import MAX_ATTEMPTS
from aicom.store.approvals import pending_approvals, record_reminder
from aicom.store.models import Run
from aicom.store.runs import stale_running_runs, transition

logger = logging.getLogger(__name__)

# stage 1: thread nudge, stage 2: DM, stage 3+: daily digest.
# Approvals NEVER expire — escalation is the only consequence of silence.
REMINDER_INTERVALS: tuple[timedelta, ...] = (
    timedelta(hours=4),
    timedelta(days=1),
    timedelta(days=1),
)


class Sweeper:
    def __init__(self, sessions: sessionmaker[Session], notifier: Notifier) -> None:
        self._sessions = sessions
        self._notifier = notifier

    def sweep_reminders(self, now: datetime) -> int:
        sent = 0
        with self._sessions() as session:
            for approval in pending_approvals(session, due_before=now):
                try:
                    stage = approval.remind_count + 1
                    view = ApprovalView(
                        approval_id=approval.id,
                        nonce=approval.nonce,
                        kind=approval.kind,
                        agent_name=approval.run.task.agent.name,
                        task_title=approval.run.task.title,
                        proposal=approval.proposal,
                        payload=approval.payload,
                        slack_channel=approval.slack_channel,
                        slack_ts=approval.slack_ts,
                    )
                    self._notifier.send_reminder(view, stage)
                    index = min(approval.remind_count, len(REMINDER_INTERVALS) - 1)
                    record_reminder(
                        session,
                        approval,
                        now=now,
                        next_after=now + REMINDER_INTERVALS[index],
                    )
                    session.commit()
                    sent += 1
                except Exception:
                    # One approval's notify (or persist) blowing up must never
                    # stop the rest of the batch from being nudged — pending
                    # approvals never expire, and the whole point of this sweep
                    # is that operators do not get silently forgotten. Roll
                    # back just this approval's uncommitted change and keep
                    # going; the next sweep will retry it.
                    session.rollback()
                    logger.exception(
                        "sweep_reminders: failed to send reminder for approval %s",
                        approval.id,
                    )
        return sent

    def recover_stale_runs(self, now: datetime, *, stale_after: timedelta) -> int:
        recovered = 0
        with self._sessions() as session:
            for run in stale_running_runs(session, older_than=now - stale_after):
                try:
                    if self._recover_one(session, run, now):
                        recovered += 1
                    session.commit()
                except Exception:
                    session.rollback()
                    logger.exception("recover_stale_runs: failed to recover run %s", run.id)
        return recovered

    def _recover_one(self, session: Session, run: Run, now: datetime) -> bool:
        # A dead worker did use up an attempt: recovery must consume one, the
        # same as any other failed attempt, or a worker that reliably crashes
        # on this run becomes an invisible infinite retry loop.
        next_attempt = run.attempt + 1
        if next_attempt >= MAX_ATTEMPTS:
            applied = transition(
                session,
                run.id,
                RunStatus.RUNNING,
                RunStatus.FAILED,
                ended_at=now,
                attempt=next_attempt,
                worker_id=None,
            )
            if not applied:
                return False
            run.task.status = TaskStatus.FAILED
            self._notifier.send_run_report(
                RunReport(
                    run_id=run.id,
                    agent_name=run.task.agent.name,
                    task_title=run.task.title,
                    status=RunStatus.FAILED,
                    summary=(
                        f"Run failed after {next_attempt} attempts "
                        "(worker died without a heartbeat)."
                    ),
                    cost_usd=run.cost_usd,
                )
            )
            return False
        return transition(
            session,
            run.id,
            RunStatus.RUNNING,
            RunStatus.QUEUED,
            worker_id=None,
            started_at=None,
            attempt=next_attempt,
        )
