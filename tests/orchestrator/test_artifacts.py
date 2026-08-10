import logging
import subprocess
import uuid
from pathlib import Path

import pytest
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
    assert {r.path for r in rows} == {"report.md", "sub/data.csv"}
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


def test_failed_commit_leaves_no_orphaned_staged_entries(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _run(session)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "report.md").write_text("findings")

    repo = tmp_path / "artifacts"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    # Force `git commit` to fail with "please tell me who you are" by hiding
    # any ambient identity config (local repo config is intentionally left
    # unset by not calling _repo(), which is what configures it).
    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.delenv("GIT_AUTHOR_NAME", raising=False)
    monkeypatch.delenv("GIT_AUTHOR_EMAIL", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_NAME", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_EMAIL", raising=False)
    monkeypatch.delenv("EMAIL", raising=False)

    with pytest.raises(subprocess.CalledProcessError):
        commit_run_artifacts(session, run_id=run.id, workspace=workspace, repo=repo, label="x")

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status.strip() == "", "index/worktree must be clean after a failed commit"


def test_nested_git_directory_is_skipped_but_other_files_still_commit(
    session: Session, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    run = _run(session)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "report.md").write_text("findings")
    nested_git = workspace / "cloned" / ".git"
    nested_git.mkdir(parents=True)
    (nested_git / "HEAD").write_text("ref: refs/heads/main\n")
    (workspace / "cloned" / "README.md").write_text("cloned content")
    repo = _repo(tmp_path)

    with caplog.at_level(logging.WARNING, logger="aicom.orchestrator.artifacts"):
        sha = commit_run_artifacts(
            session, run_id=run.id, workspace=workspace, repo=repo, label="x"
        )
    session.commit()

    assert sha is not None
    dest = repo / str(run.id)
    assert (dest / "report.md").read_text() == "findings"
    assert (dest / "cloned" / "README.md").read_text() == "cloned content"
    assert not (dest / "cloned" / ".git").exists()

    rows = list(session.scalars(select(Artifact).where(Artifact.run_id == run.id)))
    assert {r.path for r in rows} == {"report.md", "cloned/README.md"}

    assert any(".git" in record.message for record in caplog.records)
    assert any("cloned" in record.message for record in caplog.records)
