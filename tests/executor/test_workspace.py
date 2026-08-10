import uuid
from pathlib import Path

from aicom.executor.workspace import cleanup_workspace, prepare_workspace


def test_prepare_creates_isolated_directory_per_run(tmp_path: Path) -> None:
    run_id = uuid.uuid4()
    ws = prepare_workspace(tmp_path, "researcher", run_id)

    assert ws.is_dir()
    assert ws.parent.name == "researcher"
    assert ws.name == str(run_id)


def test_prepare_is_idempotent(tmp_path: Path) -> None:
    run_id = uuid.uuid4()
    first = prepare_workspace(tmp_path, "a", run_id)
    (first / "keep.txt").write_text("data")
    second = prepare_workspace(tmp_path, "a", run_id)

    assert first == second
    assert (second / "keep.txt").read_text() == "data"


def test_cleanup_removes_tree_and_tolerates_missing(tmp_path: Path) -> None:
    ws = prepare_workspace(tmp_path, "a", uuid.uuid4())
    cleanup_workspace(ws)
    assert not ws.exists()
    cleanup_workspace(ws)  # must not raise
