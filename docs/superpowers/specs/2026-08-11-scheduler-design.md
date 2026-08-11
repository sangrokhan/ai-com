# Scheduler (S3) — Design Spec

Date: 2026-08-11
Status: Approved for planning
Scope: S3 — periodic scheduling for the agent orchestrator
Builds on: `2026-08-10-agent-orchestrator-design.md` (S1 + S2, shipped)

---

## 1. Purpose

S1 and S2 gave the system a worker that executes agent tasks and a Slack gate that
requires human sign-off before money or publication. But a task only exists because
something created it, and today the only creator is a human calling `POST /tasks`.

S3 makes the system self-starting: recurring definitions that fire on a cron schedule
and create tasks on their own. This is what turns the orchestrator from a request
handler into the always-on background service the project set out to build.

## 2. Key decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Where recurrence lives | A new `schedule` entity, not a field on `task` | A task stays a single unit of work with a single outcome; a schedule is a recurring definition. Conflating them makes `task.status` meaningless and hides whether `run.attempt` means a retry or a new cycle |
| Missed firings | Skip, with a single catch-up | The system pauses for hours by design when the LLM account limit is hit. Replaying every missed slot on wake would burn the freshly-reset limit immediately |
| Overlapping firings | Skip while the previous cycle is unfinished | A sign-off can sit for days; without this, identical proposals pile up in Slack |
| Cron evaluation | `croniter`, with an IANA timezone per schedule | DST and leap years fail silently in hand-rolled implementations, and "9am at market open" must not drift by an hour twice a year |
| Pause interaction | None — no explicit check | Falls out of the overlap rule; see §4.3 |

## 3. Data model

### 3.1 New entity: `schedule`

- `id` (uuid, application-side)
- `agent_id` (FK → `agent`)
- `name` (unique per agent — the operator-facing label)
- `enabled` (bool, default true)
- `cron` (5-field expression), `timezone` (IANA string, e.g. `Asia/Seoul`)
- `title_template`, `goal_template` (markdown, copied onto each task it creates)
- `next_due_at` (timestamptz — the firing clock and the concurrency token)
- `last_fired_at`, `last_skipped_at`, `last_skip_reason` (nullable)
- `created_at`

### 3.2 Changes to `task`

- Add `schedule_id` (FK → `schedule`, nullable — a manually created task has none).
- **Drop `task.schedule`.** That column was reserved in S1 and never read; the recurrence
  definition now lives in its own entity.

One Alembic migration covers both.

### 3.3 Timezone handling

`next_due_at` is stored in UTC like every other timestamp in the system. The `timezone`
column is used only when computing the next occurrence: the cron expression is evaluated
in the schedule's local time, then converted to UTC for storage. An operator writing
`0 9 * * 1-5` with `Asia/Seoul` gets 09:00 Seoul time year-round, not a fixed UTC offset.

## 4. Firing semantics

### 4.1 The tick

```
select enabled schedules where next_due_at <= now
for each, independently:
    if an unfinished task exists for this schedule:
        record skip (reason: previous cycle unfinished) and advance the clock
    else:
        create task + its first queued run, in one transaction
        set last_fired_at = now
    next_due_at = cron.next(after=now)          # from NOW, not from the missed slot
```

"Unfinished" means the schedule's most recent task is in a non-terminal state — its
`task.status` is `open` or `blocked`, or any of its runs is still `queued`, `running`, or
`awaiting_approval`.

Task creation follows the same invariant as `POST /tasks`: the task and its first queued
run are created in one transaction. A task with no run is invisible to the worker and
simply never happens.

### 4.2 Why the clock advances from `now`

Computing `next_due_at` from the current time rather than from the missed slot is what
produces "skip with a single catch-up" without any backlog bookkeeping. After a six-hour
pause the schedule fires once on wake, then resumes its normal cadence. Nothing
accumulates and nothing needs draining.

The clock advances on a skip as well as on a firing. A schedule blocked behind a
week-long sign-off does not build up a queue of overdue slots to work through when the
approval finally lands.

### 4.3 Why no pause check is needed

While the system is paused for a usage limit, a fired task sits in `queued`. That task is
by definition unfinished, so every subsequent firing of that schedule is skipped. At most
one pending task per schedule can exist at any time, whether the pause lasts ten minutes
or ten hours.

The consequence, and it is deliberate: a schedule whose sign-off goes unanswered for a
week does not run for that week. The alternative — letting cycles accumulate behind a
pending approval — produces a pile of identical proposals in Slack and a burst of work on
approval. Being blocked is the honest signal.

### 4.4 Concurrency

A firing is claimed with a conditional UPDATE on the clock value the tick observed:

```sql
UPDATE schedule SET next_due_at = :computed, ...
WHERE id = :id AND next_due_at = :observed
```

Only one caller can win, so two scheduler instances cannot double-fire the same slot.
This is the same discipline every run-state transition already follows: the affected-row
count is the authority, and the caller must check it before treating the firing as its own.

## 5. Components

| Path | Responsibility | Notes |
|------|----------------|-------|
| `src/aicom/domain/cron.py` | `next_fire(cron, timezone, after) -> datetime`, `validate_cron(cron, timezone) -> None` | Pure: no I/O, no clock reads. `mypy --strict` applies |
| `src/aicom/store/schedules.py` | `due_schedules`, `claim_firing`, `record_skip`, `latest_task_unfinished`, CRUD helpers | Conditional-update discipline as in `store/runs.py` |
| `src/aicom/orchestrator/scheduler.py` | `Scheduler(sessions, notifier)` with `tick(now) -> int` returning the number of tasks created | Per-schedule isolation as in `orchestrator/sweeper.py` |
| `src/aicom/api/routes.py` | Schedule CRUD endpoints | Mounted on the existing router |
| `src/aicom/main.py` | Calls `scheduler.tick()` in the existing worker loop | Alongside the sweeper |

`domain/cron.py` stays pure so the calendar logic — the part most likely to be subtly
wrong — is testable without a database or a clock.

## 6. HTTP API

| Endpoint | Behaviour |
|----------|-----------|
| `POST /schedules` | Create. Validates the cron expression and timezone up front, and that the agent exists and is enabled. 400 on an invalid expression, naming the problem |
| `GET /schedules` | List, with `next_due_at` and the last firing/skip |
| `PATCH /schedules/{id}` | Enable, disable, or change the cron/templates. Changing the cron recomputes `next_due_at` from now |
| `DELETE /schedules/{id}` | Remove. The `task.schedule_id` FK is declared `ON DELETE SET NULL`, so the tasks it created survive with their runs, events, and artifacts intact and simply lose the back-link. Deleting a schedule must never delete history |

These endpoints inherit the existing REST surface's lack of authentication (see the S1
spec's known limitations). `POST /schedules` is a standing instruction to an autonomous
agent, so it is at least as sensitive as `POST /tasks`.

## 7. Error handling

| Failure | Handling |
|---------|----------|
| Cron expression no longer parses | Disable the schedule, record the reason, notify the operator. A single bad expression must never stop the scheduler loop |
| Agent disabled or deleted | Skip with that reason recorded; do not disable the schedule (the operator may re-enable the agent) |
| Task creation fails mid-tick | That schedule's transaction rolls back and the loop continues; the clock is not advanced, so the next tick retries |
| Notifier raises | Logged, does not abort the tick — same isolation the sweeper already uses |

Every schedule is processed in its own try/commit. One failing schedule cannot starve the
others, which is the failure mode the sweeper's batch-commit bug produced in S2.

## 8. Testing strategy

1. **`domain/cron.py`** — pure unit tests: ordinary advancement, DST spring-forward and
   fall-back in a non-UTC zone, invalid expressions, invalid timezone names.
2. **`store/schedules.py`** — against real Postgres: `claim_firing` returns false when the
   observed clock has moved (proving two schedulers cannot both fire a slot).
3. **`orchestrator/scheduler.py`** — against real Postgres with a `FakeNotifier`: a due
   schedule creates exactly one task and one queued run; an unfinished previous cycle
   causes a skip with the reason recorded; a schedule overdue by many cycles fires once
   and lands on the next normal slot; a schedule with a broken cron is disabled and the
   operator notified; one failing schedule does not prevent the others from firing.
4. **API** — create/list/patch/delete, and that an invalid cron is rejected at creation
   rather than at firing time.

## 9. Out of scope

- Backfill or replay of historical slots — §4.2 is a deliberate decision against it.
- Per-schedule catch-up policy (`schedule.catchup`). One policy until a second is needed.
- Sub-minute schedules. The worker loop's poll interval bounds resolution.
- Authentication for the new endpoints — inherited from S1 and tracked there.
