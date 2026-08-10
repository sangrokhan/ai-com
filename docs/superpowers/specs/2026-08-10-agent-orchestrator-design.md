# Agent Orchestrator (S1 + S2) — Design Spec

Date: 2026-08-10
Status: Approved for planning
Scope: S1 (core orchestrator) + S2 (Slack approval bridge)

---

## 1. Purpose

Build a platform that runs AI agents as background workers to pursue profit-generating
work. The platform is domain-agnostic; profit domains (trading, content, freelance,
opportunity research) are pluggable agent packs specified separately.

The human operator is an approver, not a supervisor. Agents work autonomously and only
interrupt the operator to get sign-off on actions that touch the outside world.

## 2. Project decomposition

| ID | Sub-project | Contents | Order |
|----|-------------|----------|-------|
| S1 | Core orchestrator | task/run/event/approval domain, Postgres schema, Claude Code CLI executor, stream-json parser | This spec |
| S2 | Approval bridge | Slack Block Kit dispatch, signature-verified inbound, approval resolution, reminders | This spec |
| S3 | Scheduler | cron periodic jobs, always-on background operation, retry/timeout sweeper | Next |
| S4 | Web console | sprite dashboard, persona/skill/MCP editing, SSE live stream | After S3 |
| S5 | Profit agent packs | trading / content / freelance / opportunity-research agents | Separate spec each |

S1+S2 are built together because the approval loop is what makes the system usable at all.
S1 leaves explicit seams for S3 and S4:

- `EventBus` — every run event is published; S4 SSE and S2 notifications subscribe.
- `ApprovalGate` — creates and resolves approvals; S2 implements the Slack channel.
- `AgentConfig` — persona/tools/MCP stored as DB records; S1 reads only, S4 adds the write UI.

## 3. Key decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Agent runtime | Claude Code CLI headless spawn (`claude -p --output-format stream-json`) | Reuses existing skill/plugin ecosystem |
| Persistence | Postgres as source of truth + git repo for artifacts | Easy queries, event streams, time-series fit for PnL |
| Notification channel | Slack | Threads per task, button approvals, mobile push for free |
| Autonomy boundary | Everything autonomous except money and publication | Maximize throughput, gate only irreversible external effects |
| Approval semantics | Sign-off (결재), never expires; escalating reminders | An unsigned item is pending, not denied |
| LLM cost | Personal subscription, not metered in USD | Usage-limit exhaustion is handled by pausing, not budgeting |
| Stack | Python + FastAPI + SQLAlchemy + Alembic + Postgres | Subprocess stream parsing, MCP SDK, and S5 domains are all Python |
| Workspace | Per-run plain directory; git worktree deferred | Runs mostly produce new files; worktree isolation is not needed yet |

## 4. Domain model

### 4.1 Entities

**`agent`**
- `id`, `name`, `persona` (markdown), `autonomy_level`
- `allowed_tools[]`, `mcp_config` (jsonb, secret references only)
- `workspace_root`, `repo_path` (nullable — reserved for future worktree mode)
- `max_run_seconds`, `max_child_tasks_per_day`, `enabled`

**`task`**
- `id`, `agent_id`, `title`, `goal` (markdown), `status`
- `parent_task_id`, `depth`, `created_by` (`human` | `agent`)
- `schedule` (nullable cron — column exists, ignored until S3)

**`run`**
- `id`, `task_id`, `attempt`, `status`
- `started_at`, `ended_at`, `heartbeat_at`
- `cost_usd` (informational only), `token_in`, `token_out`
- `exit_reason`, `session_id` (for `--resume`), `workspace_path`

**`event`** — append-only raw stream-json
- `id`, `run_id`, `seq`, `ts`, `type`, `payload` (jsonb)

**`approval`**
- `id`, `run_id`, `kind` (`spend` | `publish` | `contact` | `execute_order`)
- `proposal` (markdown — the completed 결재안), `payload` (jsonb)
- `status` (`pending` | `approved` | `rejected`)
- `nonce`, `consumed_at`
- `remind_after`, `remind_count`, `last_reminded_at`
- `slack_channel`, `slack_ts`, `decided_by`, `decided_at`, `response` (jsonb)

**`artifact`** — `id`, `run_id`, `kind`, `git_ref`, `path`, `summary`

**`spend_ledger`** — schema only in S1; records approved external spend for later profitability reporting.

**`system_state`** — single row; holds `llm_paused_until`, `pause_reason`, `pause_notified_at`.

### 4.2 Run state machine

```
queued → running → { succeeded | failed | timed_out }
           ↓ ↑
     awaiting_approval
           ↓
       cancelled
```

Rules:

- Every transition is a conditional DB UPDATE (`WHERE status = <expected>`), so multiple
  workers are safe.
- `awaiting_approval` entry happens only when the agent calls the `gate.request` MCP tool.
  On entry the CLI process **exits**; `session_id` is stored. Holding a process open for a
  multi-day sign-off would leak resources.
- `awaiting_approval` runs occupy no worker slot. Other queued tasks keep running while a
  sign-off is pending — one blocked approval never stalls the system.
- Ambiguity is **not** an approval. The agent states an assumption, proceeds, and records it
  in the report. If genuinely blocked, the task moves to `blocked` and appears in the daily
  digest — a separate path that never pollutes the approval queue.

## 5. The approval gate

### 5.1 Enforcement, not persuasion

The autonomy boundary is enforced by tool permissions and process boundaries, never by
asking the model nicely in a prompt:

1. `--allowedTools` whitelist excludes every dangerous tool (network-capable Bash, payment
   calls, deploy commands).
2. The only path to a gated action is the orchestrator-provided MCP server `gate`, exposing
   one tool: `gate.request(kind, proposal, payload)`.
3. Calling it transitions the run to `awaiting_approval`, exits the process, and dispatches
   to Slack.
4. On approval, the run resumes with the decision injected and the specific dangerous tool
   temporarily whitelisted for that action only.

A jailbroken or confused agent still cannot spend money.

### 5.2 What requires sign-off

Gated (external world effects only):
- Actual spending of money
- Order execution
- External publication or transmission (deploy, post, send)
- Contacting third parties

Never gated: research, analysis, code, backtests, drafting, internal file writes.

**All external spend requires sign-off regardless of amount.** No auto-approve threshold.
(The operator will request one later if profit justifies it.)

LLM token consumption is not a gated spend — it is system operating cost under a personal
subscription, governed by §7 quota handling.

### 5.3 Minimizing interruptions

- The agent must submit a **complete proposal**: what, why, how much, alternatives
  considered, and whether it is reversible. "May I do this?" questions are forbidden —
  those are decisions the agent makes itself.
- **Batch approval**: multiple pending items of the same `kind` are collapsed into one
  Slack message that can be signed off together.

### 5.4 Reminder escalation (no expiry)

Approvals never expire. A sweeper re-pushes pending items:

| Stage | Delay | Channel |
|-------|-------|---------|
| 1 | 30 min | reminder in the Slack thread |
| 2 | 4 h | DM |
| 3+ | daily | digest — "3 items awaiting sign-off" |

Intervals are configurable per `approval.kind` (spend short, publish longer).

### 5.5 Inbound security

The Slack inbound endpoint is public. Without verification, anyone could forge a request and
approve real spending. Required:

- `X-Slack-Signature` HMAC verification with a 5-minute timestamp replay window.
- Approver allowlist by Slack `user_id`.
- Single-use nonce consumed atomically:
  `UPDATE approval SET consumed_at = now() WHERE id = ? AND consumed_at IS NULL RETURNING *`.
  A duplicate button click resolves to nothing.

### 5.6 Secrets

`mcp_config` stores references only: `{"api_key": {"secret_ref": "broker/alpaca"}}`.
The executor resolves references immediately before spawn and injects them as environment
variables. Plaintext secrets never touch the database.

## 6. Components

Each module is independently testable with a narrow interface.

| Module | Responsibility | Depends on |
|--------|---------------|------------|
| `domain/` | entities, run state machine, transition rules — pure, no I/O | — |
| `store/` | SQLAlchemy repositories, Alembic migrations, advisory locks | domain |
| `executor/` | `Executor` interface + `ClaudeCliExecutor`: spawn, stream-json parsing, workspace prep/cleanup | domain |
| `gate/` | MCP server exposing `gate.request` | store |
| `quota/` | usage-limit detection, reset-time parsing, global pause | store |
| `notify/` | `Notifier` interface + `SlackNotifier` (Block Kit) | domain |
| `inbound/` | FastAPI: Slack signature verification, button actions → approval resolution | store, notify |
| `orchestrator/` | worker loop: claim queued → spawn → persist events → finalize | all |
| `api/` | task CRUD, run queries, SSE event stream (seam for S4) | store |

`domain/` is pure, so state-machine tests need no database or subprocess. The `Executor` and
`Notifier` interfaces let integration tests run against fakes.

## 7. Data flows

### Flow A — normal run

```
task(queued) → worker claims → quota check (llm_paused_until?) → create run workspace
→ claude -p --output-format stream-json --mcp-config gate
         --allowedTools <whitelist> --add-dir <workspace>
→ stream events persisted, heartbeat updated
→ process exits → artifacts copied to artifact repo and committed (serialized, no race)
→ run(succeeded) → Slack summary report
```

Workspace is `workspaces/<agent>/<run_id>/`. `--add-dir` confines filesystem access to it.
Artifact commits are performed serially by the orchestrator, so no git contention arises.

### Flow B — approval gate

```
agent calls gate.request(kind="spend", proposal=…, payload=…)
→ gate MCP creates approval(nonce) and transitions run → awaiting_approval
→ tool returns "pending sign-off, terminate now" → CLI exits cleanly, session_id saved
→ notify sends Slack Block Kit (approve/reject buttons, value = nonce)
→ [operator clicks] → inbound: verify signature → check approver allowlist
   → consume nonce atomically
→ approval(approved) → run(queued, resume flagged)
→ worker: claude -p --resume <session_id> with the decision injected
   and the specific dangerous tool temporarily allowed
```

### Flow C — reminder

```
sweeper scans approvals where status='pending'
→ compares now() against remind_after / last_reminded_at
→ escalates per §5.4, increments remind_count
```

### Flow D — usage limit exhausted

```
CLI exits → usage-limit signal detected (stream-json error / stderr / exit code)
→ parse reset time → system_state.llm_paused_until
→ run returns to queued; attempt NOT incremented (not the agent's fault)
→ worker loop suspends all new spawns until llm_paused_until
→ Slack notified once ("limit reached, resuming at HH:MM"), duplicates suppressed
→ time reached → automatic resume, backlog drained in order
```

If the reset time cannot be parsed: fallback backoff 15 min → 30 min → 1 h cap. Never hammer
the account. The pause is global (`system_state` single row) because the limit is
account-wide — pausing a single agent would be meaningless.

## 8. Error handling

| Failure | Handling |
|---------|----------|
| CLI crash / abnormal exit | `attempt+1`, exponential backoff; 3 failures → `failed` + Slack alert |
| Usage limit exhausted | Global pause (§7 Flow D). Not a retry, not a failure |
| Run timeout | Kill on `agent.max_run_seconds` → `timed_out`, one retry |
| stream-json parse error | Store raw payload and continue — a parser bug must not kill a run |
| Slack dispatch failure | Retry queue; approval already exists in DB so nothing is lost; recoverable via digest/dashboard |
| Duplicate approval click | Atomic nonce consumption; second click ignored |
| Resume failure (session expired) | Fall back to a fresh run with the decision plus a summary of prior context injected |
| Worker process death | `run.heartbeat_at` staleness detected by sweeper → run returned to `queued` |

Idempotency principle: every state transition is a conditional UPDATE, so concurrent workers
converge safely.

## 9. Deferred to later sub-projects (schema only in S1)

- **Concurrency / workspace isolation beyond `--add-dir`** — `agent.repo_path` and git
  worktree mode: field exists, unimplemented.
- **Agent-created task explosion** — `task.depth` and `agent.max_child_tasks_per_day`
  columns exist; enforcement is minimal.
- **Retry policy and event retention** — `attempt` exists with simple backoff; poison-task
  detection and 90-day event summarization deferred.
- **`task.schedule`** — column exists, ignored until S3.
- **Metrics dashboard, sprite UI** — S4.

## 10. Testing strategy

TDD, outside-in:

1. **`domain/` state machine** — pure unit tests covering every legal transition and
   rejection of every illegal one. No DB, no subprocess.
2. **`FakeExecutor`** — replays scripted stream-json event sequences: gate call, crash,
   timeout, usage-limit error.
3. **Integration** — real Postgres (testcontainer) + `FakeExecutor` + `FakeNotifier`,
   covering Flows A, B, C, and D end to end.
4. **Slack inbound** — signature verification (valid / forged / replayed), rejection of
   non-allowlisted users, rejection of nonce reuse.
5. **CLI smoke test** — one real trivial task actually executed; tagged to skip in CI.
