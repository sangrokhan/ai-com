<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-11 | Updated: 2026-08-11 -->

# domain

## Purpose
Pure business logic for the orchestrator: enum vocabularies, the `Run` state machine, and small frozen dataclasses used to hand data across boundaries (e.g. to Slack, to workers). No I/O, no DB session, no subprocess call may appear in this directory. `mypy --strict` is configured for this directory only and must pass.

## Key Files
| File | Description |
|------|-------------|
| `enums.py` | `StrEnum` definitions: `RunStatus`, `TaskStatus`, `ApprovalStatus`, `ApprovalKind`, `ExitReason`, `TaskOrigin`. |
| `transitions.py` | Legal `RunStatus` transition table plus `can_transition` / `assert_transition`. |
| `views.py` | Frozen dataclasses (`ApprovalView`, `RunReport`, `DispatchRef`) for passing run/approval data without exposing ORM models. |
| `cron.py` | Pure cron arithmetic: validates a cron expression/timezone pair and computes the next fire time. |

## Public Interface
```python
# transitions.py
TERMINAL: frozenset[RunStatus]  # SUCCEEDED, FAILED, TIMED_OUT, CANCELLED, SUPERSEDED

class IllegalTransition(Exception):
    def __init__(self, frm: RunStatus, to: RunStatus) -> None: ...

def can_transition(frm: RunStatus, to: RunStatus) -> bool: ...
def assert_transition(frm: RunStatus, to: RunStatus) -> None:  # raises IllegalTransition

# views.py
@dataclass(frozen=True, slots=True)
class ApprovalView:
    approval_id: uuid.UUID
    nonce: str
    kind: ApprovalKind
    agent_name: str
    task_title: str
    proposal: str
    payload: dict[str, Any]
    slack_channel: str | None = None
    slack_ts: str | None = None

@dataclass(frozen=True, slots=True)
class RunReport:
    run_id: uuid.UUID
    agent_name: str
    task_title: str
    status: RunStatus
    summary: str
    cost_usd: float | None = None

@dataclass(frozen=True, slots=True)
class DispatchRef:
    channel: str
    ts: str

# cron.py
class InvalidCron(Exception): ...

def validate_cron(cron: str, timezone: str) -> None: ...  # raises InvalidCron
def next_fire(cron: str, timezone: str, after: datetime) -> datetime: ...  # UTC
```

## For AI Agents

### Working In This Directory
- **Import-cycle-free, and it must stay that way.** `domain` is the bottom layer: `store`, `quota`, and everything else import from it, never the reverse. Do not add an import from `aicom.store`, `aicom.quota`, or any I/O-touching package here.
- **No I/O.** No `sqlalchemy.orm.Session`, no `open()`, no `subprocess`, no network calls. If a function needs the current time, take it as a `now: datetime` parameter (see `quota/reset.py` for the pattern used elsewhere in this codebase) rather than calling `datetime.now()`.
- **The transition table is the single source of truth for legality**, but it does not enforce anything by itself — `store/runs.py`'s `transition()` is what makes a transition atomic against the DB. `assert_transition` here only tells you whether a transition *would* be legal in memory.
- Legal transitions (`_ALLOWED` in `transitions.py`):
  - `QUEUED → RUNNING | CANCELLED`
  - `RUNNING → SUCCEEDED | FAILED | TIMED_OUT | AWAITING_APPROVAL | QUEUED | CANCELLED | SUPERSEDED`
  - `AWAITING_APPROVAL → QUEUED | CANCELLED` (never straight to `RUNNING` — a resumed run must re-enter the queue and be claimed like any other run)
  - Terminal states (`TERMINAL`): `SUCCEEDED`, `FAILED`, `TIMED_OUT`, `CANCELLED`, `SUPERSEDED` — no outgoing edges exist for these.
- `RUNNING → QUEUED` is overloaded and serves two distinct scenarios: (1) a worker hits the account usage limit mid-run and requeues its own run to retry later, and (2) the stale-run sweeper (see `store/runs.py::stale_running_runs`) recovers a run whose heartbeat went silent. Both land the run back in `QUEUED` with no other domain-level distinction.
- `SUPERSEDED` means a *retry* replaced this attempt (a new `Run` row with a higher `attempt` was created for the same task) — it is not used for an operator cancelling work. Operator/administrative cancellation is always `CANCELLED`. Do not conflate the two when writing new call sites.

### Testing Requirements
- `tests/domain/test_transitions.py` covers `can_transition` / `assert_transition` / `TERMINAL`. There is no test file yet for `views.py` (plain dataclasses, low risk).
- Run with `.venv/bin/pytest tests/domain` (no Docker, no external services needed).
- Run `mypy --strict src/aicom/domain` before committing — this directory is the one place in the repo held to strict mode.

### Common Patterns
- Enums are `StrEnum` throughout, so a `RunStatus` compares equal to its string value — useful when values cross a JSON/DB boundary.
- Dataclasses in `views.py` are `frozen=True, slots=True`: treat them as immutable value objects, construct a new instance rather than mutating.

## Dependencies

### Internal
- Imports nothing from `aicom`.
- Imported by: `aicom.store` (models, runs, approvals use `RunStatus`/`ApprovalKind`/etc. and `assert_transition`), and by higher layers (worker, gate, Slack integration) that consume `views.py` dataclasses.

### External
- Python standard library only (`enum`, `dataclasses`, `uuid`, `typing`). No third-party dependency.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
