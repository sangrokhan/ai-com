import uuid
from pathlib import Path

from aicom.executor.base import RunRequest
from aicom.executor.cli import build_argv


def _req(tmp_path: Path, resume: str | None = None) -> RunRequest:
    return RunRequest(
        run_id=uuid.uuid4(),
        prompt="research widgets",
        workspace=tmp_path / "ws",
        allowed_tools=("Read", "Grep", "mcp__gate__request_approval"),
        mcp_config={},
        env={},
        resume_session_id=resume,
        timeout_seconds=120,
    )


def test_argv_pins_stream_json_whitelist_and_workspace(tmp_path: Path) -> None:
    argv = build_argv(_req(tmp_path), binary="claude", mcp_config_path=tmp_path / "mcp.json")

    assert argv[0] == "claude"
    assert "-p" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    assert argv[argv.index("--allowedTools") + 1] == "Read,Grep,mcp__gate__request_approval"
    assert argv[argv.index("--add-dir") + 1] == str(tmp_path / "ws")
    assert argv[argv.index("--mcp-config") + 1] == str(tmp_path / "mcp.json")
    assert "--resume" not in argv
    assert argv[-1] == "research widgets"
    # `--mcp-config` (like `--allowedTools` and `--add-dir`) is a variadic CLI
    # option: with no `--` to stop it, it greedily swallows the prompt that
    # follows as another config value instead of leaving it as the positional
    # prompt argument. Pin the separator immediately before the prompt, not
    # just "present somewhere", so moving or dropping it fails this test.
    assert argv[-2] == "--"
    assert argv.index("--") > argv.index("--mcp-config") + 1


def test_resume_flag_present_only_when_session_id_given(tmp_path: Path) -> None:
    argv = build_argv(
        _req(tmp_path, resume="sess-77"), binary="claude", mcp_config_path=tmp_path / "mcp.json"
    )
    assert argv[argv.index("--resume") + 1] == "sess-77"
    assert argv[-2] == "--"
    assert argv[-1] == "research widgets"
    assert argv.index("--") > argv.index("--mcp-config") + 1
