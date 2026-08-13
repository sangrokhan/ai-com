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
- **A pack's persona is delivered to the CLI as part of "## Operating rules".**
  `orchestrator/prompts.py:build_prompt(task_title, goal, persona, resume_note)` puts
  `agent.persona` (stripped, blank line appended) at the top of the operating-rules
  section, ahead of the standard autonomy-boundary paragraph — not appended after the
  goal, and not a separate trailing section, so the agent reads what kind of agent it
  is before it starts acting on the goal. An agent with `persona == ""` (every agent
  row before this pack) gets exactly the prompt it always got: the standard rules
  paragraph is the first thing under the heading, unchanged. See "Verified against the
  live CLI" below.

### Verified against the live CLI (S5a, Task 4)

A first run of this pack surfaced that `agent.persona` was not being routed into the
prompt at all — `build_prompt` used only `Task.title`/`Task.goal`, so the persona's two
operating instructions ("write to `report.md`", "read `previous/` first") never reached
the agent, and the memory feature (`staging.py`) was consequently unreachable: neither
of two live runs wrote `report.md`, so `stage_previous_reports` (which filters on
`Artifact.path == REPORT_FILENAME`, see `store/AGENTS.md`) found nothing to stage on the
second run, which then repeated several findings from the first.

That gap is fixed (see "Working In This Directory" above). Re-run against the live CLI
after the fix, twice: both runs wrote `report.md`; the second run's workspace received a
`previous/00-<run-id>.md` directory containing the first report; and the second report
opened with "**Nothing has changed on this beat since my last report**" and spent the
rest of the file on clearly-labelled backfill and open items carried forward from the
first report, rather than repeating any of its findings. Full transcripts and both
report texts: `.superpowers/sdd/2026-08-13-opportunity-pack/task-4-report.md`.

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
