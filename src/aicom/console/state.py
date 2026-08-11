"""Derives what the console shows from the database.

Pure given a session and a `now`, so every status rule is testable without
HTTP or a browser.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aicom.domain.enums import ApprovalStatus, RunStatus
from aicom.store.models import Agent, Approval, Event, Run, SystemState, Task

AGENT_STATUSES: tuple[str, ...] = ("waiting", "working", "paused", "failed", "idle")

_TERMINAL_FAILURE = (RunStatus.FAILED, RunStatus.TIMED_OUT)

# Every status a run can end up in once it has stopped changing. Used to find
# the most recently finished run for an agent so a stale failure can be
# reported, without requiring `ended_at` to be set (worker crashes leave it
# null even though the run is done). Ordered by `started_at` rather than
# `ended_at`: every run that ever executed has a `started_at`, set by
# `claim_next_queued` the moment a worker claims it (src/aicom/store/runs.py),
# so a crashed run with no `ended_at` still sorts correctly relative to a
# stamped success. `ended_at` would rank any timestamped row above any null
# row regardless of true recency.
_TERMINAL_STATUSES = (
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.TIMED_OUT,
    RunStatus.CANCELLED,
    RunStatus.SUPERSEDED,
)


@dataclass(frozen=True, slots=True)
class AgentView:
    agent_id: str
    name: str
    status: str
    run_id: str | None
    activity: str | None


@dataclass(frozen=True, slots=True)
class ConsoleSnapshot:
    generated_at: str
    paused_until: str | None
    pending_approvals: int
    agents: list[AgentView]


def build_snapshot(session: Session, *, now: datetime) -> ConsoleSnapshot:
    paused_until = _paused_until(session, now)
    return ConsoleSnapshot(
        generated_at=now.isoformat(),
        paused_until=paused_until.isoformat() if paused_until else None,
        pending_approvals=_pending_approvals(session),
        agents=[
            _agent_view(session, agent, paused=paused_until is not None)
            for agent in session.scalars(
                select(Agent).where(Agent.enabled.is_(True)).order_by(Agent.name)
            )
        ],
    )


def _paused_until(session: Session, now: datetime) -> datetime | None:
    state = session.get(SystemState, 1)
    if state is None or state.llm_paused_until is None:
        return None
    return state.llm_paused_until if state.llm_paused_until > now else None


def _pending_approvals(session: Session) -> int:
    stmt = select(func.count()).select_from(Approval).where(
        Approval.status == ApprovalStatus.PENDING
    )
    return int(session.scalar(stmt) or 0)


def _runs_for(session: Session, agent_id: uuid.UUID, status: RunStatus) -> list[Run]:
    stmt = (
        select(Run)
        .join(Task, Run.task_id == Task.id)
        .where(Task.agent_id == agent_id, Run.status == status)
        .order_by(Run.id)
    )
    return list(session.scalars(stmt))


def _agent_view(session: Session, agent: Agent, *, paused: bool) -> AgentView:
    # Priority is explicit: what needs a human outranks what is merely
    # happening, and a global pause outranks a stale failure.
    waiting = _runs_for(session, agent.id, RunStatus.AWAITING_APPROVAL)
    if waiting:
        return AgentView(str(agent.id), agent.name, "waiting", str(waiting[0].id), None)

    running = _runs_for(session, agent.id, RunStatus.RUNNING)
    if running:
        run = running[0]
        return AgentView(
            str(agent.id), agent.name, "working", str(run.id), _activity(session, run.id)
        )

    if paused and _runs_for(session, agent.id, RunStatus.QUEUED):
        return AgentView(str(agent.id), agent.name, "paused", None, None)

    if _latest_terminal_failed(session, agent.id):
        return AgentView(str(agent.id), agent.name, "failed", None, None)

    return AgentView(str(agent.id), agent.name, "idle", None, None)


def _latest_terminal_failed(session: Session, agent_id: uuid.UUID) -> bool:
    stmt = (
        select(Run.status)
        .join(Task, Run.task_id == Task.id)
        .where(Task.agent_id == agent_id, Run.status.in_(_TERMINAL_STATUSES))
        .order_by(Run.started_at.desc().nulls_last())
        .limit(1)
    )
    status = session.scalar(stmt)
    return status in _TERMINAL_FAILURE


def _activity(session: Session, run_id: uuid.UUID) -> str | None:
    """One line describing what this run is doing, from its newest event."""
    stmt = (
        select(Event).where(Event.run_id == run_id).order_by(Event.seq.desc()).limit(1)
    )
    event = session.scalar(stmt)
    if event is None:
        return "starting up"
    payload = event.payload or {}
    tool = payload.get("name")
    if event.type == "tool_use" and isinstance(tool, str):
        return f"using {tool}"
    if event.type == "assistant":
        return "thinking"
    if event.type == "result":
        return "wrapping up"
    return event.type.replace("_", " ")
