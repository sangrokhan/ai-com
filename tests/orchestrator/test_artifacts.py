import subprocess
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from aicom.orchestrator.artifacts import commit_run_artifacts
from aicom.store.models import Artifact, Run, Task
from tests.store.test_models import make_agent


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "artifacts"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    return repo


def _run(session: Session) -> Run:
    agent = make_agent(session)
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="t", goal="g")
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1)
    session.add(run)
    session.commit()
    return run


def test_commits_workspace_files_and_records_artifact_rows(
    session: Session, tmp_path: Path
) -> None:
    run = _run(session)
    workspace = tmp_path / "ws"
    (workspace / "sub").mkdir(parents=True)
    (workspace / "report.md").write_text("findings")
    (workspace / "sub" / "data.csv").write_text("a,b")
    repo = _repo(tmp_path)

    sha = commit_run_artifacts(
        session, run_id=run.id, workspace=workspace, repo=repo, label="researcher/t"
    )
    session.commit()

    assert sha is not None and len(sha) == 40
    dest = repo / str(run.id)
    assert (dest / "report.md").read_text() == "findings"
    assert (dest / "sub" / "data.csv").read_text() == "a,b"

    rows = list(session.scalars(select(Artifact).where(Artifact.run_id == run.id)))
    assert {Path(r.path).name for r in rows} == {"report.md", "data.csv"}
    assert {r.git_ref for r in rows} == {sha}


def test_empty_workspace_produces_no_commit(session: Session, tmp_path: Path) -> None:
    run = _run(session)
    workspace = tmp_path / "empty"
    workspace.mkdir()

    assert (
        commit_run_artifacts(
            session, run_id=run.id, workspace=workspace, repo=_repo(tmp_path), label="x"
        )
        is None
    )
