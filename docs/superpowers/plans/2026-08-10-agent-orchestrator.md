# Agent Orchestrator (S1 + S2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Postgres-backed orchestrator that runs Claude Code CLI agents headlessly in the background and interrupts the human operator only for Slack sign-off on money and publication actions.

**Architecture:** A pure `domain/` state machine drives a `run` lifecycle persisted in Postgres. A worker loop claims queued runs, spawns `claude -p --output-format stream-json`, and streams parsed events into an append-only table. Gated actions are impossible except through an MCP server the orchestrator supplies; calling it exits the process and parks the run in `awaiting_approval` until a signature-verified Slack button resolves it, after which the run resumes with `--resume`.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 (ORM, typed), Alembic, PostgreSQL 16, FastAPI + Uvicorn, `mcp` (Python SDK), `slack-sdk`, pytest, `testcontainers[postgres]`, `httpx`.

## Global Constraints

- Python 3.12; SQLAlchemy 2.0 typed ORM (`Mapped[...]` / `mapped_column`); PostgreSQL 16.
- Package root is `src/aicom/`. Tests mirror it under `tests/`.
- **Every run-state transition is a conditional UPDATE** (`WHERE status = <expected>`) returning affected-row count. Never read-then-write.
- **Approvals never expire.** No `expires_at` column, no auto-deny path anywhere.
- **Every external spend requires sign-off regardless of amount.** No auto-approve threshold, no minimum.
- **No plaintext secrets in the database.** `mcp_config` stores `{"secret_ref": "<name>"}` only; values resolve from environment at spawn time.
- LLM token usage is never treated as a budget. Usage-limit exhaustion pauses the whole system; it is never a run failure and never increments `attempt`.
- All timestamps are `TIMESTAMP WITH TIME ZONE`, stored UTC. Never call `datetime.now()` without `tz=UTC`.
- Primary keys are `uuid` generated application-side (`uuid7`-style ordering not required; use `uuid4`).
- Line length 100, `ruff` for lint+format, `mypy --strict` on `src/aicom/domain/`.
- Commit after every task using Conventional Commits.

---

## File Structure

| Path | Responsibility |
|------|----------------|
| `pyproject.toml` | deps, ruff/mypy/pytest config |
| `src/aicom/config.py` | env-backed settings object |
| `src/aicom/domain/enums.py` | `RunStatus`, `TaskStatus`, `ApprovalStatus`, `ApprovalKind`, `ExitReason` |
| `src/aicom/domain/transitions.py` | legal run transitions, `assert_transition` |
| `src/aicom/domain/views.py` | frozen dataclasses crossing module boundaries (`ApprovalView`, `RunReport`) |
| `src/aicom/store/models.py` | SQLAlchemy ORM tables |
| `src/aicom/store/db.py` | engine + session factory |
| `src/aicom/store/runs.py` | run claim/transition repository |
| `src/aicom/store/approvals.py` | approval create / nonce consume / reminder queries |
| `src/aicom/store/system_state.py` | global pause row |
| `src/aicom/store/events.py` | append-only event writer |
| `src/aicom/executor/base.py` | `Executor` protocol, `RunRequest`, `RunOutcome` |
| `src/aicom/executor/stream.py` | stream-json line parser + usage accumulator |
| `src/aicom/executor/workspace.py` | per-run directory create/cleanup |
| `src/aicom/executor/cli.py` | `ClaudeCliExecutor` (subprocess spawn, arg build, secret injection) |
| `src/aicom/executor/fake.py` | `FakeExecutor` replaying scripted event lines |
| `src/aicom/quota/reset.py` | usage-limit reset-time parsing + fallback backoff |
| `src/aicom/quota/pause.py` | read/write global pause, dedupe notification |
| `src/aicom/gate/server.py` | MCP server exposing `request_approval` |
| `src/aicom/notify/base.py` | `Notifier` protocol |
| `src/aicom/notify/blocks.py` | Slack Block Kit builders |
| `src/aicom/notify/slack.py` | `SlackNotifier` |
| `src/aicom/notify/fake.py` | `FakeNotifier` recording calls |
| `src/aicom/inbound/verify.py` | Slack signature + replay-window verification |
| `src/aicom/inbound/app.py` | FastAPI app, interaction endpoint |
| `src/aicom/orchestrator/worker.py` | claim → spawn → persist → finalize loop |
| `src/aicom/orchestrator/artifacts.py` | copy run outputs into artifact repo, commit |
| `src/aicom/orchestrator/sweeper.py` | reminders, stale-run recovery, pause resume |
| `src/aicom/api/routes.py` | task CRUD, run queries, SSE stream (S4 seam) |

---

### Task 1: Project scaffolding and the run state machine

**Files:**
- Create: `pyproject.toml`, `src/aicom/__init__.py`, `src/aicom/domain/__init__.py`, `src/aicom/domain/enums.py`, `src/aicom/domain/transitions.py`
- Test: `tests/domain/test_transitions.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `RunStatus`, `TaskStatus`, `ApprovalStatus`, `ApprovalKind`, `ExitReason` enums; `can_transition(frm: RunStatus, to: RunStatus) -> bool`; `assert_transition(frm, to) -> None` raising `IllegalTransition`; `TERMINAL: frozenset[RunStatus]`.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "aicom"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "sqlalchemy>=2.0",
    "alembic>=1.13",
    "psycopg[binary]>=3.1",
    "fastapi>=0.110",
    "uvicorn>=0.29",
    "slack-sdk>=3.27",
    "mcp>=1.0",
    "pydantic-settings>=2.2",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "testcontainers[postgres]>=4.0",
    "httpx>=0.27",
    "ruff>=0.4",
    "mypy>=1.9",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/aicom"]

[tool.ruff]
line-length = 100

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]

[tool.mypy]
strict = true
files = ["src/aicom/domain"]
```

- [ ] **Step 2: Write the failing test**

Create `tests/domain/test_transitions.py`:

```python
import pytest

from aicom.domain.enums import RunStatus
from aicom.domain.transitions import IllegalTransition, assert_transition, can_transition


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (RunStatus.QUEUED, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.SUCCEEDED),
        (RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL),
        (RunStatus.RUNNING, RunStatus.QUEUED),  # usage-limit requeue, stale recovery
        (RunStatus.AWAITING_APPROVAL, RunStatus.QUEUED),  # resume after sign-off
        (RunStatus.AWAITING_APPROVAL, RunStatus.CANCELLED),
    ],
)
def test_legal_transitions(frm: RunStatus, to: RunStatus) -> None:
    assert can_transition(frm, to) is True


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (RunStatus.QUEUED, RunStatus.SUCCEEDED),
        (RunStatus.SUCCEEDED, RunStatus.RUNNING),
        (RunStatus.FAILED, RunStatus.QUEUED),
        (RunStatus.AWAITING_APPROVAL, RunStatus.RUNNING),  # must go through queued
        (RunStatus.RUNNING, RunStatus.RUNNING),
    ],
)
def test_illegal_transitions(frm: RunStatus, to: RunStatus) -> None:
    assert can_transition(frm, to) is False
    with pytest.raises(IllegalTransition):
        assert_transition(frm, to)


def test_terminal_states_have_no_successors() -> None:
    from aicom.domain.transitions import TERMINAL

    assert TERMINAL == {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.TIMED_OUT,
        RunStatus.CANCELLED,
    }
    for status in TERMINAL:
        assert all(not can_transition(status, other) for other in RunStatus)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/domain/test_transitions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.domain.enums'`

- [ ] **Step 4: Write `src/aicom/domain/enums.py`**

```python
from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


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
```

- [ ] **Step 5: Write `src/aicom/domain/transitions.py`**

```python
from aicom.domain.enums import RunStatus

TERMINAL: frozenset[RunStatus] = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.TIMED_OUT, RunStatus.CANCELLED}
)

_ALLOWED: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.TIMED_OUT,
            RunStatus.AWAITING_APPROVAL,
            RunStatus.QUEUED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.AWAITING_APPROVAL: frozenset({RunStatus.QUEUED, RunStatus.CANCELLED}),
}


class IllegalTransition(Exception):
    def __init__(self, frm: RunStatus, to: RunStatus) -> None:
        super().__init__(f"illegal run transition: {frm} -> {to}")
        self.frm = frm
        self.to = to


def can_transition(frm: RunStatus, to: RunStatus) -> bool:
    return to in _ALLOWED.get(frm, frozenset())


def assert_transition(frm: RunStatus, to: RunStatus) -> None:
    if not can_transition(frm, to):
        raise IllegalTransition(frm, to)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/domain -v && mypy && ruff check src tests`
Expected: PASS, no type errors, no lint errors.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/aicom tests/domain
git commit -m "feat(domain): add run state machine and core enums"
```

---

### Task 2: Database schema and ORM models

**Files:**
- Create: `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_initial.py`, `src/aicom/store/__init__.py`, `src/aicom/store/models.py`, `src/aicom/store/db.py`, `src/aicom/config.py`
- Test: `tests/store/conftest.py`, `tests/store/test_models.py`

**Interfaces:**
- Consumes: enums from Task 1.
- Produces: ORM classes `Agent`, `Task`, `Run`, `Event`, `Approval`, `Artifact`, `SpendLedger`, `SystemState`; `Base`; `make_engine(url) -> Engine`; `session_factory(engine) -> sessionmaker[Session]`; `Settings` with fields `database_url`, `slack_bot_token`, `slack_signing_secret`, `slack_channel`, `slack_approver_ids`, `workspace_root`, `artifact_repo_path`, `claude_binary`.

- [ ] **Step 1: Write `src/aicom/config.py`**

```python
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AICOM_", env_file=".env")

    database_url: str = "postgresql+psycopg://aicom:aicom@localhost:5432/aicom"
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_channel: str = ""
    slack_approver_ids: tuple[str, ...] = ()
    workspace_root: Path = Path("./workspaces")
    artifact_repo_path: Path = Path("./artifacts")
    claude_binary: str = "claude"
    worker_poll_seconds: float = 2.0


def load_settings() -> Settings:
    return Settings()
```

- [ ] **Step 2: Write the failing test**

Create `tests/store/conftest.py`:

```python
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.postgres import PostgresContainer

from aicom.store.db import make_engine, session_factory
from aicom.store.models import Base


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    with PostgresContainer("postgres:16-alpine") as pg:
        url = pg.get_connection_url().replace("postgresql+psycopg2", "postgresql+psycopg")
        eng = make_engine(url)
        Base.metadata.create_all(eng)
        yield eng


@pytest.fixture()
def sessions(engine: Engine) -> sessionmaker[Session]:
    return session_factory(engine)


@pytest.fixture()
def session(sessions: sessionmaker[Session]) -> Iterator[Session]:
    with sessions() as s:
        yield s
        s.rollback()
```

Create `tests/store/test_models.py`:

```python
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from aicom.domain.enums import ApprovalKind, ApprovalStatus, RunStatus, TaskStatus
from aicom.store.models import Agent, Approval, Run, Task


def make_agent(session: Session, name: str = "researcher") -> Agent:
    agent = Agent(
        id=uuid.uuid4(),
        name=f"{name}-{uuid.uuid4().hex[:6]}",
        persona="# Researcher\nYou research things.",
        allowed_tools=["Read", "Grep", "WebSearch"],
        mcp_config={"gate": {"command": "python", "args": ["-m", "aicom.gate.server"]}},
    )
    session.add(agent)
    session.flush()
    return agent


def test_agent_task_run_roundtrip(session: Session) -> None:
    agent = make_agent(session)
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="Find leads", goal="Find 10 leads.")
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1)
    session.add(run)
    session.flush()

    loaded = session.scalar(select(Run).where(Run.id == run.id))
    assert loaded is not None
    assert loaded.status is RunStatus.QUEUED
    assert loaded.task.status is TaskStatus.OPEN
    assert loaded.task.agent.allowed_tools == ["Read", "Grep", "WebSearch"]


def test_approval_defaults_have_no_expiry_and_a_nonce(session: Session) -> None:
    agent = make_agent(session)
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="Buy data", goal="Buy a data feed.")
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1)
    session.add(run)
    session.flush()

    approval = Approval(
        id=uuid.uuid4(),
        run_id=run.id,
        kind=ApprovalKind.SPEND,
        proposal="Buy feed X for $20/mo. Alternative: free tier. Reversible: cancel anytime.",
        payload={"amount_usd": 20},
        nonce=uuid.uuid4().hex,
    )
    session.add(approval)
    session.flush()

    assert approval.status is ApprovalStatus.PENDING
    assert approval.consumed_at is None
    assert approval.remind_count == 0
    assert not hasattr(approval, "expires_at")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/store/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.store.db'`

- [ ] **Step 4: Write `src/aicom/store/db.py`**

```python
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, future=True)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
```

- [ ] **Step 5: Write `src/aicom/store/models.py`**

```python
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
    # column exists for S3; the S1 worker ignores it
    schedule: Mapped[str | None] = mapped_column(String(120), default=None)
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/store/test_models.py -v`
Expected: PASS (Docker must be running for testcontainers).

- [ ] **Step 7: Generate the Alembic baseline migration**

```bash
alembic init -t generic alembic
```

Edit `alembic/env.py` so it imports the metadata and URL:

```python
from aicom.config import load_settings
from aicom.store.models import Base

target_metadata = Base.metadata
config.set_main_option("sqlalchemy.url", load_settings().database_url)
```

Then:

```bash
alembic revision --autogenerate -m "initial schema"
alembic upgrade head
```

Rename the generated file to `alembic/versions/0001_initial.py`.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml alembic alembic.ini src/aicom/config.py src/aicom/store tests/store
git commit -m "feat(store): add ORM schema and initial migration"
```

---

### Task 3: Repositories with conditional-update transitions

**Files:**
- Create: `src/aicom/store/runs.py`, `src/aicom/store/events.py`, `src/aicom/store/system_state.py`
- Test: `tests/store/test_runs.py`

**Interfaces:**
- Consumes: models from Task 2, `assert_transition` from Task 1.
- Produces:
  - `claim_next_queued(session, *, worker_id: str, now: datetime) -> Run | None`
  - `transition(session, run_id: UUID, frm: RunStatus, to: RunStatus, **fields: object) -> bool`
  - `touch_heartbeat(session, run_id: UUID, now: datetime) -> None`
  - `stale_running_runs(session, *, older_than: datetime) -> list[Run]`
  - `append_event(session, run_id: UUID, seq: int, type_: str, payload: dict) -> None`
  - `get_pause(session) -> datetime | None`, `set_pause(session, until: datetime, reason: str) -> None`, `clear_pause(session) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/store/test_runs.py`:

```python
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from aicom.domain.enums import RunStatus
from aicom.store.models import Run, Task
from aicom.store.runs import (
    claim_next_queued,
    stale_running_runs,
    touch_heartbeat,
    transition,
)
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _queued_run(session: Session) -> Run:
    agent = make_agent(session)
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="t", goal="g")
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1)
    session.add(run)
    session.commit()
    return run


def test_claim_marks_running_and_is_exclusive(session: Session) -> None:
    run = _queued_run(session)

    claimed = claim_next_queued(session, worker_id="w1", now=NOW)
    session.commit()
    assert claimed is not None and claimed.id == run.id
    assert claimed.status is RunStatus.RUNNING
    assert claimed.worker_id == "w1"
    assert claimed.started_at == NOW

    again = claim_next_queued(session, worker_id="w2", now=NOW)
    assert again is None


def test_transition_is_conditional_on_current_status(session: Session) -> None:
    run = _queued_run(session)
    assert transition(session, run.id, RunStatus.QUEUED, RunStatus.RUNNING) is True
    session.commit()
    # second attempt from the same expected state must not apply
    assert transition(session, run.id, RunStatus.QUEUED, RunStatus.RUNNING) is False


def test_transition_rejects_illegal_pairs(session: Session) -> None:
    import pytest

    from aicom.domain.transitions import IllegalTransition

    run = _queued_run(session)
    with pytest.raises(IllegalTransition):
        transition(session, run.id, RunStatus.QUEUED, RunStatus.SUCCEEDED)


def test_stale_running_runs_detected_by_heartbeat(session: Session) -> None:
    run = _queued_run(session)
    claim_next_queued(session, worker_id="w1", now=NOW)
    touch_heartbeat(session, run.id, NOW)
    session.commit()

    assert stale_running_runs(session, older_than=NOW - timedelta(minutes=1)) == []
    stale = stale_running_runs(session, older_than=NOW + timedelta(minutes=5))
    assert [r.id for r in stale] == [run.id]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/store/test_runs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.store.runs'`

- [ ] **Step 3: Write `src/aicom/store/runs.py`**

```python
import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from aicom.domain.enums import RunStatus
from aicom.domain.transitions import assert_transition
from aicom.store.models import Run


def claim_next_queued(session: Session, *, worker_id: str, now: datetime) -> Run | None:
    """Atomically grab one queued run. SKIP LOCKED lets many workers poll safely."""
    stmt = (
        select(Run.id)
        .where(Run.status == RunStatus.QUEUED)
        .order_by(Run.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    run_id = session.scalar(stmt)
    if run_id is None:
        return None
    applied = transition(
        session,
        run_id,
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        worker_id=worker_id,
        started_at=now,
        heartbeat_at=now,
    )
    if not applied:
        return None
    return session.get(Run, run_id)


def transition(
    session: Session,
    run_id: uuid.UUID,
    frm: RunStatus,
    to: RunStatus,
    **fields: object,
) -> bool:
    assert_transition(frm, to)
    result = session.execute(
        update(Run)
        .where(Run.id == run_id, Run.status == frm)
        .values(status=to, **fields)
        .execution_options(synchronize_session="fetch")
    )
    return bool(result.rowcount)


def touch_heartbeat(session: Session, run_id: uuid.UUID, now: datetime) -> None:
    session.execute(update(Run).where(Run.id == run_id).values(heartbeat_at=now))


def stale_running_runs(session: Session, *, older_than: datetime) -> list[Run]:
    stmt = select(Run).where(
        Run.status == RunStatus.RUNNING, Run.heartbeat_at < older_than
    )
    return list(session.scalars(stmt))
```

- [ ] **Step 4: Write `src/aicom/store/events.py`**

```python
import uuid

from sqlalchemy.orm import Session

from aicom.store.models import Event


def append_event(
    session: Session, run_id: uuid.UUID, seq: int, type_: str, payload: dict
) -> None:
    session.add(
        Event(id=uuid.uuid4(), run_id=run_id, seq=seq, type=type_, payload=payload)
    )
```

- [ ] **Step 5: Write `src/aicom/store/system_state.py`**

```python
from datetime import datetime

from sqlalchemy.orm import Session

from aicom.store.models import SystemState


def _row(session: Session) -> SystemState:
    state = session.get(SystemState, 1)
    if state is None:
        state = SystemState(id=1)
        session.add(state)
        session.flush()
    return state


def get_pause(session: Session) -> datetime | None:
    return _row(session).llm_paused_until


def set_pause(session: Session, until: datetime, reason: str) -> None:
    state = _row(session)
    state.llm_paused_until = until
    state.pause_reason = reason
    state.consecutive_pauses += 1


def mark_pause_notified(session: Session, at: datetime) -> None:
    _row(session).pause_notified_at = at


def pause_notified_at(session: Session) -> datetime | None:
    return _row(session).pause_notified_at


def clear_pause(session: Session) -> None:
    state = _row(session)
    state.llm_paused_until = None
    state.pause_reason = None
    state.pause_notified_at = None
    state.consecutive_pauses = 0
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/store -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/aicom/store tests/store
git commit -m "feat(store): add conditional-update run repository and pause state"
```

---

### Task 4: stream-json parser and usage accumulator

**Files:**
- Create: `src/aicom/executor/__init__.py`, `src/aicom/executor/stream.py`
- Test: `tests/executor/test_stream.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `ParsedEvent(seq: int, type: str, payload: dict)` frozen dataclass; `StreamParser` with `feed(line: str) -> ParsedEvent | None` and read-only properties `session_id: str | None`, `cost_usd: float | None`, `token_in: int`, `token_out: int`, `saw_usage_limit: bool`, `usage_limit_text: str | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/executor/test_stream.py`:

```python
import json

from aicom.executor.stream import StreamParser


def test_parses_lines_and_assigns_sequence_numbers() -> None:
    parser = StreamParser()
    first = parser.feed(json.dumps({"type": "system", "subtype": "init", "session_id": "s-1"}))
    second = parser.feed(json.dumps({"type": "assistant", "message": {"content": []}}))

    assert first is not None and first.seq == 0 and first.type == "system"
    assert second is not None and second.seq == 1
    assert parser.session_id == "s-1"


def test_blank_and_malformed_lines_do_not_raise() -> None:
    parser = StreamParser()
    assert parser.feed("") is None
    assert parser.feed("   ") is None

    event = parser.feed("{not json")
    assert event is not None
    assert event.type == "unparsed"
    assert event.payload["raw"] == "{not json"


def test_accumulates_cost_and_tokens_from_result_event() -> None:
    parser = StreamParser()
    parser.feed(
        json.dumps(
            {
                "type": "result",
                "total_cost_usd": 0.42,
                "usage": {"input_tokens": 1200, "output_tokens": 300},
            }
        )
    )
    assert parser.cost_usd == 0.42
    assert parser.token_in == 1200
    assert parser.token_out == 300


def test_detects_usage_limit_from_result_error() -> None:
    parser = StreamParser()
    parser.feed(
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": "Claude AI usage limit reached|1786000000",
            }
        )
    )
    assert parser.saw_usage_limit is True
    assert parser.usage_limit_text == "Claude AI usage limit reached|1786000000"


def test_ordinary_error_is_not_a_usage_limit() -> None:
    parser = StreamParser()
    parser.feed(json.dumps({"type": "result", "is_error": True, "result": "tool failed"}))
    assert parser.saw_usage_limit is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/executor/test_stream.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.executor.stream'`

- [ ] **Step 3: Write `src/aicom/executor/stream.py`**

```python
import json
from dataclasses import dataclass

_USAGE_LIMIT_MARKERS = ("usage limit reached", "rate limit", "limit exceeded")


@dataclass(frozen=True, slots=True)
class ParsedEvent:
    seq: int
    type: str
    payload: dict


class StreamParser:
    """Turns raw stream-json lines into events. Never raises on bad input:
    a parser bug must not kill a run."""

    def __init__(self) -> None:
        self._seq = 0
        self._session_id: str | None = None
        self._cost_usd: float | None = None
        self._token_in = 0
        self._token_out = 0
        self._usage_limit_text: str | None = None

    def feed(self, line: str) -> ParsedEvent | None:
        stripped = line.strip()
        if not stripped:
            return None
        try:
            payload = json.loads(stripped)
            if not isinstance(payload, dict):
                raise ValueError("not an object")
            type_ = str(payload.get("type", "unknown"))
        except Exception:
            payload = {"raw": stripped}
            type_ = "unparsed"
        else:
            self._absorb(payload)
        event = ParsedEvent(seq=self._seq, type=type_, payload=payload)
        self._seq += 1
        return event

    def _absorb(self, payload: dict) -> None:
        if session_id := payload.get("session_id"):
            self._session_id = str(session_id)
        if payload.get("type") != "result":
            return
        if (cost := payload.get("total_cost_usd")) is not None:
            self._cost_usd = float(cost)
        usage = payload.get("usage") or {}
        self._token_in += int(usage.get("input_tokens", 0))
        self._token_out += int(usage.get("output_tokens", 0))
        if payload.get("is_error"):
            text = str(payload.get("result", ""))
            if any(marker in text.lower() for marker in _USAGE_LIMIT_MARKERS):
                self._usage_limit_text = text

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def cost_usd(self) -> float | None:
        return self._cost_usd

    @property
    def token_in(self) -> int:
        return self._token_in

    @property
    def token_out(self) -> int:
        return self._token_out

    @property
    def saw_usage_limit(self) -> bool:
        return self._usage_limit_text is not None

    @property
    def usage_limit_text(self) -> str | None:
        return self._usage_limit_text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/executor/test_stream.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/aicom/executor tests/executor
git commit -m "feat(executor): add fault-tolerant stream-json parser"
```

---

### Task 5: Usage-limit reset parsing and fallback backoff

**Files:**
- Create: `src/aicom/quota/__init__.py`, `src/aicom/quota/reset.py`
- Test: `tests/quota/test_reset.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `parse_reset_at(text: str, *, now: datetime) -> datetime | None`; `fallback_backoff(consecutive: int, *, now: datetime) -> datetime` implementing 15 min → 30 min → 1 h cap.

- [ ] **Step 1: Write the failing test**

Create `tests/quota/test_reset.py`:

```python
from datetime import UTC, datetime, timedelta

from aicom.quota.reset import fallback_backoff, parse_reset_at

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def test_parses_pipe_delimited_epoch_seconds() -> None:
    epoch = int((NOW + timedelta(hours=3)).timestamp())
    assert parse_reset_at(f"Claude AI usage limit reached|{epoch}", now=NOW) == NOW + timedelta(
        hours=3
    )


def test_parses_iso8601_timestamp() -> None:
    assert parse_reset_at("limit reached, resets at 2026-08-10T15:30:00Z", now=NOW) == datetime(
        2026, 8, 10, 15, 30, tzinfo=UTC
    )


def test_parses_clock_time_and_rolls_to_tomorrow_when_already_past() -> None:
    assert parse_reset_at("resets at 3pm", now=NOW) == datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
    later = datetime(2026, 8, 10, 20, 0, tzinfo=UTC)
    assert parse_reset_at("resets at 3pm", now=later) == datetime(
        2026, 8, 11, 15, 0, tzinfo=UTC
    )


def test_returns_none_when_no_time_present() -> None:
    assert parse_reset_at("usage limit reached", now=NOW) is None


def test_past_epoch_is_rejected() -> None:
    epoch = int((NOW - timedelta(hours=1)).timestamp())
    assert parse_reset_at(f"limit|{epoch}", now=NOW) is None


def test_fallback_backoff_steps_then_caps_at_one_hour() -> None:
    assert fallback_backoff(0, now=NOW) == NOW + timedelta(minutes=15)
    assert fallback_backoff(1, now=NOW) == NOW + timedelta(minutes=30)
    assert fallback_backoff(2, now=NOW) == NOW + timedelta(hours=1)
    assert fallback_backoff(9, now=NOW) == NOW + timedelta(hours=1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/quota/test_reset.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.quota.reset'`

- [ ] **Step 3: Write `src/aicom/quota/reset.py`**

```python
import re
from datetime import UTC, datetime, timedelta

_EPOCH = re.compile(r"\|(\d{9,11})\b")
_ISO = re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)(Z|[+-]\d{2}:?\d{2})?")
_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)

_BACKOFF_STEPS = (timedelta(minutes=15), timedelta(minutes=30), timedelta(hours=1))


def parse_reset_at(text: str, *, now: datetime) -> datetime | None:
    """Extract when the account limit resets. Returns None if unknown or already past."""
    for candidate in (_from_epoch(text), _from_iso(text), _from_clock(text, now)):
        if candidate is not None and candidate > now:
            return candidate
    return None


def _from_epoch(text: str) -> datetime | None:
    match = _EPOCH.search(text)
    if not match:
        return None
    return datetime.fromtimestamp(int(match.group(1)), tz=UTC)


def _from_iso(text: str) -> datetime | None:
    match = _ISO.search(text)
    if not match:
        return None
    raw = match.group(1).replace(" ", "T")
    suffix = match.group(2) or "Z"
    try:
        return datetime.fromisoformat(raw + suffix.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _from_clock(text: str, now: datetime) -> datetime | None:
    match = _CLOCK.search(text)
    if not match:
        return None
    hour = int(match.group(1)) % 12
    minute = int(match.group(2) or 0)
    if match.group(3).lower() == "pm":
        hour += 12
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def fallback_backoff(consecutive: int, *, now: datetime) -> datetime:
    """Used when the reset time cannot be parsed. Never hammer the account."""
    index = min(max(consecutive, 0), len(_BACKOFF_STEPS) - 1)
    return now + _BACKOFF_STEPS[index]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/quota -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/aicom/quota tests/quota
git commit -m "feat(quota): parse usage-limit reset times with capped fallback backoff"
```

---

### Task 6: Executor interface, workspace manager, and FakeExecutor

**Files:**
- Create: `src/aicom/executor/base.py`, `src/aicom/executor/workspace.py`, `src/aicom/executor/fake.py`
- Test: `tests/executor/test_workspace.py`, `tests/executor/test_fake.py`

**Interfaces:**
- Consumes: `ParsedEvent` (Task 4), `ExitReason` (Task 1).
- Produces:
  - `RunRequest` frozen dataclass: `run_id: UUID`, `prompt: str`, `workspace: Path`, `allowed_tools: tuple[str, ...]`, `mcp_config: dict`, `env: dict[str, str]`, `resume_session_id: str | None`, `timeout_seconds: int`.
  - `RunOutcome` frozen dataclass: `reason: ExitReason`, `exit_code: int`, `session_id: str | None`, `cost_usd: float | None`, `token_in: int`, `token_out: int`, `usage_limit_text: str | None`.
  - `Executor` protocol: `run(req: RunRequest, on_event: Callable[[ParsedEvent], None]) -> RunOutcome`.
  - `prepare_workspace(root: Path, agent_name: str, run_id: UUID) -> Path`, `cleanup_workspace(path: Path) -> None`.
  - `FakeExecutor(scripts: dict[UUID, list[str]] | None = None)` with `queue(run_id, lines)` and attribute `requests: list[RunRequest]`.

- [ ] **Step 1: Write the failing test**

Create `tests/executor/test_workspace.py`:

```python
import uuid
from pathlib import Path

from aicom.executor.workspace import cleanup_workspace, prepare_workspace


def test_prepare_creates_isolated_directory_per_run(tmp_path: Path) -> None:
    run_id = uuid.uuid4()
    ws = prepare_workspace(tmp_path, "researcher", run_id)

    assert ws.is_dir()
    assert ws.parent.name == "researcher"
    assert ws.name == str(run_id)


def test_prepare_is_idempotent(tmp_path: Path) -> None:
    run_id = uuid.uuid4()
    first = prepare_workspace(tmp_path, "a", run_id)
    (first / "keep.txt").write_text("data")
    second = prepare_workspace(tmp_path, "a", run_id)

    assert first == second
    assert (second / "keep.txt").read_text() == "data"


def test_cleanup_removes_tree_and_tolerates_missing(tmp_path: Path) -> None:
    ws = prepare_workspace(tmp_path, "a", uuid.uuid4())
    cleanup_workspace(ws)
    assert not ws.exists()
    cleanup_workspace(ws)  # must not raise
```

Create `tests/executor/test_fake.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/executor/test_workspace.py tests/executor/test_fake.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.executor.workspace'`

- [ ] **Step 3: Write `src/aicom/executor/base.py`**

```python
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
```

- [ ] **Step 4: Write `src/aicom/executor/workspace.py`**

```python
import shutil
import uuid
from pathlib import Path


def prepare_workspace(root: Path, agent_name: str, run_id: uuid.UUID) -> Path:
    workspace = Path(root) / agent_name / str(run_id)
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def cleanup_workspace(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
```

- [ ] **Step 5: Write `src/aicom/executor/fake.py`**

```python
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/executor -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/aicom/executor tests/executor
git commit -m "feat(executor): add executor protocol, workspace manager, and fake executor"
```

---

### Task 7: ClaudeCliExecutor

**Files:**
- Create: `src/aicom/executor/cli.py`, `src/aicom/executor/secrets.py`
- Test: `tests/executor/test_cli_args.py`, `tests/executor/test_secrets.py`

**Interfaces:**
- Consumes: `RunRequest`, `RunOutcome`, `Executor`, `StreamParser`.
- Produces: `build_argv(req: RunRequest, *, binary: str, mcp_config_path: Path) -> list[str]`; `ClaudeCliExecutor(binary: str)` implementing `Executor`; `resolve_secrets(mcp_config: dict, lookup: Callable[[str], str | None]) -> tuple[dict, dict[str, str]]` returning `(sanitized_config, env_vars)`.

- [ ] **Step 1: Write the failing test for secret resolution**

Create `tests/executor/test_secrets.py`:

```python
import pytest

from aicom.executor.secrets import MissingSecret, resolve_secrets


def test_secret_refs_become_env_vars_and_never_appear_inline() -> None:
    config = {
        "broker": {
            "command": "npx",
            "args": ["broker-mcp"],
            "env": {"API_KEY": {"secret_ref": "broker/alpaca"}},
        }
    }
    sanitized, env = resolve_secrets(config, lambda ref: "s3cr3t" if ref == "broker/alpaca" else None)

    assert env == {"AICOM_SECRET_BROKER_ALPACA": "s3cr3t"}
    assert sanitized["broker"]["env"]["API_KEY"] == "${AICOM_SECRET_BROKER_ALPACA}"
    assert "s3cr3t" not in str(sanitized)


def test_missing_secret_is_a_hard_error() -> None:
    config = {"x": {"env": {"K": {"secret_ref": "nope"}}}}
    with pytest.raises(MissingSecret, match="nope"):
        resolve_secrets(config, lambda _ref: None)


def test_config_without_refs_passes_through_unchanged() -> None:
    config = {"gate": {"command": "python", "args": ["-m", "aicom.gate.server"]}}
    sanitized, env = resolve_secrets(config, lambda _ref: None)
    assert sanitized == config
    assert env == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/executor/test_secrets.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.executor.secrets'`

- [ ] **Step 3: Write `src/aicom/executor/secrets.py`**

```python
import re
from collections.abc import Callable

_SLUG = re.compile(r"[^A-Za-z0-9]+")


class MissingSecret(Exception):
    pass


def env_var_name(ref: str) -> str:
    return "AICOM_SECRET_" + _SLUG.sub("_", ref).strip("_").upper()


def resolve_secrets(
    mcp_config: dict, lookup: Callable[[str], str | None]
) -> tuple[dict, dict[str, str]]:
    """Replace {"secret_ref": name} nodes with ${ENV_VAR} placeholders and
    return the env vars to inject. Plaintext never reaches disk or the DB."""
    env: dict[str, str] = {}

    def walk(node: object) -> object:
        if isinstance(node, dict):
            ref = node.get("secret_ref")
            if isinstance(ref, str) and len(node) == 1:
                value = lookup(ref)
                if value is None:
                    raise MissingSecret(f"unresolved secret_ref: {ref}")
                name = env_var_name(ref)
                env[name] = value
                return "${" + name + "}"
            return {key: walk(val) for key, val in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    sanitized = walk(mcp_config)
    assert isinstance(sanitized, dict)
    return sanitized, env
```

- [ ] **Step 4: Write the failing test for argv construction**

Create `tests/executor/test_cli_args.py`:

```python
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


def test_resume_flag_present_only_when_session_id_given(tmp_path: Path) -> None:
    argv = build_argv(
        _req(tmp_path, resume="sess-77"), binary="claude", mcp_config_path=tmp_path / "mcp.json"
    )
    assert argv[argv.index("--resume") + 1] == "sess-77"
```

- [ ] **Step 5: Run test to verify it fails**

Run: `pytest tests/executor/test_cli_args.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_argv'`

- [ ] **Step 6: Write `src/aicom/executor/cli.py`**

```python
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
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/executor -v && ruff check src tests`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/aicom/executor tests/executor
git commit -m "feat(executor): add Claude CLI executor with secret-ref injection"
```

---

### Task 8: Gate MCP server

**Files:**
- Create: `src/aicom/gate/__init__.py`, `src/aicom/gate/service.py`, `src/aicom/gate/server.py`, `src/aicom/store/approvals.py`
- Test: `tests/gate/test_service.py`

**Interfaces:**
- Consumes: run repository (Task 3), models (Task 2).
- Produces:
  - `create_approval(session, *, run_id, kind, proposal, payload, now) -> Approval` in `store/approvals.py`
  - `consume_nonce(session, *, nonce, decided_by, approved, now) -> Approval | None`
  - `pending_approvals(session, *, due_before: datetime) -> list[Approval]`
  - `request_approval(session, *, run_id: UUID, kind: str, proposal: str, payload: dict, now: datetime) -> GateResult` in `gate/service.py`, where `GateResult(approval_id: UUID, message: str)`
  - MCP server module runnable as `python -m aicom.gate.server`, exposing tool `request_approval`.

- [ ] **Step 1: Write the failing test**

Create `tests/gate/test_service.py`:

```python
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from aicom.domain.enums import ApprovalKind, ApprovalStatus, RunStatus
from aicom.gate.service import UnknownApprovalKind, request_approval
from aicom.store.approvals import consume_nonce, pending_approvals
from aicom.store.models import Approval, Run, Task
from aicom.store.runs import claim_next_queued
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _running_run(session: Session) -> Run:
    agent = make_agent(session)
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="t", goal="g")
    session.add(task)
    session.flush()
    session.add(Run(id=uuid.uuid4(), task_id=task.id, attempt=1))
    session.commit()
    run = claim_next_queued(session, worker_id="w1", now=NOW)
    session.commit()
    assert run is not None
    return run


def test_gate_request_parks_run_and_creates_pending_approval(session: Session) -> None:
    run = _running_run(session)

    result = request_approval(
        session,
        run_id=run.id,
        kind="spend",
        proposal="Buy feed X, $20/mo, alternative free tier, cancellable.",
        payload={"amount_usd": 20},
        now=NOW,
    )
    session.commit()
    session.refresh(run)

    approval = session.get(Approval, result.approval_id)
    assert approval is not None
    assert approval.status is ApprovalStatus.PENDING
    assert approval.kind is ApprovalKind.SPEND
    assert approval.nonce
    assert run.status is RunStatus.AWAITING_APPROVAL
    assert "terminate" in result.message.lower()


def test_gate_rejects_unknown_kind(session: Session) -> None:
    run = _running_run(session)
    with pytest.raises(UnknownApprovalKind):
        request_approval(
            session, run_id=run.id, kind="delete_prod", proposal="p", payload={}, now=NOW
        )


def test_nonce_is_single_use(session: Session) -> None:
    run = _running_run(session)
    result = request_approval(
        session, run_id=run.id, kind="spend", proposal="p", payload={}, now=NOW
    )
    session.commit()
    approval = session.get(Approval, result.approval_id)
    assert approval is not None

    first = consume_nonce(
        session, nonce=approval.nonce, decided_by="U123", approved=True, now=NOW
    )
    session.commit()
    assert first is not None and first.status is ApprovalStatus.APPROVED
    assert first.decided_by == "U123"

    second = consume_nonce(
        session, nonce=approval.nonce, decided_by="U999", approved=False, now=NOW
    )
    assert second is None


def test_pending_approvals_returns_items_due_for_reminder(session: Session) -> None:
    run = _running_run(session)
    request_approval(
        session, run_id=run.id, kind="publish", proposal="p", payload={}, now=NOW
    )
    session.commit()

    assert pending_approvals(session, due_before=NOW) == []
    due = pending_approvals(session, due_before=NOW + timedelta(hours=1))
    assert len(due) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/gate/test_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.gate.service'`

- [ ] **Step 3: Write `src/aicom/store/approvals.py`**

```python
import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from aicom.domain.enums import ApprovalKind, ApprovalStatus
from aicom.store.models import Approval

# first reminder delay per kind; later stages come from sweeper escalation
FIRST_REMINDER: dict[ApprovalKind, timedelta] = {
    ApprovalKind.SPEND: timedelta(minutes=30),
    ApprovalKind.EXECUTE_ORDER: timedelta(minutes=30),
    ApprovalKind.PUBLISH: timedelta(hours=2),
    ApprovalKind.CONTACT: timedelta(hours=2),
}


def create_approval(
    session: Session,
    *,
    run_id: uuid.UUID,
    kind: ApprovalKind,
    proposal: str,
    payload: dict,
    now: datetime,
) -> Approval:
    approval = Approval(
        id=uuid.uuid4(),
        run_id=run_id,
        kind=kind,
        proposal=proposal,
        payload=payload,
        nonce=secrets.token_urlsafe(32),
        remind_after=now + FIRST_REMINDER[kind],
    )
    session.add(approval)
    session.flush()
    return approval


def consume_nonce(
    session: Session, *, nonce: str, decided_by: str, approved: bool, now: datetime
) -> Approval | None:
    """Atomically claim an unconsumed approval. Duplicate clicks return None."""
    status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
    result = session.execute(
        update(Approval)
        .where(Approval.nonce == nonce, Approval.consumed_at.is_(None))
        .values(
            status=status,
            consumed_at=now,
            decided_by=decided_by,
            decided_at=now,
            response={"approved": approved},
        )
        .returning(Approval.id)
    )
    approval_id = result.scalar_one_or_none()
    if approval_id is None:
        return None
    session.expire_all()
    return session.get(Approval, approval_id)


def pending_approvals(session: Session, *, due_before: datetime) -> list[Approval]:
    stmt = select(Approval).where(
        Approval.status == ApprovalStatus.PENDING,
        Approval.remind_after.is_not(None),
        Approval.remind_after < due_before,
    )
    return list(session.scalars(stmt))


def record_reminder(session: Session, approval: Approval, *, now: datetime, next_after: datetime) -> None:
    approval.remind_count += 1
    approval.last_reminded_at = now
    approval.remind_after = next_after


def attach_slack_ref(session: Session, approval_id: uuid.UUID, channel: str, ts: str) -> None:
    session.execute(
        update(Approval)
        .where(Approval.id == approval_id)
        .values(slack_channel=channel, slack_ts=ts)
    )
```

- [ ] **Step 4: Write `src/aicom/gate/service.py`**

```python
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from aicom.domain.enums import ApprovalKind, RunStatus
from aicom.store.approvals import create_approval
from aicom.store.runs import transition

_TERMINATE_MESSAGE = (
    "Approval request {approval_id} recorded and is pending human sign-off. "
    "Stop working now and terminate. You will be resumed automatically with the "
    "decision once it is signed off. Do not attempt the gated action by any other means."
)


class UnknownApprovalKind(Exception):
    pass


class RunNotRunning(Exception):
    pass


@dataclass(frozen=True, slots=True)
class GateResult:
    approval_id: uuid.UUID
    message: str


def request_approval(
    session: Session,
    *,
    run_id: uuid.UUID,
    kind: str,
    proposal: str,
    payload: dict,
    now: datetime,
) -> GateResult:
    try:
        approval_kind = ApprovalKind(kind)
    except ValueError as exc:
        allowed = ", ".join(k.value for k in ApprovalKind)
        raise UnknownApprovalKind(f"kind must be one of: {allowed}") from exc

    approval = create_approval(
        session,
        run_id=run_id,
        kind=approval_kind,
        proposal=proposal,
        payload=payload,
        now=now,
    )
    if not transition(session, run_id, RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL):
        raise RunNotRunning(f"run {run_id} is not running")
    return GateResult(
        approval_id=approval.id,
        message=_TERMINATE_MESSAGE.format(approval_id=approval.id),
    )
```

- [ ] **Step 5: Write `src/aicom/gate/server.py`**

```python
"""MCP server exposing the single gated-action tool.

This is the ONLY route an agent has to money, publication, or third-party
contact. Dangerous tools are absent from --allowedTools, so an agent cannot
reach them directly no matter what its prompt says.
"""

import os
import uuid
from datetime import UTC, datetime

from mcp.server.fastmcp import FastMCP

from aicom.config import load_settings
from aicom.gate.service import request_approval
from aicom.store.db import make_engine, session_factory

mcp = FastMCP("gate")
_settings = load_settings()
_sessions = session_factory(make_engine(_settings.database_url))


@mcp.tool()
def request_approval_tool(kind: str, proposal: str, payload: dict | None = None) -> str:
    """Submit a completed proposal for human sign-off, then terminate.

    kind: one of spend, publish, contact, execute_order.
    proposal: markdown stating WHAT, WHY, HOW MUCH, ALTERNATIVES, and
        whether the action is REVERSIBLE. Never ask an open question here.
    payload: structured details, e.g. {"amount_usd": 20, "vendor": "X"}.
    """
    run_id = uuid.UUID(os.environ["AICOM_RUN_ID"])
    with _sessions() as session:
        result = request_approval(
            session,
            run_id=run_id,
            kind=kind,
            proposal=proposal,
            payload=payload or {},
            now=datetime.now(UTC),
        )
        session.commit()
    return result.message


if __name__ == "__main__":
    mcp.run()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/gate -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/aicom/gate src/aicom/store/approvals.py tests/gate
git commit -m "feat(gate): add MCP approval gate and approval repository"
```

---

### Task 9: Slack notifier and Block Kit builders

**Files:**
- Create: `src/aicom/notify/__init__.py`, `src/aicom/notify/base.py`, `src/aicom/notify/blocks.py`, `src/aicom/notify/slack.py`, `src/aicom/notify/fake.py`, `src/aicom/domain/views.py`
- Test: `tests/notify/test_blocks.py`

**Interfaces:**
- Consumes: enums (Task 1).
- Produces:
  - `ApprovalView(approval_id: UUID, nonce: str, kind: ApprovalKind, agent_name: str, task_title: str, proposal: str, payload: dict)` and `RunReport(run_id: UUID, agent_name: str, task_title: str, status: RunStatus, summary: str, cost_usd: float | None)` in `domain/views.py`.
  - `DispatchRef(channel: str, ts: str)`.
  - `Notifier` protocol: `send_approval_request(view) -> DispatchRef`, `send_batch_approval_request(views) -> DispatchRef`, `send_reminder(view, stage: int) -> None`, `send_run_report(report) -> None`, `send_system_notice(text: str) -> None`.
  - `approval_blocks(view) -> list[dict]`, `batch_approval_blocks(views) -> list[dict]`, `run_report_blocks(report) -> list[dict]`.
  - `SlackNotifier(client, channel)`, `FakeNotifier` recording every call.

- [ ] **Step 1: Write the failing test**

Create `tests/notify/test_blocks.py`:

```python
import json
import uuid

from aicom.domain.enums import ApprovalKind
from aicom.domain.views import ApprovalView
from aicom.notify.blocks import approval_blocks, batch_approval_blocks


def _view(kind: ApprovalKind = ApprovalKind.SPEND) -> ApprovalView:
    return ApprovalView(
        approval_id=uuid.uuid4(),
        nonce="nonce-abc",
        kind=kind,
        agent_name="researcher",
        task_title="Buy market data",
        proposal="Buy feed X for $20/mo. Alternative: free tier. Reversible: yes.",
        payload={"amount_usd": 20},
    )


def test_approval_blocks_carry_nonce_in_both_buttons() -> None:
    view = _view()
    blocks = approval_blocks(view)
    actions = [b for b in blocks if b["type"] == "actions"][0]

    values = {json.loads(el["value"])["nonce"] for el in actions["elements"]}
    action_ids = {el["action_id"] for el in actions["elements"]}
    assert values == {"nonce-abc"}
    assert action_ids == {"approve", "reject"}


def test_approval_blocks_show_kind_agent_task_and_proposal() -> None:
    blocks = approval_blocks(_view())
    text = json.dumps(blocks)
    assert "spend" in text
    assert "researcher" in text
    assert "Buy market data" in text
    assert "Alternative: free tier" in text


def test_batch_blocks_hold_one_button_pair_per_item_plus_approve_all() -> None:
    views = [_view(), _view(ApprovalKind.PUBLISH)]
    blocks = batch_approval_blocks(views)
    text = json.dumps(blocks)

    assert text.count('"action_id": "approve"') == 2
    assert '"action_id": "approve_all"' in text
    payload = json.loads(
        [b for b in blocks if b["type"] == "actions" and any(
            el["action_id"] == "approve_all" for el in b["elements"]
        )][0]["elements"][0]["value"]
    )
    assert set(payload["nonces"]) == {v.nonce for v in views}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/notify/test_blocks.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.domain.views'`

- [ ] **Step 3: Write `src/aicom/domain/views.py`**

```python
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
```

- [ ] **Step 4: Write `src/aicom/notify/blocks.py`**

```python
import json
from collections.abc import Sequence

from aicom.domain.views import ApprovalView, RunReport


def _header(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": text[:150]}}


def _section(markdown: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": markdown[:2900]}}


def _decision_buttons(nonce: str) -> dict:
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "action_id": "approve",
                "style": "primary",
                "text": {"type": "plain_text", "text": "Approve"},
                "value": json.dumps({"nonce": nonce}),
            },
            {
                "type": "button",
                "action_id": "reject",
                "style": "danger",
                "text": {"type": "plain_text", "text": "Reject"},
                "value": json.dumps({"nonce": nonce}),
            },
        ],
    }


def approval_blocks(view: ApprovalView) -> list[dict]:
    return [
        _header(f"Sign-off needed: {view.kind.value}"),
        _section(f"*{view.agent_name}* · _{view.task_title}_"),
        _section(view.proposal),
        _section(f"```{json.dumps(view.payload, indent=2, ensure_ascii=False)}```"),
        _decision_buttons(view.nonce),
    ]


def batch_approval_blocks(views: Sequence[ApprovalView]) -> list[dict]:
    blocks: list[dict] = [_header(f"{len(views)} items awaiting sign-off")]
    for view in views:
        blocks.append(_section(f"*{view.kind.value}* · {view.agent_name} · _{view.task_title}_"))
        blocks.append(_section(view.proposal))
        blocks.append(_decision_buttons(view.nonce))
        blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "approve_all",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve all"},
                    "value": json.dumps({"nonces": [v.nonce for v in views]}),
                }
            ],
        }
    )
    return blocks


def run_report_blocks(report: RunReport) -> list[dict]:
    cost = f" · ${report.cost_usd:.2f}" if report.cost_usd is not None else ""
    return [
        _section(f"*{report.agent_name}* · _{report.task_title}_ → `{report.status.value}`{cost}"),
        _section(report.summary),
    ]
```

- [ ] **Step 5: Write `src/aicom/notify/base.py`**

```python
from collections.abc import Sequence
from typing import Protocol

from aicom.domain.views import ApprovalView, DispatchRef, RunReport


class Notifier(Protocol):
    def send_approval_request(self, view: ApprovalView) -> DispatchRef: ...
    def send_batch_approval_request(
        self, views: Sequence[ApprovalView]
    ) -> DispatchRef: ...
    def send_reminder(self, view: ApprovalView, stage: int) -> None: ...
    def send_run_report(self, report: RunReport) -> None: ...
    def send_system_notice(self, text: str) -> None: ...
```

- [ ] **Step 6: Write `src/aicom/notify/slack.py`**

```python
from collections.abc import Sequence

from slack_sdk import WebClient

from aicom.domain.views import ApprovalView, DispatchRef, RunReport
from aicom.notify.blocks import approval_blocks, batch_approval_blocks, run_report_blocks

_STAGE_CHANNEL = {1: "thread", 2: "dm", 3: "digest"}


class SlackNotifier:
    def __init__(self, client: WebClient, channel: str, dm_user_id: str = "") -> None:
        self._client = client
        self._channel = channel
        self._dm_user_id = dm_user_id

    def send_approval_request(self, view: ApprovalView) -> DispatchRef:
        response = self._client.chat_postMessage(
            channel=self._channel,
            blocks=approval_blocks(view),
            text=f"Sign-off needed: {view.kind.value} — {view.task_title}",
        )
        return DispatchRef(channel=response["channel"], ts=response["ts"])

    def send_batch_approval_request(self, views: Sequence[ApprovalView]) -> DispatchRef:
        response = self._client.chat_postMessage(
            channel=self._channel,
            blocks=batch_approval_blocks(views),
            text=f"{len(views)} items awaiting sign-off",
        )
        return DispatchRef(channel=response["channel"], ts=response["ts"])

    def send_reminder(self, view: ApprovalView, stage: int) -> None:
        target = _STAGE_CHANNEL.get(stage, "digest")
        text = f"Reminder ({stage}): {view.kind.value} — {view.task_title} still awaiting sign-off"
        if target == "thread":
            self._client.chat_postMessage(
                channel=self._channel, thread_ts=None, text=text
            )
        elif target == "dm" and self._dm_user_id:
            self._client.chat_postMessage(channel=self._dm_user_id, text=text)
        else:
            self._client.chat_postMessage(channel=self._channel, text=text)

    def send_run_report(self, report: RunReport) -> None:
        self._client.chat_postMessage(
            channel=self._channel,
            blocks=run_report_blocks(report),
            text=f"{report.task_title} → {report.status.value}",
        )

    def send_system_notice(self, text: str) -> None:
        self._client.chat_postMessage(channel=self._channel, text=text)
```

- [ ] **Step 7: Write `src/aicom/notify/fake.py`**

```python
from collections.abc import Sequence
from dataclasses import dataclass, field

from aicom.domain.views import ApprovalView, DispatchRef, RunReport


@dataclass(slots=True)
class FakeNotifier:
    approvals: list[ApprovalView] = field(default_factory=list)
    batches: list[list[ApprovalView]] = field(default_factory=list)
    reminders: list[tuple[ApprovalView, int]] = field(default_factory=list)
    reports: list[RunReport] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    _counter: int = 0

    def send_approval_request(self, view: ApprovalView) -> DispatchRef:
        self.approvals.append(view)
        self._counter += 1
        return DispatchRef(channel="C1", ts=f"{self._counter}.000")

    def send_batch_approval_request(self, views: Sequence[ApprovalView]) -> DispatchRef:
        self.batches.append(list(views))
        self._counter += 1
        return DispatchRef(channel="C1", ts=f"{self._counter}.000")

    def send_reminder(self, view: ApprovalView, stage: int) -> None:
        self.reminders.append((view, stage))

    def send_run_report(self, report: RunReport) -> None:
        self.reports.append(report)

    def send_system_notice(self, text: str) -> None:
        self.notices.append(text)
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `pytest tests/notify -v`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add src/aicom/notify src/aicom/domain/views.py tests/notify
git commit -m "feat(notify): add Slack Block Kit approval messages and notifier protocol"
```

---

### Task 10: Slack inbound — signature verification and approval resolution

**Files:**
- Create: `src/aicom/inbound/__init__.py`, `src/aicom/inbound/verify.py`, `src/aicom/inbound/app.py`
- Test: `tests/inbound/test_verify.py`, `tests/inbound/test_app.py`

**Interfaces:**
- Consumes: `consume_nonce` (Task 8), `transition` (Task 3).
- Produces: `verify_slack_signature(*, signing_secret: str, timestamp: str, body: bytes, signature: str, now: datetime) -> bool`; `create_app(sessions, settings) -> FastAPI` serving `POST /slack/interactions`; `resolve_approval(session, *, nonce, decided_by, approved, now) -> bool` which consumes the nonce and re-queues the parked run.

- [ ] **Step 1: Write the failing test for signature verification**

Create `tests/inbound/test_verify.py`:

```python
import hashlib
import hmac
from datetime import UTC, datetime, timedelta

from aicom.inbound.verify import verify_slack_signature

SECRET = "shhh"
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
BODY = b"payload=%7B%22ok%22%3Atrue%7D"


def _sign(timestamp: str, body: bytes, secret: str = SECRET) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def test_valid_signature_accepted() -> None:
    ts = str(int(NOW.timestamp()))
    assert verify_slack_signature(
        signing_secret=SECRET, timestamp=ts, body=BODY, signature=_sign(ts, BODY), now=NOW
    )


def test_forged_signature_rejected() -> None:
    ts = str(int(NOW.timestamp()))
    assert not verify_slack_signature(
        signing_secret=SECRET,
        timestamp=ts,
        body=BODY,
        signature=_sign(ts, BODY, secret="wrong"),
        now=NOW,
    )


def test_tampered_body_rejected() -> None:
    ts = str(int(NOW.timestamp()))
    assert not verify_slack_signature(
        signing_secret=SECRET,
        timestamp=ts,
        body=b"payload=evil",
        signature=_sign(ts, BODY),
        now=NOW,
    )


def test_replay_outside_five_minute_window_rejected() -> None:
    old = NOW - timedelta(minutes=6)
    ts = str(int(old.timestamp()))
    assert not verify_slack_signature(
        signing_secret=SECRET, timestamp=ts, body=BODY, signature=_sign(ts, BODY), now=NOW
    )


def test_garbage_timestamp_rejected() -> None:
    assert not verify_slack_signature(
        signing_secret=SECRET, timestamp="nope", body=BODY, signature="v0=abc", now=NOW
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/inbound/test_verify.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.inbound.verify'`

- [ ] **Step 3: Write `src/aicom/inbound/verify.py`**

```python
import hashlib
import hmac
from datetime import datetime

REPLAY_WINDOW_SECONDS = 300


def verify_slack_signature(
    *, signing_secret: str, timestamp: str, body: bytes, signature: str, now: datetime
) -> bool:
    """The interaction endpoint is public. Without this, anyone could forge a
    request and approve real spending."""
    if not signing_secret:
        return False
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(now.timestamp()) - sent_at) > REPLAY_WINDOW_SECONDS:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")
```

- [ ] **Step 4: Write the failing test for the endpoint**

Create `tests/inbound/test_app.py`:

```python
import hashlib
import hmac
import json
import urllib.parse
import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from aicom.config import Settings
from aicom.domain.enums import ApprovalStatus, RunStatus
from aicom.gate.service import request_approval
from aicom.inbound.app import create_app
from aicom.store.models import Approval, Run, Task
from aicom.store.runs import claim_next_queued
from tests.store.test_models import make_agent

SECRET = "shhh"
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(
        slack_signing_secret=SECRET,
        slack_approver_ids=("U_OWNER",),
        database_url="postgresql+psycopg://unused/unused",
    )


def _post(client: TestClient, payload: dict) -> object:
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
    ts = str(int(datetime.now(UTC).timestamp()))
    base = b"v0:" + ts.encode() + b":" + body
    sig = "v0=" + hmac.new(SECRET.encode(), base, hashlib.sha256).hexdigest()
    return client.post(
        "/slack/interactions",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
        },
    )


def _parked_approval(session: Session) -> Approval:
    agent = make_agent(session)
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="t", goal="g")
    session.add(task)
    session.flush()
    session.add(Run(id=uuid.uuid4(), task_id=task.id, attempt=1))
    session.commit()
    run = claim_next_queued(session, worker_id="w1", now=NOW)
    assert run is not None
    result = request_approval(
        session, run_id=run.id, kind="spend", proposal="p", payload={}, now=NOW
    )
    session.commit()
    approval = session.get(Approval, result.approval_id)
    assert approval is not None
    return approval


def _action(nonce: str, action_id: str = "approve", user: str = "U_OWNER") -> dict:
    return {
        "type": "block_actions",
        "user": {"id": user},
        "actions": [{"action_id": action_id, "value": json.dumps({"nonce": nonce})}],
    }


def test_approve_requeues_the_parked_run(
    sessions: sessionmaker[Session], session: Session
) -> None:
    approval = _parked_approval(session)
    client = TestClient(create_app(sessions, _settings()))

    response = _post(client, _action(approval.nonce))
    assert response.status_code == 200

    session.expire_all()
    refreshed = session.get(Approval, approval.id)
    assert refreshed is not None
    assert refreshed.status is ApprovalStatus.APPROVED
    assert refreshed.run.status is RunStatus.QUEUED
    assert refreshed.run.resume_pending is True


def test_non_allowlisted_user_cannot_approve(
    sessions: sessionmaker[Session], session: Session
) -> None:
    approval = _parked_approval(session)
    client = TestClient(create_app(sessions, _settings()))

    response = _post(client, _action(approval.nonce, user="U_STRANGER"))
    assert response.status_code == 403

    session.expire_all()
    refreshed = session.get(Approval, approval.id)
    assert refreshed is not None
    assert refreshed.status is ApprovalStatus.PENDING


def test_bad_signature_rejected(sessions: sessionmaker[Session], session: Session) -> None:
    approval = _parked_approval(session)
    client = TestClient(create_app(sessions, _settings()))

    body = urllib.parse.urlencode({"payload": json.dumps(_action(approval.nonce))}).encode()
    response = client.post(
        "/slack/interactions",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": str(int(datetime.now(UTC).timestamp())),
            "X-Slack-Signature": "v0=deadbeef",
        },
    )
    assert response.status_code == 401


def test_duplicate_click_is_a_no_op(
    sessions: sessionmaker[Session], session: Session
) -> None:
    approval = _parked_approval(session)
    client = TestClient(create_app(sessions, _settings()))

    assert _post(client, _action(approval.nonce)).status_code == 200
    second = _post(client, _action(approval.nonce, action_id="reject"))
    assert second.status_code == 200

    session.expire_all()
    refreshed = session.get(Approval, approval.id)
    assert refreshed is not None
    assert refreshed.status is ApprovalStatus.APPROVED  # unchanged by the second click
```

- [ ] **Step 5: Run test to verify it fails**

Run: `pytest tests/inbound/test_app.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.inbound.app'`

- [ ] **Step 6: Write `src/aicom/inbound/app.py`**

```python
import json
from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response
from sqlalchemy.orm import Session, sessionmaker

from aicom.config import Settings
from aicom.domain.enums import RunStatus
from aicom.inbound.verify import verify_slack_signature
from aicom.store.approvals import consume_nonce
from aicom.store.runs import transition


def resolve_approval(
    session: Session, *, nonce: str, decided_by: str, approved: bool, now: datetime
) -> bool:
    approval = consume_nonce(
        session, nonce=nonce, decided_by=decided_by, approved=approved, now=now
    )
    if approval is None:
        return False
    note = "APPROVED" if approved else "REJECTED"
    transition(
        session,
        approval.run_id,
        RunStatus.AWAITING_APPROVAL,
        RunStatus.QUEUED,
        resume_pending=True,
        resume_note=f"Sign-off decision for approval {approval.id}: {note}.",
    )
    return True


def create_app(sessions: sessionmaker[Session], settings: Settings) -> FastAPI:
    app = FastAPI()

    @app.post("/slack/interactions")
    async def interactions(request: Request) -> Response:
        body = await request.body()
        now = datetime.now(UTC)
        if not verify_slack_signature(
            signing_secret=settings.slack_signing_secret,
            timestamp=request.headers.get("X-Slack-Request-Timestamp", ""),
            body=body,
            signature=request.headers.get("X-Slack-Signature", ""),
            now=now,
        ):
            return Response(status_code=401)

        form = await request.form()
        payload = json.loads(str(form.get("payload", "{}")))
        user_id = str(payload.get("user", {}).get("id", ""))
        if user_id not in settings.slack_approver_ids:
            return Response(status_code=403)

        nonces: list[tuple[str, bool]] = []
        for action in payload.get("actions", []):
            value = json.loads(action.get("value") or "{}")
            action_id = action.get("action_id")
            if action_id == "approve_all":
                nonces += [(n, True) for n in value.get("nonces", [])]
            elif action_id in {"approve", "reject"}:
                nonces.append((value["nonce"], action_id == "approve"))

        with sessions() as session:
            for nonce, approved in nonces:
                resolve_approval(
                    session, nonce=nonce, decided_by=user_id, approved=approved, now=now
                )
            session.commit()
        return Response(status_code=200)

    return app
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/inbound -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/aicom/inbound tests/inbound
git commit -m "feat(inbound): verify Slack signatures and resolve approvals to requeue runs"
```

---

### Task 11: Artifact commit

**Files:**
- Create: `src/aicom/orchestrator/__init__.py`, `src/aicom/orchestrator/artifacts.py`
- Test: `tests/orchestrator/test_artifacts.py`

**Interfaces:**
- Consumes: models (Task 2).
- Produces: `commit_run_artifacts(session, *, run_id: UUID, workspace: Path, repo: Path, label: str) -> str | None` returning the commit sha, or `None` when the workspace produced no files.

- [ ] **Step 1: Write the failing test**

Create `tests/orchestrator/test_artifacts.py`:

```python
import subprocess
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from aicom.orchestrator.artifacts import commit_run_artifacts
from aicom.store.models import Artifact, Run, Task
from tests.store.test_models import make_agent


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "artifacts"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    return repo


def _run(session: Session) -> Run:
    agent = make_agent(session)
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="t", goal="g")
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1)
    session.add(run)
    session.commit()
    return run


def test_commits_workspace_files_and_records_artifact_rows(
    session: Session, tmp_path: Path
) -> None:
    run = _run(session)
    workspace = tmp_path / "ws"
    (workspace / "sub").mkdir(parents=True)
    (workspace / "report.md").write_text("findings")
    (workspace / "sub" / "data.csv").write_text("a,b")
    repo = _repo(tmp_path)

    sha = commit_run_artifacts(
        session, run_id=run.id, workspace=workspace, repo=repo, label="researcher/t"
    )
    session.commit()

    assert sha is not None and len(sha) == 40
    dest = repo / str(run.id)
    assert (dest / "report.md").read_text() == "findings"
    assert (dest / "sub" / "data.csv").read_text() == "a,b"

    rows = list(session.scalars(select(Artifact).where(Artifact.run_id == run.id)))
    assert {Path(r.path).name for r in rows} == {"report.md", "data.csv"}
    assert {r.git_ref for r in rows} == {sha}


def test_empty_workspace_produces_no_commit(session: Session, tmp_path: Path) -> None:
    run = _run(session)
    workspace = tmp_path / "empty"
    workspace.mkdir()

    assert (
        commit_run_artifacts(
            session, run_id=run.id, workspace=workspace, repo=_repo(tmp_path), label="x"
        )
        is None
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/orchestrator/test_artifacts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.orchestrator.artifacts'`

- [ ] **Step 3: Write `src/aicom/orchestrator/artifacts.py`**

```python
import shutil
import subprocess
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from aicom.store.models import Artifact


def commit_run_artifacts(
    session: Session, *, run_id: uuid.UUID, workspace: Path, repo: Path, label: str
) -> str | None:
    """Copy run outputs into the artifact repo and commit them.

    Called serially by the orchestrator, so concurrent runs never contend on git.
    """
    files = [p for p in workspace.rglob("*") if p.is_file()]
    if not files:
        return None

    dest_root = repo / str(run_id)
    for src in files:
        dest = dest_root / src.relative_to(workspace)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    subprocess.run(["git", "add", "--", str(dest_root)], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", f"artifacts: {label} ({run_id})"], cwd=repo, check=True
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    for src in files:
        rel = src.relative_to(workspace)
        session.add(
            Artifact(
                id=uuid.uuid4(),
                run_id=run_id,
                kind=src.suffix.lstrip(".") or "file",
                git_ref=sha,
                path=str(rel),
            )
        )
    return sha
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/orchestrator/test_artifacts.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/aicom/orchestrator tests/orchestrator
git commit -m "feat(orchestrator): commit run artifacts to the artifact repo"
```

---

### Task 12: Worker loop

**Files:**
- Create: `src/aicom/orchestrator/prompts.py`, `src/aicom/orchestrator/worker.py`
- Test: `tests/orchestrator/test_worker.py`

**Interfaces:**
- Consumes: everything from Tasks 1–11.
- Produces: `build_prompt(task_title: str, goal: str, resume_note: str | None) -> str`; `Worker(sessions, executor, notifier, settings, worker_id: str)` with `tick(now: datetime | None = None) -> bool` (returns True when a run was processed) and `finalize(session: Session, run: Run, outcome: RunOutcome, request: RunRequest, now: datetime) -> None`. Module constants `MAX_ATTEMPTS = 3` and `GATE_TOOL = "mcp__gate__request_approval_tool"`.

- [ ] **Step 1: Write the failing test**

Create `tests/orchestrator/test_worker.py`:

```python
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from aicom.config import Settings
from aicom.domain.enums import ExitReason, RunStatus
from aicom.executor.fake import FakeExecutor
from aicom.notify.fake import FakeNotifier
from aicom.orchestrator.worker import Worker
from aicom.store.models import Run, Task
from aicom.store.system_state import get_pause
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        workspace_root=tmp_path / "ws",
        artifact_repo_path=tmp_path / "artifacts",
        database_url="postgresql+psycopg://unused/unused",
    )


def _queued(session: Session) -> Run:
    agent = make_agent(session)
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="Research", goal="Find things.")
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1)
    session.add(run)
    session.commit()
    return run


def _worker(
    sessions: sessionmaker[Session],
    executor: FakeExecutor,
    notifier: FakeNotifier,
    tmp_path: Path,
) -> Worker:
    (tmp_path / "artifacts").mkdir(parents=True, exist_ok=True)
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path / "artifacts", check=True)
    subprocess.run(
        ["git", "config", "user.email", "a@b.c"], cwd=tmp_path / "artifacts", check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=tmp_path / "artifacts", check=True
    )
    return Worker(sessions, executor, notifier, _settings(tmp_path), worker_id="w1")


def test_successful_run_persists_events_and_reports(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    run = _queued(session)
    executor, notifier = FakeExecutor(), FakeNotifier()
    executor.queue(
        run.id,
        [
            json.dumps({"type": "system", "session_id": "s-1"}),
            json.dumps({"type": "result", "total_cost_usd": 0.3, "usage": {"output_tokens": 9}}),
        ],
    )

    assert _worker(sessions, executor, notifier, tmp_path).tick(NOW) is True

    session.expire_all()
    refreshed = session.get(Run, run.id)
    assert refreshed is not None
    assert refreshed.status is RunStatus.SUCCEEDED
    assert refreshed.session_id == "s-1"
    assert refreshed.cost_usd == 0.3
    assert refreshed.token_out == 9
    assert len(notifier.reports) == 1
    assert executor.requests[0].allowed_tools[-1] == "mcp__gate__request_approval_tool"


def test_crash_retries_then_fails_after_max_attempts(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    run = _queued(session)
    executor, notifier = FakeExecutor(), FakeNotifier()
    worker = _worker(sessions, executor, notifier, tmp_path)

    for _ in range(3):
        pending = session.execute(
            Run.__table__.select().where(Run.status == RunStatus.QUEUED)
        ).first()
        assert pending is not None
        executor.queue(pending.id, [], reason=ExitReason.CRASHED, exit_code=1)
        assert worker.tick(NOW) is True
        session.expire_all()

    statuses = [
        r.status for r in session.query(Run).filter(Run.task_id == run.task_id).all()
    ]
    assert statuses.count(RunStatus.FAILED) == 1
    assert any("failed" in n.lower() for n in notifier.notices + [
        r.summary for r in notifier.reports
    ])


def test_usage_limit_pauses_globally_and_requeues_without_burning_an_attempt(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    run = _queued(session)
    reset_epoch = int((NOW + timedelta(hours=2)).timestamp())
    executor, notifier = FakeExecutor(), FakeNotifier()
    executor.queue(
        run.id,
        [json.dumps({"type": "result", "is_error": True, "result": f"usage limit reached|{reset_epoch}"})],
    )
    worker = _worker(sessions, executor, notifier, tmp_path)

    assert worker.tick(NOW) is True

    session.expire_all()
    refreshed = session.get(Run, run.id)
    assert refreshed is not None
    assert refreshed.status is RunStatus.QUEUED
    assert refreshed.attempt == 1  # not the agent's fault
    assert get_pause(session) == NOW + timedelta(hours=2)
    assert len(notifier.notices) == 1

    # while paused, no new work is claimed and no duplicate notice is sent
    assert worker.tick(NOW + timedelta(minutes=1)) is False
    assert len(notifier.notices) == 1

    # after the reset time the worker resumes
    executor.queue(run.id, [json.dumps({"type": "result"})])
    assert worker.tick(NOW + timedelta(hours=2, minutes=1)) is True


def test_gate_request_leaves_run_parked_and_sends_approval(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    from aicom.gate.service import request_approval

    run = _queued(session)
    executor, notifier = FakeExecutor(), FakeNotifier()

    class GateExecutor(FakeExecutor):
        def run(self, req, on_event):  # type: ignore[no-untyped-def]
            with sessions() as s:
                request_approval(
                    s,
                    run_id=req.run_id,
                    kind="spend",
                    proposal="Buy X for $20. Alt: free tier. Reversible: yes.",
                    payload={"amount_usd": 20},
                    now=NOW,
                )
                s.commit()
            return super().run(req, on_event)

    gate_executor = GateExecutor()
    gate_executor.queue(run.id, [json.dumps({"type": "system", "session_id": "s-7"})])

    _worker(sessions, gate_executor, notifier, tmp_path).tick(NOW)

    session.expire_all()
    refreshed = session.get(Run, run.id)
    assert refreshed is not None
    assert refreshed.status is RunStatus.AWAITING_APPROVAL
    assert refreshed.session_id == "s-7"
    assert len(notifier.approvals) == 1
    assert notifier.approvals[0].payload == {"amount_usd": 20}


def test_resume_passes_session_id_and_decision_note(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    run = _queued(session)
    run.session_id = "s-42"
    run.resume_pending = True
    run.resume_note = "Sign-off decision for approval abc: APPROVED."
    session.commit()

    executor, notifier = FakeExecutor(), FakeNotifier()
    executor.queue(run.id, [json.dumps({"type": "result"})])
    _worker(sessions, executor, notifier, tmp_path).tick(NOW)

    request = executor.requests[0]
    assert request.resume_session_id == "s-42"
    assert "APPROVED" in request.prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/orchestrator/test_worker.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.orchestrator.worker'`

- [ ] **Step 3: Write `src/aicom/orchestrator/prompts.py`**

```python
_BASE = """# Task: {title}

{goal}

## Operating rules

You work autonomously. Research, analysis, code, backtests, and drafts need no
permission — do them and report what you did.

Four actions are impossible for you to take directly and require human sign-off:
spending money, executing orders, publishing or transmitting anything outside this
workspace, and contacting third parties. To take one, call the
`request_approval_tool` MCP tool with a COMPLETE proposal: what, why, how much,
what alternatives you considered, and whether it is reversible. Never use it to ask
an open question — decisions that are yours to make, you make.

If a requirement is ambiguous, choose the most reasonable interpretation, state the
assumption explicitly in your final report, and continue. Do not stop to ask.

Write all outputs as files in your working directory.
"""

_RESUME = """
## Sign-off result

{note}

Continue from where you stopped, acting on this decision.
"""


def build_prompt(task_title: str, goal: str, resume_note: str | None) -> str:
    prompt = _BASE.format(title=task_title, goal=goal)
    if resume_note:
        prompt += _RESUME.format(note=resume_note)
    return prompt
```

- [ ] **Step 4: Write `src/aicom/orchestrator/worker.py`**

```python
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aicom.config import Settings
from aicom.domain.enums import ApprovalStatus, ExitReason, RunStatus, TaskStatus
from aicom.domain.views import ApprovalView, RunReport
from aicom.executor.base import Executor, RunOutcome, RunRequest
from aicom.executor.secrets import resolve_secrets
from aicom.executor.stream import ParsedEvent
from aicom.executor.workspace import prepare_workspace
from aicom.notify.base import Notifier
from aicom.orchestrator.artifacts import commit_run_artifacts
from aicom.orchestrator.prompts import build_prompt
from aicom.quota.reset import fallback_backoff, parse_reset_at
from aicom.store.events import append_event
from aicom.store.models import Approval, Run, SystemState
from aicom.store.runs import claim_next_queued, touch_heartbeat, transition
from aicom.store.system_state import (
    clear_pause,
    get_pause,
    mark_pause_notified,
    pause_notified_at,
    set_pause,
)

MAX_ATTEMPTS = 3
GATE_TOOL = "mcp__gate__request_approval_tool"


class Worker:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        executor: Executor,
        notifier: Notifier,
        settings: Settings,
        worker_id: str,
    ) -> None:
        self._sessions = sessions
        self._executor = executor
        self._notifier = notifier
        self._settings = settings
        self._worker_id = worker_id

    def tick(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        with self._sessions() as session:
            paused_until = get_pause(session)
            if paused_until is not None and paused_until > now:
                session.commit()
                return False
            if paused_until is not None:
                clear_pause(session)
            run = claim_next_queued(session, worker_id=self._worker_id, now=now)
            session.commit()
            if run is None:
                return False
            request = self._build_request(session, run)
            session.commit()

        outcome = self._execute(run.id, request)

        with self._sessions() as session:
            fresh = session.get(Run, run.id)
            assert fresh is not None
            self.finalize(session, fresh, outcome, request, now)
            session.commit()
        return True

    def _build_request(self, session: Session, run: Run) -> RunRequest:
        agent = run.task.agent
        workspace = prepare_workspace(
            self._settings.workspace_root, agent.name, run.id
        )
        mcp_config, env = resolve_secrets(agent.mcp_config, _lookup_secret)
        run.workspace_path = str(workspace)
        allowed = tuple(agent.allowed_tools) + (GATE_TOOL,)
        return RunRequest(
            run_id=run.id,
            prompt=build_prompt(run.task.title, run.task.goal, run.resume_note),
            workspace=workspace,
            allowed_tools=allowed,
            mcp_config=mcp_config,
            env=env,
            resume_session_id=run.session_id if run.resume_pending else None,
            timeout_seconds=agent.max_run_seconds,
        )

    def _execute(self, run_id: uuid.UUID, request: RunRequest) -> RunOutcome:
        def on_event(event: ParsedEvent) -> None:
            with self._sessions() as session:
                append_event(session, run_id, event.seq, event.type, event.payload)
                touch_heartbeat(session, run_id, datetime.now(UTC))
                session.commit()

        return self._executor.run(request, on_event)

    def finalize(
        self,
        session: Session,
        run: Run,
        outcome: RunOutcome,
        request: RunRequest,
        now: datetime,
    ) -> None:
        run.session_id = outcome.session_id or run.session_id
        run.cost_usd = outcome.cost_usd
        run.token_in = outcome.token_in
        run.token_out = outcome.token_out
        run.exit_reason = outcome.reason.value
        run.resume_pending = False
        run.resume_note = None
        session.flush()

        if outcome.reason is ExitReason.USAGE_LIMIT:
            self._handle_usage_limit(session, run, outcome, now)
            return

        parked = session.scalar(
            select(Approval).where(
                Approval.run_id == run.id, Approval.status == ApprovalStatus.PENDING
            )
        )
        if parked is not None and run.status is RunStatus.AWAITING_APPROVAL:
            self._dispatch_approval(session, run, parked)
            return

        if outcome.reason in {ExitReason.CRASHED, ExitReason.TIMEOUT}:
            self._handle_failure(session, run, outcome, now)
            return

        sha = commit_run_artifacts(
            session,
            run_id=run.id,
            workspace=request.workspace,
            repo=self._settings.artifact_repo_path,
            label=f"{run.task.agent.name}/{run.task.title}",
        )
        transition(session, run.id, RunStatus.RUNNING, RunStatus.SUCCEEDED, ended_at=now)
        run.task.status = TaskStatus.DONE
        self._notifier.send_run_report(
            RunReport(
                run_id=run.id,
                agent_name=run.task.agent.name,
                task_title=run.task.title,
                status=RunStatus.SUCCEEDED,
                summary=f"Completed. Artifacts: {sha or 'none'}",
                cost_usd=outcome.cost_usd,
            )
        )

    def _handle_usage_limit(
        self, session: Session, run: Run, outcome: RunOutcome, now: datetime
    ) -> None:
        state = session.get(SystemState, 1) or SystemState(id=1)
        until = parse_reset_at(outcome.usage_limit_text or "", now=now)
        if until is None:
            until = fallback_backoff(state.consecutive_pauses, now=now)
        set_pause(session, until, outcome.usage_limit_text or "usage limit")
        # not the agent's fault: requeue without burning an attempt
        transition(
            session,
            run.id,
            RunStatus.RUNNING,
            RunStatus.QUEUED,
            worker_id=None,
            started_at=None,
        )
        if pause_notified_at(session) is None:
            self._notifier.send_system_notice(
                f"LLM usage limit reached. Resuming at {until.isoformat()}."
            )
            mark_pause_notified(session, now)

    def _handle_failure(
        self, session: Session, run: Run, outcome: RunOutcome, now: datetime
    ) -> None:
        target = (
            RunStatus.TIMED_OUT if outcome.reason is ExitReason.TIMEOUT else RunStatus.FAILED
        )
        transition(session, run.id, RunStatus.RUNNING, target, ended_at=now)
        if run.attempt < MAX_ATTEMPTS:
            session.add(
                Run(id=uuid.uuid4(), task_id=run.task_id, attempt=run.attempt + 1)
            )
            return
        run.task.status = TaskStatus.FAILED
        self._notifier.send_run_report(
            RunReport(
                run_id=run.id,
                agent_name=run.task.agent.name,
                task_title=run.task.title,
                status=target,
                summary=f"Run failed after {run.attempt} attempts ({outcome.reason.value}).",
                cost_usd=outcome.cost_usd,
            )
        )

    def _dispatch_approval(
        self, session: Session, run: Run, approval: Approval
    ) -> None:
        from aicom.store.approvals import attach_slack_ref

        view = ApprovalView(
            approval_id=approval.id,
            nonce=approval.nonce,
            kind=approval.kind,
            agent_name=run.task.agent.name,
            task_title=run.task.title,
            proposal=approval.proposal,
            payload=approval.payload,
        )
        ref = self._notifier.send_approval_request(view)
        attach_slack_ref(session, approval.id, ref.channel, ref.ts)


def _lookup_secret(ref: str) -> str | None:
    import os

    from aicom.executor.secrets import env_var_name

    return os.environ.get(env_var_name(ref))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/orchestrator -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/aicom/orchestrator tests/orchestrator
git commit -m "feat(orchestrator): add worker loop with gate parking, retry, and usage-limit pause"
```

---

### Task 13: Sweeper — reminders, stale-run recovery

**Files:**
- Create: `src/aicom/orchestrator/sweeper.py`
- Test: `tests/orchestrator/test_sweeper.py`

**Interfaces:**
- Consumes: `pending_approvals`, `record_reminder` (Task 8), `stale_running_runs`, `transition` (Task 3), `Notifier` (Task 9).
- Produces: `Sweeper(sessions, notifier)` with `sweep_reminders(now: datetime) -> int` and `recover_stale_runs(now: datetime, *, stale_after: timedelta) -> int`; `REMINDER_INTERVALS: tuple[timedelta, ...]`.

- [ ] **Step 1: Write the failing test**

Create `tests/orchestrator/test_sweeper.py`:

```python
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from aicom.domain.enums import RunStatus
from aicom.gate.service import request_approval
from aicom.notify.fake import FakeNotifier
from aicom.orchestrator.sweeper import Sweeper
from aicom.store.models import Approval, Run, Task
from aicom.store.runs import claim_next_queued, touch_heartbeat
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _parked(session: Session) -> Approval:
    agent = make_agent(session)
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="t", goal="g")
    session.add(task)
    session.flush()
    session.add(Run(id=uuid.uuid4(), task_id=task.id, attempt=1))
    session.commit()
    run = claim_next_queued(session, worker_id="w1", now=NOW)
    assert run is not None
    result = request_approval(
        session, run_id=run.id, kind="spend", proposal="p", payload={}, now=NOW
    )
    session.commit()
    approval = session.get(Approval, result.approval_id)
    assert approval is not None
    return approval


def test_reminders_escalate_through_stages_and_never_deny(
    sessions: sessionmaker[Session], session: Session
) -> None:
    approval = _parked(session)
    notifier = FakeNotifier()
    sweeper = Sweeper(sessions, notifier)

    assert sweeper.sweep_reminders(NOW) == 0  # first reminder not due yet
    assert sweeper.sweep_reminders(NOW + timedelta(minutes=31)) == 1
    assert sweeper.sweep_reminders(NOW + timedelta(minutes=40)) == 0  # backed off
    assert sweeper.sweep_reminders(NOW + timedelta(hours=5)) == 1
    assert sweeper.sweep_reminders(NOW + timedelta(days=2)) == 1

    assert [stage for _v, stage in notifier.reminders] == [1, 2, 3]

    session.expire_all()
    refreshed = session.get(Approval, approval.id)
    assert refreshed is not None
    assert refreshed.status.value == "pending"  # never auto-denied
    assert refreshed.remind_count == 3


def test_stale_running_run_returns_to_queued(
    sessions: sessionmaker[Session], session: Session
) -> None:
    agent = make_agent(session)
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="t", goal="g")
    session.add(task)
    session.flush()
    session.add(Run(id=uuid.uuid4(), task_id=task.id, attempt=1))
    session.commit()
    run = claim_next_queued(session, worker_id="dead-worker", now=NOW)
    assert run is not None
    touch_heartbeat(session, run.id, NOW)
    session.commit()

    recovered = Sweeper(sessions, FakeNotifier()).recover_stale_runs(
        NOW + timedelta(minutes=10), stale_after=timedelta(minutes=5)
    )
    assert recovered == 1

    session.expire_all()
    refreshed = session.get(Run, run.id)
    assert refreshed is not None
    assert refreshed.status is RunStatus.QUEUED
    assert refreshed.worker_id is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/orchestrator/test_sweeper.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.orchestrator.sweeper'`

- [ ] **Step 3: Write `src/aicom/orchestrator/sweeper.py`**

```python
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from aicom.domain.enums import RunStatus
from aicom.domain.views import ApprovalView
from aicom.notify.base import Notifier
from aicom.store.approvals import pending_approvals, record_reminder
from aicom.store.runs import stale_running_runs, transition

# stage 1: thread nudge, stage 2: DM, stage 3+: daily digest.
# Approvals NEVER expire — escalation is the only consequence of silence.
REMINDER_INTERVALS: tuple[timedelta, ...] = (
    timedelta(hours=4),
    timedelta(days=1),
    timedelta(days=1),
)


class Sweeper:
    def __init__(self, sessions: sessionmaker[Session], notifier: Notifier) -> None:
        self._sessions = sessions
        self._notifier = notifier

    def sweep_reminders(self, now: datetime) -> int:
        sent = 0
        with self._sessions() as session:
            for approval in pending_approvals(session, due_before=now):
                stage = approval.remind_count + 1
                view = ApprovalView(
                    approval_id=approval.id,
                    nonce=approval.nonce,
                    kind=approval.kind,
                    agent_name=approval.run.task.agent.name,
                    task_title=approval.run.task.title,
                    proposal=approval.proposal,
                    payload=approval.payload,
                )
                self._notifier.send_reminder(view, stage)
                index = min(approval.remind_count, len(REMINDER_INTERVALS) - 1)
                record_reminder(
                    session,
                    approval,
                    now=now,
                    next_after=now + REMINDER_INTERVALS[index],
                )
                sent += 1
            session.commit()
        return sent

    def recover_stale_runs(self, now: datetime, *, stale_after: timedelta) -> int:
        recovered = 0
        with self._sessions() as session:
            for run in stale_running_runs(session, older_than=now - stale_after):
                if transition(
                    session,
                    run.id,
                    RunStatus.RUNNING,
                    RunStatus.QUEUED,
                    worker_id=None,
                    started_at=None,
                ):
                    recovered += 1
            session.commit()
        return recovered
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/orchestrator -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/aicom/orchestrator tests/orchestrator
git commit -m "feat(orchestrator): add reminder escalation and stale-run recovery sweeper"
```

---

### Task 14: HTTP API and runnable entrypoints

**Files:**
- Create: `src/aicom/api/__init__.py`, `src/aicom/api/routes.py`, `src/aicom/main.py`, `README.md`
- Modify: `src/aicom/inbound/app.py` (mount the API router)
- Test: `tests/api/test_routes.py`

**Interfaces:**
- Consumes: models (Task 2), `Worker` (Task 12), `Sweeper` (Task 13).
- Produces: `router` with `POST /tasks`, `GET /tasks`, `GET /runs/{run_id}`, `GET /runs/{run_id}/events`, `GET /approvals`; `run_worker_forever(settings) -> None` in `main.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_routes.py`:

```python
import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from aicom.api.routes import make_router
from aicom.store.models import Run, Task
from tests.store.test_models import make_agent


def _client(sessions: sessionmaker[Session]) -> TestClient:
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(make_router(sessions))
    return TestClient(app)


def test_create_task_also_creates_a_queued_run(
    sessions: sessionmaker[Session], session: Session
) -> None:
    agent = make_agent(session)
    session.commit()
    client = _client(sessions)

    response = client.post(
        "/tasks",
        json={"agent_id": str(agent.id), "title": "Research widgets", "goal": "Find 5."},
    )
    assert response.status_code == 201
    task_id = uuid.UUID(response.json()["id"])

    session.expire_all()
    runs = session.query(Run).filter(Run.task_id == task_id).all()
    assert len(runs) == 1
    assert runs[0].status.value == "queued"


def test_get_run_events_returns_ordered_payloads(
    sessions: sessionmaker[Session], session: Session
) -> None:
    from aicom.store.events import append_event

    agent = make_agent(session)
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="t", goal="g")
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1)
    session.add(run)
    session.flush()
    append_event(session, run.id, 1, "assistant", {"n": 1})
    append_event(session, run.id, 0, "system", {"n": 0})
    session.commit()

    response = _client(sessions).get(f"/runs/{run.id}/events")
    assert response.status_code == 200
    assert [e["seq"] for e in response.json()] == [0, 1]


def test_unknown_run_returns_404(sessions: sessionmaker[Session]) -> None:
    response = _client(sessions).get(f"/runs/{uuid.uuid4()}")
    assert response.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/api/test_routes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.api.routes'`

- [ ] **Step 3: Write `src/aicom/api/routes.py`**

```python
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aicom.domain.enums import ApprovalStatus
from aicom.store.models import Approval, Event, Run, Task


class CreateTask(BaseModel):
    agent_id: uuid.UUID
    title: str
    goal: str
    parent_task_id: uuid.UUID | None = None


def make_router(sessions: sessionmaker[Session]) -> APIRouter:
    router = APIRouter()

    @router.post("/tasks", status_code=201)
    def create_task(body: CreateTask) -> dict:
        with sessions() as session:
            parent_depth = 0
            if body.parent_task_id is not None:
                parent = session.get(Task, body.parent_task_id)
                if parent is None:
                    raise HTTPException(404, "parent task not found")
                parent_depth = parent.depth + 1
            task = Task(
                id=uuid.uuid4(),
                agent_id=body.agent_id,
                title=body.title,
                goal=body.goal,
                parent_task_id=body.parent_task_id,
                depth=parent_depth,
            )
            session.add(task)
            session.flush()
            session.add(Run(id=uuid.uuid4(), task_id=task.id, attempt=1))
            session.commit()
            return {"id": str(task.id)}

    @router.get("/tasks")
    def list_tasks() -> list[dict]:
        with sessions() as session:
            tasks = session.scalars(select(Task).order_by(Task.created_at.desc()))
            return [
                {
                    "id": str(t.id),
                    "title": t.title,
                    "status": t.status.value,
                    "agent": t.agent.name,
                }
                for t in tasks
            ]

    @router.get("/runs/{run_id}")
    def get_run(run_id: uuid.UUID) -> dict:
        with sessions() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise HTTPException(404, "run not found")
            return {
                "id": str(run.id),
                "task_id": str(run.task_id),
                "status": run.status.value,
                "attempt": run.attempt,
                "exit_reason": run.exit_reason,
                "cost_usd": run.cost_usd,
            }

    @router.get("/runs/{run_id}/events")
    def get_events(run_id: uuid.UUID) -> list[dict]:
        with sessions() as session:
            events = session.scalars(
                select(Event).where(Event.run_id == run_id).order_by(Event.seq)
            )
            return [
                {"seq": e.seq, "type": e.type, "payload": e.payload} for e in events
            ]

    @router.get("/approvals")
    def list_pending() -> list[dict]:
        with sessions() as session:
            rows = session.scalars(
                select(Approval).where(Approval.status == ApprovalStatus.PENDING)
            )
            return [
                {
                    "id": str(a.id),
                    "kind": a.kind.value,
                    "proposal": a.proposal,
                    "remind_count": a.remind_count,
                    "task_title": a.run.task.title,
                }
                for a in rows
            ]

    return router
```

- [ ] **Step 4: Mount the router in `src/aicom/inbound/app.py`**

Add to `create_app`, immediately before `return app`:

```python
    from aicom.api.routes import make_router

    app.include_router(make_router(sessions))
```

- [ ] **Step 5: Write `src/aicom/main.py`**

```python
import time
from datetime import UTC, datetime, timedelta

from slack_sdk import WebClient

from aicom.config import load_settings
from aicom.executor.cli import ClaudeCliExecutor
from aicom.notify.slack import SlackNotifier
from aicom.orchestrator.sweeper import Sweeper
from aicom.orchestrator.worker import Worker
from aicom.store.db import make_engine, session_factory

STALE_AFTER = timedelta(minutes=10)


def run_worker_forever() -> None:
    settings = load_settings()
    sessions = session_factory(make_engine(settings.database_url))
    notifier = SlackNotifier(
        WebClient(token=settings.slack_bot_token), settings.slack_channel
    )
    worker = Worker(
        sessions, ClaudeCliExecutor(settings.claude_binary), notifier, settings, "w1"
    )
    sweeper = Sweeper(sessions, notifier)

    while True:
        now = datetime.now(UTC)
        sweeper.recover_stale_runs(now, stale_after=STALE_AFTER)
        sweeper.sweep_reminders(now)
        if not worker.tick(now):
            time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    run_worker_forever()
```

- [ ] **Step 6: Write `README.md`**

```markdown
# ai-com

Background agent orchestrator. Agents run autonomously; the operator signs off only on
money and publication.

## Run it

```bash
docker run -d -e POSTGRES_PASSWORD=aicom -e POSTGRES_USER=aicom -e POSTGRES_DB=aicom \
  -p 5432:5432 postgres:16-alpine
alembic upgrade head
uvicorn aicom.inbound.app:create_app --factory   # HTTP: API + Slack interactions
python -m aicom.main                             # worker + sweeper loop
```

Point the Slack app's Interactivity Request URL at `POST /slack/interactions`.

## Configuration

All settings use the `AICOM_` env prefix — see `src/aicom/config.py`.
Secrets referenced from `agent.mcp_config` resolve from `AICOM_SECRET_<SLUG>` env vars.
```

- [ ] **Step 7: Run the full suite**

Run: `pytest -v && ruff check src tests && mypy`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/aicom/api src/aicom/main.py src/aicom/inbound/app.py README.md tests/api
git commit -m "feat(api): add task/run/approval endpoints and worker entrypoint"
```

---

### Task 15: End-to-end integration and CLI smoke test

**Files:**
- Create: `tests/integration/test_flows.py`, `tests/integration/test_cli_smoke.py`
- Modify: `pyproject.toml` (register the `smoke` marker)

**Interfaces:**
- Consumes: everything.
- Produces: no new production interfaces.

- [ ] **Step 1: Register the smoke marker in `pyproject.toml`**

```toml
[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
markers = ["smoke: hits the real claude binary; skipped in CI"]
```

- [ ] **Step 2: Write the end-to-end test**

Create `tests/integration/test_flows.py`:

```python
import json
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from aicom.config import Settings
from aicom.domain.enums import ApprovalStatus, RunStatus
from aicom.executor.base import RunOutcome, RunRequest
from aicom.executor.fake import FakeExecutor
from aicom.gate.service import request_approval
from aicom.inbound.app import create_app
from aicom.notify.fake import FakeNotifier
from aicom.orchestrator.sweeper import Sweeper
from aicom.orchestrator.worker import Worker
from aicom.store.models import Approval, Run
from tests.inbound.test_app import SECRET, _post, _action
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    repo = tmp_path / "artifacts"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    return Settings(
        workspace_root=tmp_path / "ws",
        artifact_repo_path=repo,
        slack_signing_secret=SECRET,
        slack_approver_ids=("U_OWNER",),
        database_url="postgresql+psycopg://unused/unused",
    )


class GateThenFinishExecutor(FakeExecutor):
    """First call parks for sign-off; second call (resume) writes a report."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        super().__init__()
        self._sessions = sessions
        self.calls = 0

    def run(self, req: RunRequest, on_event) -> RunOutcome:  # type: ignore[no-untyped-def]
        self.calls += 1
        self.requests.append(req)
        if self.calls == 1:
            with self._sessions() as s:
                request_approval(
                    s,
                    run_id=req.run_id,
                    kind="spend",
                    proposal="Buy feed X, $20/mo. Alt: free tier. Reversible: yes.",
                    payload={"amount_usd": 20},
                    now=NOW,
                )
                s.commit()
            return RunOutcome(reason=__import__(
                "aicom.domain.enums", fromlist=["ExitReason"]
            ).ExitReason.GATE_REQUESTED, exit_code=0, session_id="sess-1")
        (req.workspace / "report.md").write_text("purchased and configured")
        return RunOutcome(
            reason=__import__(
                "aicom.domain.enums", fromlist=["ExitReason"]
            ).ExitReason.COMPLETED,
            exit_code=0,
            session_id="sess-1",
        )


def test_full_sign_off_cycle(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    agent = make_agent(session)
    session.commit()
    settings = _settings(tmp_path)
    notifier = FakeNotifier()
    executor = GateThenFinishExecutor(sessions)
    worker = Worker(sessions, executor, notifier, settings, worker_id="w1")
    client = TestClient(create_app(sessions, settings))

    created = client.post(
        "/tasks",
        json={"agent_id": str(agent.id), "title": "Buy data", "goal": "Get a feed."},
    )
    assert created.status_code == 201

    # 1. run parks for sign-off
    assert worker.tick(NOW) is True
    session.expire_all()
    run = session.query(Run).one()
    assert run.status is RunStatus.AWAITING_APPROVAL
    assert len(notifier.approvals) == 1

    # 2. other work is not blocked: no run is claimable, tick returns False cleanly
    assert worker.tick(NOW + timedelta(seconds=1)) is False

    # 3. reminder escalates without ever denying
    assert Sweeper(sessions, notifier).sweep_reminders(NOW + timedelta(minutes=31)) == 1
    session.expire_all()
    assert session.query(Approval).one().status is ApprovalStatus.PENDING

    # 4. operator approves in Slack
    approval = session.query(Approval).one()
    assert _post(client, _action(approval.nonce)).status_code == 200

    # 5. run resumes with the decision and finishes
    assert worker.tick(NOW + timedelta(minutes=40)) is True
    session.expire_all()
    run = session.query(Run).one()
    assert run.status is RunStatus.SUCCEEDED
    assert executor.requests[1].resume_session_id == "sess-1"
    assert "APPROVED" in executor.requests[1].prompt
    assert any(
        (settings.artifact_repo_path / str(run.id)).exists() for _ in [0]
    )


def test_usage_limit_pause_then_automatic_resume(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    agent = make_agent(session)
    session.commit()
    settings = _settings(tmp_path)
    notifier = FakeNotifier()
    executor = FakeExecutor()
    worker = Worker(sessions, executor, notifier, settings, worker_id="w1")
    client = TestClient(create_app(sessions, settings))
    client.post(
        "/tasks", json={"agent_id": str(agent.id), "title": "t", "goal": "g"}
    )

    session.expire_all()
    run_id = session.query(Run).one().id
    reset = int((NOW + timedelta(hours=1)).timestamp())
    executor.queue(
        run_id,
        [json.dumps({"type": "result", "is_error": True, "result": f"usage limit|{reset}"})],
    )

    assert worker.tick(NOW) is True
    assert worker.tick(NOW + timedelta(minutes=5)) is False
    assert len(notifier.notices) == 1

    executor.queue(run_id, [json.dumps({"type": "result"})])
    assert worker.tick(NOW + timedelta(hours=1, minutes=1)) is True
    session.expire_all()
    assert session.get(Run, run_id).status is RunStatus.SUCCEEDED  # type: ignore[union-attr]
```

- [ ] **Step 3: Run the integration test**

Run: `pytest tests/integration/test_flows.py -v`
Expected: PASS

- [ ] **Step 4: Write the CLI smoke test**

Create `tests/integration/test_cli_smoke.py`:

```python
import shutil
import uuid
from pathlib import Path

import pytest

from aicom.executor.base import RunRequest
from aicom.executor.cli import ClaudeCliExecutor
from aicom.domain.enums import ExitReason


@pytest.mark.smoke
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude binary not installed")
def test_real_cli_completes_a_trivial_task(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    request = RunRequest(
        run_id=uuid.uuid4(),
        prompt="Write the single word 'ok' to a file named result.txt. Then stop.",
        workspace=workspace,
        allowed_tools=("Write",),
        mcp_config={},
        env={},
        resume_session_id=None,
        timeout_seconds=180,
    )

    outcome = ClaudeCliExecutor().run(request, lambda _e: None)

    assert outcome.reason is ExitReason.COMPLETED
    assert outcome.session_id
    assert (workspace / "result.txt").read_text().strip().lower().startswith("ok")
```

- [ ] **Step 5: Run the full suite excluding smoke**

Run: `pytest -v -m "not smoke" && ruff check src tests && mypy`
Expected: PASS

- [ ] **Step 6: Run the smoke test once manually**

Run: `pytest -v -m smoke`
Expected: PASS (requires the `claude` binary and a logged-in account).

- [ ] **Step 7: Commit**

```bash
git add tests/integration pyproject.toml
git commit -m "test: add end-to-end sign-off and usage-limit flows plus CLI smoke test"
```

---

## Post-plan verification

After Task 15, confirm each spec requirement maps to shipped code:

| Spec requirement | Where |
|---|---|
| Create tasks for agents | Task 14 — `POST /tasks` |
| Permanent history | Task 2 (`event`, `run` tables) + Task 11 (artifact repo commits) |
| Progress tracking and reports | Task 12 (`send_run_report`) + Task 14 (`GET /runs/{id}/events`) |
| Sign-off to continue / clarify | Tasks 8, 10, 12 (gate → Slack → resume) |
| Background operation | Task 14 (`run_worker_forever`); periodic scheduling is S3 |
| Web console | Task 14 provides the API seam; the UI is S4 |
| Money and publication gated | Task 8 (MCP-only route) + Task 12 (`--allowedTools` whitelist) |
| Approvals never expire | Task 13 (escalation only, no deny path) |
| Usage-limit pause with reset parsing | Tasks 5 and 12 |
| No plaintext secrets | Task 7 (`resolve_secrets`) |
