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
