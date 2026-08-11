<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-11 | Updated: 2026-08-11 -->

# docs

## Purpose
Design and planning documentation for the agent orchestrator, under the
`superpowers/` workflow convention: `specs/` holds the design source of
truth, `plans/` holds the task-by-task implementation record derived from
it. The project is decomposed into five sub-projects, S1 through S5; only
S1 and S2 are covered by the current spec and have shipped.

## Key Files
| File | Description |
|------|-------------|
| `superpowers/specs/2026-08-10-agent-orchestrator-design.md` | Design spec for S1 (core orchestrator: task/run/event/approval domain, Postgres schema, Claude Code CLI executor, stream-json parser) + S2 (Slack approval bridge: Block Kit dispatch, signature-verified inbound, approval resolution, reminders). Source of truth for architecture — read before changing schema, executor, or gate behavior. |
| `superpowers/plans/2026-08-10-agent-orchestrator.md` | Task-by-task implementation plan/record for building out the spec. |

## Subdirectories
| Dir | Contents |
|-----|----------|
| `superpowers/specs/` | Design documents — the "why" and "what," written before implementation. Currently one file (S1+S2). |
| `superpowers/plans/` | Implementation plans/records — the "how it was built," task breakdown against a spec. Currently one file (S1+S2). |

## For AI Agents

### Working In This Directory
- Treat `specs/` as authoritative for architecture questions (schema shape,
  component boundaries, deferred-vs-implemented scope) and `plans/` as the
  historical record of how that spec was executed in tasks — don't infer
  scope decisions from the plan when the spec disagrees; the spec is the
  source of truth.
- Sub-project scope, per the spec's own breakdown table:
  - **S1** — core orchestrator. Shipped.
  - **S2** — Slack approval bridge. Shipped (built together with S1, "because
    the approval loop is what makes the system usable at all").
  - **S3** — scheduler (cron periodic jobs, always-on operation, retry/timeout
    sweeper). Not started; `task.schedule` column exists in the schema but is
    ignored until S3 lands.
  - **S4** — web console (sprite dashboard, persona/skill/MCP editing, SSE
    live stream). Not started; depends on S3.
  - **S5** — profit agent packs (trading / content / freelance /
    opportunity-research agents). Not started; the spec notes S5 gets its own
    spec document per agent pack, separate from this one.
- S1 leaves explicit extension seams for later sub-projects — don't treat
  their absence as missing functionality: `EventBus` (S4 SSE and S2
  notifications both subscribe to it), `ApprovalGate` (S2 implements the
  Slack channel on top of it), `AgentConfig` (S1 only reads persona/tools/MCP
  from the DB; S4 adds the write UI).

### Testing Requirements
- These are documentation files; no build/test step. Keep them accurate to
  the shipped code — if a spec claim and the actual implementation diverge,
  trust the code and flag the doc as stale rather than silently following it.

### Common Patterns
- Filenames are dated (`YYYY-MM-DD-<slug>.md`), one spec paired with one plan
  per sub-project increment.

## Dependencies

### Internal
- Describes `src/aicom/*` (domain, store, executor, gate, notify, inbound,
  orchestrator, api) and the `tests/` and `alembic/` trees documented
  elsewhere in this repo.

### External
- None (Markdown only).

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
