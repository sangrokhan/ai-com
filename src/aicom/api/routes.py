import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aicom.domain.enums import ApprovalStatus
from aicom.store.models import Agent, Approval, Event, Run, Task


class CreateTask(BaseModel):
    agent_id: uuid.UUID
    title: str
    goal: str
    parent_task_id: uuid.UUID | None = None


def make_router(sessions: sessionmaker[Session]) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @router.post("/tasks", status_code=201)
    def create_task(body: CreateTask) -> dict:
        with sessions() as session:
            agent = session.get(Agent, body.agent_id)
            if agent is None:
                raise HTTPException(404, "agent not found")
            if not agent.enabled:
                raise HTTPException(400, "agent is disabled")
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
            return [{"seq": e.seq, "type": e.type, "payload": e.payload} for e in events]

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
