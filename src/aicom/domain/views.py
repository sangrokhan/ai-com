import uuid
from dataclasses import dataclass

from aicom.domain.enums import ApprovalKind, RunStatus


@dataclass(frozen=True, slots=True)
class ApprovalView:
    approval_id: uuid.UUID
    nonce: str
    kind: ApprovalKind
    agent_name: str
    task_title: str
    proposal: str
    payload: dict
    slack_channel: str | None = None
    slack_ts: str | None = None


@dataclass(frozen=True, slots=True)
class RunReport:
    run_id: uuid.UUID
    agent_name: str
    task_title: str
    status: RunStatus
    summary: str
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class DispatchRef:
    channel: str
    ts: str
