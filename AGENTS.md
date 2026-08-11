<!-- Generated: 2026-08-11 | Updated: 2026-08-11 -->

# ai-com

## Purpose

A background service that runs Claude Code CLI agents headlessly as workers to pursue
profit-generating work. Agents research, analyse, code, and draft autonomously. Four
action classes — spending money, executing orders, publishing or transmitting
externally, and contacting third parties — are unreachable to an agent except through
an MCP tool that parks its run and requires the human operator's Slack sign-off.

The operator is an approver, not a supervisor. The design goal is that the system
interrupts a human as rarely as possible, and only for things that touch the outside
world.

## Read This First

Three documents, in this order, before making a non-trivial change:

1. `docs/superpowers/specs/2026-08-10-agent-orchestrator-design.md` — the design source
   of truth. §5 is the approval gate; §7 is the four data flows.
2. This file — layout, commands, and the invariants that hold repo-wide.
3. The `AGENTS.md` in whichever directory you are editing.

## Key Files

| File | Description |
|------|-------------|
| `pyproject.toml` | Dependencies, ruff/mypy/pytest config, the `smoke` marker |
| `alembic.ini` | Migration config; the URL comes from app settings, not from here |
| `README.md` | How an operator runs the system |
| `src/aicom/config.py` | `Settings`, env prefix `AICOM_` — the full configuration surface |
| `src/aicom/main.py` | Process entrypoint for the worker + sweeper + scheduler loop |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `src/aicom/` | The package (see `src/aicom/AGENTS.md`) |
| `tests/` | Test suite (see `tests/AGENTS.md`) |
| `alembic/` | Database migrations (see `alembic/AGENTS.md`) |
| `docs/` | Spec and implementation plan (see `docs/AGENTS.md`) |

## Commands

The virtualenv lives at `.venv`; prefix commands with `.venv/bin/` or activate it.

| Command | What it does |
|---------|--------------|
| `.venv/bin/pytest -m "not smoke"` | The suite. Needs Docker — most packages spin up a real Postgres 16 via testcontainers |
| `.venv/bin/pytest -m smoke` | The one test that shells out to the real `claude` binary |
| `.venv/bin/ruff check src tests` | Lint; must be clean |
| `.venv/bin/mypy --strict src/aicom/domain` | Type check; strict, and configured for `domain/` only |
| `.venv/bin/alembic upgrade head` | Apply migrations |

## For AI Agents

### Repo-Wide Invariants

These are not style preferences. Each one has a bug behind it.

- **Every run-state transition goes through `store/runs.py:transition()`, and every
  caller must check its return value.** It is a conditional UPDATE that returns whether
  it applied. Ignoring the result caused three separate real bugs where a task was
  marked done, a duplicate retry was created, or a failure was reported for a run that
  had actually been re-queued by an incoming sign-off.
- **Approvals never expire.** There is no TTL, no auto-deny, and no status change on
  silence anywhere in the system. Silence escalates a reminder; that is its only
  consequence. Do not add an expiry, however reasonable it looks.
- **All external spend requires sign-off regardless of amount.** No threshold, no
  minimum, no auto-approve.
- **The autonomy boundary is enforced by tool permissions and process boundaries, never
  by prompt instructions.** Dangerous tools are absent from `--allowedTools`; the only
  route to a gated action is the `gate` MCP server, which parks the run and exits the
  process. A prompt asking the model to behave is not a control.
- **No plaintext secrets in the database or on disk.** `agent.mcp_config` stores
  `{"secret_ref": "<name>"}` references; values resolve to environment variables at
  spawn time, and a guard refuses to write an unsanitised config.
- **LLM usage is not a budget.** The project runs on a personal Claude subscription.
  Hitting the account limit pauses the whole system until the parsed reset time; it is
  never a run failure and never consumes an attempt.
- Timestamps are timezone-aware UTC everywhere. Pure modules take `now` as a parameter
  rather than reading the clock, which is what makes their tests deterministic.

### Working In This Repo

- Follow TDD: write the failing test, watch it fail for the right reason, then implement.
- `src/aicom/domain/` must stay pure — no I/O, no database, no subprocess — and must keep
  passing `mypy --strict`.
- A schema change needs BOTH a model edit and an Alembic migration. The test suite builds
  its schema with `create_all`, so a missing migration will not fail any test but will
  break production.
- Line length 100. Conventional Commits.

### Architecture At A Glance

```
POST /tasks ──> task + first queued run
                      │
                      ▼
        worker claims run ──> spawns `claude -p --output-format stream-json`
                      │          with --allowedTools whitelist + injected gate MCP server
                      │
         ┌────────────┼────────────┬──────────────────┐
         ▼            ▼            ▼                  ▼
     succeeded    gate called   crashed /         usage limit
         │        (parks run)   timed out         (global pause,
         │            │              │             attempt unchanged)
    artifacts     Slack sign-off  retry until
    committed     request         MAX_ATTEMPTS
                      │
              operator clicks ──> signature verified, nonce consumed
                      │
              run re-queued, resumes with --resume and the decision,
              the approved gated tool granted for exactly one execution
```

### Project Roadmap

The work is decomposed into five sub-projects. S1, S2, and S3 are shipped; the rest
are not started, though their schema seams exist.

| ID | Scope | State |
|----|-------|-------|
| S1 | Core orchestrator: domain, store, executor, worker, artifacts | Shipped |
| S2 | Slack approval bridge: gate, notify, inbound, sweeper | Shipped |
| S3 | Scheduler: cron periodic jobs | Shipped |
| S4 | Web console: sprite dashboard, persona/skill editing, SSE. `api/` is the seam | Not started |
| S5 | Profit agent packs: trading, content, freelance, opportunity research | Not started |

### Known Limitations

- **The REST endpoints have no authentication.** `POST /tasks` queues work for an
  autonomous agent. Bind to localhost or front it with real access control. Only the
  Slack webhook path is signature-verified.
- S1 ships no gated tools yet, so `agent.gated_tools` is normally empty. Before S5
  ships one, verify end to end that an agent calling `request_approval_tool` actually
  parks its run — the smoke test proves the CLI accepts the gate config, not that the
  gate server connected, because `claude` tolerates a failed MCP server.

## Dependencies

### External

SQLAlchemy 2.0 (typed ORM) · Alembic · PostgreSQL 16 · FastAPI + Uvicorn ·
`mcp` (pinned `<2.0`; 2.0 removed `mcp.server.fastmcp.FastMCP`) · `slack-sdk` ·
`pydantic-settings` · pytest + testcontainers.

The `claude` CLI binary is a runtime dependency of the worker: it spawns it per run.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
