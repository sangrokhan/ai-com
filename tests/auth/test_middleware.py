import hashlib
import hmac
import json
import time
import urllib.parse

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from aicom.auth.password import SESSION_COOKIE
from aicom.config import Settings
from aicom.inbound.app import create_app

PASSWORD = "hunter2"
SLACK_SECRET = "slack-secret"


def _settings() -> Settings:
    return Settings(
        console_password=PASSWORD,
        session_secret="session-secret",
        slack_signing_secret=SLACK_SECRET,
        slack_approver_ids=("U_OWNER",),
        database_url="postgresql+psycopg://unused/unused",
    )


def _client(sessions: sessionmaker[Session]) -> TestClient:
    return TestClient(create_app(sessions, _settings()))


def test_health_is_reachable_without_a_cookie(sessions: sessionmaker[Session]) -> None:
    # The container healthcheck cannot log in.
    assert _client(sessions).get("/health").status_code == 200


def test_protected_route_rejected_without_a_cookie(
    sessions: sessionmaker[Session],
) -> None:
    response = _client(sessions).get("/tasks")
    assert response.status_code == 401


def test_write_route_rejected_without_a_cookie(sessions: sessionmaker[Session]) -> None:
    # POST /tasks queues work for an autonomous agent; it must not be open.
    response = _client(sessions).post("/tasks", json={})
    assert response.status_code == 401


def test_login_with_the_right_password_grants_access(
    sessions: sessionmaker[Session],
) -> None:
    client = _client(sessions)
    login = client.post("/auth/login", json={"password": PASSWORD})
    assert login.status_code == 200
    assert SESSION_COOKIE in login.cookies

    assert client.get("/tasks").status_code == 200


def test_login_with_the_wrong_password_is_rejected(
    sessions: sessionmaker[Session],
) -> None:
    client = _client(sessions)
    assert client.post("/auth/login", json={"password": "nope"}).status_code == 401
    assert client.get("/tasks").status_code == 401


def test_logout_clears_the_cookie_client_side(sessions: sessionmaker[Session]) -> None:
    client = _client(sessions)
    login = client.post("/auth/login", json={"password": PASSWORD})
    token = login.cookies[SESSION_COOKIE]

    assert client.post("/auth/logout").status_code == 200
    # The client's own cookie jar is cleared, so its next request is rejected...
    assert client.get("/tasks").status_code == 401

    # ...but logout is a client-side courtesy only: the token itself is not
    # revoked server-side, so re-presenting the SAME cookie value still
    # succeeds. Real revocation needs server-side session state, which this
    # task does not add -- this assertion documents that limitation rather
    # than implying a guarantee we do not provide.
    still_client = _client(sessions)
    still_client.cookies.set(SESSION_COOKIE, token)
    assert still_client.get("/tasks").status_code == 200


def test_login_with_a_non_ascii_wrong_password_is_a_401_not_a_500(
    sessions: sessionmaker[Session],
) -> None:
    response = _client(sessions).post("/auth/login", json={"password": "pässwörd"})
    assert response.status_code == 401


def test_slack_webhook_still_works_without_a_cookie(
    sessions: sessionmaker[Session],
) -> None:
    # It authenticates with a signature; a cookie is impossible for it.
    payload = {"type": "block_actions", "user": {"id": "U_OWNER"}, "actions": []}
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
    ts = str(int(time.time()))
    base = b"v0:" + ts.encode() + b":" + body
    sig = "v0=" + hmac.new(SLACK_SECRET.encode(), base, hashlib.sha256).hexdigest()

    response = _client(sessions).post(
        "/slack/interactions",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
        },
    )
    assert response.status_code == 200
