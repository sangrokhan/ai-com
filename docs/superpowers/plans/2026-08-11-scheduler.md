# Scheduler (S3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the orchestrator recurring `schedule` definitions that fire on a cron expression and create tasks by themselves, so the system runs without a human creating every unit of work.

**Architecture:** A new `schedule` entity holds the recurrence; a pure `domain/cron.py` computes the next occurrence in the schedule's own timezone; a repository claims each firing with a conditional UPDATE on the observed clock value so two schedulers cannot double-fire; and a `Scheduler.tick()` in the existing worker loop creates a task plus its first queued run per firing. Missed slots are skipped by recomputing the clock from *now* rather than from the missed slot.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 typed ORM, Alembic, PostgreSQL 16, FastAPI, `croniter`, pytest + testcontainers.

**Spec:** `docs/superpowers/specs/2026-08-11-scheduler-design.md` — read §4 (firing semantics) before Task 4.

## Global Constraints

- Python 3.12; SQLAlchemy 2.0 typed ORM (`Mapped[...]` / `mapped_column`); PostgreSQL 16.
- Package root `src/aicom/`; tests mirror it under `tests/`. Line length 100.
- `ruff check src tests` must be clean repo-wide; `mypy --strict src/aicom/domain` must be clean.
- All timestamps are `TIMESTAMP WITH TIME ZONE` stored UTC. Never call `datetime.now()` without `tz=UTC`, and never inside `src/aicom/domain/` at all — pure functions take `now`/`after` as a parameter.
- **A firing is claimed with a conditional UPDATE whose affected-row count is checked.** Same discipline as `store/runs.py:transition()`. An unchecked claim double-fires.
- **`next_due_at` always advances from `now`, never from the missed slot** — on a firing AND on a skip. This is what makes "skip with a single catch-up" work without backlog bookkeeping.
- Every schedule is processed in its own try/commit; one failing schedule must never starve the others.
- Primary keys are application-side `uuid4`.
- Conventional Commits. `__pycache__`/`*.pyc` are gitignored; do not commit them.

## Parallel Execution Groups

Tasks within a group touch disjoint files and may be implemented concurrently. Groups are sequential.

| Group | Tasks | Why they are independent |
|-------|-------|--------------------------|
| A | 1, 2 | Task 1 is pure Python with no imports from the project; Task 2 is schema only |
| B | 3 | Needs Task 2's model |
| C | 4, 5 | Both consume Task 3; Task 4 writes `orchestrator/`, Task 5 writes `api/` |
| D | 6 | Wires and end-to-end tests everything above |

---

## File Structure

| Path | Responsibility |
|------|----------------|
| `src/aicom/domain/cron.py` | Pure next-occurrence computation and expression validation |
| `src/aicom/store/models.py` (modify) | `Schedule` model; `Task.schedule_id`; drop `Task.schedule` |
| `alembic/versions/0003_schedule.py` | Migration for the above |
| `src/aicom/store/schedules.py` | Due-schedule query, conditional-UPDATE firing claim, skip recording, unfinished-cycle check |
| `src/aicom/orchestrator/scheduler.py` | The tick: claim, decide fire-or-skip, create task + run |
| `src/aicom/api/routes.py` (modify) | Schedule CRUD endpoints |
| `src/aicom/main.py` (modify) | Call `scheduler.tick()` in the loop |
| `tests/domain/test_cron.py` | Calendar edge cases, no DB |
| `tests/store/test_schedules.py` | Claim exclusivity against real Postgres |
| `tests/orchestrator/test_scheduler.py` | Fire, skip, catch-up, broken cron, isolation |
| `tests/api/test_schedule_routes.py` | CRUD and cron validation at creation |
| `tests/integration/test_schedule_flow.py` | Schedule → task → run end to end |

---

### Task 1: Pure cron computation

**Files:**
- Create: `src/aicom/domain/cron.py`
- Modify: `pyproject.toml` (add the `croniter` dependency)
- Test: `tests/domain/test_cron.py`

**Interfaces:**
- Consumes: nothing from the project.
- Produces: `next_fire(cron: str, timezone: str, after: datetime) -> datetime` returning a UTC-aware datetime; `validate_cron(cron: str, timezone: str) -> None` raising `InvalidCron`; exception class `InvalidCron(Exception)`.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, add to `[project] dependencies`:

```toml
    "croniter>=2.0",
```

Then install it: `.venv/bin/pip install -e ".[dev]"`

- [ ] **Step 2: Write the failing test**

Create `tests/domain/test_cron.py`:

```python
from datetime import UTC, datetime

import pytest

from aicom.domain.cron import InvalidCron, next_fire, validate_cron


def test_next_fire_returns_utc_for_a_local_daily_schedule() -> None:
    # 09:00 in Seoul (UTC+9, no DST) is 00:00 UTC.
    after = datetime(2026, 8, 11, 3, 0, tzinfo=UTC)
    assert next_fire("0 9 * * *", "Asia/Seoul", after) == datetime(
        2026, 8, 12, 0, 0, tzinfo=UTC
    )


def test_next_fire_is_strictly_after_the_given_moment() -> None:
    exactly_on_the_hour = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
    result = next_fire("0 9 * * *", "Asia/Seoul", exactly_on_the_hour)
    assert result > exactly_on_the_hour


def test_next_fire_accepts_a_naive_datetime_as_utc() -> None:
    naive = datetime(2026, 8, 11, 3, 0)
    assert next_fire("0 9 * * *", "Asia/Seoul", naive) == datetime(
        2026, 8, 12, 0, 0, tzinfo=UTC
    )


def test_dst_spring_forward_keeps_local_wall_clock_time() -> None:
    # New York moves to EDT on 2026-03-08. 09:00 local is 14:00 UTC before
    # the change and 13:00 UTC after it.
    before = next_fire("0 9 * * *", "America/New_York", datetime(2026, 3, 6, 20, 0, tzinfo=UTC))
    after = next_fire("0 9 * * *", "America/New_York", datetime(2026, 3, 8, 20, 0, tzinfo=UTC))
    assert before == datetime(2026, 3, 7, 14, 0, tzinfo=UTC)
    assert after == datetime(2026, 3, 9, 13, 0, tzinfo=UTC)


def test_dst_fall_back_keeps_local_wall_clock_time() -> None:
    # New York returns to EST on 2026-11-01.
    after = next_fire("0 9 * * *", "America/New_York", datetime(2026, 11, 1, 20, 0, tzinfo=UTC))
    assert after == datetime(2026, 11, 2, 14, 0, tzinfo=UTC)


def test_weekday_only_schedule_skips_the_weekend() -> None:
    # 2026-08-14 is a Friday; the next weekday firing is Monday the 17th.
    friday_evening = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    assert next_fire("0 9 * * 1-5", "Asia/Seoul", friday_evening) == datetime(
        2026, 8, 17, 0, 0, tzinfo=UTC
    )


@pytest.mark.parametrize("expression", ["", "not a cron", "* * * *", "99 * * * *"])
def test_invalid_expression_raises(expression: str) -> None:
    with pytest.raises(InvalidCron):
        validate_cron(expression, "Asia/Seoul")
    with pytest.raises(InvalidCron):
        next_fire(expression, "Asia/Seoul", datetime(2026, 8, 11, tzinfo=UTC))


def test_invalid_timezone_raises() -> None:
    with pytest.raises(InvalidCron):
        validate_cron("0 9 * * *", "Mars/Olympus_Mons")


def test_validate_accepts_a_good_expression() -> None:
    assert validate_cron("*/15 * * * *", "UTC") is None
```

- [ ] **Step 3: Run the test and verify it fails**

Run: `.venv/bin/pytest tests/domain/test_cron.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.domain.cron'`

- [ ] **Step 4: Write `src/aicom/domain/cron.py`**

```python
"""Pure cron arithmetic.

Kept free of I/O and of clock reads so the calendar logic — the part most
likely to be subtly wrong — is testable without a database. The caller
always supplies the moment to compute from.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, croniter


class InvalidCron(Exception):
    """The expression or the timezone name cannot be used."""


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidCron(f"unknown timezone: {timezone!r}") from exc


def validate_cron(cron: str, timezone: str) -> None:
    """Raise InvalidCron if this pair could not be scheduled."""
    _zone(timezone)
    if not croniter.is_valid(cron):
        raise InvalidCron(f"invalid cron expression: {cron!r}")


def next_fire(cron: str, timezone: str, after: datetime) -> datetime:
    """The first occurrence strictly after `after`, returned in UTC.

    The expression is evaluated in the schedule's own timezone, so "09:00"
    stays 09:00 local across a DST change rather than drifting by an hour.
    """
    validate_cron(cron, timezone)
    moment = after if after.tzinfo is not None else after.replace(tzinfo=UTC)
    local = moment.astimezone(_zone(timezone))
    try:
        upcoming = croniter(cron, local).get_next(datetime)
    except CroniterBadCronError as exc:
        raise InvalidCron(f"invalid cron expression: {cron!r}") from exc
    return upcoming.astimezone(UTC)
```

- [ ] **Step 5: Run the tests and the type check**

Run: `.venv/bin/pytest tests/domain/test_cron.py -v && .venv/bin/mypy --strict src/aicom/domain && .venv/bin/ruff check src tests`
Expected: all pass. If mypy complains that `croniter` has no stubs, add to `pyproject.toml`:

```toml
[[tool.mypy.overrides]]
module = "croniter.*"
ignore_missing_imports = true
```

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/aicom/domain/cron.py tests/domain/test_cron.py
git commit -m "feat(domain): add pure cron next-occurrence computation"
```

---

### Task 2: Schedule schema and migration

**Files:**
- Modify: `src/aicom/store/models.py`
- Create: `alembic/versions/0003_schedule.py`
- Test: `tests/store/test_schedule_model.py`

**Interfaces:**
- Consumes: `Base`, `Agent`, `Task` from `src/aicom/store/models.py`.
- Produces: ORM class `Schedule` with fields `id`, `agent_id`, `name`, `enabled`, `cron`, `timezone`, `title_template`, `goal_template`, `next_due_at`, `last_fired_at`, `last_skipped_at`, `last_skip_reason`, `created_at`, and relationship `agent`. Adds `Task.schedule_id`. Removes `Task.schedule`.

- [ ] **Step 1: Write the failing test**

Create `tests/store/test_schedule_model.py`:

```python
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from aicom.store.models import Schedule, Task
from tests.store.test_models import make_agent


def test_schedule_roundtrip_with_defaults(session: Session) -> None:
    agent = make_agent(session)
    schedule = Schedule(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name="daily market scan",
        cron="0 9 * * 1-5",
        timezone="Asia/Seoul",
        title_template="Market scan",
        goal_template="Scan the market and report anything unusual.",
        next_due_at=datetime(2026, 8, 12, 0, 0, tzinfo=UTC),
    )
    session.add(schedule)
    session.flush()

    loaded = session.scalar(select(Schedule).where(Schedule.id == schedule.id))
    assert loaded is not None
    assert loaded.enabled is True
    assert loaded.last_fired_at is None
    assert loaded.last_skipped_at is None
    assert loaded.last_skip_reason is None
    assert loaded.agent.id == agent.id


def test_task_links_back_to_its_schedule(session: Session) -> None:
    agent = make_agent(session)
    schedule = Schedule(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name="hourly",
        cron="0 * * * *",
        timezone="UTC",
        title_template="t",
        goal_template="g",
        next_due_at=datetime(2026, 8, 11, tzinfo=UTC),
    )
    session.add(schedule)
    session.flush()

    task = Task(
        id=uuid.uuid4(), agent_id=agent.id, title="t", goal="g", schedule_id=schedule.id
    )
    session.add(task)
    session.flush()

    assert task.schedule_id == schedule.id


def test_manual_task_has_no_schedule_link(session: Session) -> None:
    agent = make_agent(session)
    task = Task(id=uuid.uuid4(), agent_id=agent.id, title="t", goal="g")
    session.add(task)
    session.flush()

    assert task.schedule_id is None


def test_old_schedule_string_column_is_gone(session: Session) -> None:
    # Recurrence now lives in its own entity; the reserved column must not
    # linger, or two places would claim to define the same thing.
    assert "schedule" not in Task.__table__.columns
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `.venv/bin/pytest tests/store/test_schedule_model.py -v`
Expected: FAIL with `ImportError: cannot import name 'Schedule' from 'aicom.store.models'`

- [ ] **Step 3: Add the `Schedule` model**

In `src/aicom/store/models.py`, add after the `Agent` class:

```python
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
```

- [ ] **Step 4: Change `Task`**

In `src/aicom/store/models.py`, delete these two lines from `Task`:

```python
    # column exists for S3; the S1 worker ignores it
    schedule: Mapped[str | None] = mapped_column(String(120), default=None)
```

and add in their place:

```python
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("schedule.id", ondelete="SET NULL"), default=None, index=True
    )
```

Deleting a schedule must never delete the history it produced, which is why the FK is
`ON DELETE SET NULL` rather than a cascade.

- [ ] **Step 5: Run the test and verify it passes**

Run: `.venv/bin/pytest tests/store/test_schedule_model.py -v`
Expected: PASS

- [ ] **Step 6: Generate and apply the migration**

```bash
.venv/bin/alembic revision --autogenerate -m "schedule entity"
.venv/bin/alembic upgrade head
```

Rename the generated file to `alembic/versions/0003_schedule.py`. Open it and verify by
reading that it creates the `schedule` table, adds `task.schedule_id` with
`ondelete="SET NULL"`, and drops `task.schedule`. Then check it reverses cleanly:

```bash
.venv/bin/alembic downgrade -1 && .venv/bin/alembic upgrade head
```

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/pytest -m "not smoke" -q && .venv/bin/ruff check src tests`
Expected: everything passes. Nothing reads `Task.schedule`, so removing it breaks nothing.

- [ ] **Step 8: Commit**

```bash
git add src/aicom/store/models.py alembic/versions tests/store/test_schedule_model.py
git commit -m "feat(store): add schedule entity and link tasks to it"
```

---

### Task 3: Schedule repository

**Files:**
- Create: `src/aicom/store/schedules.py`
- Test: `tests/store/test_schedules.py`

**Interfaces:**
- Consumes: `Schedule`, `Task`, `Run` from `store/models.py`; `TaskStatus`, `RunStatus` from `domain/enums.py`.
- Produces:
  - `due_schedules(session, *, now: datetime) -> list[Schedule]`
  - `claim_firing(session, schedule_id: UUID, observed_due_at: datetime, next_due_at: datetime, **fields) -> bool`
  - `has_unfinished_cycle(session, schedule_id: UUID) -> bool`
  - `record_skip(session, schedule_id: UUID, *, now: datetime, reason: str, next_due_at: datetime) -> bool`
  - `disable(session, schedule_id: UUID, *, reason: str, now: datetime) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/store/test_schedules.py`:

```python
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from aicom.domain.enums import RunStatus, TaskStatus
from aicom.store.models import Run, Schedule, Task
from aicom.store.schedules import (
    claim_firing,
    disable,
    due_schedules,
    has_unfinished_cycle,
    record_skip,
)
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _schedule(session: Session, *, due: datetime, enabled: bool = True) -> Schedule:
    agent = make_agent(session)
    schedule = Schedule(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name=f"s-{uuid.uuid4().hex[:6]}",
        cron="0 * * * *",
        timezone="UTC",
        title_template="t",
        goal_template="g",
        next_due_at=due,
        enabled=enabled,
    )
    session.add(schedule)
    session.commit()
    return schedule


def test_due_schedules_returns_only_enabled_and_due(session: Session) -> None:
    due = _schedule(session, due=NOW - timedelta(minutes=1))
    _schedule(session, due=NOW + timedelta(hours=1))
    _schedule(session, due=NOW - timedelta(hours=1), enabled=False)

    assert [s.id for s in due_schedules(session, now=NOW)] == [due.id]


def test_claim_firing_succeeds_once_for_a_given_clock_value(session: Session) -> None:
    schedule = _schedule(session, due=NOW - timedelta(minutes=1))
    observed = schedule.next_due_at
    later = NOW + timedelta(hours=1)

    assert claim_firing(
        session, schedule.id, observed, later, last_fired_at=NOW
    ) is True
    session.commit()

    # A second scheduler that observed the same value must lose.
    assert claim_firing(session, schedule.id, observed, later, last_fired_at=NOW) is False


def test_unfinished_cycle_detected_for_queued_run(session: Session) -> None:
    schedule = _schedule(session, due=NOW)
    task = Task(
        id=uuid.uuid4(),
        agent_id=schedule.agent_id,
        title="t",
        goal="g",
        schedule_id=schedule.id,
    )
    session.add(task)
    session.flush()
    session.add(Run(id=uuid.uuid4(), task_id=task.id, attempt=1))
    session.commit()

    assert has_unfinished_cycle(session, schedule.id) is True


def test_finished_cycle_is_not_unfinished(session: Session) -> None:
    schedule = _schedule(session, due=NOW)
    task = Task(
        id=uuid.uuid4(),
        agent_id=schedule.agent_id,
        title="t",
        goal="g",
        schedule_id=schedule.id,
        status=TaskStatus.DONE,
    )
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1, status=RunStatus.SUCCEEDED)
    session.add(run)
    session.commit()

    assert has_unfinished_cycle(session, schedule.id) is False


def test_a_schedule_with_no_tasks_has_no_unfinished_cycle(session: Session) -> None:
    schedule = _schedule(session, due=NOW)
    assert has_unfinished_cycle(session, schedule.id) is False


def test_record_skip_advances_the_clock_and_stores_the_reason(session: Session) -> None:
    schedule = _schedule(session, due=NOW - timedelta(minutes=1))
    observed = schedule.next_due_at
    later = NOW + timedelta(hours=1)

    assert record_skip(
        session, schedule.id, now=NOW, reason="previous cycle unfinished", next_due_at=later
    ) is True
    session.commit()
    session.refresh(schedule)

    assert schedule.next_due_at == later
    assert schedule.last_skipped_at == NOW
    assert schedule.last_skip_reason == "previous cycle unfinished"
    assert schedule.last_fired_at is None
    # The observed value is gone, so a racing caller cannot also claim it.
    assert claim_firing(session, schedule.id, observed, later) is False


def test_disable_records_the_reason_and_stops_it_being_due(session: Session) -> None:
    schedule = _schedule(session, due=NOW - timedelta(minutes=1))

    assert disable(session, schedule.id, reason="invalid cron", now=NOW) is True
    session.commit()

    assert due_schedules(session, now=NOW) == []
    session.refresh(schedule)
    assert schedule.enabled is False
    assert schedule.last_skip_reason == "invalid cron"
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `.venv/bin/pytest tests/store/test_schedules.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.store.schedules'`

- [ ] **Step 3: Write `src/aicom/store/schedules.py`**

```python
import uuid
from datetime import datetime

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from aicom.domain.enums import RunStatus, TaskStatus
from aicom.store.models import Run, Schedule, Task

# A cycle is still in flight while its task is open or blocked, or while any
# of its runs has not reached a terminal state.
_LIVE_TASK_STATUSES = (TaskStatus.OPEN, TaskStatus.BLOCKED)
_LIVE_RUN_STATUSES = (
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.AWAITING_APPROVAL,
)


def due_schedules(session: Session, *, now: datetime) -> list[Schedule]:
    stmt = (
        select(Schedule)
        .where(Schedule.enabled.is_(True), Schedule.next_due_at <= now)
        .order_by(Schedule.next_due_at)
    )
    return list(session.scalars(stmt))


def claim_firing(
    session: Session,
    schedule_id: uuid.UUID,
    observed_due_at: datetime,
    next_due_at: datetime,
    **fields: object,
) -> bool:
    """Take ownership of one firing.

    The clock value the caller observed is part of the WHERE clause, so only
    one scheduler can win a given slot. Callers MUST check the result before
    creating a task; treating an unclaimed firing as claimed double-fires.
    """
    result = session.execute(
        update(Schedule)
        .where(Schedule.id == schedule_id, Schedule.next_due_at == observed_due_at)
        .values(next_due_at=next_due_at, **fields)
    )
    return bool(result.rowcount)


def has_unfinished_cycle(session: Session, schedule_id: uuid.UUID) -> bool:
    live_task = exists().where(
        Task.schedule_id == schedule_id, Task.status.in_(_LIVE_TASK_STATUSES)
    )
    live_run = exists().where(
        Task.schedule_id == schedule_id,
        Run.task_id == Task.id,
        Run.status.in_(_LIVE_RUN_STATUSES),
    )
    return bool(session.scalar(select(live_task | live_run)))


def record_skip(
    session: Session,
    schedule_id: uuid.UUID,
    *,
    now: datetime,
    reason: str,
    next_due_at: datetime,
) -> bool:
    """Advance the clock without firing. The clock moves on a skip too, so a
    blocked schedule does not accumulate overdue slots to work through."""
    result = session.execute(
        update(Schedule)
        .where(Schedule.id == schedule_id)
        .values(next_due_at=next_due_at, last_skipped_at=now, last_skip_reason=reason)
    )
    return bool(result.rowcount)


def disable(
    session: Session, schedule_id: uuid.UUID, *, reason: str, now: datetime
) -> bool:
    result = session.execute(
        update(Schedule)
        .where(Schedule.id == schedule_id)
        .values(enabled=False, last_skipped_at=now, last_skip_reason=reason)
    )
    return bool(result.rowcount)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/store/test_schedules.py -v && .venv/bin/ruff check src tests`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/aicom/store/schedules.py tests/store/test_schedules.py
git commit -m "feat(store): add schedule repository with conditional firing claim"
```

---

### Task 4: The scheduler tick

**Files:**
- Create: `src/aicom/orchestrator/scheduler.py`
- Test: `tests/orchestrator/test_scheduler.py`

**Interfaces:**
- Consumes: `next_fire`, `InvalidCron` (Task 1); `due_schedules`, `claim_firing`, `has_unfinished_cycle`, `record_skip`, `disable` (Task 3); `Schedule`, `Task`, `Run` from `store/models.py`; `Notifier` from `notify/base.py`.
- Produces: `Scheduler(sessions: sessionmaker[Session], notifier: Notifier)` with `tick(now: datetime) -> int` returning the number of tasks created.

Read §4 of `docs/superpowers/specs/2026-08-11-scheduler-design.md` before starting.

- [ ] **Step 1: Write the failing test**

Create `tests/orchestrator/test_scheduler.py`:

```python
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aicom.domain.enums import RunStatus
from aicom.notify.fake import FakeNotifier
from aicom.orchestrator.scheduler import Scheduler
from aicom.store.models import Run, Schedule, Task
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _schedule(
    session: Session,
    *,
    due: datetime,
    cron: str = "0 * * * *",
    enabled: bool = True,
) -> Schedule:
    agent = make_agent(session)
    schedule = Schedule(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name=f"s-{uuid.uuid4().hex[:6]}",
        cron=cron,
        timezone="UTC",
        title_template="Market scan",
        goal_template="Scan the market.",
        next_due_at=due,
        enabled=enabled,
    )
    session.add(schedule)
    session.commit()
    return schedule


def test_due_schedule_creates_one_task_and_one_queued_run(
    sessions: sessionmaker[Session], session: Session
) -> None:
    schedule = _schedule(session, due=NOW - timedelta(minutes=1))

    created = Scheduler(sessions, FakeNotifier()).tick(NOW)
    assert created == 1

    session.expire_all()
    tasks = list(session.scalars(select(Task).where(Task.schedule_id == schedule.id)))
    assert len(tasks) == 1
    assert tasks[0].title == "Market scan"
    assert tasks[0].goal == "Scan the market."
    assert tasks[0].agent_id == schedule.agent_id

    runs = list(session.scalars(select(Run).where(Run.task_id == tasks[0].id)))
    assert len(runs) == 1
    assert runs[0].status is RunStatus.QUEUED

    refreshed = session.get(Schedule, schedule.id)
    assert refreshed is not None
    assert refreshed.last_fired_at == NOW
    assert refreshed.next_due_at > NOW


def test_a_schedule_not_yet_due_does_not_fire(
    sessions: sessionmaker[Session], session: Session
) -> None:
    _schedule(session, due=NOW + timedelta(hours=1))
    assert Scheduler(sessions, FakeNotifier()).tick(NOW) == 0


def test_unfinished_previous_cycle_is_skipped_with_a_reason(
    sessions: sessionmaker[Session], session: Session
) -> None:
    schedule = _schedule(session, due=NOW - timedelta(minutes=1))
    scheduler = Scheduler(sessions, FakeNotifier())
    assert scheduler.tick(NOW) == 1

    # The first cycle's run is still queued, so the next firing must skip.
    session.expire_all()
    refreshed = session.get(Schedule, schedule.id)
    assert refreshed is not None
    later = refreshed.next_due_at + timedelta(seconds=1)

    assert scheduler.tick(later) == 0

    session.expire_all()
    refreshed = session.get(Schedule, schedule.id)
    assert refreshed is not None
    assert refreshed.last_skip_reason == "previous cycle unfinished"
    assert refreshed.last_skipped_at == later
    assert refreshed.next_due_at > later
    assert len(list(session.scalars(select(Task).where(Task.schedule_id == schedule.id)))) == 1


def test_a_long_overdue_schedule_fires_once_and_rejoins_the_normal_cadence(
    sessions: sessionmaker[Session], session: Session
) -> None:
    # Six hours overdue on an hourly schedule: one firing, not six.
    schedule = _schedule(session, due=NOW - timedelta(hours=6))

    assert Scheduler(sessions, FakeNotifier()).tick(NOW) == 1

    session.expire_all()
    tasks = list(session.scalars(select(Task).where(Task.schedule_id == schedule.id)))
    assert len(tasks) == 1

    refreshed = session.get(Schedule, schedule.id)
    assert refreshed is not None
    assert refreshed.next_due_at == datetime(2026, 8, 11, 13, 0, tzinfo=UTC)


def test_broken_cron_disables_the_schedule_and_notifies(
    sessions: sessionmaker[Session], session: Session
) -> None:
    schedule = _schedule(session, due=NOW - timedelta(minutes=1), cron="not a cron")
    notifier = FakeNotifier()

    assert Scheduler(sessions, notifier).tick(NOW) == 0

    session.expire_all()
    refreshed = session.get(Schedule, schedule.id)
    assert refreshed is not None
    assert refreshed.enabled is False
    assert len(notifier.notices) == 1
    assert schedule.name in notifier.notices[0]


def test_a_disabled_agent_skips_without_disabling_the_schedule(
    sessions: sessionmaker[Session], session: Session
) -> None:
    schedule = _schedule(session, due=NOW - timedelta(minutes=1))
    schedule.agent.enabled = False
    session.commit()

    assert Scheduler(sessions, FakeNotifier()).tick(NOW) == 0

    session.expire_all()
    refreshed = session.get(Schedule, schedule.id)
    assert refreshed is not None
    assert refreshed.enabled is True
    assert refreshed.last_skip_reason == "agent disabled"


def test_one_broken_schedule_does_not_stop_the_others(
    sessions: sessionmaker[Session], session: Session
) -> None:
    _schedule(session, due=NOW - timedelta(minutes=1), cron="not a cron")
    good = _schedule(session, due=NOW - timedelta(minutes=1))

    assert Scheduler(sessions, FakeNotifier()).tick(NOW) == 1

    session.expire_all()
    assert len(list(session.scalars(select(Task).where(Task.schedule_id == good.id)))) == 1
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `.venv/bin/pytest tests/orchestrator/test_scheduler.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.orchestrator.scheduler'`

- [ ] **Step 3: Write `src/aicom/orchestrator/scheduler.py`**

```python
import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from aicom.domain.cron import InvalidCron, next_fire
from aicom.notify.base import Notifier
from aicom.store.models import Run, Schedule, Task
from aicom.store.schedules import (
    claim_firing,
    disable,
    due_schedules,
    has_unfinished_cycle,
    record_skip,
)

logger = logging.getLogger(__name__)


class Scheduler:
    """Creates tasks from recurring schedules.

    Runs alongside the worker and the sweeper in the same loop.
    """

    def __init__(self, sessions: sessionmaker[Session], notifier: Notifier) -> None:
        self._sessions = sessions
        self._notifier = notifier

    def tick(self, now: datetime) -> int:
        """Fire every schedule that is due. Returns the number of tasks created."""
        created = 0
        with self._sessions() as session:
            for schedule in due_schedules(session, now=now):
                try:
                    if self._process(session, schedule, now):
                        created += 1
                    session.commit()
                except Exception:
                    # One schedule must never starve the rest.
                    session.rollback()
                    logger.exception("schedule %s failed to process", schedule.id)
        return created

    def _process(self, session: Session, schedule: Schedule, now: datetime) -> bool:
        observed = schedule.next_due_at
        try:
            upcoming = next_fire(schedule.cron, schedule.timezone, now)
        except InvalidCron as exc:
            disable(session, schedule.id, reason=str(exc), now=now)
            self._notifier.send_system_notice(
                f"Schedule '{schedule.name}' disabled: {exc}"
            )
            return False

        if not schedule.agent.enabled:
            record_skip(
                session,
                schedule.id,
                now=now,
                reason="agent disabled",
                next_due_at=upcoming,
            )
            return False

        if has_unfinished_cycle(session, schedule.id):
            record_skip(
                session,
                schedule.id,
                now=now,
                reason="previous cycle unfinished",
                next_due_at=upcoming,
            )
            return False

        if not claim_firing(
            session, schedule.id, observed, upcoming, last_fired_at=now
        ):
            # Another scheduler took this slot.
            return False

        task = Task(
            id=uuid.uuid4(),
            agent_id=schedule.agent_id,
            title=schedule.title_template,
            goal=schedule.goal_template,
            schedule_id=schedule.id,
        )
        session.add(task)
        session.flush()
        session.add(Run(id=uuid.uuid4(), task_id=task.id, attempt=1))
        return True
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/orchestrator/test_scheduler.py -v && .venv/bin/ruff check src tests`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/aicom/orchestrator/scheduler.py tests/orchestrator/test_scheduler.py
git commit -m "feat(orchestrator): add scheduler tick creating tasks from schedules"
```

---

### Task 5: Schedule REST endpoints

**Files:**
- Modify: `src/aicom/api/routes.py`
- Test: `tests/api/test_schedule_routes.py`

**Interfaces:**
- Consumes: `validate_cron`, `InvalidCron`, `next_fire` (Task 1); `Schedule` (Task 2); `make_router(sessions)` in `src/aicom/api/routes.py`.
- Produces: `POST /schedules`, `GET /schedules`, `PATCH /schedules/{schedule_id}`, `DELETE /schedules/{schedule_id}`; request models `CreateSchedule` and `UpdateSchedule`.

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_schedule_routes.py`:

```python
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from aicom.api.routes import make_router
from aicom.store.models import Schedule
from tests.store.test_models import make_agent


def _client(sessions: sessionmaker[Session]) -> TestClient:
    app = FastAPI()
    app.include_router(make_router(sessions))
    return TestClient(app)


def _body(agent_id: uuid.UUID, **over: object) -> dict:
    body = {
        "agent_id": str(agent_id),
        "name": "daily market scan",
        "cron": "0 9 * * 1-5",
        "timezone": "Asia/Seoul",
        "title_template": "Market scan",
        "goal_template": "Scan the market.",
    }
    body.update(over)
    return body


def test_create_schedule_sets_the_first_due_time(
    sessions: sessionmaker[Session], session: Session
) -> None:
    agent = make_agent(session)
    session.commit()

    response = _client(sessions).post("/schedules", json=_body(agent.id))
    assert response.status_code == 201

    session.expire_all()
    schedule = session.get(Schedule, uuid.UUID(response.json()["id"]))
    assert schedule is not None
    assert schedule.enabled is True
    assert schedule.next_due_at is not None


def test_invalid_cron_is_rejected_at_creation(
    sessions: sessionmaker[Session], session: Session
) -> None:
    agent = make_agent(session)
    session.commit()

    response = _client(sessions).post("/schedules", json=_body(agent.id, cron="nope"))
    assert response.status_code == 400
    assert "cron" in response.json()["detail"].lower()

    session.expire_all()
    assert session.query(Schedule).count() == 0


def test_invalid_timezone_is_rejected_at_creation(
    sessions: sessionmaker[Session], session: Session
) -> None:
    agent = make_agent(session)
    session.commit()

    response = _client(sessions).post(
        "/schedules", json=_body(agent.id, timezone="Mars/Olympus_Mons")
    )
    assert response.status_code == 400


def test_unknown_or_disabled_agent_is_rejected(
    sessions: sessionmaker[Session], session: Session
) -> None:
    client = _client(sessions)
    assert client.post("/schedules", json=_body(uuid.uuid4())).status_code == 404

    agent = make_agent(session)
    agent.enabled = False
    session.commit()
    assert client.post("/schedules", json=_body(agent.id)).status_code == 400


def test_list_schedules(sessions: sessionmaker[Session], session: Session) -> None:
    agent = make_agent(session)
    session.commit()
    client = _client(sessions)
    client.post("/schedules", json=_body(agent.id))

    rows = client.get("/schedules").json()
    assert len(rows) == 1
    assert rows[0]["name"] == "daily market scan"
    assert rows[0]["enabled"] is True
    assert rows[0]["next_due_at"] is not None


def test_patch_can_disable_and_change_cron(
    sessions: sessionmaker[Session], session: Session
) -> None:
    agent = make_agent(session)
    session.commit()
    client = _client(sessions)
    schedule_id = client.post("/schedules", json=_body(agent.id)).json()["id"]

    session.expire_all()
    before = session.get(Schedule, uuid.UUID(schedule_id))
    assert before is not None
    original_due = before.next_due_at

    assert client.patch(f"/schedules/{schedule_id}", json={"enabled": False}).status_code == 200
    assert (
        client.patch(f"/schedules/{schedule_id}", json={"cron": "*/5 * * * *"}).status_code
        == 200
    )

    session.expire_all()
    after = session.get(Schedule, uuid.UUID(schedule_id))
    assert after is not None
    assert after.enabled is False
    assert after.cron == "*/5 * * * *"
    assert after.next_due_at != original_due


def test_patch_rejects_an_invalid_cron(
    sessions: sessionmaker[Session], session: Session
) -> None:
    agent = make_agent(session)
    session.commit()
    client = _client(sessions)
    schedule_id = client.post("/schedules", json=_body(agent.id)).json()["id"]

    assert client.patch(f"/schedules/{schedule_id}", json={"cron": "nope"}).status_code == 400


def test_delete_removes_the_schedule(
    sessions: sessionmaker[Session], session: Session
) -> None:
    agent = make_agent(session)
    session.commit()
    client = _client(sessions)
    schedule_id = client.post("/schedules", json=_body(agent.id)).json()["id"]

    assert client.delete(f"/schedules/{schedule_id}").status_code == 204
    assert client.delete(f"/schedules/{schedule_id}").status_code == 404

    session.expire_all()
    assert session.query(Schedule).count() == 0
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `.venv/bin/pytest tests/api/test_schedule_routes.py -v`
Expected: FAIL with 404s, because the routes do not exist yet.

- [ ] **Step 3: Add the request models**

In `src/aicom/api/routes.py`, next to `CreateTask`:

```python
class CreateSchedule(BaseModel):
    agent_id: uuid.UUID
    name: str
    cron: str
    timezone: str = "UTC"
    title_template: str
    goal_template: str


class UpdateSchedule(BaseModel):
    enabled: bool | None = None
    cron: str | None = None
    timezone: str | None = None
    title_template: str | None = None
    goal_template: str | None = None
```

Add these imports at the top of the file:

```python
from datetime import UTC, datetime

from aicom.domain.cron import InvalidCron, next_fire, validate_cron
from aicom.store.models import Schedule
```

- [ ] **Step 4: Add the routes**

In `src/aicom/api/routes.py`, inside `make_router`, before `return router`:

```python
    @router.post("/schedules", status_code=201)
    def create_schedule(body: CreateSchedule) -> dict:
        try:
            validate_cron(body.cron, body.timezone)
        except InvalidCron as exc:
            raise HTTPException(400, str(exc)) from exc
        with sessions() as session:
            agent = session.get(Agent, body.agent_id)
            if agent is None:
                raise HTTPException(404, "agent not found")
            if not agent.enabled:
                raise HTTPException(400, "agent is disabled")
            now = datetime.now(UTC)
            schedule = Schedule(
                id=uuid.uuid4(),
                agent_id=body.agent_id,
                name=body.name,
                cron=body.cron,
                timezone=body.timezone,
                title_template=body.title_template,
                goal_template=body.goal_template,
                next_due_at=next_fire(body.cron, body.timezone, now),
            )
            session.add(schedule)
            session.commit()
            return {"id": str(schedule.id)}

    @router.get("/schedules")
    def list_schedules() -> list[dict]:
        with sessions() as session:
            rows = session.scalars(select(Schedule).order_by(Schedule.created_at.desc()))
            return [
                {
                    "id": str(s.id),
                    "name": s.name,
                    "agent": s.agent.name,
                    "cron": s.cron,
                    "timezone": s.timezone,
                    "enabled": s.enabled,
                    "next_due_at": s.next_due_at.isoformat(),
                    "last_fired_at": s.last_fired_at.isoformat() if s.last_fired_at else None,
                    "last_skip_reason": s.last_skip_reason,
                }
                for s in rows
            ]

    @router.patch("/schedules/{schedule_id}")
    def update_schedule(schedule_id: uuid.UUID, body: UpdateSchedule) -> dict:
        with sessions() as session:
            schedule = session.get(Schedule, schedule_id)
            if schedule is None:
                raise HTTPException(404, "schedule not found")

            cron = body.cron if body.cron is not None else schedule.cron
            timezone = body.timezone if body.timezone is not None else schedule.timezone
            if body.cron is not None or body.timezone is not None:
                try:
                    validate_cron(cron, timezone)
                except InvalidCron as exc:
                    raise HTTPException(400, str(exc)) from exc
                schedule.cron = cron
                schedule.timezone = timezone
                # A changed expression takes effect from now, not from the
                # old clock, which may belong to a cadence that no longer exists.
                schedule.next_due_at = next_fire(cron, timezone, datetime.now(UTC))

            if body.enabled is not None:
                schedule.enabled = body.enabled
            if body.title_template is not None:
                schedule.title_template = body.title_template
            if body.goal_template is not None:
                schedule.goal_template = body.goal_template
            session.commit()
            return {"id": str(schedule.id)}

    @router.delete("/schedules/{schedule_id}", status_code=204)
    def delete_schedule(schedule_id: uuid.UUID) -> None:
        with sessions() as session:
            schedule = session.get(Schedule, schedule_id)
            if schedule is None:
                raise HTTPException(404, "schedule not found")
            # task.schedule_id is ON DELETE SET NULL: the tasks this schedule
            # created keep their runs, events, and artifacts.
            session.delete(schedule)
            session.commit()
```

If `Agent` is not already imported in this file, add it to the existing
`from aicom.store.models import ...` line.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest tests/api -v && .venv/bin/ruff check src tests`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/aicom/api/routes.py tests/api/test_schedule_routes.py
git commit -m "feat(api): add schedule CRUD endpoints"
```

---

### Task 6: Wire the scheduler into the service and prove it end to end

**Files:**
- Modify: `src/aicom/main.py`
- Modify: `README.md`
- Modify: `src/aicom/orchestrator/AGENTS.md`, `src/aicom/api/AGENTS.md`, `src/aicom/store/AGENTS.md`, `src/aicom/domain/AGENTS.md`, root `AGENTS.md`
- Test: `tests/integration/test_schedule_flow.py`

**Interfaces:**
- Consumes: `Scheduler` (Task 4); the schedule endpoints (Task 5); `Worker`, `Sweeper`, `FakeExecutor`, `FakeNotifier`.
- Produces: no new public interfaces.

- [ ] **Step 1: Write the failing integration test**

Create `tests/integration/test_schedule_flow.py`:

```python
import json
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aicom.api.routes import make_router
from aicom.config import Settings
from aicom.domain.enums import RunStatus, TaskStatus
from aicom.executor.fake import FakeExecutor
from aicom.notify.fake import FakeNotifier
from aicom.orchestrator.scheduler import Scheduler
from aicom.orchestrator.worker import Worker
from aicom.store.models import Run, Schedule, Task
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    repo = tmp_path / "artifacts"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    return Settings(
        workspace_root=tmp_path / "ws",
        artifact_repo_path=repo,
        database_url="postgresql+psycopg://unused/unused",
    )


def test_schedule_creates_a_task_the_worker_then_executes(
    sessions: sessionmaker[Session], session: Session, tmp_path: Path
) -> None:
    agent = make_agent(session)
    session.commit()

    app = FastAPI()
    app.include_router(make_router(sessions))
    client = TestClient(app)
    created = client.post(
        "/schedules",
        json={
            "agent_id": str(agent.id),
            "name": "hourly scan",
            "cron": "0 * * * *",
            "timezone": "UTC",
            "title_template": "Hourly scan",
            "goal_template": "Scan and report.",
        },
    )
    assert created.status_code == 201
    schedule_id = uuid.UUID(created.json()["id"])

    # Make it due.
    session.expire_all()
    schedule = session.get(Schedule, schedule_id)
    assert schedule is not None
    schedule.next_due_at = NOW - timedelta(minutes=1)
    session.commit()

    notifier = FakeNotifier()
    assert Scheduler(sessions, notifier).tick(NOW) == 1

    session.expire_all()
    task = session.scalar(select(Task).where(Task.schedule_id == schedule_id))
    assert task is not None
    run = session.scalar(select(Run).where(Run.task_id == task.id))
    assert run is not None
    assert run.status is RunStatus.QUEUED

    executor = FakeExecutor()
    executor.queue(run.id, [json.dumps({"type": "result"})])
    worker = Worker(sessions, executor, notifier, _settings(tmp_path), worker_id="w1")
    assert worker.tick(NOW) is True

    session.expire_all()
    finished = session.get(Run, run.id)
    assert finished is not None
    assert finished.status is RunStatus.SUCCEEDED
    assert finished.task.status is TaskStatus.DONE

    # The cycle is finished, so the next due firing is no longer skipped.
    refreshed = session.get(Schedule, schedule_id)
    assert refreshed is not None
    assert Scheduler(sessions, notifier).tick(refreshed.next_due_at + timedelta(seconds=1)) == 1
    session.expire_all()
    assert len(list(session.scalars(select(Task).where(Task.schedule_id == schedule_id)))) == 2
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `.venv/bin/pytest tests/integration/test_schedule_flow.py -v`
Expected: FAIL — the schedule endpoints and `Scheduler` exist by now, so this should pass
once Tasks 1-5 are merged. If it fails for any other reason, fix the cause rather than the
assertion.

- [ ] **Step 3: Wire the scheduler into `main.py`**

In `src/aicom/main.py`, add the import:

```python
from aicom.orchestrator.scheduler import Scheduler
```

Construct it next to the sweeper:

```python
    scheduler = Scheduler(sessions, notifier)
```

And call it inside the loop's `try`, before the worker tick:

```python
            scheduler.tick(now)
```

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/pytest -m "not smoke" -q && .venv/bin/ruff check src tests && .venv/bin/mypy --strict src/aicom/domain`
Expected: all clean.

- [ ] **Step 5: Update the documentation**

Make these edits, each one sentence to a short paragraph — do not restructure the files:

- `src/aicom/domain/AGENTS.md` — add `cron.py` to Key Files and its two functions to Public Interface.
- `src/aicom/store/AGENTS.md` — add `schedules.py` to Key Files and its functions to Public Interface; note that `claim_firing`'s result must be checked, for the same reason `transition`'s must be.
- `src/aicom/orchestrator/AGENTS.md` — add `scheduler.py` and the firing rules: the clock advances from `now` on both a firing and a skip, and one pending task per schedule is the intended maximum.
- `src/aicom/api/AGENTS.md` — add the four schedule endpoints.
- Root `AGENTS.md` — move S3 from "Not started" to "Shipped" in the roadmap table, and remove the sentence saying `task.schedule` exists and is ignored.
- `README.md` — add a short "Schedules" subsection showing a `curl` that creates one.

- [ ] **Step 6: Commit**

```bash
git add src/aicom/main.py README.md tests/integration/test_schedule_flow.py \
        AGENTS.md src/aicom/domain/AGENTS.md src/aicom/store/AGENTS.md \
        src/aicom/orchestrator/AGENTS.md src/aicom/api/AGENTS.md
git commit -m "feat: run the scheduler in the service loop and document it"
```

---

## Post-plan verification

After Task 6, confirm each spec section maps to shipped code:

| Spec section | Where |
|---|---|
| §3.1 `schedule` entity | Task 2 |
| §3.2 `task.schedule_id`, drop `task.schedule` | Task 2 |
| §3.3 timezone handling | Task 1 (`next_fire` evaluates in local time, stores UTC) |
| §4.1 the tick | Task 4 |
| §4.2 clock advances from now | Task 3 (`record_skip`, `claim_firing`) + Task 4 |
| §4.3 no pause check needed | Task 4 — proven by the unfinished-cycle skip test |
| §4.4 concurrency | Task 3 `claim_firing` + its exclusivity test |
| §5 components | Tasks 1, 3, 4, 5, 6 |
| §6 HTTP API | Task 5 |
| §7 error handling | Task 4 (broken cron, disabled agent, per-schedule isolation) |
| §8 testing strategy | Tasks 1, 3, 4, 5, 6 |
