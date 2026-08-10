from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class TaskStatus(StrEnum):
    OPEN = "open"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalKind(StrEnum):
    SPEND = "spend"
    PUBLISH = "publish"
    CONTACT = "contact"
    EXECUTE_ORDER = "execute_order"


class ExitReason(StrEnum):
    COMPLETED = "completed"
    GATE_REQUESTED = "gate_requested"
    CRASHED = "crashed"
    TIMEOUT = "timeout"
    USAGE_LIMIT = "usage_limit"


class TaskOrigin(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
