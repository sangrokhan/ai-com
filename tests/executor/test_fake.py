import json
import uuid
from pathlib import Path

from aicom.domain.enums import ExitReason
from aicom.executor.base import RunRequest
from aicom.executor.fake import FakeExecutor
from aicom.executor.stream import ParsedEvent


def _req(run_id: uuid.UUID, tmp_path: Path) -> RunRequest:
    return RunRequest(
        run_id=run_id,
        prompt="do the thing",
        workspace=tmp_path,
        allowed_tools=("Read",),
        mcp_config={},
        env={},
        resume_session_id=None,
        timeout_seconds=60,
    )


def test_fake_replays_scripted_lines_and_reports_completion(tmp_path: Path) -> None:
    run_id = uuid.uuid4()
    executor = FakeExecutor()
    executor.queue(
        run_id,
        [
            json.dumps({"type": "system", "session_id": "s-9"}),
            json.dumps(
                {"type": "result", "total_cost_usd": 0.1, "usage": {"input_tokens": 5}}
            ),
        ],
    )
    seen: list[ParsedEvent] = []

    outcome = executor.run(_req(run_id, tmp_path), seen.append)

    assert [e.type for e in seen] == ["system", "result"]
    assert outcome.reason is ExitReason.COMPLETED
    assert outcome.session_id == "s-9"
    assert outcome.cost_usd == 0.1
    assert outcome.token_in == 5
    assert executor.requests[0].prompt == "do the thing"


def test_fake_reports_usage_limit(tmp_path: Path) -> None:
    run_id = uuid.uuid4()
    executor = FakeExecutor()
    executor.queue(
        run_id,
        [json.dumps({"type": "result", "is_error": True, "result": "usage limit reached|9"})],
    )

    outcome = executor.run(_req(run_id, tmp_path), lambda _e: None)

    assert outcome.reason is ExitReason.USAGE_LIMIT
    assert outcome.usage_limit_text == "usage limit reached|9"


def test_fake_can_simulate_crash_and_timeout(tmp_path: Path) -> None:
    executor = FakeExecutor()
    crash_id, timeout_id = uuid.uuid4(), uuid.uuid4()
    executor.queue(crash_id, [], reason=ExitReason.CRASHED, exit_code=1)
    executor.queue(timeout_id, [], reason=ExitReason.TIMEOUT, exit_code=-9)

    assert executor.run(_req(crash_id, tmp_path), lambda _e: None).reason is ExitReason.CRASHED
    assert executor.run(_req(timeout_id, tmp_path), lambda _e: None).reason is ExitReason.TIMEOUT
