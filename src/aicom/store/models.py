import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from aicom.domain.enums import (
    ApprovalKind,
    ApprovalStatus,
    RunStatus,
    TaskOrigin,
    TaskStatus,
)


class Base(DeclarativeBase):
    pass


def _ts(**kw: object) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kw)  # type: ignore[arg-type]


class Agent(Base):
    __tablename__ = "agent"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    persona: Mapped[str] = mapped_column(Text, default="")
    autonomy_level: Mapped[str] = mapped_column(String(32), default="standard")
    allowed_tools: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    # Tools that exist for this agent but are reachable ONLY through a signed-off
    # gate approval: the worker adds the single approved tool to --allowedTools for
    # exactly one execution (spec §5.1 step 4). A tool listed here must never also
    # appear in allowed_tools -- that would make it permanently callable and
    # silently disable the boundary -- so the worker refuses to run such an agent.
    gated_tools: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    mcp_config: Mapped[dict] = mapped_column(JSONB, default=dict)
    workspace_root: Mapped[str | None] = mapped_column(String(512), default=None)
    # reserved for a future git-worktree executor mode; unused in S1
    repo_path: Mapped[str | None] = mapped_column(String(512), default=None)
    max_run_seconds: Mapped[int] = mapped_column(Integer, default=1800)
    max_child_tasks_per_day: Mapped[int] = mapped_column(Integer, default=20)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Schedule(Base):
    """A recurring definition that creates tasks on a cron schedule."""

    __tablename__ = "schedule"
    __table_args__ = (UniqueConstraint("agent_id", "name", name="uq_schedule_agent_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent.id"))
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    cron: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    title_template: Mapped[str] = mapped_column(String(300))
    goal_template: Mapped[str] = mapped_column(Text)
    # The firing clock AND the concurrency token: a firing is claimed by a
    # conditional UPDATE matching the value the caller observed.
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_skipped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_skip_reason: Mapped[str | None] = mapped_column(String(200), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    agent: Mapped[Agent] = relationship(lazy="joined")


class Task(Base):
    __tablename__ = "task"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent.id"))
    title: Mapped[str] = mapped_column(String(300))
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status", native_enum=False), default=TaskStatus.OPEN
    )
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("task.id"), default=None
    )
    depth: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[TaskOrigin] = mapped_column(
        Enum(TaskOrigin, name="task_origin", native_enum=False), default=TaskOrigin.HUMAN
    )
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("schedule.id", ondelete="SET NULL"), default=None, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    agent: Mapped[Agent] = relationship(lazy="joined")


class Run(Base):
    __tablename__ = "run"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="run_status", native_enum=False),
        default=RunStatus.QUEUED,
        index=True,
    )
    worker_id: Mapped[str | None] = mapped_column(String(80), default=None)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    resume_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    resume_note: Mapped[str | None] = mapped_column(Text, default=None)
    cost_usd: Mapped[float | None] = mapped_column(Float, default=None)
    token_in: Mapped[int] = mapped_column(BigInteger, default=0)
    token_out: Mapped[int] = mapped_column(BigInteger, default=0)
    exit_reason: Mapped[str | None] = mapped_column(String(40), default=None)
    session_id: Mapped[str | None] = mapped_column(String(120), default=None)
    workspace_path: Mapped[str | None] = mapped_column(String(512), default=None)
    # Set by the gate MCP subprocess (a separate OS process, possibly its own
    # separate DB session/connection from the worker's) when it could not
    # durably record an approval. A blind UPDATE by primary key -- deliberately
    # NOT routed through the `event` table, whose (run_id, seq) ordering is
    # owned in-process by the worker's stdout reader and would race a
    # concurrent writer in a different process. Checked by the worker at
    # finalize time so a run that finishes SUCCEEDED after a silently-dropped
    # gate call is still surfaced to the operator (Slack + logs), not just to
    # the agent mid-run.
    gate_error: Mapped[str | None] = mapped_column(Text, default=None)

    task: Mapped[Task] = relationship(lazy="joined")


class Event(Base):
    __tablename__ = "event"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_event_run_seq"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("run.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSONB)


class Approval(Base):
    __tablename__ = "approval"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("run.id"), index=True)
    kind: Mapped[ApprovalKind] = mapped_column(
        Enum(ApprovalKind, name="approval_kind", native_enum=False)
    )
    proposal: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, name="approval_status", native_enum=False),
        default=ApprovalStatus.PENDING,
        index=True,
    )
    nonce: Mapped[str] = mapped_column(String(64), unique=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    remind_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    remind_count: Mapped[int] = mapped_column(Integer, default=0)
    last_reminded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    slack_channel: Mapped[str | None] = mapped_column(String(80), default=None)
    slack_ts: Mapped[str | None] = mapped_column(String(40), default=None)
    decided_by: Mapped[str | None] = mapped_column(String(80), default=None)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    response: Mapped[dict | None] = mapped_column(JSONB, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    run: Mapped[Run] = relationship(lazy="joined")


class Artifact(Base):
    __tablename__ = "artifact"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("run.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    git_ref: Mapped[str | None] = mapped_column(String(64), default=None)
    path: Mapped[str] = mapped_column(String(512))
    summary: Mapped[str | None] = mapped_column(Text, default=None)


class SpendLedger(Base):
    """Approved external spend. Written in S1, reported on in S5."""

    __tablename__ = "spend_ledger"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("approval.id"), unique=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent.id"))
    amount_usd: Mapped[float] = mapped_column(Numeric(14, 4))
    memo: Mapped[str | None] = mapped_column(Text, default=None)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SystemState(Base):
    """Single row, id=1. Account-wide LLM pause."""

    __tablename__ = "system_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    llm_paused_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    pause_reason: Mapped[str | None] = mapped_column(String(200), default=None)
    pause_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    consecutive_pauses: Mapped[int] = mapped_column(Integer, default=0)
