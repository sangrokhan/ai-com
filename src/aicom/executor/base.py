import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from aicom.domain.enums import ExitReason
from aicom.executor.stream import ParsedEvent


@dataclass(frozen=True, slots=True)
class RunRequest:
    run_id: uuid.UUID
    prompt: str
    workspace: Path
    allowed_tools: tuple[str, ...]
    mcp_config: dict
    env: dict[str, str]
    resume_session_id: str | None
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class RunOutcome:
    reason: ExitReason
    exit_code: int
    session_id: str | None = None
    cost_usd: float | None = None
    token_in: int = 0
    token_out: int = 0
    usage_limit_text: str | None = None


class Executor(Protocol):
    def run(
        self, req: RunRequest, on_event: Callable[[ParsedEvent], None]
    ) -> RunOutcome: ...
