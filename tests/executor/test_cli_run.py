"""Exercises ClaudeCliExecutor.run's subprocess-management logic against a
mocked subprocess.Popen. These are not smoke tests against the real `claude`
binary (that's Task 15's job) - they validate our own thread/deadline/guard
code by substituting a controllable fake process.
"""

import json
import os
import subprocess
import threading
import uuid
from pathlib import Path

import pytest

from aicom.domain.enums import ExitReason
from aicom.executor.base import RunRequest
from aicom.executor.cli import ClaudeCliExecutor
from aicom.executor.secrets import MissingSecret


def _req(tmp_path: Path, *, mcp_config: dict | None = None, timeout_seconds: int = 5) -> RunRequest:
    return RunRequest(
        run_id=uuid.uuid4(),
        prompt="do the thing",
        workspace=tmp_path / "ws",
        allowed_tools=("Read",),
        mcp_config=mcp_config if mcp_config is not None else {},
        env={},
        resume_session_id=None,
        timeout_seconds=timeout_seconds,
    )


def test_unsanitized_mcp_config_raises_before_spawning_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned = False

    def fake_popen(*_args: object, **_kwargs: object) -> None:
        nonlocal spawned
        spawned = True
        raise AssertionError("subprocess.Popen must not be called for an unsanitized config")

    monkeypatch.setattr("aicom.executor.cli.subprocess.Popen", fake_popen)

    req = _req(tmp_path, mcp_config={"broker": {"env": {"K": {"secret_ref": "broker/alpaca"}}}})
    executor = ClaudeCliExecutor(binary="claude")

    with pytest.raises(MissingSecret):
        executor.run(req, lambda _e: None)
    assert not spawned


def test_timeout_is_enforced_even_when_child_holds_stdout_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout_r, stdout_w = os.pipe()
    stderr_r, stderr_w = os.pipe()

    class HangingProcess:
        def __init__(self) -> None:
            self.stdout = os.fdopen(stdout_r, "r")
            self.stderr = os.fdopen(stderr_r, "r")
            self.returncode: int | None = None
            self._killed = threading.Event()

        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None:
                # Simulate a child that is alive but never produces more
                # output and never exits on its own: the deadline must
                # still fire without waiting for stdout to close.
                raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
            self._killed.wait(5)
            self.returncode = -9
            return self.returncode

        def kill(self) -> None:
            os.close(stdout_w)
            os.close(stderr_w)
            self._killed.set()

    fake_proc = HangingProcess()
    monkeypatch.setattr("aicom.executor.cli.subprocess.Popen", lambda *a, **kw: fake_proc)

    req = _req(tmp_path, timeout_seconds=1)
    executor = ClaudeCliExecutor(binary="claude")

    outcome = executor.run(req, lambda _e: None)

    assert outcome.reason is ExitReason.TIMEOUT


def test_large_concurrent_stdout_and_stderr_do_not_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout_r, stdout_w = os.pipe()
    stderr_r, stderr_w = os.pipe()

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = os.fdopen(stdout_r, "r")
            self.stderr = os.fdopen(stderr_r, "r")
            self.returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

        def kill(self) -> None:  # pragma: no cover - not expected to be hit
            pass

    def writer() -> None:
        # Bigger than a typical 64KB OS pipe buffer. A parent that reads
        # stdout to EOF before touching stderr would deadlock here, since
        # this write blocks until stderr is drained.
        big = "x" * 200_000
        with os.fdopen(stderr_w, "w") as werr:
            werr.write(big)
        with os.fdopen(stdout_w, "w") as wout:
            for _ in range(5):
                wout.write(json.dumps({"type": "assistant"}) + "\n")

    fake_proc = FakeProcess()

    def fake_popen(*_args: object, **_kwargs: object) -> FakeProcess:
        threading.Thread(target=writer, daemon=True).start()
        return fake_proc

    monkeypatch.setattr("aicom.executor.cli.subprocess.Popen", fake_popen)

    req = _req(tmp_path, timeout_seconds=10)
    executor = ClaudeCliExecutor(binary="claude")
    seen = []

    outcome = executor.run(req, seen.append)

    assert len(seen) == 5
    assert outcome.exit_code == 0
    assert outcome.reason is ExitReason.COMPLETED
