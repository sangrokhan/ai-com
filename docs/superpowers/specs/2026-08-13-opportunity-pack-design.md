# Opportunity Research Pack (S5a) — Design Spec

Date: 2026-08-13
Status: Approved for planning
Scope: S5a — the first agent pack
Builds on: S1+S2 (orchestrator and Slack gate), S3 (scheduler). S4a (console) is independent.

---

## 1. Purpose

Everything built so far is scaffolding. The orchestrator runs agents, gates their dangerous
actions behind human sign-off, starts itself on a schedule, and shows its state on a
screen — but no agent has ever been given a job worth doing.

S5a is the first pack: an agent that watches a defined beat and reports what changed. It is
deliberately the cheapest and least dangerous of the four planned packs, chosen so the
platform can be exercised end to end before anything touches money.

**This pack does not earn money.** It produces "this looks worth doing" reports; acting on
them is a human's job, or a later pack's. Reaching the project's stated goal of a
profitable system still requires the trading or content pack. S5a is how we find out
whether the machinery holds up under a real recurring job first.

## 2. What a pack is

Not a new subsystem. A pack is **data plus a thin piece of wiring**: one `agent` row and
one or more `schedule` rows. There is no agent-creation UI yet — that is S4b — so a pack
ships as a seed command that upserts its rows, re-runnable without creating duplicates.

The only production code this pack needs is described in §4.

## 3. What the agent does

A beat is a standing instruction: *these sources, this subject, report what is new,
changed, or gone*. One beat is one `schedule`, and the beat lives entirely in that
schedule's `goal_template`. Adding a beat means adding a schedule — no new table, no new
concept.

Agent definition:

- `allowed_tools`: `WebSearch`, `WebFetch`, `Read`, `Write`, `Glob`, `Grep`. No `Bash`.
- `gated_tools`: empty. This pack spends nothing, publishes nothing, and contacts nobody,
  so it never reaches the approval gate.
- `persona`: instructs the agent to read its previous reports first, to report only what
  is new, changed, or gone, and — the part that matters most — **to say plainly when
  nothing happened**. Without that permission an agent asked daily for findings will
  manufacture them.

Output is a single `report.md` in the run's workspace. The existing machinery takes it from
there: it is committed to the artifact repository, and the run report reaches Slack with a
short summary while the full text stays in the repo.

## 4. Memory: previous reports, staged into the workspace

A monitoring agent that cannot remember what it already said will re-report the same
things every morning, which makes the whole schedule worthless.

The obvious approach — give the agent read access to the artifact repository — is
**rejected**. Today `--add-dir` confines each run to its own workspace; opening the
artifact repo would let every agent read everything every agent has ever produced, and
would widen a boundary this project has been careful about.

Instead, **the worker stages the recent reports into the workspace before spawning**: when
a run belongs to a schedule, its most recent prior reports for that same schedule are
copied into `previous/` inside the run's own workspace. The agent finds them where it
already has access. No new tool permissions, no widened filesystem scope, and the
agent knows exactly what it has already said.

This is the only production change the pack requires: one step in the worker's request
building, plus the query that finds a schedule's recent reports.

Rules:

- Only runs that have a `schedule_id` get a `previous/` directory. A manually created task
  gets nothing, because there is no series for it to belong to.
- A bounded number of the most recent reports, newest first, so a year-old schedule does
  not eventually stage hundreds of files into every run.
- A schedule with no prior reports gets no directory rather than an empty one — the agent
  should not have to distinguish "nothing yet" from "staging is broken".
- Staging failures are logged and the run proceeds. A missing memory makes the agent
  repeat itself; a failed run produces nothing at all. The first is the lesser harm.

## 5. Components

| Path | Responsibility |
|------|----------------|
| `src/aicom/packs/__init__.py` | Pack registry — name to definition |
| `src/aicom/packs/opportunity.py` | This pack's agent and schedule definitions as data |
| `src/aicom/packs/seed.py` | `python -m aicom.packs.seed <pack>` — idempotent upsert |
| `src/aicom/store/artifacts_query.py` | Finds a schedule's most recent report artifacts |
| `src/aicom/orchestrator/worker.py` (modify) | Stages `previous/` when the run has a schedule |

Pack definitions are plain data so a second pack is a new file rather than a new mechanism.

## 6. Error handling

| Failure | Handling |
|---------|----------|
| Seed run twice | Upsert by agent name and by (agent, schedule name); no duplicates, existing rows updated |
| Seed names an unknown pack | Exit non-zero listing the known packs |
| No prior reports exist | No `previous/` directory; the agent treats it as a first run |
| A prior report file is missing from disk | Skip it, log, stage the rest |
| Staging raises | Log and run anyway — see §4 |
| The agent finds nothing to report | It says so; an empty report is a valid result, not a failure |

## 7. Testing

- Seed: running it twice leaves one agent and one schedule per definition, and the second
  run updates rather than duplicates.
- The recent-reports query: returns only artifacts belonging to the given schedule, newest
  first, bounded, and never leaks another schedule's or another agent's reports.
- Staging: a scheduled run gets `previous/` with the expected files; a manual run gets
  none; no prior reports means no directory; a staging failure does not fail the run.
- Not automatically tested: whether the agent writes a *good* report. That is judged by
  running it once for real and reading the output.

## 8. Out of scope

- Earning money. This pack reports; it does not act.
- The other three packs (trading, content, freelance), each of which needs its own spec.
- Structured findings storage or mechanical deduplication — the agent reads its own prose
  and decides. If that proves unreliable in practice, a `finding` table is the next step,
  but building it now would be guessing at what "the same finding" means.
- Any UI for creating or editing packs; that is S4b.
