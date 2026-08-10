import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    subprocess.run(
        ["git", "config", "user.email", "a@b.c"], cwd=tmp_path / "artifacts", check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=tmp_path / "artifacts", check=True
    )
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

    statuses = [
        r.status for r in session.query(Run).filter(Run.task_id == run.task_id).all()
    ]
    assert statuses.count(RunStatus.FAILED) == 1
    assert any("failed" in n.lower() for n in notifier.notices + [
        r.summary for r in notifier.reports
    ])


def test_usage_limit_pauses_globally_and_requeues_without_burning_an_attempt(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    run = _queued(session)
    reset_epoch = int((NOW + timedelta(hours=2)).timestamp())
    executor, notifier = FakeExecutor(), FakeNotifier()
    executor.queue(
        run.id,
        [json.dumps({"type": "result", "is_error": True, "result": f"usage limit reached|{reset_epoch}"})],
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

    run = _queued(session)
    executor, notifier = FakeExecutor(), FakeNotifier()

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
