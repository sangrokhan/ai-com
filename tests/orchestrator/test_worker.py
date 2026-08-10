import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aicom.config import Settings
from aicom.domain.enums import ExitReason, RunStatus
from aicom.executor.fake import FakeExecutor
from aicom.notify.fake import FakeNotifier
from aicom.orchestrator.worker import Worker
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


def _queued(session: Session) -> Run:
    agent = make_agent(session)
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
    worker.finalize(session, fresh, outcome, request, NOW)
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
    worker.finalize(session, fresh, crashed, request, NOW)
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
