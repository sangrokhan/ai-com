import logging
import shutil
import subprocess
import uuid
from collections import Counter
from pathlib import Path

from sqlalchemy.orm import Session

from aicom.store.models import Artifact

logger = logging.getLogger(__name__)


def commit_run_artifacts(
    session: Session, *, run_id: uuid.UUID, workspace: Path, repo: Path, label: str
) -> str | None:
    """Copy run outputs into the artifact repo and commit them.

    Called serially by the orchestrator, so concurrent runs never contend on git;
    no locking or concurrency handling is needed here.
    """
    all_files = [(p, p.relative_to(workspace).parts) for p in workspace.rglob("*") if p.is_file()]
    files = [p for p, parts in all_files if ".git" not in parts]

    skipped_git_dirs = Counter(
        parts[: parts.index(".git") + 1] for _, parts in all_files if ".git" in parts
    )
    for git_dir_parts, count in skipped_git_dirs.items():
        logger.warning(
            "skipping nested .git directory %s in workspace %s (%d files not committed)",
            Path(*git_dir_parts),
            workspace,
            count,
        )

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
            [
                "git",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "--no-verify",
                "-m",
                f"artifacts: {label} ({run_id})",
            ],
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
        # Unstage whatever this call staged before removing the files, so a
        # failed commit never leaves orphaned index entries for the next
        # (unrelated) call to sweep into its own commit.
        subprocess.run(
            ["git", "reset", "--", str(dest_root)],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
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
