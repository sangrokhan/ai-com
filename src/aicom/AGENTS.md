<!-- Parent: ../../AGENTS.md -->
<!-- Generated: 2026-08-11 | Updated: 2026-08-11 -->

# aicom

## Purpose

The orchestrator package. Nine modules with deliberately narrow interfaces: a pure
domain layer at the bottom, a persistence layer above it, adapters to the outside world
(the Claude CLI, the MCP gate, Slack in and out), and an orchestration layer that joins
them. Every module is independently testable, and the two that touch the outside world
have fake implementations used by every integration test.

## Key Files

| File | Description |
|------|-------------|
| `config.py` | `Settings` (env prefix `AICOM_`) and `load_settings()` — the whole configuration surface |
| `main.py` | `run_worker_forever()` — the worker + sweeper process entrypoint |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `domain/` | Pure state machine, enums, cross-boundary view dataclasses (see `domain/AGENTS.md`) |
| `store/` | SQLAlchemy models and conditional-update repositories (see `store/AGENTS.md`) |
| `executor/` | Claude CLI spawn, stream-json parsing, secret injection, workspaces (see `executor/AGENTS.md`) |
| `gate/` | The MCP server that is the only route to a gated action (see `gate/AGENTS.md`) |
| `quota/` | Usage-limit reset-time parsing and capped backoff (see `quota/AGENTS.md`) |
| `notify/` | Slack Block Kit builders and the `Notifier` abstraction (see `notify/AGENTS.md`) |
| `inbound/` | The signature-verified Slack interaction endpoint and the FastAPI app (see `inbound/AGENTS.md`) |
| `orchestrator/` | Worker loop, sweeper, artifact commits, agent prompts (see `orchestrator/AGENTS.md`) |
| `api/` | Task/run/approval REST endpoints, the seam for the future web console (see `api/AGENTS.md`) |
| `auth/` | Password check, signed session cookie, the `AuthMiddleware` that gates every route (see `auth/AGENTS.md`) |
| `console/` | Derives and serves the read-only console snapshot (poll + SSE) (see `console/AGENTS.md`) |

## Dependency Direction

Arrows point at what a module imports. Nothing points back up.

```
                    domain/  (pure: no I/O, mypy --strict)
                       ▲
                       │
     ┌─────────────────┼──────────────────┬───────────┐
   store/          executor/           notify/      quota/
     ▲                 ▲                   ▲           ▲
     │                 │                   │           │
   gate/               └────────┬──────────┴───────────┘
     ▲                          │
     │                     orchestrator/
   inbound/  ◄──── api/         │
     ▲            ▲             │
     │            │             │
   auth/       console/         │
     ▲            ▲             │
     └────────────┴─────────────┘
                 main.py
```

`orchestrator/worker.py` is where everything meets — it is the largest module and the
one where cross-module invariants are easiest to break. `inbound/app.py` mounts `auth/`'s
middleware and router first, then `api/`'s and `console/`'s routers, onto the same
FastAPI app that serves the Slack webhook, then mounts `web/dist` (see `web/AGENTS.md`)
as a static SPA at `/` when that directory exists.

## For AI Agents

### Working In This Directory

- Respect the dependency direction above. `domain/` importing from `store/` would create
  a cycle and break the property that makes the state machine testable without a database.
- Adding a new outside-world adapter means adding a fake alongside it. `executor/fake.py`
  and `notify/fake.py` are what let the integration tests exercise real flows without a
  subprocess or a Slack token, and they are expected to stay faithful — `FakeExecutor`
  drives a real `StreamParser` rather than fabricating outcomes.
- New configuration goes in `config.py` with an `AICOM_`-prefixed env var and a default
  that is safe when unset. Do not read `os.environ` directly elsewhere.
- Two names are derived rather than duplicated: `GATE_TOOL` is built from the gate
  server's name and tool function name so they cannot drift apart. Do not hardcode the
  string.

### Testing Requirements

`.venv/bin/pytest -m "not smoke"` from the repo root. Most packages need Docker (real
Postgres 16 via testcontainers); `domain/`, `executor/`, `notify/`, and `quota/` tests do
not. See `tests/AGENTS.md` for the fixture architecture.

### Common Patterns

- Protocols (`Executor`, `Notifier`) for anything crossing a process or network boundary,
  with a real implementation and a recording fake.
- Frozen dataclasses (`RunRequest`, `RunOutcome`, `ApprovalView`, `RunReport`) for values
  crossing module boundaries — never ORM objects, which carry session state.
- Repository functions take an explicit `Session` as their first argument; they never open
  or commit one. Transaction boundaries belong to the caller.
- Pure functions take `now: datetime` rather than reading the clock.

## Dependencies

### External

SQLAlchemy 2.0 · FastAPI · `mcp` (pinned `<2.0`) · `slack-sdk` · `pydantic-settings` ·
`psycopg`. The worker additionally requires the `claude` CLI binary at runtime.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
