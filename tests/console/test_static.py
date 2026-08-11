from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from aicom.config import Settings
from aicom.inbound.app import create_app

PASSWORD = "hunter2"


def _settings(static_dir: Path) -> Settings:
    return Settings(
        console_password=PASSWORD,
        session_secret="session-secret",
        console_static_dir=static_dir,
        database_url="postgresql+psycopg://unused/unused",
    )


def test_api_still_answers_when_a_bundle_is_mounted(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>console</title>")
    client = TestClient(create_app(sessions, _settings(tmp_path)))
    client.post("/auth/login", json={"password": PASSWORD})

    # The catch-all mount must not shadow the API.
    assert client.get("/console/state").status_code == 200
    assert client.get("/health").status_code == 200


def test_missing_bundle_does_not_break_the_app(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    client = TestClient(create_app(sessions, _settings(tmp_path / "absent")))
    client.post("/auth/login", json={"password": PASSWORD})
    assert client.get("/console/state").status_code == 200


def test_root_serves_the_built_bundle(sessions: sessionmaker[Session], tmp_path: Path) -> None:
    marker = "TASK-4-BUNDLE-MARKER-4f8c2e"
    (tmp_path / "index.html").write_text(f"<!doctype html><title>{marker}</title>")
    client = TestClient(create_app(sessions, _settings(tmp_path)))
    client.post("/auth/login", json={"password": PASSWORD})

    response = client.get("/")
    assert response.status_code == 200
    assert marker in response.text


def test_root_without_a_bundle_does_not_500(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    client = TestClient(create_app(sessions, _settings(tmp_path / "absent")))
    client.post("/auth/login", json={"password": PASSWORD})

    response = client.get("/")
    assert response.status_code < 500
