# Opportunity Research Pack (S5a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the first agent pack — an agent that watches a defined beat on a schedule and reports only what is new, changed, or gone.

**Architecture:** A pack is data plus thin wiring: one `agent` row and its `schedule` rows, installed by a re-runnable seed command. The one production change is memory — before spawning a scheduled run, the worker copies that schedule's most recent reports into a `previous/` directory inside the run's own workspace, so the agent can see what it already said without anyone widening its filesystem access.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0, PostgreSQL 16, pytest + testcontainers.

**Spec:** `docs/superpowers/specs/2026-08-13-opportunity-pack-design.md` — read §4 (memory) before Task 2.

## Global Constraints

- Python 3.12; SQLAlchemy 2.0 typed ORM. Line length 100; `ruff check src tests` clean repo-wide; `mypy --strict src/aicom/domain` clean.
- Timestamps timezone-aware UTC. Repository functions take an explicit `Session` first and never open or commit one.
- **This pack reaches no gated action.** Its agent's `gated_tools` is empty and its `allowed_tools` contains no `Bash`. Nothing in this plan may add a route to spending, publishing, or contacting anyone.
- **Do not widen filesystem access.** `--add-dir` stays confined to the run's own workspace; memory works by staging files into that workspace, never by granting access to the artifact repository.
- **Staging failures must not fail the run.** A missing memory makes the agent repeat itself; a failed run produces nothing. Log and continue.
- The seed command is idempotent — running it twice leaves one agent and one schedule per definition.
- Conventional Commits. `__pycache__`/`*.pyc` gitignored.

## Parallel Execution Groups

| Group | Tasks | Why |
|-------|-------|-----|
| A | 1, 3 | Task 1 writes `store/`, Task 3 writes `packs/`; disjoint files |
| B | 2 | Needs Task 1's query |
| C | 4 | Needs everything |

---

## File Structure

| Path | Responsibility |
|------|----------------|
| `src/aicom/store/artifacts_query.py` | Finds a schedule's most recent report artifacts |
| `src/aicom/orchestrator/staging.py` | Copies those reports into a run's workspace |
| `src/aicom/orchestrator/worker.py` (modify) | Calls the staging step when a run has a schedule |
| `src/aicom/packs/__init__.py` | `PACKS` registry and the definition dataclasses |
| `src/aicom/packs/opportunity.py` | This pack's agent and schedules, as data |
| `src/aicom/packs/seed.py` | `python -m aicom.packs.seed <pack>` |

---

### Task 1: Find a schedule's recent reports

**Files:**
- Create: `src/aicom/store/artifacts_query.py`
- Test: `tests/store/test_artifacts_query.py`

**Interfaces:**
- Consumes: `Artifact`, `Run`, `Task`, `Schedule` from `src/aicom/store/models.py`.
- Produces: `REPORT_FILENAME: str = "report.md"`; `recent_schedule_reports(session: Session, schedule_id: uuid.UUID, *, limit: int) -> list[tuple[uuid.UUID, str]]` returning `(run_id, path)` pairs, newest run first.

Ordering note: `Artifact` has no timestamp column, and `Run.ended_at` is null when a worker
crashed. Order by `Run.started_at`, which is written when a run is claimed, so every run
that ever executed has one. This exact trap has already bitten this codebase once.

- [ ] **Step 1: Write the failing test**

Create `tests/store/test_artifacts_query.py`:

```python
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from aicom.store.artifacts_query import REPORT_FILENAME, recent_schedule_reports
from aicom.store.models import Artifact, Run, Schedule, Task
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def _schedule(session: Session, name: str = "beat") -> Schedule:
    agent = make_agent(session)
    schedule = Schedule(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name=f"{name}-{uuid.uuid4().hex[:6]}",
        cron="0 9 * * *",
        timezone="UTC",
        title_template="t",
        goal_template="g",
        next_due_at=NOW,
    )
    session.add(schedule)
    session.flush()
    return schedule


def _run_with_report(
    session: Session,
    schedule: Schedule,
    *,
    started_at: datetime | None,
    path: str = REPORT_FILENAME,
) -> Run:
    task = Task(
        id=uuid.uuid4(),
        agent_id=schedule.agent_id,
        title="t",
        goal="g",
        schedule_id=schedule.id,
    )
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1, started_at=started_at)
    session.add(run)
    session.flush()
    session.add(
        Artifact(id=uuid.uuid4(), run_id=run.id, kind="md", path=path, git_ref="abc")
    )
    session.flush()
    return run


def test_returns_reports_newest_first(session: Session) -> None:
    schedule = _schedule(session)
    older = _run_with_report(session, schedule, started_at=NOW - timedelta(days=2))
    newer = _run_with_report(session, schedule, started_at=NOW - timedelta(days=1))
    session.commit()

    found = recent_schedule_reports(session, schedule.id, limit=10)

    assert [run_id for run_id, _ in found] == [newer.id, older.id]
    assert all(path == REPORT_FILENAME for _, path in found)


def test_limit_is_respected(session: Session) -> None:
    schedule = _schedule(session)
    for day in range(5):
        _run_with_report(session, schedule, started_at=NOW - timedelta(days=day))
    session.commit()

    assert len(recent_schedule_reports(session, schedule.id, limit=2)) == 2


def test_another_schedules_reports_are_never_returned(session: Session) -> None:
    mine = _schedule(session, "mine")
    theirs = _schedule(session, "theirs")
    _run_with_report(session, theirs, started_at=NOW)
    session.commit()

    assert recent_schedule_reports(session, mine.id, limit=10) == []


def test_non_report_artifacts_are_ignored(session: Session) -> None:
    schedule = _schedule(session)
    _run_with_report(session, schedule, started_at=NOW, path="data.csv")
    session.commit()

    assert recent_schedule_reports(session, schedule.id, limit=10) == []


def test_a_run_that_never_started_sorts_last(session: Session) -> None:
    # ended_at would be null for a crashed worker; started_at is written on
    # claim, so only a run that never ran at all has none.
    schedule = _schedule(session)
    started = _run_with_report(session, schedule, started_at=NOW - timedelta(days=1))
    _run_with_report(session, schedule, started_at=None)
    session.commit()

    assert recent_schedule_reports(session, schedule.id, limit=1)[0][0] == started.id


def test_a_schedule_with_no_reports_returns_nothing(session: Session) -> None:
    schedule = _schedule(session)
    session.commit()

    assert recent_schedule_reports(session, schedule.id, limit=10) == []
```

- [ ] **Step 2: Run it and verify it fails**

Run: `.venv/bin/pytest tests/store/test_artifacts_query.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.store.artifacts_query'`

- [ ] **Step 3: Write `src/aicom/store/artifacts_query.py`**

```python
"""Finds the reports a schedule has already produced.

Used to give a monitoring agent memory of what it has already said.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from aicom.store.models import Artifact, Run, Task

REPORT_FILENAME = "report.md"


def recent_schedule_reports(
    session: Session, schedule_id: uuid.UUID, *, limit: int
) -> list[tuple[uuid.UUID, str]]:
    """`(run_id, path)` for this schedule's most recent reports, newest first.

    Ordered by `Run.started_at`: `Artifact` carries no timestamp, and
    `ended_at` is null whenever a worker crashed, so it cannot order runs.
    """
    stmt = (
        select(Artifact.run_id, Artifact.path)
        .join(Run, Artifact.run_id == Run.id)
        .join(Task, Run.task_id == Task.id)
        .where(Task.schedule_id == schedule_id, Artifact.path == REPORT_FILENAME)
        .order_by(Run.started_at.desc().nulls_last())
        .limit(limit)
    )
    return [(run_id, path) for run_id, path in session.execute(stmt)]
```

- [ ] **Step 4: Run it and verify it passes**

Run: `.venv/bin/pytest tests/store/test_artifacts_query.py -v && .venv/bin/ruff check src tests`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/aicom/store/artifacts_query.py tests/store/test_artifacts_query.py
git commit -m "feat(store): find a schedule's recent reports"
```

---

### Task 2: Stage previous reports into the workspace

**Files:**
- Create: `src/aicom/orchestrator/staging.py`
- Modify: `src/aicom/orchestrator/worker.py`
- Test: `tests/orchestrator/test_staging.py`

**Interfaces:**
- Consumes: `recent_schedule_reports`, `REPORT_FILENAME` (Task 1); `Run`, `Task` from `store/models.py`; `Worker._build_request` in `src/aicom/orchestrator/worker.py`.
- Produces: `PREVIOUS_DIR: str = "previous"`; `PREVIOUS_REPORT_LIMIT: int = 5`; `stage_previous_reports(session: Session, run: Run, workspace: Path, artifact_repo: Path, *, limit: int = PREVIOUS_REPORT_LIMIT) -> int` returning how many files were staged.

Read §4 of the spec before starting.

- [ ] **Step 1: Write the failing test**

Create `tests/orchestrator/test_staging.py`:

```python
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from aicom.orchestrator.staging import PREVIOUS_DIR, stage_previous_reports
from aicom.store.artifacts_query import REPORT_FILENAME
from aicom.store.models import Artifact, Run, Schedule, Task
from tests.store.test_models import make_agent

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def _schedule(session: Session) -> Schedule:
    agent = make_agent(session)
    schedule = Schedule(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name=f"beat-{uuid.uuid4().hex[:6]}",
        cron="0 9 * * *",
        timezone="UTC",
        title_template="t",
        goal_template="g",
        next_due_at=NOW,
    )
    session.add(schedule)
    session.flush()
    return schedule


def _finished_run(
    session: Session,
    schedule: Schedule | None,
    repo: Path,
    *,
    body: str,
    started_at: datetime,
    write_file: bool = True,
) -> Run:
    agent_id = schedule.agent_id if schedule else make_agent(session).id
    task = Task(
        id=uuid.uuid4(),
        agent_id=agent_id,
        title="t",
        goal="g",
        schedule_id=schedule.id if schedule else None,
    )
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1, started_at=started_at)
    session.add(run)
    session.flush()
    session.add(
        Artifact(
            id=uuid.uuid4(), run_id=run.id, kind="md", path=REPORT_FILENAME, git_ref="a"
        )
    )
    session.flush()
    if write_file:
        destination = repo / str(run.id)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / REPORT_FILENAME).write_text(body)
    return run


def _pending_run(session: Session, schedule: Schedule | None) -> Run:
    agent_id = schedule.agent_id if schedule else make_agent(session).id
    task = Task(
        id=uuid.uuid4(),
        agent_id=agent_id,
        title="t",
        goal="g",
        schedule_id=schedule.id if schedule else None,
    )
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1)
    session.add(run)
    session.commit()
    return run


def test_scheduled_run_gets_its_previous_reports(
    session: Session, tmp_path: Path
) -> None:
    repo = tmp_path / "artifacts"
    schedule = _schedule(session)
    _finished_run(session, schedule, repo, body="older", started_at=NOW - timedelta(days=2))
    _finished_run(session, schedule, repo, body="newer", started_at=NOW - timedelta(days=1))
    run = _pending_run(session, schedule)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    staged = stage_previous_reports(session, run, workspace, repo)

    assert staged == 2
    files = sorted((workspace / PREVIOUS_DIR).iterdir())
    assert len(files) == 2
    # Newest first, so the ordering is visible in the filenames.
    assert files[0].read_text() == "newer"
    assert files[1].read_text() == "older"


def test_a_manual_run_gets_nothing(session: Session, tmp_path: Path) -> None:
    repo = tmp_path / "artifacts"
    run = _pending_run(session, None)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    assert stage_previous_reports(session, run, workspace, repo) == 0
    assert not (workspace / PREVIOUS_DIR).exists()


def test_no_previous_reports_leaves_no_directory(
    session: Session, tmp_path: Path
) -> None:
    # The agent should not have to tell "nothing yet" from "staging broke".
    repo = tmp_path / "artifacts"
    schedule = _schedule(session)
    run = _pending_run(session, schedule)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    assert stage_previous_reports(session, run, workspace, repo) == 0
    assert not (workspace / PREVIOUS_DIR).exists()


def test_a_report_missing_from_disk_is_skipped(
    session: Session, tmp_path: Path
) -> None:
    repo = tmp_path / "artifacts"
    schedule = _schedule(session)
    _finished_run(
        session, schedule, repo, body="gone", started_at=NOW - timedelta(days=2),
        write_file=False,
    )
    _finished_run(session, schedule, repo, body="here", started_at=NOW - timedelta(days=1))
    run = _pending_run(session, schedule)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    assert stage_previous_reports(session, run, workspace, repo) == 1
    assert (workspace / PREVIOUS_DIR).iterdir().__next__().read_text() == "here"


def test_limit_bounds_what_is_staged(session: Session, tmp_path: Path) -> None:
    repo = tmp_path / "artifacts"
    schedule = _schedule(session)
    for day in range(4):
        _finished_run(
            session, schedule, repo, body=f"day{day}", started_at=NOW - timedelta(days=day)
        )
    run = _pending_run(session, schedule)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    assert stage_previous_reports(session, run, workspace, repo, limit=2) == 2
```

- [ ] **Step 2: Run it and verify it fails**

Run: `.venv/bin/pytest tests/orchestrator/test_staging.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.orchestrator.staging'`

- [ ] **Step 3: Write `src/aicom/orchestrator/staging.py`**

```python
"""Gives a scheduled agent memory of what it already reported.

The agent is never granted access to the artifact repository — that would let
every agent read everything every agent has produced. Instead the reports it
needs are copied into its own workspace before it starts.
"""

import logging
import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from aicom.store.artifacts_query import recent_schedule_reports
from aicom.store.models import Run

logger = logging.getLogger(__name__)

PREVIOUS_DIR = "previous"
PREVIOUS_REPORT_LIMIT = 5


def stage_previous_reports(
    session: Session,
    run: Run,
    workspace: Path,
    artifact_repo: Path,
    *,
    limit: int = PREVIOUS_REPORT_LIMIT,
) -> int:
    """Copy this schedule's recent reports into `workspace/previous/`.

    Returns how many were staged. Creates no directory when there is nothing
    to stage, so the agent cannot mistake an empty directory for a failure.
    """
    schedule_id = run.task.schedule_id
    if schedule_id is None:
        return 0

    target = workspace / PREVIOUS_DIR
    staged = 0
    for index, (run_id, path) in enumerate(
        recent_schedule_reports(session, schedule_id, limit=limit)
    ):
        source = Path(artifact_repo) / str(run_id) / path
        if not source.is_file():
            logger.warning("previous report missing from disk: %s", source)
            continue
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target / f"{index:02d}-{run_id}.md")
        staged += 1
    return staged
```

- [ ] **Step 4: Run it and verify it passes**

Run: `.venv/bin/pytest tests/orchestrator/test_staging.py -v`
Expected: PASS

- [ ] **Step 5: Wire it into the worker**

In `src/aicom/orchestrator/worker.py`, add the import:

```python
from aicom.orchestrator.staging import stage_previous_reports
```

and in `_build_request`, immediately after the line that creates the workspace
(`workspace = prepare_workspace(...)`) and before the request is assembled:

```python
        try:
            stage_previous_reports(
                session, run, workspace, self._settings.artifact_repo_path
            )
        except Exception:
            # Losing the memory makes the agent repeat itself; failing the run
            # produces nothing at all. The first is the lesser harm.
            logger.exception("staging previous reports failed for run %s", run.id)
```

- [ ] **Step 6: Write the failing test for the wiring**

Add to `tests/orchestrator/test_staging.py`:

```python
def test_staging_failure_does_not_fail_the_run(
    sessions, session: Session, tmp_path: Path, monkeypatch
) -> None:
    import json

    from aicom.config import Settings
    from aicom.executor.fake import FakeExecutor
    from aicom.notify.fake import FakeNotifier
    from aicom.orchestrator import worker as worker_module
    from aicom.orchestrator.worker import Worker
    from aicom.store.runs import claim_next_queued

    def explode(*args: object, **kwargs: object) -> int:
        raise RuntimeError("staging is broken")

    monkeypatch.setattr(worker_module, "stage_previous_reports", explode)

    repo = tmp_path / "artifacts"
    repo.mkdir(parents=True)
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)

    schedule = _schedule(session)
    run = _pending_run(session, schedule)

    settings = Settings(
        workspace_root=tmp_path / "ws",
        artifact_repo_path=repo,
        database_url="postgresql+psycopg://unused/unused",
    )
    executor = FakeExecutor()
    executor.queue(run.id, [json.dumps({"type": "result"})])
    worker = Worker(sessions, executor, FakeNotifier(), settings, worker_id="w1")

    assert worker.tick(NOW) is True

    session.expire_all()
    assert session.get(Run, run.id).status.value == "succeeded"
```

- [ ] **Step 7: Run the whole suite**

Run: `.venv/bin/pytest -m "not smoke" -q && .venv/bin/ruff check src tests`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/aicom/orchestrator/staging.py src/aicom/orchestrator/worker.py \
        tests/orchestrator/test_staging.py
git commit -m "feat(orchestrator): stage a schedule's previous reports into the workspace"
```

---

### Task 3: Pack definitions and the seed command

**Files:**
- Create: `src/aicom/packs/__init__.py`, `src/aicom/packs/opportunity.py`, `src/aicom/packs/seed.py`
- Test: `tests/packs/__init__.py`, `tests/packs/conftest.py`, `tests/packs/test_seed.py`

**Interfaces:**
- Consumes: `Agent`, `Schedule` from `store/models.py`; `next_fire` from `src/aicom/domain/cron.py`.
- Produces: `PackAgent` and `PackSchedule` frozen dataclasses; `Pack` frozen dataclass with `name`, `agent`, `schedules`; `PACKS: dict[str, Pack]`; `seed_pack(session: Session, pack: Pack, *, now: datetime) -> tuple[uuid.UUID, int]` returning the agent id and how many schedules were written; `UnknownPack(Exception)`.

- [ ] **Step 1: Write the failing test**

Create `tests/packs/__init__.py` (empty), `tests/packs/conftest.py`:

```python
from tests.store.conftest import engine, session, sessions  # noqa: F401
```

Create `tests/packs/test_seed.py`:

```python
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from aicom.packs import PACKS, UnknownPack, get_pack
from aicom.packs.seed import seed_pack
from aicom.store.models import Agent, Schedule

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def test_the_opportunity_pack_is_registered() -> None:
    assert "opportunity" in PACKS
    assert get_pack("opportunity").name == "opportunity"


def test_unknown_pack_raises_listing_the_known_ones() -> None:
    with pytest.raises(UnknownPack, match="opportunity"):
        get_pack("nope")


def test_seeding_creates_the_agent_and_its_schedules(session: Session) -> None:
    pack = get_pack("opportunity")

    agent_id, written = seed_pack(session, pack, now=NOW)
    session.commit()

    agent = session.get(Agent, agent_id)
    assert agent is not None
    assert agent.name == pack.agent.name
    assert agent.enabled is True
    assert written == len(pack.schedules)

    schedules = list(session.scalars(select(Schedule).where(Schedule.agent_id == agent_id)))
    assert len(schedules) == len(pack.schedules)
    assert all(s.next_due_at > NOW for s in schedules)


def test_seeding_twice_does_not_duplicate(session: Session) -> None:
    pack = get_pack("opportunity")

    first_id, _ = seed_pack(session, pack, now=NOW)
    session.commit()
    second_id, _ = seed_pack(session, pack, now=NOW)
    session.commit()

    assert first_id == second_id
    assert session.query(Agent).count() == 1
    assert session.query(Schedule).count() == len(pack.schedules)


def test_seeding_again_updates_a_changed_definition(session: Session) -> None:
    pack = get_pack("opportunity")
    agent_id, _ = seed_pack(session, pack, now=NOW)
    session.commit()

    agent = session.get(Agent, agent_id)
    assert agent is not None
    agent.persona = "clobbered by hand"
    session.commit()

    seed_pack(session, pack, now=NOW)
    session.commit()

    session.expire_all()
    assert session.get(Agent, agent_id).persona == pack.agent.persona


def test_the_pack_reaches_no_gated_action() -> None:
    # It spends nothing, publishes nothing, contacts nobody.
    pack = get_pack("opportunity")
    assert pack.agent.gated_tools == ()
    assert "Bash" not in pack.agent.allowed_tools


def test_the_persona_permits_reporting_nothing() -> None:
    # Without this an agent asked daily for findings invents them.
    persona = get_pack("opportunity").agent.persona.lower()
    assert "nothing" in persona
```

- [ ] **Step 2: Run it and verify it fails**

Run: `.venv/bin/pytest tests/packs -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aicom.packs'`

- [ ] **Step 3: Write `src/aicom/packs/types.py` and `src/aicom/packs/__init__.py`**

The dataclasses live in their own module so a definition file can import them without
importing the registry that imports the definition file. Putting them in `__init__.py`
would make that a genuine import cycle.

`src/aicom/packs/types.py`:

```python
"""The shape of a pack. Kept apart from the registry to avoid an import cycle."""

from dataclasses import dataclass


class UnknownPack(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PackAgent:
    name: str
    persona: str
    allowed_tools: tuple[str, ...]
    gated_tools: tuple[str, ...] = ()
    max_run_seconds: int = 1800


@dataclass(frozen=True, slots=True)
class PackSchedule:
    name: str
    cron: str
    timezone: str
    title_template: str
    goal_template: str


@dataclass(frozen=True, slots=True)
class Pack:
    name: str
    agent: PackAgent
    schedules: tuple[PackSchedule, ...]
```

`src/aicom/packs/__init__.py`:

```python
"""Agent packs: an agent and its schedules, as data.

A pack is not a subsystem. Adding one is a new definition file plus a registry
entry, not a new mechanism.
"""

from aicom.packs.opportunity import OPPORTUNITY
from aicom.packs.types import Pack, PackAgent, PackSchedule, UnknownPack

__all__ = ["PACKS", "Pack", "PackAgent", "PackSchedule", "UnknownPack", "get_pack"]

PACKS: dict[str, Pack] = {OPPORTUNITY.name: OPPORTUNITY}


def get_pack(name: str) -> Pack:
    try:
        return PACKS[name]
    except KeyError as exc:
        known = ", ".join(sorted(PACKS))
        raise UnknownPack(f"unknown pack {name!r}; known packs: {known}") from exc
```

- [ ] **Step 4: Write `src/aicom/packs/opportunity.py`**

```python
"""The opportunity research pack: watch a beat, report what changed."""

from aicom.packs.types import Pack, PackAgent, PackSchedule

PERSONA = """\
You watch a defined beat and report what changed.

Before writing anything, read every file in `previous/` if that directory
exists. Those are your own most recent reports, newest first. Whatever you
already told the operator, do not tell them again.

Report only what is new, what changed, and what disappeared. For each item say
what it is, why it might matter, and where you found it.

If nothing has changed since your last report, say exactly that in one line and
stop. An empty report is a correct answer, and it is far more useful than an
invented one. You will be asked this question every day; most days the honest
answer is short.

Write your report to `report.md` in your working directory.
"""

OPPORTUNITY = Pack(
    name="opportunity",
    agent=PackAgent(
        name="scout",
        persona=PERSONA,
        allowed_tools=("WebSearch", "WebFetch", "Read", "Write", "Glob", "Grep"),
        gated_tools=(),
        max_run_seconds=900,
    ),
    schedules=(
        PackSchedule(
            name="ai-agent-tooling",
            cron="0 9 * * 1-5",
            timezone="Asia/Seoul",
            title_template="Scan: AI agent tooling",
            goal_template=(
                "Watch the AI agent tooling space. Look for newly released or "
                "substantially changed frameworks, orchestration tools, and "
                "agent platforms, and for tools that were widely used and have "
                "gone quiet or been abandoned.\n\n"
                "Report what a solo builder running autonomous agents would "
                "want to know that they did not know yesterday."
            ),
        ),
    ),
)
```

- [ ] **Step 5: Write `src/aicom/packs/seed.py`**

```python
"""Installs a pack's agent and schedules. Safe to run repeatedly."""

import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from aicom.domain.cron import next_fire
from aicom.packs import Pack, UnknownPack, get_pack
from aicom.store.db import make_engine, session_factory
from aicom.store.models import Agent, Schedule


def seed_pack(session: Session, pack: Pack, *, now: datetime) -> tuple[uuid.UUID, int]:
    """Upsert the pack's agent and schedules. Returns (agent id, schedules written)."""
    agent = session.scalar(select(Agent).where(Agent.name == pack.agent.name))
    if agent is None:
        agent = Agent(id=uuid.uuid4(), name=pack.agent.name)
        session.add(agent)
    agent.persona = pack.agent.persona
    agent.allowed_tools = list(pack.agent.allowed_tools)
    agent.gated_tools = list(pack.agent.gated_tools)
    agent.max_run_seconds = pack.agent.max_run_seconds
    agent.enabled = True
    session.flush()

    for definition in pack.schedules:
        schedule = session.scalar(
            select(Schedule).where(
                Schedule.agent_id == agent.id, Schedule.name == definition.name
            )
        )
        if schedule is None:
            schedule = Schedule(
                id=uuid.uuid4(),
                agent_id=agent.id,
                name=definition.name,
                next_due_at=next_fire(definition.cron, definition.timezone, now),
            )
            session.add(schedule)
        schedule.cron = definition.cron
        schedule.timezone = definition.timezone
        schedule.title_template = definition.title_template
        schedule.goal_template = definition.goal_template
        session.flush()

    return agent.id, len(pack.schedules)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m aicom.packs.seed <pack>", file=sys.stderr)
        return 2
    try:
        pack = get_pack(argv[1])
    except UnknownPack as exc:
        print(str(exc), file=sys.stderr)
        return 2

    from aicom.config import load_settings

    sessions = session_factory(make_engine(load_settings().database_url))
    with sessions() as session:
        agent_id, written = seed_pack(session, pack, now=datetime.now(UTC))
        session.commit()
    print(f"seeded pack {pack.name}: agent {agent_id}, {written} schedule(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/pytest tests/packs -v && .venv/bin/ruff check src tests`
Expected: PASS. If ruff objects to the mid-file import in `__init__.py`, move the
`PACKS` registry and `get_pack` into a small `aicom/packs/registry.py` that imports the
definition module, and re-export them from `__init__.py` — do not silence the rule.

- [ ] **Step 7: Commit**

```bash
git add src/aicom/packs tests/packs
git commit -m "feat(packs): add the opportunity pack and an idempotent seed command"
```

---

### Task 4: Run it for real, then document it

**Files:**
- Modify: `README.md`, `AGENTS.md`, `src/aicom/AGENTS.md`, `src/aicom/orchestrator/AGENTS.md`, `src/aicom/store/AGENTS.md`
- Create: `src/aicom/packs/AGENTS.md`

**Interfaces:**
- Consumes: everything above.
- Produces: no new code interfaces.

This task's first half is an experiment, not a test. The pack's real question — does the
agent write a report worth reading — cannot be asserted, only judged.

- [ ] **Step 1: Seed the pack against a real database**

Start Postgres if it is not running, apply migrations, and seed:

```bash
docker run -d --name aicom-pg -e POSTGRES_PASSWORD=aicom -e POSTGRES_USER=aicom \
  -e POSTGRES_DB=aicom -p 5432:5432 postgres:16-alpine || true
.venv/bin/alembic upgrade head
.venv/bin/python -m aicom.packs.seed opportunity
```

Expected: prints the agent id and one schedule. Run it a second time and confirm it
prints the same agent id rather than creating another.

- [ ] **Step 2: Make the schedule due and run one real cycle**

Write a short script in the scratchpad (not in the repository) that sets the seeded
schedule's `next_due_at` into the past, then runs `Scheduler(sessions, notifier).tick(now)`
followed by `Worker(...).tick(now)` with the REAL `ClaudeCliExecutor` and a `FakeNotifier`.
This spends real Claude subscription usage.

- [ ] **Step 3: Read the report and judge it**

Find the committed artifact under the artifact repository and read it. Record in the
report file: whether the agent produced `report.md`, whether the content is specific
enough to act on or is generic filler, and whether it respected the instruction to say
so when nothing changed. If the agent wrote nothing useful, say that plainly — the fix
is the persona or the beat's `goal_template`, and knowing which is the point of running it.

- [ ] **Step 4: Run a second cycle and check the memory works**

Make the schedule due again, run another cycle, and confirm the second run's workspace
received a `previous/` directory containing the first report, and that the second report
does not simply repeat the first. This is the one behaviour that cannot be verified any
other way.

- [ ] **Step 5: Write `src/aicom/packs/AGENTS.md`**

Follow the existing tree's template (parent tag, Purpose, Key Files, Public Interface with
real signatures, For AI Agents, Dependencies). Record: that a pack is data, not a
subsystem; that adding one is a new definition file plus a registry entry; that the seed
command is idempotent and safe to re-run; and that a pack's `gated_tools` must never
overlap its `allowed_tools`, because the worker refuses to run such an agent.

- [ ] **Step 6: Update the rest of the documentation**

- `src/aicom/AGENTS.md` — add `packs/` to the module table and the dependency diagram.
- `src/aicom/orchestrator/AGENTS.md` — add `staging.py`, and record why memory is staged
  into the workspace rather than granting access to the artifact repository.
- `src/aicom/store/AGENTS.md` — add `artifacts_query.py`, and note that ordering uses
  `Run.started_at` because `Artifact` has no timestamp and `ended_at` is null after a crash.
- Root `AGENTS.md` — move S5 to "In progress (S5a)" in the roadmap, and note that the
  system still earns nothing: this pack reports, it does not act.
- `README.md` — a short "Packs" section: what a pack is, how to seed one, and that the
  opportunity pack watches a beat defined in its schedule's `goal_template`.

- [ ] **Step 7: Run everything**

Run: `.venv/bin/pytest -m "not smoke" -q && .venv/bin/ruff check src tests && .venv/bin/mypy --strict src/aicom/domain`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add README.md AGENTS.md src/aicom/AGENTS.md src/aicom/orchestrator/AGENTS.md \
        src/aicom/store/AGENTS.md src/aicom/packs/AGENTS.md
git commit -m "docs: document packs and how to seed them"
```

---

## Post-plan verification

| Spec section | Where |
|---|---|
| §2 a pack is data plus wiring | Task 3 |
| §3 agent definition, tools, persona | Task 3 (including tests that the gate is unreachable and that reporting nothing is permitted) |
| §4 memory staged into the workspace | Tasks 1 and 2 |
| §4 only scheduled runs, bounded, no empty directory, failures do not fail the run | Task 2's tests |
| §5 components | Tasks 1–3 |
| §6 error handling | Task 1 (query scoping), Task 2 (missing file, staging failure), Task 3 (idempotence, unknown pack) |
| §7 testing, including the parts that must be judged by hand | Tasks 1–3 automated; Task 4 steps 1–4 by hand |
| §8 out of scope | Nothing in this plan spends, publishes, or contacts |
