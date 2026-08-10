import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from aicom.domain.enums import ExitReason
from aicom.executor.base import Executor, RunOutcome, RunRequest
from aicom.executor.stream import ParsedEvent, StreamParser


def build_argv(req: RunRequest, *, binary: str, mcp_config_path: Path) -> list[str]:
    argv = [
        binary,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--allowedTools",
        ",".join(req.allowed_tools),
        "--add-dir",
        str(req.workspace),
        "--mcp-config",
        str(mcp_config_path),
    ]
    if req.resume_session_id:
        argv += ["--resume", req.resume_session_id]
    argv.append(req.prompt)
    return argv


class ClaudeCliExecutor(Executor):
    def __init__(self, binary: str = "claude") -> None:
        self._binary = binary

    def run(
        self, req: RunRequest, on_event: Callable[[ParsedEvent], None]
    ) -> RunOutcome:
        parser = StreamParser()
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "mcp.json"
            config_path.write_text(json.dumps({"mcpServers": req.mcp_config}))
            argv = build_argv(req, binary=self._binary, mcp_config_path=config_path)
            env = {**os.environ, **req.env, "AICOM_RUN_ID": str(req.run_id)}

            proc = subprocess.Popen(
                argv,
                cwd=req.workspace,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            timed_out = False
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    event = parser.feed(line)
                    if event is not None:
                        on_event(event)
                proc.wait(timeout=req.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                proc.wait()
            stderr = proc.stderr.read() if proc.stderr else ""

        return RunOutcome(
            reason=self._reason(proc.returncode, timed_out, parser, stderr),
            exit_code=proc.returncode,
            session_id=parser.session_id,
            cost_usd=parser.cost_usd,
            token_in=parser.token_in,
            token_out=parser.token_out,
            usage_limit_text=parser.usage_limit_text or _limit_text(stderr),
        )

    @staticmethod
    def _reason(
        code: int, timed_out: bool, parser: StreamParser, stderr: str
    ) -> ExitReason:
        if timed_out:
            return ExitReason.TIMEOUT
        if parser.saw_usage_limit or _limit_text(stderr):
            return ExitReason.USAGE_LIMIT
        return ExitReason.COMPLETED if code == 0 else ExitReason.CRASHED


def _limit_text(stderr: str) -> str | None:
    lowered = stderr.lower()
    if "usage limit" in lowered or "rate limit" in lowered:
        return stderr.strip()[:500]
    return None
