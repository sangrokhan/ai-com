import shutil
import uuid
from pathlib import Path


def prepare_workspace(root: Path, agent_name: str, run_id: uuid.UUID) -> Path:
    workspace = Path(root) / agent_name / str(run_id)
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def cleanup_workspace(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
