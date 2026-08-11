<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-11 | Updated: 2026-08-11 -->

# tests

## Purpose
Full test tree for the agent orchestrator: domain logic, persistence, the CLI
executor, quota/gate/notify subsystems, the Slack inbound webhook, the
orchestrator worker/sweeper, the HTTP API, and end-to-end integration flows.
One AGENTS.md covers all packages here deliberately — most are a handful of
files re-exporting the same two fixtures, and per-directory docs would cost
more to read than they'd save.

## Key Files
| File | Description |
|------|-------------|
| `store/conftest.py` | Owns the session-scoped Postgres container and the truncate-between-tests teardown. Source of truth for `engine`/`sessions`/`session` fixtures. |
| `store/test_models.py` | Defines `make_agent`, the shared factory for building a persisted `Agent` row. |
| `inbound/test_app.py` | Defines `SECRET`, `_post`, `_action`, the shared helpers for signing and posting fake Slack interaction payloads. |
| `integration/test_flows.py` | The two full end-to-end flow tests (sign-off cycle, usage-limit pause/resume). |
| `integration/test_cli_smoke.py` | The one `@pytest.mark.smoke` test; spawns the real `claude` binary. |

## Subdirectories
| Dir | Needs Docker? | Notes |
|-----|----------------|-------|
| `domain/` | No | Pure transition logic, no DB fixtures. |
| `executor/` | No | Mocked `subprocess.Popen`; `test_cli_run.py` explicitly is not a smoke test. |
| `quota/` | No | Pure logic (reset windows). |
| `notify/` | No | Slack Block Kit formatting + fake notifier, no DB. |
| `store/` | Yes | Owns the Postgres container fixtures everyone else re-exports. |
| `gate/` | Yes | Re-exports store fixtures. |
| `inbound/` | Yes | Re-exports store fixtures; also owns the Slack signing helpers. |
| `orchestrator/` | Yes | Has its own `conftest.py` (re-export) plus worker/sweeper/artifact tests. |
| `api/` | Yes | Re-exports store fixtures for the FastAPI routes. |
| `integration/` | Yes (+ real `claude` binary for the smoke test) | Full-stack flows; smoke test additionally needs the CLI installed. |

## For AI Agents

### Working In This Directory
- Run from the repo root: `.venv/bin/pytest`. Use `-m "not smoke"` to exclude
  the one test that shells out to the real `claude` binary (slow, needs the
  binary on PATH); use `-m smoke` to run only it. The only marker registered
  in `pyproject.toml` (`[tool.pytest.ini_options]`) is
  `smoke: hits the real claude binary; skipped in CI` — there is no `docker`
  or `integration` marker, so Docker-dependent tests are not separately
  selectable by marker; they just fail if Docker isn't running.
- Every test package directory must keep its `__init__.py` — bare `pytest`
  fails at collection without it (module name collisions across dirs).
  Confirmed present in all ten packages plus `tests/` itself.
- Don't duplicate the Postgres fixtures. `tests/store/conftest.py` is the only
  place that starts the `testcontainers` `PostgresContainer("postgres:16-alpine")`
  and defines the autouse-style truncate-after-each-test cleanup (it lives in
  the `session` fixture's teardown, not a separate autouse fixture — after
  `s.rollback()`, it deletes rows from every table in child-to-parent FK order
  so committed rows from SKIP-LOCKED-style tests don't leak into the next
  test). Every other package that needs a database re-exports the three
  fixtures instead:
  ```python
  from tests.store.conftest import engine, session, sessions  # noqa: F401
  ```
  Copy this exact line into a new package's `conftest.py` if it needs the DB.

### Testing Requirements
- Docker must be running for every package except `domain/`, `executor/`,
  `quota/`, `notify/`.
- `-m smoke` additionally needs the `claude` CLI on PATH; the smoke test
  `skipif`s itself when it isn't installed.

### Common Patterns
- Cross-package shared helpers, not duplicated:
  - `make_agent(session: Session, name: str = "researcher", *, allowed_tools: list[str] | None = None, gated_tools: list[str] | None = None, mcp_config: dict | None = None) -> Agent` in `tests/store/test_models.py`.
  - `SECRET = "shhh"`, `_post(client: TestClient, payload: dict) -> object`,
    `_action(nonce: str, action_id: str = "approve", user: str = "U_OWNER") -> dict`
    in `tests/inbound/test_app.py` — sign and post a fake Slack
    `/slack/interactions` request with a valid `X-Slack-Signature`.
  - `tests/integration/test_flows.py` imports both directly:
    `from tests.inbound.test_app import SECRET, _action, _post` and
    `from tests.store.test_models import make_agent`.
- `tests/integration/test_flows.py` has two tests:
  - `test_full_sign_off_cycle` — task creation through worker park, Slack
    approval reminder sweep, operator approval via the signed webhook, and
    resume-to-completion with the resumed prompt carrying `"APPROVED"` and an
    artifact written under `artifact_repo_path`.
  - `test_usage_limit_pause_then_automatic_resume` — a `usage limit reached`
    executor result requeues the run without burning an attempt, pauses
    globally with exactly one notification, ticks are no-ops while paused,
    then the worker resumes automatically once the reset time passes.
- `tests/integration/test_cli_smoke.py` is not redundant with the unit tests:
  it caught a real regression where the CLI's variadic `--mcp-config` flag
  swallowed the positional prompt argument, breaking every real invocation
  while every mocked unit test still passed. Keep it green; do not delete or
  weaken it to make CI faster.
- Assertions pin the specific transition or side effect (e.g. `run.status is
  RunStatus.AWAITING_APPROVAL`, `executor.requests[1].resume_session_id ==
  "sess-1"`), not a generic "no error" or a status nothing else could have
  set.

### Writing a New Test
- Put it in the package matching the module under test (e.g. a new gate rule
  goes in `tests/gate/`).
- If it needs the database, add `conftest.py` with the one-line re-export
  shown above rather than redefining fixtures.
- Follow the house style: assert the specific field/status transition or
  exact side effect your change produces, not something a no-op would also
  satisfy.

## Dependencies

### Internal
- `aicom.store.models`, `aicom.store.db` (fixtures)
- `aicom.domain.enums`, `aicom.executor.*`, `aicom.gate.service`,
  `aicom.inbound.app`, `aicom.notify.fake`, `aicom.orchestrator.*`

### External
- `pytest`, `testcontainers` (Postgres 16), `sqlalchemy`, `fastapi.testclient`
- Docker daemon (all packages except domain/executor/quota/notify)
- `claude` CLI binary on PATH (smoke test only)

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
