import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from aicom.config import Settings
from aicom.domain.enums import ApprovalStatus, ExitReason, RunStatus
from aicom.executor.base import RunOutcome, RunRequest
from aicom.executor.fake import FakeExecutor
from aicom.executor.stream import ParsedEvent
from aicom.gate.service import request_approval
from aicom.inbound.app import create_app
from aicom.notify.fake import FakeNotifier
from aicom.orchestrator.sweeper import Sweeper
from aicom.orchestrator.worker import Worker
from aicom.store.models import Approval, Run
from tests.inbound.test_app import SECRET, _action, _post
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
CONSOLE_PASSWORD = "test-console-password"


def _settings(tmp_path: Path) -> Settings:
    repo = tmp_path / "artifacts"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    return Settings(
        workspace_root=tmp_path / "ws",
        artifact_repo_path=repo,
        slack_signing_secret=SECRET,
        slack_approver_ids=("U_OWNER",),
        database_url="postgresql+psycopg://unused/unused",
        console_password=CONSOLE_PASSWORD,
        session_secret="session-secret",
    )


def _logged_in_client(app: FastAPI) -> TestClient:
    client = TestClient(app)
    login = client.post("/auth/login", json={"password": CONSOLE_PASSWORD})
    assert login.status_code == 200
    return client


class GateThenFinishExecutor(FakeExecutor):
    """First call parks for sign-off; second call (resume) writes a report."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        super().__init__()
        self._sessions = sessions
        self.calls = 0

    def run(
        self, req: RunRequest, on_event: Callable[[ParsedEvent], None]
    ) -> RunOutcome:
        self.calls += 1
        self.requests.append(req)
        if self.calls == 1:
            with self._sessions() as s:
                request_approval(
                    s,
                    run_id=req.run_id,
                    kind="spend",
                    proposal="Buy feed X, $20/mo. Alt: free tier. Reversible: yes.",
                    payload={"amount_usd": 20},
                    now=NOW,
                )
                s.commit()
            return RunOutcome(reason=ExitReason.GATE_REQUESTED, exit_code=0, session_id="sess-1")
        (req.workspace / "report.md").write_text("purchased and configured")
        return RunOutcome(reason=ExitReason.COMPLETED, exit_code=0, session_id="sess-1")


def test_full_sign_off_cycle(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    agent = make_agent(session)
    session.commit()
    settings = _settings(tmp_path)
    notifier = FakeNotifier()
    executor = GateThenFinishExecutor(sessions)
    worker = Worker(sessions, executor, notifier, settings, worker_id="w1")
    client = _logged_in_client(create_app(sessions, settings))

    created = client.post(
        "/tasks",
        json={"agent_id": str(agent.id), "title": "Buy data", "goal": "Get a feed."},
    )
    assert created.status_code == 201

    # 1. run parks for sign-off
    assert worker.tick(NOW) is True
    session.expire_all()
    run = session.query(Run).one()
    assert run.status is RunStatus.AWAITING_APPROVAL
    assert len(notifier.approvals) == 1

    # 2. other work is not blocked: no run is claimable, tick returns False cleanly
    assert worker.tick(NOW + timedelta(seconds=1)) is False

    # 3. reminder escalates without ever denying (approvals never expire)
    assert Sweeper(sessions, notifier).sweep_reminders(NOW + timedelta(minutes=31)) == 1
    session.expire_all()
    assert session.query(Approval).one().status is ApprovalStatus.PENDING

    # 4. operator approves in Slack
    approval = session.query(Approval).one()
    assert _post(client, _action(approval.nonce)).status_code == 200

    # 5. run resumes with the decision and finishes
    assert worker.tick(NOW + timedelta(minutes=40)) is True
    session.expire_all()
    run = session.query(Run).one()
    assert run.status is RunStatus.SUCCEEDED
    assert executor.requests[1].resume_session_id == "sess-1"
    assert "APPROVED" in executor.requests[1].prompt
    assert (settings.artifact_repo_path / str(run.id)).exists()


def test_usage_limit_pause_then_automatic_resume(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    agent = make_agent(session)
    session.commit()
    settings = _settings(tmp_path)
    notifier = FakeNotifier()
    executor = FakeExecutor()
    worker = Worker(sessions, executor, notifier, settings, worker_id="w1")
    client = _logged_in_client(create_app(sessions, settings))
    client.post("/tasks", json={"agent_id": str(agent.id), "title": "t", "goal": "g"})

    session.expire_all()
    run_id = session.query(Run).one().id
    reset = int((NOW + timedelta(hours=1)).timestamp())
    executor.queue(
        run_id,
        [
            json.dumps(
                {"type": "result", "is_error": True, "result": f"usage limit reached|{reset}"}
            )
        ],
    )

    # 1. usage-limit outcome requeues without burning an attempt, pauses globally, notifies once
    assert worker.tick(NOW) is True
    session.expire_all()
    paused_run = session.get(Run, run_id)
    assert paused_run is not None
    assert paused_run.status is RunStatus.QUEUED
    assert paused_run.attempt == 1
    assert len(notifier.notices) == 1

    # 2. while paused, tick() claims nothing and sends no duplicate notice
    assert worker.tick(NOW + timedelta(minutes=5)) is False
    assert len(notifier.notices) == 1

    # 3. once the reset time passes, the worker resumes automatically
    executor.queue(run_id, [json.dumps({"type": "result"})])
    assert worker.tick(NOW + timedelta(hours=1, minutes=1)) is True
    session.expire_all()
    finished = session.get(Run, run_id)
    assert finished is not None
    assert finished.status is RunStatus.SUCCEEDED
