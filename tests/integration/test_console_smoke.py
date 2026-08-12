"""One real-browser check that the console actually renders.

The three.js scene is not unit tested — this is the single test that would
catch a bundle that builds but does not run. `text=researcher` alone is not
enough: the `<AgentTable>` fallback (rendered when WebGL2 is unavailable)
shows the same agent name, so a headless Chromium without WebGL2 support
would pass this test without the scene ever running. This also asserts a
`canvas` element and an `.overlay .pill` label exist, which only the
three.js/`OfficeView` path renders — `AgentTable` has neither.
"""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy.orm import Session, sessionmaker

from aicom.config import Settings
from aicom.inbound.app import create_app
from tests.store.test_models import make_agent

PASSWORD = "hunter2"
DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


@pytest.fixture()
def server(sessions: sessionmaker[Session], session: Session) -> Iterator[str]:
    make_agent(session, "researcher")
    session.commit()

    app = create_app(
        sessions,
        Settings(
            console_password=PASSWORD,
            session_secret="session-secret",
            console_static_dir=DIST,
            database_url="postgresql+psycopg://unused/unused",
        ),
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=8123, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        pass
    yield "http://127.0.0.1:8123"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.mark.browser
@pytest.mark.skipif(not DIST.is_dir(), reason="run `npm run build` in web/ first")
def test_operator_can_log_in_and_see_an_agent(server: str) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(server)

        page.fill("input[type=password]", PASSWORD)
        page.click("button[type=submit]")

        page.wait_for_selector("text=researcher", timeout=15000)
        assert page.locator("header").is_visible()
        # These only exist on the three.js path: AgentTable (the no-WebGL2
        # fallback) renders neither a canvas nor an `.overlay .pill` label,
        # so asserting them proves the scene itself ran, not just that some
        # view showing "researcher" ran.
        assert page.locator("canvas").is_visible()
        assert page.locator(".overlay .pill").first.is_visible()
        browser.close()
