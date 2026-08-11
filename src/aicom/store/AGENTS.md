<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-11 | Updated: 2026-08-11 -->

# store

## Purpose
The persistence layer: SQLAlchemy ORM models and the query/command functions that read and write them against Postgres. This is the only place in the codebase that talks to the database. It enforces run-state legality (via `domain.transitions`) at the SQL level with conditional `UPDATE`s so concurrent workers and sweepers can never race each other into an inconsistent state.

## Key Files
| File | Description |
|------|-------------|
| `models.py` | SQLAlchemy `DeclarativeBase` models: `Agent`, `Task`, `Run`, `Event`, `Approval`, `Artifact`, `SpendLedger`, `SystemState`. |
| `db.py` | Engine/session factory helpers: `make_engine`, `session_factory`. |
| `runs.py` | Run lifecycle: claiming, state transitions, heartbeats, stale-run detection. |
| `approvals.py` | Approval creation, nonce consumption, reminder scheduling. |
| `events.py` | Append-only per-run event log writer. |
| `system_state.py` | Single-row account-wide LLM pause state. |
| `schedules.py` | Recurring-schedule queries and clock advancement: which schedules are due, claiming a firing, recording a skip, disabling a broken schedule. |

## Public Interface
```python
# db.py
def make_engine(url: str) -> Engine: ...
def session_factory(engine: Engine) -> sessionmaker[Session]: ...

# runs.py
def claim_next_queued(session: Session, *, worker_id: str, now: datetime) -> Run | None: ...
def transition(session: Session, run_id: uuid.UUID, frm: RunStatus, to: RunStatus, **fields: object) -> bool: ...
def touch_heartbeat(session: Session, run_id: uuid.UUID, now: datetime, *, worker_id: str) -> bool: ...
def stale_running_runs(session: Session, *, older_than: datetime) -> list[Run]: ...

# approvals.py
FIRST_REMINDER: dict[ApprovalKind, timedelta]
def create_approval(session: Session, *, run_id: uuid.UUID, kind: ApprovalKind, proposal: str, payload: dict, now: datetime) -> Approval: ...
def consume_nonce(session: Session, *, nonce: str, decided_by: str, approved: bool, now: datetime) -> Approval | None: ...
def pending_approvals(session: Session, *, due_before: datetime) -> list[Approval]: ...
def record_reminder(session: Session, approval: Approval, *, now: datetime, next_after: datetime) -> None: ...
def attach_slack_ref(session: Session, approval_id: uuid.UUID, channel: str, ts: str) -> None: ...

# events.py
def append_event(session: Session, run_id: uuid.UUID, seq: int, type_: str, payload: dict) -> None: ...

# system_state.py
def get_pause(session: Session) -> datetime | None: ...
def set_pause(session: Session, until: datetime, reason: str) -> None: ...
def mark_pause_notified(session: Session, at: datetime) -> None: ...
def pause_notified_at(session: Session) -> datetime | None: ...
def clear_pause(session: Session) -> None: ...

# schedules.py
def due_schedules(session: Session, *, now: datetime) -> list[Schedule]: ...
def claim_firing(session: Session, schedule_id: uuid.UUID, observed_due_at: datetime, next_due_at: datetime, **fields: object) -> bool: ...
def has_unfinished_cycle(session: Session, schedule_id: uuid.UUID) -> bool: ...
def record_skip(session: Session, schedule_id: uuid.UUID, *, now: datetime, reason: str, next_due_at: datetime) -> bool: ...
def disable(session: Session, schedule_id: uuid.UUID, *, reason: str, now: datetime) -> bool: ...
```

## For AI Agents

### Working In This Directory
- **Every run-state change MUST go through `runs.transition()`.** It is a conditional `UPDATE ... WHERE id = :id AND status = :frm` returning a `bool` rowcount — this is what makes state changes safe under concurrent workers/sweepers. Never write `run.status = X; session.add(run)` directly.
- **Callers MUST check the return value of `transition()`.** `False` means someone else already moved the run out of `frm` (lost a race) — several real bugs in this codebase came from callers ignoring this and proceeding as if the transition happened. `claim_next_queued` shows the correct pattern: check `applied`, return `None`/bail if it's `False`.
- `transition()` calls `assert_transition(frm, to)` from `aicom.domain.transitions` first, so an illegal transition raises `IllegalTransition` before any SQL runs — it does not silently no-op.
- **Identity-map staleness gotcha:** a bulk `UPDATE ... execution_options(synchronize_session="fetch")` (used by `transition`) only syncs the attributes it touched on any already-loaded ORM instance in the session's identity map. A later plain `session.get(Run, id)` in the *same* session can therefore return an object missing fields the UPDATE set (e.g. `worker_id`, `started_at`). Always pass `populate_existing=True` to `session.get(...)` after such an update — see `claim_next_queued` for the pattern — or you'll read stale data back.
- `claim_next_queued` uses `SELECT ... FOR UPDATE SKIP LOCKED` to pick one `QUEUED` run: this lets many worker processes poll the same table concurrently without blocking on each other or double-claiming a row.
- `touch_heartbeat` filters on **both** `status == RUNNING` and `worker_id == worker_id`. Both matter: without the worker filter, a worker whose run the sweeper already recovered (moved back to `QUEUED`) keeps stamping `heartbeat_at` from its own zombie ticker even after a different worker claims the run, making the new attempt look alive regardless of its real state; without the status filter, the same ticker would stamp a run the sign-off race already returned to `QUEUED`. The `bool` return tells the caller "still mine" vs. "no longer mine" — check it.
- **Approvals have no expiry column by design.** There is no `expires_at` on `Approval`; sign-off is meant to remain valid indefinitely until acted on (approvals never expire per system design). Do not add expiry logic without confirming this is an intentional product change, not an oversight.
- **`Agent.mcp_config` (JSONB) stores only `{"secret_ref": ...}`-shaped references, never plaintext secrets.** Never write a raw credential/token/API key into this column.
- `consume_nonce` is the only way to resolve an `Approval` — it's a conditional `UPDATE ... WHERE nonce = :nonce AND consumed_at IS NULL`, so a duplicate Slack click (or retry) safely returns `None` the second time rather than double-processing.
- **Callers MUST check the return value of `claim_firing`, for the same reason as `transition()`.** It is a conditional `UPDATE ... WHERE next_due_at = :observed_due_at`, so it returns `False` when another scheduler already won this slot; treating that as a successful claim would double-fire the schedule.

### Testing Requirements
- `tests/store/test_models.py`, `tests/store/test_runs.py` (with `tests/store/conftest.py` providing the fixture). No dedicated test file yet for `approvals.py`, `events.py`, or `system_state.py`.
- These tests spin up a real Postgres 16 via `testcontainers` — **Docker must be running**.
- Run with `.venv/bin/pytest tests/store`.

### Common Patterns
- Every write function takes `session: Session` as the first parameter and lets the caller own the transaction/commit — none of these functions call `session.commit()` themselves.
- Time is always passed in explicitly as `now: datetime` (never `datetime.now()` inside store functions), matching the pure-time-injection convention used in `domain` and `quota`.
- `_row(session)` in `system_state.py` is a get-or-create helper for the singleton `id=1` row — follow this pattern if you add another singleton table.

## Dependencies

### Internal
- Imports from `aicom.domain` (`enums`, `transitions`) for status types and `assert_transition`.
- Imported by the worker/scheduler/gate layers (outside this directory) that need to claim runs, create approvals, and log events.

### External
- `sqlalchemy` (ORM + Core `select`/`update`), `sqlalchemy.dialects.postgresql` (`ARRAY`, `JSONB`, `UUID`).
- Tests depend on `testcontainers` for a real Postgres 16 instance.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
