<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-11 | Updated: 2026-08-13 -->

# orchestrator

## Purpose
The runtime core of the system: claims queued tasks, spawns the Claude Code CLI per
run, wires in the approval-gate MCP server, streams and persists events, and decides
what happens next when a run ends. Every other module (`domain`, `store`, `executor`,
`notify`, `quota`, `gate`) is consumed here. `main.py` (one level up) is the only
caller of `Worker` and `Sweeper` in production.

## Key Files
| File | Description |
|------|-------------|
| `worker.py` | `Worker.tick()`: claim a run, build the CLI request, execute it, finalize the outcome. |
| `sweeper.py` | `Sweeper`: escalates unanswered approval reminders, recovers runs with a dead worker. |
| `scheduler.py` | `Scheduler`: turns due recurring `Schedule` rows into a `Task` + first `Run`. |
| `artifacts.py` | `commit_run_artifacts()`: copies a run's workspace files into the artifact git repo and commits. |
| `prompts.py` | `build_prompt()`: renders the task prompt, including the resume/sign-off addendum. |
| `staging.py` | `stage_previous_reports()`: copies a schedule's recent report artifacts into `workspace/previous/` before the run starts. |

## Public Interface
```python
# worker.py
GATE_TOOL: str  # f"mcp__{GATE_SERVER_NAME}__{GATE_TOOL_FUNCTION}", derived not hardcoded
MAX_ATTEMPTS = 3

def gate_server_spec() -> dict[str, object]: ...

class GatedToolMisconfiguration(Exception): ...

class Worker:
    def __init__(self, sessions: sessionmaker[Session], executor: Executor,
                 notifier: Notifier, settings: Settings, worker_id: str) -> None: ...
    def tick(self, now: datetime | None = None) -> bool: ...  # True if it did work

# sweeper.py
REMINDER_INTERVALS: tuple[timedelta, ...]  # (4h, 1d, 1d) for stage 1/2/3+

class Sweeper:
    def __init__(self, sessions: sessionmaker[Session], notifier: Notifier) -> None: ...
    def sweep_reminders(self, now: datetime) -> int: ...
    def recover_stale_runs(self, now: datetime, *, stale_after: timedelta) -> int: ...

# artifacts.py
def commit_run_artifacts(session: Session, *, run_id: uuid.UUID, workspace: Path,
                          repo: Path, label: str) -> str | None: ...  # returns commit sha or None

# prompts.py
def build_prompt(task_title: str, goal: str, resume_note: str | None) -> str: ...

# scheduler.py
class Scheduler:
    def __init__(self, sessions: sessionmaker[Session], notifier: Notifier) -> None: ...
    def tick(self, now: datetime) -> int: ...  # number of tasks created

# staging.py
PREVIOUS_DIR = "previous"
PREVIOUS_REPORT_LIMIT = 5

def stage_previous_reports(session: Session, run: Run, workspace: Path,
                            artifact_repo: Path, *, limit: int = PREVIOUS_REPORT_LIMIT) -> int: ...
    # returns how many were staged; creates no `previous/` dir when there is nothing to stage
```

## For AI Agents

### Working In This Directory

- **Every `transition()` call's return value must be checked before any side effect
  runs.** `transition()` is a conditional `UPDATE ... WHERE status = <expected>`
  (see `store/runs.py`); it returns `False` when the run moved out from under the
  caller (typically a Slack approval landing mid-execution). This exact bug has been
  fixed three times: (1) success path — an approval racing in before `_finalize`
  ran made `transition` a no-op while the code still marked the task `DONE` and sent
  a success report; (2) `_handle_failure` — both branches, since not checking would
  either create a duplicate retry run or report failure for a run actually queued to
  resume; (3) the artifact commit in `_finalize` must happen strictly AFTER the
  `applied` check, because `commit_run_artifacts` makes an irreversible git commit —
  calling it before the check risks double-committing artifacts.
- **`GATE_TOOL` is derived, never spelled out twice.** It is
  `f"mcp__{GATE_SERVER_NAME}__{GATE_TOOL_FUNCTION}"`, built from the MCP server key
  injected in `gate_server_spec()` and the `@mcp.tool()` function name in
  `aicom/gate/server.py`. Do not hardcode the resulting string anywhere; if the gate
  server or tool function is renamed, only `GATE_SERVER_NAME` /
  `GATE_TOOL_FUNCTION` need to change.
- **A gated tool in an agent's permanent `allowed_tools` is refused, not tolerated.**
  `_build_request` raises `GatedToolMisconfiguration` if `agent.gated_tools` overlaps
  `agent.allowed_tools`, before any workspace or mutation happens. Letting it through
  would make the tool permanently callable with no sign-off, silently killing the
  autonomy boundary — see spec §5.1.
- **The one-execution grant.** `_approved_gated_tool` only grants a tool on a resume
  carrying an approved decision, only when the tool named in
  `approval.payload["tool"]` is a member of `agent.gated_tools` (the payload is
  agent-supplied, so anything outside that list is refused and logged), and the
  grant is appended to `allowed_tools` for that `RunRequest` only — never persisted.
- **`_resume_flags` is per-`Worker`, in-process state.** `_finalize` reads/pops
  `self._resume_flags[run.id]`, captured at request-build time (not re-derived,
  since `RunRequest.resume_session_id` is a lossy proxy — see the comment on the
  field). `_finalize` may only be called on the same `Worker` instance that built
  the request; `tick()` is the only caller and guarantees this.
- **Heartbeat is timer-driven, not event-driven.** `_execute` starts a daemon
  `heartbeat_loop` thread ticking every `settings.worker_heartbeat_seconds`, on top
  of the `on_event` heartbeat touch, so a long silent run is not reclaimed as stale.
- **Late events from leaked reader threads are dropped.** `on_event` checks
  `done.is_set()` — the executor's reader threads are daemons that can outlive
  `_execute` if a grandchild process holds stdout open; a late write would corrupt
  the `(run_id, seq)` sequence of whatever runs that `run_id` next.
- **`Scheduler.tick` advances `Schedule.next_due_at` on both a firing and a skip.**
  `record_skip` and `claim_firing` both move the clock forward to the *next* cron
  occurrence after `now`, never leaving it at the missed slot. This is deliberate:
  if the schedule was blocked for six hours (a disabled agent, an unfinished
  cycle), the clock does not sit at the first missed slot and replay six queued
  firings once it is unblocked — it always fires at most once per tick, for the
  most recent due slot. That is also why a six-hour outage against, say, an
  hourly schedule produces exactly one firing against a freshly reset account
  limit rather than six runs racing to reuse a quota that just came back.
- **At most one pending task per schedule is the intended maximum, not a special
  case to guard against.** `has_unfinished_cycle` skips firing while the
  previous cycle's task/run is still live, so a schedule can never have more
  than one open task in flight. This is also why `Scheduler` needs no explicit
  check against the system-wide LLM pause: a paused account simply leaves the
  previous run `QUEUED`/`RUNNING`, which `has_unfinished_cycle` already treats
  as an unfinished cycle and skips.
- **Memory is staged into the run's own workspace, never granted as access to the
  artifact repository.** `_build_request` calls `stage_previous_reports` right after
  `prepare_workspace`, before the gate MCP server is wired in. The alternative —
  giving an agent `--add-dir` onto the artifact repo — was rejected by design (spec
  §4): `--add-dir` today confines each run to its own workspace, and opening the
  artifact repo would let every agent read everything every agent has ever produced,
  widening a filesystem boundary this project has been careful about. Copying the
  handful of files it actually needs into `workspace/previous/` keeps the boundary
  intact while still giving the agent its memory, at the cost of the copy being a
  snapshot rather than live.
- **A staging failure is logged and the run proceeds anyway.** `_build_request` wraps
  `stage_previous_reports` in a bare `except Exception` — the choice is deliberate
  (spec §4): losing the memory makes the agent repeat itself, which is a worse report
  but still a report; failing the run over a staging error produces nothing at all.
  The first is treated as the lesser harm.
- **`stage_previous_reports` only recognizes files whose committed `Artifact.path`
  is exactly `store.artifacts_query.REPORT_FILENAME` (`"report.md"`).** An agent whose
  persona asks it to "write your report to `report.md`" but that in practice writes to
  some other filename produces an artifact staging silently treats as not a report —
  no error, no directory, just nothing carried forward. This is not hypothetical: see
  the "Known limitation" note in `packs/AGENTS.md`, found by running the opportunity
  pack for real (S5a Task 4) — `agent.persona` is not currently included in the prompt
  at all (`prompts.py:build_prompt` only uses `Task.title`/`Task.goal`), so neither of
  the opportunity pack's two live runs wrote `report.md`, and the second run got no
  `previous/` directory as a result.

### Testing Requirements
Covered by `tests/orchestrator/test_worker.py`, `test_sweeper.py`, `test_artifacts.py`,
`test_scheduler.py`, `test_staging.py`, using `FakeExecutor`/`FakeNotifier` fixtures from
`tests/orchestrator/conftest.py` and a real Postgres via testcontainers. Run:
`.venv/bin/pytest tests/orchestrator -m "not smoke"`. Whether a scheduled agent's report
is actually *good*, and whether memory in practice stops it repeating itself, cannot be
asserted by this suite — see the "Known limitation" note above and
`.superpowers/sdd/2026-08-13-opportunity-pack/task-4-report.md` for the judgement from
running it against the real CLI.

### Common Patterns
- Every DB-mutating step opens its own `with self._sessions() as session:` block and
  commits before returning — no long-lived session spans an executor call.
- `try/finally` around `_execute` in `tick()` guarantees `_resume_flags.pop(run.id, ...)`
  runs even on an exception, so a stale flag can never leak into a later execution of
  the same run id.
- Session-safe reads via `session.get(Run, run.id, populate_existing=True)` after a
  rollback, to see the true committed state before acting again.

## Dependencies

### Internal
Imports `domain` (enums, views), `executor` (`Executor`, `RunOutcome`, `RunRequest`,
secrets, stream parsing, workspace prep), `notify` (`Notifier`), `quota` (reset-time
parsing/backoff), `store` (events, models, runs, system_state, approvals, schedules).
Imported only by `aicom/main.py` (`Worker`, `Sweeper`, `Scheduler`).

### External
`sqlalchemy` (`Session`, `sessionmaker`, `select`, `func`).

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->

## `_finalize` outcome routing

`_finalize` is called once per completed CLI execution and takes exactly one of four
paths, checked in this order:

1. **Usage-limit** (`outcome.reason is ExitReason.USAGE_LIMIT`) — handled first,
   before any status/approval check. `_handle_usage_limit` parses a reset time (or
   falls back to capped backoff), sets the global `system_state.llm_paused_until`,
   `transition()`s the run back to `QUEUED` with `attempt` unchanged (not the
   agent's fault), and notifies the operator exactly once per pause
   (`pause_notified_at(session) is None` gate). This is NOT a failure path.
2. **Approval-parked** — if a `PENDING` `Approval` exists for this run and
   `run.status is RunStatus.AWAITING_APPROVAL`, `_dispatch_approval` sends the Slack
   request and attaches the `slack_channel`/`slack_ts` ref. No `transition()` call
   here — the run already moved to `AWAITING_APPROVAL` when the agent called
   `gate.request` inside the CLI process, before it exited.
3. **Failure/retry** (`CRASHED` or `TIMEOUT`) — `_handle_failure`: if
   `run.attempt < MAX_ATTEMPTS`, `transition()`s to `SUPERSEDED` (checked!) and adds
   a new `Run` row with `attempt + 1`, carrying forward `resume_note` (but not
   `resume_pending`/`session_id` — a crashed CLI session cannot be resumed). Once
   attempts are exhausted, `transition()`s to `FAILED` or `TIMED_OUT` and sends a
   failure report. Both `transition()` calls are checked before their side effects.
4. **Success** — the default fallthrough. `transition()`s `RUNNING → SUCCEEDED`
   (checked before `commit_run_artifacts`, since that git commit is irreversible),
   then commits artifacts, flips `task.status = DONE`, and sends a success report.
