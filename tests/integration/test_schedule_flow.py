import json
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aicom.api.routes import make_router
from aicom.config import Settings
from aicom.domain.enums import RunStatus, TaskStatus
from aicom.executor.fake import FakeExecutor
from aicom.notify.fake import FakeNotifier
from aicom.orchestrator.scheduler import Scheduler
from aicom.orchestrator.worker import Worker
from aicom.store.models import Run, Schedule, Task
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    repo = tmp_path / "artifacts"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    return Settings(
        workspace_root=tmp_path / "ws",
        artifact_repo_path=repo,
        database_url="postgresql+psycopg://unused/unused",
    )


def test_schedule_creates_a_task_the_worker_then_executes(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    agent = make_agent(session)
    session.commit()

    app = FastAPI()
    app.include_router(make_router(sessions))
    client = TestClient(app)
    created = client.post(
        "/schedules",
        json={
            "agent_id": str(agent.id),
            "name": "hourly scan",
            "cron": "0 * * * *",
            "timezone": "UTC",
            "title_template": "Hourly scan",
            "goal_template": "Scan and report.",
        },
    )
    assert created.status_code == 201
    schedule_id = uuid.UUID(created.json()["id"])

    # Make it due.
    session.expire_all()
    schedule = session.get(Schedule, schedule_id)
    assert schedule is not None
    schedule.next_due_at = NOW - timedelta(minutes=1)
    session.commit()

    notifier = FakeNotifier()
    assert Scheduler(sessions, notifier).tick(NOW) == 1

    session.expire_all()
    task = session.scalar(select(Task).where(Task.schedule_id == schedule_id))
    assert task is not None
    run = session.scalar(select(Run).where(Run.task_id == task.id))
    assert run is not None
    assert run.status is RunStatus.QUEUED

    executor = FakeExecutor()
    executor.queue(run.id, [json.dumps({"type": "result"})])
    worker = Worker(sessions, executor, notifier, _settings(tmp_path), worker_id="w1")
    assert worker.tick(NOW) is True

    session.expire_all()
    finished = session.get(Run, run.id)
    assert finished is not None
    assert finished.status is RunStatus.SUCCEEDED
    assert finished.task.status is TaskStatus.DONE

    # The cycle is finished, so the next due firing is no longer skipped.
    refreshed = session.get(Schedule, schedule_id)
    assert refreshed is not None
    assert Scheduler(sessions, notifier).tick(refreshed.next_due_at + timedelta(seconds=1)) == 1
    session.expire_all()
    assert len(list(session.scalars(select(Task).where(Task.schedule_id == schedule_id)))) == 2
