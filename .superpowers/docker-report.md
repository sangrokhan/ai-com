# Docker packaging report

## What was created

- `Dockerfile` — multi-stage (`builder` compiles a venv with `build-essential`,
  `runtime` is `python:3.12-slim` with no build toolchain). `pip install .`
  installs the project itself, not just requirements. Runtime stage also
  installs Node.js + npm just long enough to `npm install -g
  @anthropic-ai/claude-code`, then purges `npm` again (Node itself stays —
  it's a runtime dependency of the `claude` CLI, not a build toolchain).
  Runs as a non-root `aicom` user. `PYTHONUNBUFFERED=1` set. Default `CMD` is
  the worker (`python -m aicom.main`); the `api` service in compose overrides
  it with the uvicorn command.
- `.dockerignore` — excludes `.venv`, `.git`, `.claude`, `.superpowers`,
  `workspaces`, `artifacts`, `__pycache__`, `*.pyc`, pytest/mypy/ruff caches,
  `.env`, `docs`.
- `docker-compose.yml` — `postgres` (postgres:16-alpine, named volume,
  `pg_isready` healthcheck), `migrate` (`alembic upgrade head`, depends on
  postgres healthy, `restart: "no"`), `api` (uvicorn
  `aicom.inbound.app:app_factory --factory`, depends on migrate completing
  successfully, port bound to `127.0.0.1:8000` only, healthcheck hits
  `GET /health`), `worker` (`python -m aicom.main`, depends on migrate,
  mounts `${HOME}/.claude` into the container). All `AICOM_*` / postgres vars
  come from `.env` via `env_file`.
- `.env.example` — every `AICOM_*` field from `src/aicom/config.py`
  (`database_url`, `slack_bot_token`, `slack_signing_secret`,
  `slack_channel`, `slack_approver_ids`, `workspace_root`,
  `artifact_repo_path`, `claude_binary`, `worker_poll_seconds`,
  `worker_heartbeat_seconds`) plus the three plain `POSTGRES_*` vars the
  `postgres` image itself needs, each with a one-line comment and a safe
  placeholder.
- `README.md` — new `## Docker` section: prerequisites (Claude auth on the
  host first), bringing the stack up, re-running migrations, tailing logs,
  tearing down, and an explicit "not hardened for public exposure" callout.
  Existing hand-run instructions were left untouched.
- `.gitignore` — added `.env` (it didn't previously exclude it; discovered
  this the hard way, see Concerns below).
- `src/aicom/api/routes.py` — added `GET /health` returning
  `{"status": "ok"}`, used as the `api` service's Docker healthcheck target
  (the existing routes didn't expose anything suitable).
- `tests/api/test_routes.py` — added `test_health_returns_ok`.

## Claude credential mounting decision

The worker spawns the real `claude` CLI as a subprocess
(`src/aicom/executor/cli.py`), and the project deliberately runs on a
personal Claude subscription rather than an API key (that's why
`src/aicom/quota/` exists). So:

- The image installs the `@anthropic-ai/claude-code` npm package (confirmed
  `claude --version` → `2.1.227 (Claude Code)` inside the built image) but
  bakes in **no credential**.
- `docker-compose.yml`'s `worker` service bind-mounts `${HOME}/.claude` (the
  host's authenticated Claude Code session directory — confirmed to exist at
  `~/.claude` with `.credentials.json` on the verification host) into the
  container at `/home/aicom/.claude`, matching the non-root user's home
  directory so the CLI finds its state in the usual place.
- No `AICOM_*`-style API-key setting was invented. README documents that the
  operator must `claude login` on the host first and that `~/.claude` must
  never be committed or copied into an image.

## Verification — exact commands and real output

**Build:**
```
docker build -t aicom:latest .
```
Succeeded. `docker images aicom:latest` → **1.26GB**.
`docker run --rm aicom:latest which claude node python` →
`/usr/local/bin/claude`, `/usr/bin/node`, `/opt/venv/bin/python`.
`docker run --rm aicom:latest whoami` → `aicom` (non-root confirmed;
`--user root` override still works for `root`, i.e. the image doesn't force
a broken UID).

**Compose up:**
```
cp .env.example .env   # filled in POSTGRES_PASSWORD/AICOM_DATABASE_URL/AICOM_SLACK_SIGNING_SECRET
docker compose up -d
```
Result: `postgres` → healthy. `migrate` → `Exited (0)`, logs show
`Running upgrade  -> d7d59d6924a4, initial schema` then
`d7d59d6924a4 -> b41c7f0a9e12, add agent.gated_tools` (both migrations
applied). `api` → `Up ... (healthy)`. `worker` → `Up`, no error logs.
`curl -sS http://127.0.0.1:8000/health` → `{"status":"ok"}`. `api` logs show
repeated `GET /health HTTP/1.1" 200 OK` from the compose healthcheck probes.

**Compose down:**
```
docker compose down -v
```
All 4 containers, all 3 named volumes (`postgres_data`, `workspaces`,
`artifacts`), and the network were removed cleanly.

**Test suite / lint / types (after removing the `.env` used for the compose
run — see Concerns):**
```
.venv/bin/pytest -m "not smoke" -q   → 120 passed, 1 deselected, 1 warning
.venv/bin/ruff check src tests       → All checks passed!
.venv/bin/mypy --strict src/aicom/domain → Success: no issues found in 4 source files
```

## Concerns / what could not be fully verified

- **`.env` in the repo root breaks pytest.** `Settings.model_config` uses
  `env_file=".env"` with `env_prefix="AICOM_"`, but pydantic-settings does
  not silently ignore non-`AICOM_`-prefixed keys that are physically present
  in a `.env` file at cwd — it raised `extra_forbidden` on `postgres_password`
  / `postgres_db` (the plain `POSTGRES_*` vars docker-compose needs) the
  first time I ran the test suite with the compose `.env` still sitting in
  the repo root. I deleted `.env` before the final pytest/ruff/mypy run and
  added `.env` to `.gitignore`. Operators must be aware: don't run the test
  suite from a directory containing a live `.env` with non-`AICOM_` keys, or
  scope the compose `.env` file elsewhere / keep it out of the repo root
  when running tests locally.
- **The worker's actual `claude` subprocess execution was not exercised
  end-to-end** — no task was queued through the REST API during the compose
  verification, so the worker loop only exercises its idle-poll path here,
  not a real `claude` CLI invocation against a mounted, authenticated
  `~/.claude`. The image-level check (`claude --version` inside the
  container) confirms the binary is present and runnable, but not a full
  authenticated run.
- Image size (1.26GB) is dominated by the Node.js/npm layer for the Claude
  CLI; this is expected given the CLI is an npm package, not something
  easily reducible further without vendoring only the CLI's runtime bits.
