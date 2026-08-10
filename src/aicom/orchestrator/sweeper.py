from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from aicom.domain.enums import RunStatus
from aicom.domain.views import ApprovalView
from aicom.notify.base import Notifier
from aicom.store.approvals import pending_approvals, record_reminder
from aicom.store.runs import stale_running_runs, transition

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
                sent += 1
            session.commit()
        return sent

    def recover_stale_runs(self, now: datetime, *, stale_after: timedelta) -> int:
        recovered = 0
        with self._sessions() as session:
            for run in stale_running_runs(session, older_than=now - stale_after):
                if transition(
                    session,
                    run.id,
                    RunStatus.RUNNING,
                    RunStatus.QUEUED,
                    worker_id=None,
                    started_at=None,
                ):
                    recovered += 1
            session.commit()
        return recovered
