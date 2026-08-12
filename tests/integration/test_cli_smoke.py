import shutil
import uuid
from pathlib import Path

import pytest

from aicom.domain.enums import ExitReason
from aicom.executor.base import RunRequest
from aicom.executor.cli import ClaudeCliExecutor
from aicom.orchestrator.worker import GATE_SERVER_NAME, GATE_TOOL, gate_server_spec


@pytest.mark.smoke
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude binary not installed")
def test_real_cli_completes_a_trivial_task(tmp_path: Path) -> None:
    """Runs the real binary with the same gate-server injection the worker
    performs, so a spawn spec the CLI cannot actually launch fails here rather
    than in production, where it would leave agents with no route to sign-off.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    request = RunRequest(
        run_id=uuid.uuid4(),
        prompt="Write the single word 'ok' to a file named result.txt. Then stop.",
        workspace=workspace,
        allowed_tools=("Write", GATE_TOOL),
        mcp_config={
            GATE_SERVER_NAME: gate_server_spec("postgresql+psycopg://unused/unused")
        },
        env={},
        resume_session_id=None,
        timeout_seconds=180,
    )

    outcome = ClaudeCliExecutor().run(request, lambda _e: None)

    assert outcome.reason is ExitReason.COMPLETED
    assert outcome.session_id
    assert (workspace / "result.txt").read_text().strip().lower().startswith("ok")
