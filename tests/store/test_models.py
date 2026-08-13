import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from aicom.domain.enums import ApprovalKind, ApprovalStatus, RunStatus, TaskStatus
from aicom.store.models import Agent, Approval, Run, Task


def make_agent(
    session: Session,
    name: str = "researcher",
    *,
    persona: str = "# Researcher\nYou research things.",
    allowed_tools: list[str] | None = None,
    gated_tools: list[str] | None = None,
    mcp_config: dict | None = None,
) -> Agent:
    agent = Agent(
        id=uuid.uuid4(),
        name=f"{name}-{uuid.uuid4().hex[:6]}",
        persona=persona,
        allowed_tools=allowed_tools if allowed_tools is not None else ["Read", "Grep", "WebSearch"],
        gated_tools=gated_tools if gated_tools is not None else [],
        mcp_config=mcp_config if mcp_config is not None else {},
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
