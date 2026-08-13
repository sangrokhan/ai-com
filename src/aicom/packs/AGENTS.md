<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-13 | Updated: 2026-08-13 -->

# packs

## Purpose

Agent packs: an `agent` and its `schedule`(s), shipped as plain data plus one idempotent
seed command. **A pack is not a subsystem.** There is no pack-specific runtime code, no
new table, and no new mechanism — a pack is consumed entirely by the orchestrator machinery
that already exists (`store.models.Agent`/`Schedule`, `Scheduler`, `Worker`). Adding a
second pack means adding a new definition file and a registry entry, nothing more.

`opportunity.py` is the first (and currently only) pack: `scout`, an agent that watches a
defined beat and reports what is new, changed, or gone. It spends nothing, publishes
nothing, and contacts no one, so it never reaches the approval gate.

## Key Files

| File | Description |
|------|-------------|
| `types.py` | `Pack`, `PackAgent`, `PackSchedule` — the shape of a pack; `UnknownPack` |
| `opportunity.py` | The opportunity-research pack's agent persona and one schedule, as data |
| `seed.py` | `python -m aicom.packs.seed <pack>` — idempotent upsert of a pack's rows |
| `__init__.py` | `PACKS` registry (`name -> Pack`) and `get_pack(name)` |

## Public Interface

```python
# types.py
class UnknownPack(Exception): ...

@dataclass(frozen=True, slots=True)
class PackAgent:
    name: str
    persona: str
    allowed_tools: tuple[str, ...]
    gated_tools: tuple[str, ...] = ()
    max_run_seconds: int = 1800

@dataclass(frozen=True, slots=True)
class PackSchedule:
    name: str
    cron: str
    timezone: str
    title_template: str
    goal_template: str

@dataclass(frozen=True, slots=True)
class Pack:
    name: str
    agent: PackAgent
    schedules: tuple[PackSchedule, ...]

# __init__.py
PACKS: dict[str, Pack]
def get_pack(name: str) -> Pack: ...  # raises UnknownPack listing the known packs

# seed.py
def seed_pack(session: Session, pack: Pack, *, now: datetime) -> tuple[uuid.UUID, int]: ...
    # returns (agent id, schedules written); upserts, never duplicates
def main(argv: list[str]) -> int: ...  # `python -m aicom.packs.seed <pack>` entry point
```

## For AI Agents

### Working In This Directory

- **A pack is data, not a subsystem.** Do not add pack-specific branches to `Worker`,
  `Scheduler`, or any store module for a new pack. If a pack needs the orchestrator to do
  something no existing pack needs, that is a new piece of general machinery (like
  `staging.py` was for memory), reviewed and built on its own — not special-cased here.
- **The seed command is idempotent and safe to re-run.** `seed_pack` matches the agent by
  `Agent.name` and each schedule by `(agent_id, Schedule.name)`; running
  `python -m aicom.packs.seed opportunity` twice updates the same rows and prints the same
  agent id both times, it never creates a duplicate. Verified by hand:
  `.venv/bin/python -m aicom.packs.seed opportunity` run twice in a row printed the
  identical `agent <uuid>` both times.
- **Seeding never prunes.** Renaming a `PackSchedule.name` between seeds leaves the old
  row behind under its old name, still enabled and still firing — `seed_pack`'s docstring
  says so explicitly. A pack maintainer who renames a schedule must delete the stale row
  by hand (or via a future `DELETE /schedules/{id}`, see `api/AGENTS.md`).
- **`next_due_at` is set only when a schedule row is first created, never recomputed on a
  later seed.** Re-seeding after editing a schedule's `cron` does not rebase the clock
  immediately; the new cadence takes effect starting from that schedule's next natural
  firing. Recomputing on every seed would risk skipping a firing that is due right now.
- **`gated_tools` must never overlap `allowed_tools`.** This is enforced at run time by
  `Worker._build_request` (`orchestrator/worker.py`), which raises
  `GatedToolMisconfiguration` and refuses to run the agent at all if the two sets
  intersect — before any workspace is created. A pack definition that lists the same tool
  in both is not a style mistake; it silently disables the entire autonomy boundary for
  that tool if it is not caught, so the worker treats it as fatal misconfiguration instead
  of tolerating it. `opportunity.py`'s `gated_tools` is `()` for exactly this reason: the
  pack has no gated action to grant, so there is nothing to accidentally overlap.
- **A pack's persona currently has no code path that delivers it to the CLI.** Read the
  "Known limitation" note below before assuming `PackAgent.persona` reaches the agent.

### Known limitation found while running this pack for real (S5a, Task 4)

`orchestrator/prompts.py:build_prompt(task_title, goal, resume_note)` builds the entire
prompt handed to the Claude CLI from the `Task.title` and `Task.goal` only.
`Worker._build_request` (`orchestrator/worker.py`) calls it exactly that way — `agent.persona`
is read nowhere in the request-building path. Concretely: `Agent.persona` is stored (seeded
by `seed_pack` from `PackAgent.persona`) but **never sent to the CLI in any run**, scheduled
or otherwise. This is a gap in the orchestrator, not in this pack's definition; it is
recorded here because running the opportunity pack for real is what surfaced it.

Effect observed against the live `claude` CLI, twice: the `scout` agent produced strong,
well-sourced reports, but ignored the two persona instructions that never reached
it — it did not write to `report.md` (each run invented its own filename instead), and,
because `stage_previous_reports` only stages artifacts whose `Artifact.path` is exactly
`report.md` (`store/artifacts_query.py:REPORT_FILENAME`), the second run got no `previous/`
directory and repeated several items from the first report's findings. See
`.superpowers/sdd/2026-08-13-opportunity-pack/task-4-report.md` for the full transcript and
judgement. Fixing this (routing `agent.persona` into the prompt) is orchestrator-layer work
outside this task's scope — do not work around it by hardcoding pack-specific behaviour
here; fix `build_prompt`/`_build_request` once, for every pack.

### Testing Requirements

`tests/packs/test_seed.py` (with `tests/packs/conftest.py`) covers `seed_pack`: running it
twice leaves one agent and one schedule, the second run updates rather than duplicates, and
an unknown pack name is rejected. Run: `.venv/bin/pytest tests/packs -m "not smoke"`.
Whether a pack's agent writes a *good* report is not something this test suite can assert —
see "Testing" in the design spec (§7) — it is judged by running the pack for real; that
judgement lives in the task report referenced above, not in a test file.

### Common Patterns

- Pack definitions are plain frozen dataclasses (`types.py`), imported and assembled as
  module-level constants (`opportunity.py:OPPORTUNITY`) — no factory functions, no
  runtime construction.
- `seed.py:main` defers `from aicom.config import load_settings` to inside the function
  body specifically so importing `seed_pack` (e.g. from tests) never triggers settings
  resolution; only running the CLI entry point does.

## Dependencies

### Internal

Imports `domain.cron` (`next_fire`, for computing a new schedule's first `next_due_at`),
`store.db` (`make_engine`, `session_factory`), `store.models` (`Agent`, `Schedule`).
Imported only by `python -m aicom.packs.seed` (operator-run CLI) and by
`tests/packs/test_seed.py`.

### External

`sqlalchemy` (`select`, `Session`).

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
