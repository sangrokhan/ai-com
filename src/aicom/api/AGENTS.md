<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-11 | Updated: 2026-08-11 -->

# api

## Purpose
Read/write surface for tasks, runs, events, and approvals — the seam S4 (the future
web console) will build on. It is intentionally thin: no business logic lives here,
only queries and one creation endpoint. `src/aicom/main.py` does not touch this
module directly; instead `aicom/inbound/app.py` mounts `make_router()`'s `APIRouter`
onto the same FastAPI app that serves the signature-verified Slack inbound routes.

## Key Files
| File | Description |
|------|-------------|
| `routes.py` | `make_router()`: task CRUD (create/list), run lookup, run event log, pending-approval list. |

## Public Interface
```python
class CreateTask(BaseModel):
    agent_id: uuid.UUID
    title: str
    goal: str
    parent_task_id: uuid.UUID | None = None

def make_router(sessions: sessionmaker[Session]) -> APIRouter: ...
```
Routes registered on the returned router:
- `POST /tasks` (201) — body `CreateTask`; 404 if `agent_id` or `parent_task_id`
  doesn't exist, 400 if the agent is disabled. Returns `{"id": str(task.id)}`.
- `GET /tasks` — list, newest first.
- `GET /runs/{run_id}` — 404 if missing.
- `GET /runs/{run_id}/events` — ordered by `seq`.
- `GET /approvals` — only `status == PENDING`.
- `GET /health` — `{"status": "ok"}`. Touches no database; it exists as the compose
  healthcheck for the `api` service, so keep it dependency-free or the container will
  report unhealthy whenever Postgres is merely slow to start.
- `POST /schedules` (201) — body `CreateSchedule`; 400 if the cron/timezone pair is
  invalid or the agent is disabled, 404 if `agent_id` doesn't exist. Computes the
  initial `next_due_at` via `domain.cron.next_fire`. Returns `{"id": str(schedule.id)}`.
- `GET /schedules` — list, newest first.
- `PATCH /schedules/{schedule_id}` — body `UpdateSchedule`; any of `enabled`, `cron`,
  `timezone`, `title_template`, `goal_template`. Changing `cron`/`timezone`
  re-validates and recomputes `next_due_at` from now, not from the old cadence.
- `DELETE /schedules/{schedule_id}` (204) — `task.schedule_id` is `ON DELETE SET
  NULL`, so tasks the schedule already created keep their runs/events/artifacts.

Related, one level up:
```python
# aicom/main.py
def run_worker_forever(settings: Settings | None = None) -> None: ...
```
Not part of this module's interface, but it is the process entrypoint: builds the
shared `sessionmaker`, `SlackNotifier`, `ClaudeCliExecutor`, constructs one `Worker`,
one `Sweeper`, and one `Scheduler`, then loops forever calling
`sweeper.recover_stale_runs(now, stale_after=STALE_AFTER)` (STALE_AFTER = 10 min),
`sweeper.sweep_reminders(now)`, `scheduler.tick(now)`, and `worker.tick(now)` in that
order per iteration, sleeping `settings.worker_poll_seconds` only when `tick()` did
no work. The whole loop body is wrapped in `try/except Exception` — there is no
external supervisor restarting this process, so one bad iteration (DB hiccup,
executor crash) must not stop every queued task and pending reminder from ever
being picked up again.

## For AI Agents

### Working In This Directory

- **These endpoints have NO authentication.** Anyone who can reach this router can
  create tasks, list all tasks/runs/events, and read every pending approval's
  proposal text. This is acceptable only because the four irreversible action
  classes (spend, execute order, publish/transmit, contact third party) are never
  reachable through this router — they require the signature-verified Slack path in
  `aicom/inbound/app.py` (`verify_slack_signature`, approver allowlist, single-use
  nonce). Do not add a mutating endpoint here that could touch money or external
  effects without first adding real auth.
- **`POST /tasks` creates the `Task` AND its first `Run` in one transaction.** See
  the single `with sessions() as session:` block: `session.add(task)`,
  `session.flush()`, then `session.add(Run(..., attempt=1))`, then one
  `session.commit()`. A task persisted without a run is invisible to
  `Worker.tick()` (which claims via `claim_next_queued`, which only sees `Run`
  rows) and would simply never execute. Do not split this into two transactions.
- `parent_task_id` sets `depth = parent.depth + 1`; there is no enforcement here of
  `agent.max_child_tasks_per_day` (schema-only per spec §9) — do not assume this
  endpoint rate-limits agent-spawned tasks.
- All handlers open and close their own session (`with sessions() as session:`);
  none hold a session open across a request boundary.

### Testing Requirements
Covered by `tests/api/test_routes.py` using fixtures from `tests/api/conftest.py`
(real Postgres via testcontainers, `TestClient`). Run:
`.venv/bin/pytest tests/api -m "not smoke"`.

### Common Patterns
- Handlers return plain `dict`/`list[dict]`, not Pydantic response models — fields
  are hand-picked per endpoint (e.g. `get_run` omits `workspace_path`, `session_id`).
- Enum fields are serialized with `.value` (e.g. `t.status.value`, `a.kind.value`).
- 404s raised via `HTTPException(404, "...")` for any missing FK lookup before doing
  anything else in the handler.

## Dependencies

### Internal
Imports `domain.enums` (`ApprovalStatus`), `domain.cron` (`InvalidCron`, `next_fire`,
`validate_cron`), and `store.models` (`Agent`, `Approval`, `Event`, `Run`, `Schedule`,
`Task`). Imported by `aicom/inbound/app.py`
(`from aicom.api.routes import make_router`, then `app.include_router(...)`) —
nothing in `orchestrator/` imports this module.

### External
`fastapi` (`APIRouter`, `HTTPException`), `pydantic` (`BaseModel`), `sqlalchemy`
(`select`, `Session`, `sessionmaker`).

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
