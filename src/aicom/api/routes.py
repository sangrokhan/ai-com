import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aicom.domain.cron import InvalidCron, next_fire, validate_cron
from aicom.domain.enums import ApprovalStatus
from aicom.store.models import Agent, Approval, Event, Run, Schedule, Task


class CreateTask(BaseModel):
    agent_id: uuid.UUID
    title: str
    goal: str
    parent_task_id: uuid.UUID | None = None


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

    return router
