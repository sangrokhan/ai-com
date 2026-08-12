<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-11 | Updated: 2026-08-11 -->

# console

## Purpose

Derives what the read-only web console shows from the database, and serves it two ways:
a polled snapshot and a server-sent-events stream of deltas. No write path — this module
never mutates state, only reads it.

## Key Files

| File | Description |
|------|--------------|
| `state.py` | `build_snapshot`, `AgentView`, `ConsoleSnapshot`, `AGENT_STATUSES` |
| `routes.py` | `make_console_router(sessions, settings)` — `GET /console/state`, `GET /console/stream` |

## Public Interface

```python
# state.py
AGENT_STATUSES: tuple[str, ...]  # ("waiting", "working", "paused", "failed", "idle")

@dataclass(frozen=True, slots=True)
class AgentView:
    agent_id: str
    name: str
    status: str
    run_id: str | None
    activity: str | None

@dataclass(frozen=True, slots=True)
class ConsoleSnapshot:
    generated_at: str
    paused_until: str | None
    pending_approvals: int
    agents: list[AgentView]

def build_snapshot(session: Session, *, now: datetime) -> ConsoleSnapshot: ...

# routes.py
def snapshot_payload(snapshot: ConsoleSnapshot) -> dict: ...
def make_console_router(sessions: sessionmaker[Session], settings: Settings) -> APIRouter: ...
```

## For AI Agents

### `build_snapshot` is pure given a session

`build_snapshot(session, now=...)` takes `now` as a parameter rather than reading the
clock, and does nothing but `SELECT`s against `session`. It has no side effects, opens no
transaction, and commits nothing. That makes every status rule testable against a real
Postgres session without HTTP, a browser, or a fixed clock — see `tests/console/` for the
per-status coverage.

### Status priority order: waiting → working → paused → failed → idle

`_agent_view` in `state.py` checks these in a fixed order and returns on the first match;
an agent's status is never a combination:

1. **waiting** — has a run `AWAITING_APPROVAL`. Outranks everything: a human being asked
   for a decision matters more than anything merely happening.
2. **working** — has a run `RUNNING`. `activity` is derived from that run's newest
   `Event` (`_activity`): `tool_use` → `"using {tool}"`, `assistant` → `"thinking"`,
   `result` → `"wrapping up"`, anything else → the event type with underscores replaced
   by spaces, or `"starting up"` if the run has no events yet.
3. **paused** — the system is globally LLM-paused (`SystemState.llm_paused_until` is in
   the future) *and* the agent has a `QUEUED` run. A global pause outranks a stale past
   failure, since the agent isn't actually failed, it's blocked.
4. **failed** — the agent's most recently *started* terminal run (by `started_at`, not
   `ended_at` — a crashed run may have no `ended_at` but always has a `started_at` from
   `claim_next_queued`) ended in `FAILED` or `TIMED_OUT`.
5. **idle** — none of the above.

### SSE stream behaviour

`GET /console/stream` recomputes the full snapshot every `settings.sse_interval_seconds`
(via `asyncio.to_thread`, since it's a blocking DB call) and only pushes a `data:` event
when the payload actually changed since the previous tick; otherwise it sends a
`: keep-alive` comment line. A snapshot-build exception inside the loop is logged and
skipped for that tick — it does not close the stream, so a single bad tick self-heals on
the next interval instead of forcing the client to reconnect.

## Dependencies

### External

SQLAlchemy 2.0 · FastAPI (`APIRouter`, `StreamingResponse`).

### Internal

`aicom.domain.enums` (`RunStatus`, `ApprovalStatus`) · `aicom.store.models` (`Agent`,
`Approval`, `Event`, `Run`, `SystemState`, `Task`) · `aicom.config.Settings` for
`sse_interval_seconds`. Mounted by `aicom.inbound.app.create_app` alongside `auth/` and
`api/`; every route here requires a session cookie via `AuthMiddleware` (see
`auth/AGENTS.md`) — this module adds no exemptions of its own.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
