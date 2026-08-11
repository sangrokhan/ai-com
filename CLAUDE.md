# CLAUDE.md

Documentation for this repo lives in hierarchical `AGENTS.md` files. Start with
[`AGENTS.md`](AGENTS.md) at the root, then read the one in the directory you are
editing — each lists its files, its public interface with real signatures, and the
invariants that hold there.

Do not rediscover the codebase by reading every file. The `AGENTS.md` tree exists so
you do not have to.

| If you are... | Read |
|---------------|------|
| Making any non-trivial change | `docs/superpowers/specs/2026-08-10-agent-orchestrator-design.md`, then root `AGENTS.md` |
| Editing a module | That module's `AGENTS.md` |
| Writing a test | `tests/AGENTS.md` |
| Changing the schema | `alembic/AGENTS.md` — a model edit without a migration passes every test and breaks production |

## The five rules that matter most

Full context and the bugs behind each are in the root `AGENTS.md`.

1. **Check the return value of `transition()`.** It is a conditional UPDATE. Three
   separate production bugs came from treating it as if it always applied.
2. **Approvals never expire.** No TTL, no auto-deny, no status change on silence.
3. **All external spend requires sign-off, regardless of amount.** No threshold.
4. **The autonomy boundary is tool permissions and process boundaries, not prompt
   text.** Dangerous tools are absent from `--allowedTools`; the `gate` MCP server is
   the only route. A prompt asking the model to behave is not a control.
5. **No plaintext secrets in the database or on disk.** `mcp_config` holds
   `{"secret_ref": ...}` references only.

## Commands

The virtualenv is at `.venv`.

```bash
.venv/bin/pytest -m "not smoke"        # the suite (needs Docker for Postgres)
.venv/bin/ruff check src tests         # must be clean
.venv/bin/mypy --strict src/aicom/domain
```

## Working style

TDD: write the failing test, watch it fail for the right reason, then implement.
Conventional Commits. Line length 100.
