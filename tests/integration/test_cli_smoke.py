import shutil
import uuid
from pathlib import Path

import pytest

from aicom.domain.enums import ExitReason
from aicom.executor.base import RunRequest
from aicom.executor.cli import ClaudeCliExecutor


@pytest.mark.smoke
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude binary not installed")
def test_real_cli_completes_a_trivial_task(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    request = RunRequest(
        run_id=uuid.uuid4(),
        prompt="Write the single word 'ok' to a file named result.txt. Then stop.",
        workspace=workspace,
        allowed_tools=("Write",),
        mcp_config={},
        env={},
        resume_session_id=None,
        timeout_seconds=180,
    )

    outcome = ClaudeCliExecutor().run(request, lambda _e: None)

    assert outcome.reason is ExitReason.COMPLETED
    assert outcome.session_id
    assert (workspace / "result.txt").read_text().strip().lower().startswith("ok")
