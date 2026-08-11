import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from aicom.api.routes import make_router
from aicom.store.models import Run, Schedule, Task
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


def test_delete_preserves_the_history_it_produced(
    sessions: sessionmaker[Session], session: Session
) -> None:
    agent = make_agent(session)
    session.commit()
    client = _client(sessions)
    schedule_id = uuid.UUID(client.post("/schedules", json=_body(agent.id)).json()["id"])

    task = Task(
        id=uuid.uuid4(),
        agent_id=agent.id,
        title="Market scan",
        goal="Scan the market.",
        schedule_id=schedule_id,
    )
    session.add(task)
    session.flush()
    run = Run(id=uuid.uuid4(), task_id=task.id, attempt=1)
    session.add(run)
    session.commit()

    assert client.delete(f"/schedules/{schedule_id}").status_code == 204

    session.expire_all()
    assert session.query(Schedule).count() == 0

    surviving_task = session.get(Task, task.id)
    assert surviving_task is not None
    assert surviving_task.schedule_id is None

    surviving_run = session.get(Run, run.id)
    assert surviving_run is not None


def test_patch_timezone_only_recomputes_next_due_at(
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

    response = client.patch(f"/schedules/{schedule_id}", json={"timezone": "UTC"})
    assert response.status_code == 200

    session.expire_all()
    after = session.get(Schedule, uuid.UUID(schedule_id))
    assert after is not None
    assert after.timezone == "UTC"
    assert after.next_due_at != original_due


def test_patch_without_cron_or_timezone_leaves_next_due_at_unchanged(
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

    response = client.patch(
        f"/schedules/{schedule_id}", json={"title_template": "Updated title"}
    )
    assert response.status_code == 200

    session.expire_all()
    after = session.get(Schedule, uuid.UUID(schedule_id))
    assert after is not None
    assert after.title_template == "Updated title"
    assert after.next_due_at == original_due
