import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aicom.config import Settings
from aicom.domain.enums import ExitReason, RunStatus
from aicom.executor.fake import FakeExecutor
from aicom.notify.fake import FakeNotifier
from aicom.orchestrator.worker import (
    GATE_SERVER_NAME,
    GATE_TOOL,
    GATE_TOOL_FUNCTION,
    Worker,
)
from aicom.store.models import Run, Task
from aicom.store.system_state import get_pause
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        workspace_root=tmp_path / "ws",
        artifact_repo_path=tmp_path / "artifacts",
        database_url="postgresql+psycopg://unused/unused",
    )


def _queued(session: Session, **agent_kw: object) -> Run:
    agent = make_agent(session, **agent_kw)  # type: ignore[arg-type]
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="Research", goal="Find things.")
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1)
    session.add(run)
    session.commit()
    return run


def _worker(
    sessions: sessionmaker[Session],
    executor: FakeExecutor,
    notifier: FakeNotifier,
    tmp_path: Path,
) -> Worker:
    (tmp_path / "artifacts").mkdir(parents=True, exist_ok=True)
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path / "artifacts", check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=tmp_path / "artifacts", check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path / "artifacts", check=True)
    return Worker(sessions, executor, notifier, _settings(tmp_path), worker_id="w1")


def test_successful_run_persists_events_and_reports(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    run = _queued(session)
    executor, notifier = FakeExecutor(), FakeNotifier()
    executor.queue(
        run.id,
        [
            json.dumps({"type": "system", "session_id": "s-1"}),
            json.dumps({"type": "result", "total_cost_usd": 0.3, "usage": {"output_tokens": 9}}),
        ],
    )

    assert _worker(sessions, executor, notifier, tmp_path).tick(NOW) is True

    session.expire_all()
    refreshed = session.get(Run, run.id)
    assert refreshed is not None
    assert refreshed.status is RunStatus.SUCCEEDED
    assert refreshed.session_id == "s-1"
    assert refreshed.cost_usd == 0.3
    assert refreshed.token_out == 9
    assert len(notifier.reports) == 1
    assert executor.requests[0].allowed_tools[-1] == "mcp__gate__request_approval_tool"


def test_crash_retries_then_fails_after_max_attempts(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    run = _queued(session)
    executor, notifier = FakeExecutor(), FakeNotifier()
    worker = _worker(sessions, executor, notifier, tmp_path)

    for _ in range(3):
        pending = session.execute(
            Run.__table__.select().where(Run.status == RunStatus.QUEUED)
        ).first()
        assert pending is not None
        executor.queue(pending.id, [], reason=ExitReason.CRASHED, exit_code=1)
        assert worker.tick(NOW) is True
        session.expire_all()

    statuses = [r.status for r in session.query(Run).filter(Run.task_id == run.task_id).all()]
    assert statuses.count(RunStatus.FAILED) == 1
    assert any(
        "failed" in n.lower() for n in notifier.notices + [r.summary for r in notifier.reports]
    )


def test_usage_limit_pauses_globally_and_requeues_without_burning_an_attempt(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    run = _queued(session)
    reset_epoch = int((NOW + timedelta(hours=2)).timestamp())
    executor, notifier = FakeExecutor(), FakeNotifier()
    executor.queue(
        run.id,
        [
            json.dumps(
                {"type": "result", "is_error": True, "result": f"usage limit reached|{reset_epoch}"}
            )
        ],
    )
    worker = _worker(sessions, executor, notifier, tmp_path)

    assert worker.tick(NOW) is True

    session.expire_all()
    refreshed = session.get(Run, run.id)
    assert refreshed is not None
    assert refreshed.status is RunStatus.QUEUED
    assert refreshed.attempt == 1  # not the agent's fault
    assert get_pause(session) == NOW + timedelta(hours=2)
    assert len(notifier.notices) == 1

    # while paused, no new work is claimed and no duplicate notice is sent
    assert worker.tick(NOW + timedelta(minutes=1)) is False
    assert len(notifier.notices) == 1

    # after the reset time the worker resumes
    executor.queue(run.id, [json.dumps({"type": "result"})])
    assert worker.tick(NOW + timedelta(hours=2, minutes=1)) is True


def test_gate_request_leaves_run_parked_and_sends_approval(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    from aicom.gate.service import request_approval
    from aicom.store.models import Approval

    run = _queued(session)
    notifier = FakeNotifier()

    class GateExecutor(FakeExecutor):
        def run(self, req, on_event):  # type: ignore[no-untyped-def]
            with sessions() as s:
                request_approval(
                    s,
                    run_id=req.run_id,
                    kind="spend",
                    proposal="Buy X for $20. Alt: free tier. Reversible: yes.",
                    payload={"amount_usd": 20},
                    now=NOW,
                )
                s.commit()
            return super().run(req, on_event)

    gate_executor = GateExecutor()
    gate_executor.queue(run.id, [json.dumps({"type": "system", "session_id": "s-7"})])

    _worker(sessions, gate_executor, notifier, tmp_path).tick(NOW)

    session.expire_all()
    refreshed = session.get(Run, run.id)
    assert refreshed is not None
    assert refreshed.status is RunStatus.AWAITING_APPROVAL
    assert refreshed.session_id == "s-7"
    assert len(notifier.approvals) == 1
    assert notifier.approvals[0].payload == {"amount_usd": 20}

    # attach_slack_ref must have run, or Task 13's reminders have nothing to
    # thread onto: the FakeNotifier hands back a DispatchRef that finalize()
    # is responsible for persisting onto the approval row.
    approval_row = session.scalar(select(Approval).where(Approval.run_id == run.id))
    assert approval_row is not None
    assert approval_row.slack_channel == "C1"
    assert approval_row.slack_ts == "1.000"


def test_resume_passes_session_id_and_decision_note(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    run = _queued(session)
    run.session_id = "s-42"
    run.resume_pending = True
    run.resume_note = "Sign-off decision for approval abc: APPROVED."
    session.commit()

    executor, notifier = FakeExecutor(), FakeNotifier()
    executor.queue(run.id, [json.dumps({"type": "result"})])
    _worker(sessions, executor, notifier, tmp_path).tick(NOW)

    request = executor.requests[0]
    assert request.resume_session_id == "s-42"
    assert "APPROVED" in request.prompt


def test_resume_note_survives_a_crash_into_the_retry_run(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    """A resumed run that crashes must not drop the sign-off decision: the
    retry run needs it too, or the agent redoes (or re-requests) the gated
    work blind. The crashed attempt itself becomes SUPERSEDED, not FAILED or
    CANCELLED, since it was replaced by a retry rather than exhausted or
    cancelled by a human."""
    run = _queued(session)
    run.session_id = "s-1"
    run.resume_pending = True
    run.resume_note = "Sign-off decision for approval abc: APPROVED."
    session.commit()

    executor, notifier = FakeExecutor(), FakeNotifier()
    executor.queue(run.id, [], reason=ExitReason.CRASHED, exit_code=1)
    worker = _worker(sessions, executor, notifier, tmp_path)

    assert worker.tick(NOW) is True

    session.expire_all()
    original = session.get(Run, run.id)
    assert original is not None
    assert original.status is RunStatus.SUPERSEDED
    assert original.resume_pending is False
    assert original.resume_note is None

    retry = session.scalar(select(Run).where(Run.task_id == run.task_id, Run.attempt == 2))
    assert retry is not None
    assert retry.status is RunStatus.QUEUED
    assert retry.resume_note == "Sign-off decision for approval abc: APPROVED."


def test_finalize_ignores_stale_success_when_run_was_resumed_from_underneath(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    """Race: the executor exits, and before finalize() opens its session, a
    Slack sign-off for this same run arrives via the inbound endpoint (Task
    10), setting resume_pending/resume_note and requeuing the run. finalize()
    must not (a) clobber that fresh resume instruction, nor (b) report
    success / mark the task DONE for a run that is no longer actually the
    one it thinks it finished."""
    from aicom.domain.enums import TaskStatus
    from aicom.executor.base import RunOutcome
    from aicom.gate.service import request_approval
    from aicom.inbound.app import ResolveOutcome, resolve_approval
    from aicom.store.models import Approval
    from aicom.store.runs import claim_next_queued

    _queued(session)
    executor, notifier = FakeExecutor(), FakeNotifier()
    worker = _worker(sessions, executor, notifier, tmp_path)

    claimed = claim_next_queued(session, worker_id="w1", now=NOW)
    assert claimed is not None
    session.commit()
    request = worker._build_request(session, claimed)
    session.commit()

    # Give the run something an (erroneous) artifact commit would actually
    # pick up, so "no commit happened" is a meaningful assertion below
    # rather than trivially true because the workspace was empty anyway.
    (request.workspace / "output.txt").write_text("draft output")
    import subprocess

    artifact_repo = tmp_path / "artifacts"
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=artifact_repo, capture_output=True, text=True, check=False
    )

    # Mid-run the agent parks the run awaiting sign-off.
    request_approval(
        session,
        run_id=claimed.id,
        kind="spend",
        proposal="Buy X for $20. Alt: free tier. Reversible: yes.",
        payload={"amount_usd": 20},
        now=NOW,
    )
    session.commit()
    approval = session.scalar(select(Approval).where(Approval.run_id == claimed.id))
    assert approval is not None

    # Before this worker's finalize() runs for the now-exited process, the
    # operator approves via Slack -- a completely different code path
    # requeues the run with a fresh resume_pending/resume_note.
    outcome_resolve = resolve_approval(
        session, nonce=approval.nonce, decided_by="U1", approved=True, now=NOW
    )
    session.commit()
    assert outcome_resolve is ResolveOutcome.REQUEUED

    # The executor's process already exited (e.g. it terminated normally
    # right after requesting approval) by the time finalize() gets to run.
    outcome = RunOutcome(reason=ExitReason.COMPLETED, exit_code=0, session_id="s-99")
    fresh = session.get(Run, claimed.id, populate_existing=True)
    assert fresh is not None
    worker._finalize(session, fresh, outcome, request, NOW)
    session.commit()

    session.expire_all()
    refreshed = session.get(Run, claimed.id)
    assert refreshed is not None
    assert refreshed.status is RunStatus.QUEUED
    assert refreshed.resume_pending is True
    assert refreshed.resume_note is not None
    assert "APPROVED" in refreshed.resume_note
    assert refreshed.task.status is not TaskStatus.DONE
    assert len(notifier.reports) == 0

    # Nothing irreversible happened either: no Artifact rows for this run,
    # and the artifact repo gained no new commit (still ahead of `applied`
    # being checked, commit_run_artifacts must never have run).
    from aicom.store.models import Artifact

    artifacts = session.scalars(select(Artifact).where(Artifact.run_id == claimed.id)).all()
    assert list(artifacts) == []
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=artifact_repo, capture_output=True, text=True, check=False
    )
    assert head_after.returncode == head_before.returncode
    assert head_after.stdout == head_before.stdout


def test_handle_failure_creates_no_retry_when_run_was_resumed_from_underneath(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    """Same race as the success-path test, but the executor crashes instead
    of completing. _handle_failure's retry branch must check `transition`'s
    return value too: if the run already moved out from under us (a Slack
    sign-off re-queued it before finalize() got here), creating a retry run
    anyway would leave two queued runs for one task and duplicate the work."""
    from aicom.gate.service import request_approval
    from aicom.inbound.app import ResolveOutcome, resolve_approval
    from aicom.store.models import Approval
    from aicom.store.runs import claim_next_queued

    run = _queued(session)
    executor, notifier = FakeExecutor(), FakeNotifier()
    worker = _worker(sessions, executor, notifier, tmp_path)

    claimed = claim_next_queued(session, worker_id="w1", now=NOW)
    assert claimed is not None
    session.commit()
    request = worker._build_request(session, claimed)
    session.commit()

    request_approval(
        session,
        run_id=claimed.id,
        kind="spend",
        proposal="Buy X for $20. Alt: free tier. Reversible: yes.",
        payload={"amount_usd": 20},
        now=NOW,
    )
    session.commit()
    approval = session.scalar(select(Approval).where(Approval.run_id == claimed.id))
    assert approval is not None

    # The operator approves before this worker's finalize() runs for the
    # already-crashed process.
    outcome_resolve = resolve_approval(
        session, nonce=approval.nonce, decided_by="U1", approved=True, now=NOW
    )
    session.commit()
    assert outcome_resolve is ResolveOutcome.REQUEUED

    from aicom.executor.base import RunOutcome

    crashed = RunOutcome(reason=ExitReason.CRASHED, exit_code=1)
    fresh = session.get(Run, claimed.id, populate_existing=True)
    assert fresh is not None
    worker._finalize(session, fresh, crashed, request, NOW)
    session.commit()

    session.expire_all()
    all_runs = session.scalars(select(Run).where(Run.task_id == run.task_id)).all()
    queued = [r for r in all_runs if r.status is RunStatus.QUEUED]
    assert len(all_runs) == 1, "no retry run should have been created"
    assert len(queued) == 1
    assert queued[0].id == claimed.id
    assert queued[0].resume_pending is True
    assert len(notifier.reports) == 0


def test_heartbeat_advances_during_a_silent_execution(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    """A run that emits no stream events for a while must still look alive
    to Task 13's stale-run sweeper: a background ticker, not on_event, is
    what keeps the heartbeat fresh."""
    import time

    run = _queued(session)

    class SlowSilentExecutor(FakeExecutor):
        def run(self, req, on_event):  # type: ignore[no-untyped-def]
            time.sleep(0.2)
            return super().run(req, on_event)

    executor, notifier = SlowSilentExecutor(), FakeNotifier()
    executor.queue(run.id, [])  # no events at all

    worker = _worker(sessions, executor, notifier, tmp_path)
    worker._settings.worker_heartbeat_seconds = 0.05

    before = datetime.now(UTC)
    assert worker.tick(NOW) is True
    after = datetime.now(UTC)

    session.expire_all()
    refreshed = session.get(Run, run.id)
    assert refreshed is not None
    assert refreshed.heartbeat_at is not None
    # claim_next_queued stamps the pinned NOW; only a real mid-execution
    # ticker tick would advance it to real wall-clock time in between.
    assert refreshed.heartbeat_at != NOW
    assert before <= refreshed.heartbeat_at <= after


# --------------------------------------------------------------------------
# C1: the gate MCP server must always be wired into the CLI invocation.
# --------------------------------------------------------------------------


def test_gate_tool_name_is_derived_from_the_server_and_tool_function() -> None:
    """GATE_TOOL is what the worker whitelists; the CLI names an MCP tool
    `mcp__<server>__<tool>`. If the constant and the actual server/tool names
    ever drift, the whitelist entry silently matches nothing and the only
    route to a gated action disappears."""
    assert GATE_TOOL == f"mcp__{GATE_SERVER_NAME}__{GATE_TOOL_FUNCTION}"

    server_src = (
        Path(__file__).resolve().parents[2] / "src" / "aicom" / "gate" / "server.py"
    ).read_text()
    assert f'FastMCP("{GATE_SERVER_NAME}")' in server_src
    assert f"def {GATE_TOOL_FUNCTION}(" in server_src


def test_gate_server_is_injected_for_an_agent_with_no_mcp_config(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    run = _queued(session, mcp_config={})
    executor, notifier = FakeExecutor(), FakeNotifier()
    executor.queue(run.id, [json.dumps({"type": "result"})])

    _worker(sessions, executor, notifier, tmp_path).tick(NOW)

    config = executor.requests[0].mcp_config
    assert GATE_SERVER_NAME in config
    assert config[GATE_SERVER_NAME]["args"] == ["-m", "aicom.gate.server"]
    assert GATE_TOOL in executor.requests[0].allowed_tools


def test_gate_server_env_carries_the_workers_own_database_url(
    sessions: sessionmaker[Session],
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate MCP server is a separate process; it has no other route to
    the run's database than what the worker puts in its spawn env. If it
    fell back to reading AICOM_DATABASE_URL from the ambient environment
    instead, it could silently bind to a different database than the one
    the run actually lives in -- the approval INSERT then fails with a
    foreign-key violation, the agent's run finishes `succeeded`, and there
    is no approval row and no Slack notification. This must never depend on
    the ambient environment agreeing with the worker's own settings."""
    # Ambient env deliberately disagrees with the worker's Settings, so a
    # fix that (re)reads os.environ instead of using settings.database_url
    # would be caught here.
    monkeypatch.setenv("AICOM_DATABASE_URL", "postgresql+psycopg://wrong-host/wrong-db")

    run = _queued(session, mcp_config={})
    executor, notifier = FakeExecutor(), FakeNotifier()
    executor.queue(run.id, [json.dumps({"type": "result"})])

    _worker(sessions, executor, notifier, tmp_path).tick(NOW)

    config = executor.requests[0].mcp_config
    assert config[GATE_SERVER_NAME]["env"]["AICOM_DATABASE_URL"] == (
        "postgresql+psycopg://unused/unused"
    )


def test_injected_gate_entry_wins_over_a_conflicting_agent_key(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    run = _queued(
        session,
        mcp_config={
            "gate": {"command": "/bin/false", "args": []},
            "docs": {"command": "docs-server", "args": []},
        },
    )
    executor, notifier = FakeExecutor(), FakeNotifier()
    executor.queue(run.id, [json.dumps({"type": "result"})])

    _worker(sessions, executor, notifier, tmp_path).tick(NOW)

    config = executor.requests[0].mcp_config
    assert config[GATE_SERVER_NAME]["args"] == ["-m", "aicom.gate.server"]
    assert config[GATE_SERVER_NAME]["command"] != "/bin/false"
    assert config["docs"] == {"command": "docs-server", "args": []}


# --------------------------------------------------------------------------
# C2: an approved gated tool is temporarily re-enabled for exactly one run.
# --------------------------------------------------------------------------

GATED = "mcp__broker__place_order"
OTHER_GATED = "mcp__broker__cancel_order"


def _decide(session: Session, run: Run, *, approved: bool, payload: dict) -> None:
    """Drive the real gate + Slack-resolution path so the run comes back
    QUEUED with resume_pending set and a decided approval attached."""
    from aicom.gate.service import request_approval
    from aicom.inbound.app import ResolveOutcome, resolve_approval
    from aicom.store.models import Approval
    from aicom.store.runs import claim_next_queued

    claimed = claim_next_queued(session, worker_id="w0", now=NOW)
    assert claimed is not None and claimed.id == run.id
    session.commit()
    request_approval(
        session,
        run_id=run.id,
        kind="execute_order",
        proposal="Place the order. Alt: skip. Reversible: no.",
        payload=payload,
        now=NOW,
    )
    session.commit()
    approval = session.scalar(select(Approval).where(Approval.run_id == run.id))
    assert approval is not None
    outcome = resolve_approval(
        session, nonce=approval.nonce, decided_by="U1", approved=approved, now=NOW
    )
    session.commit()
    assert outcome is ResolveOutcome.REQUEUED


def test_approved_resume_grants_exactly_the_approved_gated_tool(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    run = _queued(session, gated_tools=[GATED, OTHER_GATED])
    _decide(session, run, approved=True, payload={"tool": GATED, "qty": 1})

    executor, notifier = FakeExecutor(), FakeNotifier()
    executor.queue(run.id, [json.dumps({"type": "result"})])
    _worker(sessions, executor, notifier, tmp_path).tick(NOW)

    allowed = executor.requests[0].allowed_tools
    assert set(allowed) == {"Read", "Grep", "WebSearch", GATE_TOOL, GATED}


def test_approved_payload_naming_a_tool_outside_gated_tools_is_refused(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    """The payload is agent-supplied. Honouring an arbitrary tool name in it
    would let the agent pick its own reward: the operator signs off on a
    proposal, not on a tool string the agent chose."""
    run = _queued(session, gated_tools=[GATED])
    _decide(session, run, approved=True, payload={"tool": "Bash"})

    executor, notifier = FakeExecutor(), FakeNotifier()
    executor.queue(run.id, [json.dumps({"type": "result"})])
    _worker(sessions, executor, notifier, tmp_path).tick(NOW)

    allowed = executor.requests[0].allowed_tools
    assert "Bash" not in allowed
    assert set(allowed) == {"Read", "Grep", "WebSearch", GATE_TOOL}


def test_rejected_approval_grants_nothing(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    run = _queued(session, gated_tools=[GATED])
    _decide(session, run, approved=False, payload={"tool": GATED})

    executor, notifier = FakeExecutor(), FakeNotifier()
    executor.queue(run.id, [json.dumps({"type": "result"})])
    _worker(sessions, executor, notifier, tmp_path).tick(NOW)

    assert set(executor.requests[0].allowed_tools) == {"Read", "Grep", "WebSearch", GATE_TOOL}


def test_temporary_grant_does_not_survive_into_a_later_run(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    run = _queued(session, gated_tools=[GATED])
    _decide(session, run, approved=True, payload={"tool": GATED})

    executor, notifier = FakeExecutor(), FakeNotifier()
    executor.queue(run.id, [json.dumps({"type": "result"})])
    worker = _worker(sessions, executor, notifier, tmp_path)
    worker.tick(NOW)
    assert GATED in executor.requests[0].allowed_tools

    # A later run of the same task, same agent: the approval row is still
    # APPROVED in the database, but the grant was for one execution only.
    later = Run(id=uuid.uuid4(), task_id=run.task_id, attempt=2)
    session.add(later)
    session.commit()
    executor.queue(later.id, [json.dumps({"type": "result"})])
    worker.tick(NOW)

    assert set(executor.requests[1].allowed_tools) == {"Read", "Grep", "WebSearch", GATE_TOOL}


def test_gated_tool_also_in_allowed_tools_fails_the_run_and_notifies(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    """A gated tool sitting in the always-allowed list makes the tool callable
    without sign-off, i.e. silently disables the entire boundary. That must
    fail loudly, not run."""
    run = _queued(
        session,
        allowed_tools=["Read", GATED],
        gated_tools=[GATED],
    )
    executor, notifier = FakeExecutor(), FakeNotifier()

    _worker(sessions, executor, notifier, tmp_path).tick(NOW)

    assert executor.requests == []  # nothing was ever executed
    session.expire_all()
    refreshed = session.get(Run, run.id)
    assert refreshed is not None
    assert refreshed.status is RunStatus.FAILED
    told = notifier.notices + [r.summary for r in notifier.reports]
    assert any(GATED in message for message in told)


# --------------------------------------------------------------------------
# I2: a leaked reader thread must not write events after finalisation.
# --------------------------------------------------------------------------


def test_late_event_after_execution_is_ignored(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    """A grandchild can keep the stdout pipe open, leaving a daemon reader
    thread alive past run(). Its on_event callbacks must be dropped, not
    written against a run that has already been finalised."""
    from aicom.executor.stream import ParsedEvent
    from aicom.store.models import Event

    run = _queued(session)

    class CapturingExecutor(FakeExecutor):
        captured: object = None

        def run(self, req, on_event):  # type: ignore[no-untyped-def]
            type(self).captured = on_event
            return super().run(req, on_event)

    executor, notifier = CapturingExecutor(), FakeNotifier()
    executor.queue(run.id, [json.dumps({"type": "assistant", "message": {}})])
    _worker(sessions, executor, notifier, tmp_path).tick(NOW)

    session.expire_all()
    before = len(session.scalars(select(Event).where(Event.run_id == run.id)).all())

    late = CapturingExecutor.captured
    assert callable(late)
    late(ParsedEvent(seq=99, type="assistant", payload={"late": True}))

    session.expire_all()
    after = session.scalars(select(Event).where(Event.run_id == run.id)).all()
    assert len(after) == before
    assert all(e.payload.get("late") is not True for e in after)
