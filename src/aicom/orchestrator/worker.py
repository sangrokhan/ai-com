import logging
import threading
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from aicom.config import Settings
from aicom.domain.enums import ApprovalStatus, ExitReason, RunStatus, TaskStatus
from aicom.domain.views import ApprovalView, RunReport
from aicom.executor.base import Executor, RunOutcome, RunRequest
from aicom.executor.secrets import resolve_secrets
from aicom.executor.stream import ParsedEvent
from aicom.executor.workspace import prepare_workspace
from aicom.notify.base import Notifier
from aicom.orchestrator.artifacts import commit_run_artifacts
from aicom.orchestrator.prompts import build_prompt
from aicom.quota.reset import fallback_backoff, parse_reset_at
from aicom.store.events import append_event
from aicom.store.models import Approval, Event, Run, SystemState
from aicom.store.runs import claim_next_queued, touch_heartbeat, transition
from aicom.store.system_state import (
    clear_pause,
    get_pause,
    mark_pause_notified,
    pause_notified_at,
    set_pause,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
GATE_TOOL = "mcp__gate__request_approval_tool"


class Worker:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        executor: Executor,
        notifier: Notifier,
        settings: Settings,
        worker_id: str,
    ) -> None:
        self._sessions = sessions
        self._executor = executor
        self._notifier = notifier
        self._settings = settings
        self._worker_id = worker_id
        # Whether the run actually had resume_pending set at the moment its
        # RunRequest was built, keyed by run_id. RunRequest.resume_session_id
        # is a lossy proxy for this (it's None whenever the run has no stored
        # session_id yet, even if resume_pending was True — e.g. a run parked
        # before the executor ever reported a session_id, then approved) so
        # finalize() must not re-derive "did this execution consume a
        # resume" from the request; it reads the fact captured here instead.
        self._resume_flags: dict[uuid.UUID, bool] = {}

    def tick(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        with self._sessions() as session:
            paused_until = get_pause(session)
            if paused_until is not None and paused_until > now:
                session.commit()
                return False
            if paused_until is not None:
                clear_pause(session)
            run = claim_next_queued(session, worker_id=self._worker_id, now=now)
            session.commit()
            if run is None:
                return False
            request = self._build_request(session, run)
            session.commit()

        outcome = self._execute(run.id, request)

        with self._sessions() as session:
            fresh = session.get(Run, run.id)
            assert fresh is not None
            self.finalize(session, fresh, outcome, request, now)
            session.commit()
        return True

    def _build_request(self, session: Session, run: Run) -> RunRequest:
        agent = run.task.agent
        workspace = prepare_workspace(self._settings.workspace_root, agent.name, run.id)
        mcp_config, env = resolve_secrets(agent.mcp_config, _lookup_secret)
        run.workspace_path = str(workspace)
        allowed = tuple(agent.allowed_tools) + (GATE_TOOL,)
        # Capture the fact directly, not a proxy for it (see _resume_flags).
        self._resume_flags[run.id] = run.resume_pending
        return RunRequest(
            run_id=run.id,
            prompt=build_prompt(run.task.title, run.task.goal, run.resume_note),
            workspace=workspace,
            allowed_tools=allowed,
            mcp_config=mcp_config,
            env=env,
            resume_session_id=run.session_id if run.resume_pending else None,
            timeout_seconds=agent.max_run_seconds,
        )

    def _execute(self, run_id: uuid.UUID, request: RunRequest) -> RunOutcome:
        # The stream parser's own seq counter always restarts at 0 per process
        # invocation, but a single run row can be executed by more than one
        # process over its lifetime (usage-limit requeue, approval resume all
        # reuse the same run_id). Offset from the highest seq already stored
        # for this run so the (run_id, seq) unique constraint never collides.
        with self._sessions() as session:
            max_seq = session.scalar(select(func.max(Event.seq)).where(Event.run_id == run_id))
            next_seq = (max_seq if max_seq is not None else -1) + 1

        def on_event(event: ParsedEvent) -> None:
            nonlocal next_seq
            with self._sessions() as session:
                append_event(session, run_id, next_seq, event.type, event.payload)
                touch_heartbeat(session, run_id, datetime.now(UTC))
                session.commit()
            next_seq += 1

        # `on_event` only fires when the agent emits stream output, so a run
        # that works silently for a long stretch would otherwise never touch
        # its heartbeat and Task 13's sweeper would wrongly reclaim it as
        # stale mid-flight. A background ticker keeps the heartbeat fresh on
        # a time basis, independent of event arrival, for as long as the
        # executor is actually running.
        stop = threading.Event()

        def heartbeat_loop() -> None:
            interval = self._settings.worker_heartbeat_seconds
            while not stop.wait(interval):
                try:
                    with self._sessions() as session:
                        touch_heartbeat(session, run_id, datetime.now(UTC))
                        session.commit()
                except Exception:  # a heartbeat hiccup must never kill the run
                    logger.exception("heartbeat tick failed for run %s", run_id)

        ticker = threading.Thread(target=heartbeat_loop, name=f"heartbeat-{run_id}", daemon=True)
        ticker.start()
        try:
            return self._executor.run(request, on_event)
        finally:
            stop.set()
            ticker.join(timeout=5)

    def finalize(
        self,
        session: Session,
        run: Run,
        outcome: RunOutcome,
        request: RunRequest,
        now: datetime,
    ) -> None:
        # Captured at request-build time, not re-derived here: this is true
        # only if THIS execution actually consumed a resume (i.e. the run was
        # resume_pending when the request was built). A resume that arrives
        # for this same run WHILE this execution was in flight — e.g. a fast
        # operator approving a gate request the run just made, moments before
        # this finalize() runs — must not be confused with it, or the fresh
        # resume_pending/resume_note that raced in gets clobbered below.
        # Popped (not just read) so a stale entry can never leak across runs.
        consumed_resume = self._resume_flags.pop(run.id, False)

        run.session_id = outcome.session_id or run.session_id
        run.cost_usd = outcome.cost_usd
        run.token_in = outcome.token_in
        run.token_out = outcome.token_out
        run.exit_reason = outcome.reason.value
        session.flush()

        if outcome.reason is ExitReason.USAGE_LIMIT:
            self._handle_usage_limit(session, run, outcome, now)
            return

        parked = session.scalar(
            select(Approval).where(
                Approval.run_id == run.id, Approval.status == ApprovalStatus.PENDING
            )
        )
        if parked is not None and run.status is RunStatus.AWAITING_APPROVAL:
            self._dispatch_approval(session, run, parked)
            return

        if outcome.reason in {ExitReason.CRASHED, ExitReason.TIMEOUT}:
            self._handle_failure(session, run, outcome, now, consumed_resume=consumed_resume)
            return

        # Fold the resume-flag clear into the same conditional UPDATE as the
        # status transition: it only takes effect if the run was still
        # RUNNING, i.e. still ours. If it wasn't — because the run moved out
        # from under us (the race above) — applied is False and this method
        # takes NO further side effects: no artifact commit, no task status
        # flip, no success report to Slack for a run that isn't actually
        # done. The transition MUST be checked before commit_run_artifacts
        # runs, not after: commit_run_artifacts makes an irreversible git
        # commit and stages Artifact rows for this same session's commit, so
        # calling it before checking `applied` would double-commit artifacts
        # the next time this same run_id actually succeeds for real.
        resume_clear = {"resume_pending": False, "resume_note": None} if consumed_resume else {}
        applied = transition(
            session, run.id, RunStatus.RUNNING, RunStatus.SUCCEEDED, ended_at=now, **resume_clear
        )
        if not applied:
            logger.warning(
                "run %s finished but was no longer RUNNING at finalize time "
                "(status changed underneath it, likely a race with a Slack "
                "resume); skipping success side effects",
                run.id,
            )
            return
        sha = commit_run_artifacts(
            session,
            run_id=run.id,
            workspace=request.workspace,
            repo=self._settings.artifact_repo_path,
            label=f"{run.task.agent.name}/{run.task.title}",
        )
        run.task.status = TaskStatus.DONE
        self._notifier.send_run_report(
            RunReport(
                run_id=run.id,
                agent_name=run.task.agent.name,
                task_title=run.task.title,
                status=RunStatus.SUCCEEDED,
                summary=f"Completed. Artifacts: {sha or 'none'}",
                cost_usd=outcome.cost_usd,
            )
        )

    def _handle_usage_limit(
        self, session: Session, run: Run, outcome: RunOutcome, now: datetime
    ) -> None:
        state = session.get(SystemState, 1) or SystemState(id=1)
        until = parse_reset_at(outcome.usage_limit_text or "", now=now)
        if until is None:
            until = fallback_backoff(state.consecutive_pauses, now=now)
        set_pause(session, until, outcome.usage_limit_text or "usage limit")
        # not the agent's fault: requeue without burning an attempt
        transition(
            session,
            run.id,
            RunStatus.RUNNING,
            RunStatus.QUEUED,
            worker_id=None,
            started_at=None,
        )
        if pause_notified_at(session) is None:
            self._notifier.send_system_notice(
                f"LLM usage limit reached. Resuming at {until.isoformat()}."
            )
            mark_pause_notified(session, now)

    def _handle_failure(
        self,
        session: Session,
        run: Run,
        outcome: RunOutcome,
        now: datetime,
        *,
        consumed_resume: bool,
    ) -> None:
        resume_clear = {"resume_pending": False, "resume_note": None} if consumed_resume else {}
        if run.attempt < MAX_ATTEMPTS:
            # Capture the note BEFORE transition(): that call updates via
            # synchronize_session="fetch", which (when resume_clear applies)
            # immediately overwrites this same in-memory `run` object's
            # resume_note to None — reading it after would always see None.
            carried_note = run.resume_note if consumed_resume else None
            # This attempt is superseded by a fresh run row, not the task's
            # final word on the matter — FAILED/TIMED_OUT is reserved for the
            # attempt that actually exhausts the retry budget, so a status
            # scan over a task's runs can tell "gave up" from "was replaced
            # by a retry" from "a human cancelled it". exit_reason (set above
            # in finalize) still records why this particular attempt ended.
            applied = transition(
                session,
                run.id,
                RunStatus.RUNNING,
                RunStatus.SUPERSEDED,
                ended_at=now,
                **resume_clear,
            )
            if not applied:
                # Same race as the success path: the run moved out from
                # under us (e.g. a Slack sign-off already re-queued it)
                # before we got here. Creating a retry row now would leave
                # TWO queued runs for one task, duplicating the work.
                logger.warning(
                    "run %s crashed but was no longer RUNNING at finalize "
                    "time; skipping retry-run creation",
                    run.id,
                )
                return
            session.add(
                Run(
                    id=uuid.uuid4(),
                    task_id=run.task_id,
                    attempt=run.attempt + 1,
                    # If this crashed run had itself just been resumed with a
                    # sign-off decision, that decision must not be lost: the
                    # retry needs to know it too, or the agent will redo (or
                    # re-request) the gated work blind. Only the note carries
                    # forward, not resume_pending/session_id — the crashed
                    # process's CLI session cannot be resumed, so the retry
                    # starts fresh but with the decision still in its prompt.
                    resume_note=carried_note,
                )
            )
            return
        target = RunStatus.TIMED_OUT if outcome.reason is ExitReason.TIMEOUT else RunStatus.FAILED
        applied = transition(
            session, run.id, RunStatus.RUNNING, target, ended_at=now, **resume_clear
        )
        if not applied:
            # Again the same race: telling the operator the task failed
            # while the run is actually queued to resume would be a false
            # report.
            logger.warning(
                "run %s exhausted retries but was no longer RUNNING at "
                "finalize time; skipping failure side effects",
                run.id,
            )
            return
        run.task.status = TaskStatus.FAILED
        self._notifier.send_run_report(
            RunReport(
                run_id=run.id,
                agent_name=run.task.agent.name,
                task_title=run.task.title,
                status=target,
                summary=f"Run failed after {run.attempt} attempts ({outcome.reason.value}).",
                cost_usd=outcome.cost_usd,
            )
        )

    def _dispatch_approval(self, session: Session, run: Run, approval: Approval) -> None:
        from aicom.store.approvals import attach_slack_ref

        view = ApprovalView(
            approval_id=approval.id,
            nonce=approval.nonce,
            kind=approval.kind,
            agent_name=run.task.agent.name,
            task_title=run.task.title,
            proposal=approval.proposal,
            payload=approval.payload,
        )
        ref = self._notifier.send_approval_request(view)
        attach_slack_ref(session, approval.id, ref.channel, ref.ts)


def _lookup_secret(ref: str) -> str | None:
    import os

    from aicom.executor.secrets import env_var_name

    return os.environ.get(env_var_name(ref))
