import json
import threading
import time
import uuid

import httpx
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from aicom.config import Settings
from aicom.inbound.app import create_app
from tests.store.test_models import make_agent

PASSWORD = "hunter2"


def _settings() -> Settings:
    return Settings(
        console_password=PASSWORD,
        session_secret="session-secret",
        sse_interval_seconds=0.05,
        database_url="postgresql+psycopg://unused/unused",
    )


def _client(sessions: sessionmaker[Session]) -> TestClient:
    client = TestClient(create_app(sessions, _settings()))
    client.post("/auth/login", json={"password": PASSWORD})
    return client


def test_state_requires_a_session(sessions: sessionmaker[Session]) -> None:
    anonymous = TestClient(create_app(sessions, _settings()))
    assert anonymous.get("/console/state").status_code == 401


def test_state_returns_every_enabled_agent(
    sessions: sessionmaker[Session], session: Session
) -> None:
    agent = make_agent(session)
    session.commit()

    body = _client(sessions).get("/console/state").json()

    assert body["pending_approvals"] == 0
    assert body["paused_until"] is None
    assert [a["name"] for a in body["agents"]] == [agent.name]
    assert body["agents"][0]["status"] == "idle"
    assert body["agents"][0]["agent_id"] == str(agent.id)


def test_stream_emits_an_initial_snapshot(
    sessions: sessionmaker[Session], session: Session
) -> None:
    # `console_stream` never terminates on its own (it pushes forever), and
    # Starlette's TestClient blocks until the whole ASGI call returns before
    # handing back so much as the first byte -- there is no way to observe a
    # partial event through it for a route that runs forever. A real server
    # on a real socket streams incrementally as any live client would, so
    # this spins one up on an ephemeral port instead of using TestClient.
    make_agent(session)
    session.commit()

    app = create_app(sessions, _settings())
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        while not server.started:
            time.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            client.post("/auth/login", json={"password": PASSWORD})
            with client.stream("GET", "/console/stream") as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                for line in response.iter_lines():
                    if line.startswith("data:"):
                        payload = json.loads(line.removeprefix("data:").strip())
                        assert "agents" in payload
                        break
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_stream_requires_a_session(sessions: sessionmaker[Session]) -> None:
    anonymous = TestClient(create_app(sessions, _settings()))
    assert anonymous.get("/console/stream").status_code == 401


def test_unknown_agent_id_is_not_leaked_by_state(
    sessions: sessionmaker[Session], session: Session
) -> None:
    make_agent(session)
    session.commit()
    body = _client(sessions).get("/console/state").json()
    assert all(uuid.UUID(a["agent_id"]) for a in body["agents"])
