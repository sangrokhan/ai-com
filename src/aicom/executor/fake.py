import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from aicom.domain.enums import ExitReason
from aicom.executor.base import RunOutcome, RunRequest
from aicom.executor.stream import ParsedEvent, StreamParser


@dataclass(slots=True)
class _Script:
    lines: list[str]
    reason: ExitReason | None
    exit_code: int


@dataclass(slots=True)
class FakeExecutor:
    """Replays scripted stream-json lines. Used by every integration test."""

    scripts: dict[uuid.UUID, _Script] = field(default_factory=dict)
    requests: list[RunRequest] = field(default_factory=list)

    def queue(
        self,
        run_id: uuid.UUID,
        lines: list[str],
        *,
        reason: ExitReason | None = None,
        exit_code: int = 0,
    ) -> None:
        self.scripts[run_id] = _Script(lines=lines, reason=reason, exit_code=exit_code)

    def run(
        self, req: RunRequest, on_event: Callable[[ParsedEvent], None]
    ) -> RunOutcome:
        self.requests.append(req)
        script = self.scripts.get(req.run_id, _Script(lines=[], reason=None, exit_code=0))
        parser = StreamParser()
        for line in script.lines:
            event = parser.feed(line)
            if event is not None:
                on_event(event)
        reason = script.reason
        if reason is None:
            reason = ExitReason.USAGE_LIMIT if parser.saw_usage_limit else ExitReason.COMPLETED
        return RunOutcome(
            reason=reason,
            exit_code=script.exit_code,
            session_id=parser.session_id,
            cost_usd=parser.cost_usd,
            token_in=parser.token_in,
            token_out=parser.token_out,
            usage_limit_text=parser.usage_limit_text,
        )
