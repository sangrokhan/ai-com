import logging
import sys
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

# Single source of truth for the gate wiring. The Claude Code CLI exposes an
# MCP tool as `mcp__<server-key>__<tool-function>`, so the whitelist entry the
# worker appends is DERIVED from the server key it injects and the name of the
# @mcp.tool() function in aicom/gate/server.py -- never spelled out twice. If
# those could drift, --allowedTools would carry a name that matches nothing and
# the agent's only route to a gated action would silently vanish.
GATE_SERVER_NAME = "gate"  # must equal FastMCP("gate") in aicom.gate.server
GATE_TOOL_FUNCTION = "request_approval_tool"  # the @mcp.tool() function name
GATE_TOOL = f"mcp__{GATE_SERVER_NAME}__{GATE_TOOL_FUNCTION}"


def gate_server_spec() -> dict[str, object]:
    """Spawn spec for the gate MCP server, injected into every run.

    Equivalent to `python -m aicom.gate.server`, but pinned to the interpreter
    running the worker so the server always imports the same aicom package and
    virtualenv. The server reads AICOM_RUN_ID from the environment, which
    ClaudeCliExecutor injects per run.

    Deliberately carries NO database_url of its own: this dict is written
    verbatim to the `mcp.json` config file on disk (see ClaudeCliExecutor),
    and a Postgres URL routinely carries a password -- `assert_resolved`
    exists precisely to keep plaintext secrets out of that file. The DB URL
    the gate server needs is instead injected via RunRequest.env, which
    ClaudeCliExecutor merges into the *subprocess environment* only
    (`{**os.environ, **req.env}`), never onto disk; the MCP child inherits
    that process environment. See `_build_request` below.
    """
    return {"command": sys.executable, "args": ["-m", "aicom.gate.server"]}


class GatedToolMisconfiguration(Exception):
    """An agent lists a gated tool in allowed_tools.

    That makes the tool permanently callable without any sign-off, which
    silently disables the entire autonomy boundary for that tool. Refuse to
    run rather than proceed with a dead gate.
    """

    def __init__(self, agent_name: str, tools: tuple[str, ...]) -> None:
        self.tools = tools
        super().__init__(
            f"agent {agent_name} lists gated tool(s) in allowed_tools: "
            f"{', '.join(tools)}. A gated tool must be reachable only through "
            f"an approved {GATE_TOOL} request; remove them from allowed_tools."
        )


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
            try:
                request = self._build_request(session, run)
            except GatedToolMisconfiguration as exc:
                session.rollback()
                fresh = session.get(Run, run.id, populate_existing=True)
                assert fresh is not None
                self._fail_misconfigured(session, fresh, exc, now)
                session.commit()
                return True
            session.commit()

        # try/finally so the _resume_flags entry for this run is always
        # removed, even if _execute raises: _finalize (which normally pops it)
        # would never run, and the stale entry would then be read by a LATER
        # execution of the same run id and wrongly clear its resume state.
        try:
            outcome = self._execute(run.id, request)

            with self._sessions() as session:
                fresh_run = session.get(Run, run.id)
                assert fresh_run is not None
                self._finalize(session, fresh_run, outcome, request, now)
                session.commit()
        finally:
            self._resume_flags.pop(run.id, None)
        return True

    def _build_request(self, session: Session, run: Run) -> RunRequest:
        agent = run.task.agent
        gated = tuple(agent.gated_tools or ())
        # Validate BEFORE any mutation or workspace creation, so a
        # misconfigured agent leaves no half-built state behind.
        overlap = tuple(sorted(set(gated) & set(agent.allowed_tools)))
        if overlap:
            raise GatedToolMisconfiguration(agent.name, overlap)

        workspace = prepare_workspace(self._settings.workspace_root, agent.name, run.id)
        # The gate server is wired in by CODE, never by convention: an agent
        # whose mcp_config happens to omit (or misname, or shadow) a "gate"
        # entry would otherwise have no route to sign-off at all, while still
        # being handed GATE_TOOL in --allowedTools. Injected last so it always
        # wins over any same-named key the agent config carries.
        mcp_config, env = resolve_secrets(
            {**agent.mcp_config, GATE_SERVER_NAME: gate_server_spec()}, _lookup_secret
        )
        # The gate server resolves its DB connection from AICOM_DATABASE_URL
        # in its own process environment, which it inherits from this run's
        # `claude` subprocess (ClaudeCliExecutor merges `{**os.environ,
        # **req.env}`). Set explicitly here -- and LAST, so it wins over both
        # the ambient environment and any same-named var an agent's own
        # secrets happened to resolve to -- so the gate server can never bind
        # to a database other than the one this run actually lives in. Never
        # placed in mcp_config: that dict is written verbatim to disk.
        env = {**env, "AICOM_DATABASE_URL": self._settings.database_url}
        run.workspace_path = str(workspace)
        # A crashed/timed-out run's retry is a brand-new Run row (see
        # _handle_failure), so it never carries a prior gate_error forward.
        # A RESUMED run (approval decided, same run.id re-executed) is not:
        # it's this same row, and a gate_error left over from an earlier,
        # failed gate call in a PRIOR execution of it must not ride along
        # into whatever this fresh execution reports, or an operator would
        # see a stale warning about a call this execution never made.
        run.gate_error = None
        allowed = tuple(agent.allowed_tools) + (GATE_TOOL,)
        granted = self._approved_gated_tool(session, run, gated)
        if granted is not None:
            # Spec §5.1 step 4: the approved tool is whitelisted for THIS
            # execution only. Nothing persists it, so a retry, a later run of
            # the same task, or any other agent starts without it.
            allowed += (granted,)
            logger.info(
                "run %s: temporarily allowing gated tool %s for this execution only",
                run.id,
                granted,
            )
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

    def _approved_gated_tool(
        self, session: Session, run: Run, gated: tuple[str, ...]
    ) -> str | None:
        """The one gated tool this execution may use, or None.

        Only a run that is actually being resumed with a decision can carry a
        grant. The tool name comes from the approval payload -- which is
        AGENT-supplied, so it is checked against the agent's gated_tools list:
        the operator signed off on a proposal, not on whatever tool string the
        agent chose to put in the payload. A payload naming anything else is
        refused, and a rejected (or tool-less) approval grants nothing.
        """
        if not run.resume_pending or not gated:
            return None
        approval = session.scalar(
            select(Approval)
            .where(Approval.run_id == run.id, Approval.decided_at.is_not(None))
            .order_by(Approval.decided_at.desc(), Approval.created_at.desc())
            .limit(1)
        )
        if approval is None or approval.status is not ApprovalStatus.APPROVED:
            return None
        tool = (approval.payload or {}).get("tool")
        if tool is None:
            return None
        if not isinstance(tool, str) or tool not in gated:
            logger.warning(
                "run %s: approval %s payload names tool %r which is not in the "
                "agent's gated_tools; refusing to grant it",
                run.id,
                approval.id,
                tool,
            )
            return None
        return tool

    def _fail_misconfigured(
        self,
        session: Session,
        run: Run,
        exc: GatedToolMisconfiguration,
        now: datetime,
    ) -> None:
        logger.error("run %s cannot start: %s", run.id, exc)
        applied = transition(session, run.id, RunStatus.RUNNING, RunStatus.FAILED, ended_at=now)
        if not applied:
            return
        run.task.status = TaskStatus.FAILED
        self._notifier.send_run_report(
            RunReport(
                run_id=run.id,
                agent_name=run.task.agent.name,
                task_title=run.task.title,
                status=RunStatus.FAILED,
                summary=f"Refused to run: {exc}",
                cost_usd=None,
            )
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

        # The executor's reader threads are daemons that can outlive run() if
        # a grandchild holds the stdout pipe open (see ClaudeCliExecutor).
        # Once this execution is done, any late callback must be dropped: it
        # would otherwise write events against an already-finalised run,
        # producing phantom events or (run_id, seq) collisions with whatever
        # executes this run next.
        done = threading.Event()

        def on_event(event: ParsedEvent) -> None:
            nonlocal next_seq
            if done.is_set():
                logger.warning(
                    "dropping event from a leaked reader thread for finished run %s", run_id
                )
                return
            with self._sessions() as session:
                append_event(session, run_id, next_seq, event.type, event.payload)
                touch_heartbeat(session, run_id, datetime.now(UTC), worker_id=self._worker_id)
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
                        touch_heartbeat(
                            session, run_id, datetime.now(UTC), worker_id=self._worker_id
                        )
                        session.commit()
                except Exception:  # a heartbeat hiccup must never kill the run
                    logger.exception("heartbeat tick failed for run %s", run_id)

        ticker = threading.Thread(target=heartbeat_loop, name=f"heartbeat-{run_id}", daemon=True)
        ticker.start()
        try:
            return self._executor.run(request, on_event)
        finally:
            done.set()
            stop.set()
            ticker.join(timeout=5)

    def _finalize(
        self,
        session: Session,
        run: Run,
        outcome: RunOutcome,
        request: RunRequest,
        now: datetime,
    ) -> None:
        """Close out one execution. PRIVATE and instance-bound by contract:
        it may only be called on the same Worker instance that built
        `request`, because it consumes that instance's in-process
        `_resume_flags` entry for this run. Called from any other Worker (or
        for a request this Worker did not build) it would read a missing flag
        and silently mis-handle the run's resume state. `tick` is the only
        caller.
        """
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

        # The gate MCP subprocess stamps this (best-effort, from its own
        # process) when it could not durably record an approval -- e.g. the
        # agent called request_approval_tool, got ApprovalRecordingFailed,
        # and still terminated as though the run were clean. That error was
        # loud to the model, but the model choosing to proceed anyway means
        # this log line is the only place an operator watching logs (rather
        # than this run's raw event stream) learns anything needed sign-off
        # and never got it. Checked here, before branching on outcome, so it
        # covers every ending this execution can have -- not just the clean
        # SUCCEEDED path where the blind spot was originally found.
        if run.gate_error:
            logger.error(
                "run %s finished (%s) but the gate reported an unrecorded "
                "approval attempt: %s",
                run.id,
                outcome.reason.value,
                run.gate_error,
            )

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
        summary = f"Completed. Artifacts: {sha or 'none'}"
        # Already logged above (covers every outcome branch); this is the
        # SUCCEEDED-specific piece: pushing it into the same Slack channel
        # every other run report already uses, since a clean-looking
        # "Completed" summary is precisely the blind spot the live
        # experiment found -- a failure report from _handle_failure or an
        # approval dispatch from _dispatch_approval already gives an
        # operator SOME signal something happened on those other paths.
        if run.gate_error:
            summary = (
                f"{summary}\n\n"
                f"WARNING: a gate approval request failed to record during this "
                f"run and was NOT signed off: {run.gate_error}"
            )
        self._notifier.send_run_report(
            RunReport(
                run_id=run.id,
                agent_name=run.task.agent.name,
                task_title=run.task.title,
                status=RunStatus.SUCCEEDED,
                summary=summary,
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
