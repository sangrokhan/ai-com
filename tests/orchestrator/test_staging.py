import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from aicom.orchestrator.staging import PREVIOUS_DIR, stage_previous_reports
from aicom.store.artifacts_query import REPORT_FILENAME
from aicom.store.models import Artifact, Run, Schedule, Task
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def _schedule(session: Session) -> Schedule:
    agent = make_agent(session)
    schedule = Schedule(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name=f"beat-{uuid.uuid4().hex[:6]}",
        cron="0 9 * * *",
        timezone="UTC",
        title_template="t",
        goal_template="g",
        next_due_at=NOW,
    )
    session.add(schedule)
    session.flush()
    return schedule


def _finished_run(
    session: Session,
    schedule: Schedule | None,
    repo: Path,
    *,
    body: str,
    started_at: datetime,
    write_file: bool = True,
) -> Run:
    agent_id = schedule.agent_id if schedule else make_agent(session).id
    task = Task(
        id=uuid.uuid4(),
        agent_id=agent_id,
        title="t",
        goal="g",
        schedule_id=schedule.id if schedule else None,
    )
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1, started_at=started_at)
    session.add(run)
    session.flush()
    session.add(
        Artifact(
            id=uuid.uuid4(), run_id=run.id, kind="md", path=REPORT_FILENAME, git_ref="a"
        )
    )
    session.flush()
    if write_file:
        destination = repo / str(run.id)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / REPORT_FILENAME).write_text(body)
    return run


def _pending_run(session: Session, schedule: Schedule | None) -> Run:
    agent_id = schedule.agent_id if schedule else make_agent(session).id
    task = Task(
        id=uuid.uuid4(),
        agent_id=agent_id,
        title="t",
        goal="g",
        schedule_id=schedule.id if schedule else None,
    )
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1)
    session.add(run)
    session.commit()
    return run


def test_scheduled_run_gets_its_previous_reports(
    session: Session, tmp_path: Path
) -> None:
    repo = tmp_path / "artifacts"
    schedule = _schedule(session)
    _finished_run(session, schedule, repo, body="older", started_at=NOW - timedelta(days=2))
    _finished_run(session, schedule, repo, body="newer", started_at=NOW - timedelta(days=1))
    run = _pending_run(session, schedule)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    staged = stage_previous_reports(session, run, workspace, repo)

    assert staged == 2
    files = sorted((workspace / PREVIOUS_DIR).iterdir())
    assert len(files) == 2
    # Newest first, so the ordering is visible in the filenames.
    assert files[0].read_text() == "newer"
    assert files[1].read_text() == "older"


def test_a_manual_run_gets_nothing(session: Session, tmp_path: Path) -> None:
    repo = tmp_path / "artifacts"
    run = _pending_run(session, None)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    assert stage_previous_reports(session, run, workspace, repo) == 0
    assert not (workspace / PREVIOUS_DIR).exists()


def test_no_previous_reports_leaves_no_directory(
    session: Session, tmp_path: Path
) -> None:
    # The agent should not have to tell "nothing yet" from "staging broke".
    repo = tmp_path / "artifacts"
    schedule = _schedule(session)
    run = _pending_run(session, schedule)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    assert stage_previous_reports(session, run, workspace, repo) == 0
    assert not (workspace / PREVIOUS_DIR).exists()


def test_a_report_missing_from_disk_is_skipped(
    session: Session, tmp_path: Path
) -> None:
    repo = tmp_path / "artifacts"
    schedule = _schedule(session)
    _finished_run(
        session, schedule, repo, body="gone", started_at=NOW - timedelta(days=2),
        write_file=False,
    )
    _finished_run(session, schedule, repo, body="here", started_at=NOW - timedelta(days=1))
    run = _pending_run(session, schedule)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    assert stage_previous_reports(session, run, workspace, repo) == 1
    assert (workspace / PREVIOUS_DIR).iterdir().__next__().read_text() == "here"


def test_limit_bounds_what_is_staged(session: Session, tmp_path: Path) -> None:
    repo = tmp_path / "artifacts"
    schedule = _schedule(session)
    for day in range(4):
        _finished_run(
            session, schedule, repo, body=f"day{day}", started_at=NOW - timedelta(days=day)
        )
    run = _pending_run(session, schedule)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    assert stage_previous_reports(session, run, workspace, repo, limit=2) == 2


def test_staging_failure_does_not_fail_the_run(
    sessions, session: Session, tmp_path: Path, monkeypatch
) -> None:
    import json

    from aicom.config import Settings
    from aicom.executor.fake import FakeExecutor
    from aicom.notify.fake import FakeNotifier
    from aicom.orchestrator import worker as worker_module
    from aicom.orchestrator.worker import Worker

    def explode(*args: object, **kwargs: object) -> int:
        raise RuntimeError("staging is broken")

    monkeypatch.setattr(worker_module, "stage_previous_reports", explode)

    repo = tmp_path / "artifacts"
    repo.mkdir(parents=True)
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)

    schedule = _schedule(session)
    run = _pending_run(session, schedule)

    settings = Settings(
        workspace_root=tmp_path / "ws",
        artifact_repo_path=repo,
        database_url="postgresql+psycopg://unused/unused",
    )
    executor = FakeExecutor()
    executor.queue(run.id, [json.dumps({"type": "result"})])
    worker = Worker(sessions, executor, FakeNotifier(), settings, worker_id="w1")

    assert worker.tick(NOW) is True

    session.expire_all()
    assert session.get(Run, run.id).status.value == "succeeded"
