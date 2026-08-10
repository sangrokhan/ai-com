import shutil
import subprocess
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from aicom.store.models import Artifact


def commit_run_artifacts(
    session: Session, *, run_id: uuid.UUID, workspace: Path, repo: Path, label: str
) -> str | None:
    """Copy run outputs into the artifact repo and commit them.

    Called serially by the orchestrator, so concurrent runs never contend on git;
    no locking or concurrency handling is needed here.
    """
    files = [
        p
        for p in workspace.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(workspace).parts
    ]
    if not files:
        return None

    dest_root = repo / str(run_id)
    for src in files:
        dest = dest_root / src.relative_to(workspace)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    try:
        subprocess.run(
            ["git", "add", "--", str(dest_root)],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", f"artifacts: {label} ({run_id})"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        shutil.rmtree(dest_root, ignore_errors=True)
        raise

    for src in files:
        rel = src.relative_to(workspace)
        session.add(
            Artifact(
                id=uuid.uuid4(),
                run_id=run_id,
                kind=src.suffix.lstrip(".") or "file",
                git_ref=sha,
                path=str(rel),
            )
        )
    return sha
