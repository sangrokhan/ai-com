import hashlib
import hmac
import json
import time
import urllib.parse
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from aicom.auth.password import SESSION_COOKIE
from aicom.config import Settings
from aicom.inbound.app import create_app

PASSWORD = "hunter2"
SLACK_SECRET = "slack-secret"


def _settings(static_dir: Path | None = None) -> Settings:
    kwargs = {"console_static_dir": static_dir} if static_dir is not None else {}
    return Settings(
        console_password=PASSWORD,
        session_secret="session-secret",
        slack_signing_secret=SLACK_SECRET,
        slack_approver_ids=("U_OWNER",),
        database_url="postgresql+psycopg://unused/unused",
        **kwargs,
    )


def _client(sessions: sessionmaker[Session], static_dir: Path | None = None) -> TestClient:
    return TestClient(create_app(sessions, _settings(static_dir)))


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


def test_the_static_bundle_is_served_without_a_cookie(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    # The chicken-and-egg bug this exists to fix: a cookie-less browser must be
    # able to load the SPA shell to reach the login form in the first place.
    marker = "TASK-7-BUNDLE-MARKER"
    (tmp_path / "index.html").write_text(f"<!doctype html><title>{marker}</title>")
    response = _client(sessions, tmp_path).get("/")
    assert response.status_code == 200
    assert marker in response.text


def test_a_bundle_asset_is_served_without_a_cookie(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>console</title>")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('hi');")
    response = _client(sessions, tmp_path).get("/assets/app.js")
    assert response.status_code == 200
    assert "hi" in response.text


def test_data_endpoints_still_require_a_cookie_with_a_bundle_mounted(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>console</title>")
    client = _client(sessions, tmp_path)
    assert client.get("/console/state").status_code == 401
    assert client.get("/tasks").status_code == 401


def test_a_route_shaped_like_an_asset_path_stays_protected(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    # A naive `path.startswith("/assets/")` exemption would wrongly let this
    # through. The exemption is filesystem-backed instead: a bundle IS mounted
    # here (index.html and a real assets/ directory both exist), so this
    # exercises the actual file-existence check rather than short-circuiting on
    # `static_dir.is_dir()` being False. A hypothetical future API route that
    # happens to live under the same "/assets/" prefix as the built assets --
    # but names no real file in the bundle -- still has to clear the session
    # check like any other route.
    (tmp_path / "index.html").write_text("<!doctype html><title>console</title>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log('hi');")
    app = create_app(sessions, _settings(tmp_path))

    @app.get("/assets/admin")
    def _future_route_under_the_assets_prefix() -> dict:
        return {"secret": "yes"}

    response = TestClient(app).get("/assets/admin")
    assert response.status_code == 401


def test_an_asset_shaped_path_naming_no_real_file_is_not_exempt(
    sessions: sessionmaker[Session], tmp_path: Path
) -> None:
    # Same point with a bundle actually mounted: "/assets/does-not-exist.js"
    # looks exactly like a built asset but names no real file, so it must not
    # be waved through by a broad prefix rule.
    (tmp_path / "index.html").write_text("<!doctype html><title>console</title>")
    (tmp_path / "assets").mkdir()
    response = _client(sessions, tmp_path).get("/assets/does-not-exist.js")
    assert response.status_code == 401


def test_a_malformed_path_with_an_embedded_null_byte_is_rejected_not_a_500(
    tmp_path: Path,
) -> None:
    # Path.resolve() raises ValueError on an embedded null byte (the kind of
    # path a request like "GET /assets/%00.js" decodes to). A bundle is
    # mounted here (real index.html + assets/) so this runs the actual
    # file-existence branch of is_static_bundle_request, not the earlier
    # static_dir.is_dir() short-circuit -- pinning that the null byte is
    # caught by the guard around resolve()/relative_to() and treated as "not
    # a bundle file" (False), never left to propagate as an unhandled 500.
    from aicom.auth.middleware import is_static_bundle_request

    (tmp_path / "index.html").write_text("<!doctype html><title>console</title>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log('hi');")

    assert is_static_bundle_request("GET", "/assets/\x00.js", tmp_path) is False
