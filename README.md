# ai-com

Background agent orchestrator. Agents run autonomously; the operator signs off only on
money and publication.

## Run it

```bash
docker run -d -e POSTGRES_PASSWORD=aicom -e POSTGRES_USER=aicom -e POSTGRES_DB=aicom \
  -p 5432:5432 postgres:16-alpine
alembic upgrade head
uvicorn aicom.inbound.app:app_factory --factory --host 127.0.0.1   # HTTP: API + Slack interactions
python -m aicom.main                                                # worker + sweeper loop
```

Point the Slack app's Interactivity Request URL at `POST /slack/interactions`.

## Docker

A `Dockerfile` and `docker-compose.yml` package both processes (they share one
image; the container command picks which one runs) plus a one-shot migration
job, on top of `postgres:16-alpine`.

### Before you start: Claude authentication

The worker container spawns the real `claude` CLI as a subprocess (see
`src/aicom/executor/cli.py`). This project runs on a **personal Claude
subscription**, not an API key, so no credential is baked into the image and
no API-key environment variable exists as a workaround. Instead:

1. Log in on the **host** first: run `claude` (or `claude login`) locally
   until you have an authenticated session under `~/.claude`.
2. `docker-compose.yml` bind-mounts that host directory,
   `${HOME}/.claude`, into the worker container at `/home/aicom/.claude`.
3. **This directory contains live session credentials. Never commit it, copy
   it into an image, or add it to a volume that leaves your machine.** It
   already exists on your host outside this repo, so nothing extra needs to
   be gitignored for it, but do not `COPY` it into the `Dockerfile`.

If `~/.claude` doesn't exist or isn't logged in yet, the worker container
will start but every run will fail at the `claude` subprocess step.

### Bring the stack up

```bash
cp .env.example .env
# edit .env: at minimum set POSTGRES_PASSWORD, AICOM_DATABASE_URL to match,
# AICOM_SLACK_BOT_TOKEN, AICOM_SLACK_SIGNING_SECRET, AICOM_SLACK_APPROVER_IDS.

docker compose up -d
```

This starts `postgres`, runs `migrate` (`alembic upgrade head`) once
`postgres` is healthy, then starts `api` and `worker` once `migrate` exits
successfully.

### Running migrations again

The `migrate` service only runs once at startup. To re-run migrations later
(e.g. after pulling new versions):

```bash
docker compose run --rm migrate
```

### Logs

```bash
docker compose logs -f api worker
```

### Tear down

```bash
docker compose down -v   # -v also drops the named postgres/workspaces/artifacts volumes
```

### Not hardened for public exposure

The `api` service publishes its port bound to `127.0.0.1` only
(`127.0.0.1:8000:8000`) — leave it that way unless something else in front of
it provides real access control. As above, `/tasks`, `/runs/*`, and
`/approvals` have no authentication; only `POST /slack/interactions` is
signature-verified. Do not change the port binding to expose this to
anything other than trusted operators or a network you've put auth in front
of.

## Configuration

All settings use the `AICOM_` env prefix — see `src/aicom/config.py`.
Secrets referenced from `agent.mcp_config` resolve from `AICOM_SECRET_<SLUG>` env vars.

## HTTP API

Mounted on the same FastAPI app as the Slack webhook (`src/aicom/api/routes.py`):

- `POST /tasks` — create a task and its first queued run, in one transaction.
- `GET /tasks` — list tasks.
- `GET /runs/{run_id}` — a single run's status.
- `GET /runs/{run_id}/events` — a run's event transcript, ordered by `seq`.
- `GET /approvals` — pending sign-off requests.

### These endpoints have no authentication

`/tasks`, `/runs/*`, and `/approvals` are **not authenticated**. Unlike
`POST /slack/interactions` (which verifies the Slack request signature and checks an
approver allowlist before doing anything), anyone who can reach this port can queue
work for an autonomous agent, or read run transcripts and pending approvals.

This is a deliberate scope decision for this task, not an oversight:

- No bespoke auth scheme has been invented or added here.
- **You must not expose this port to anything other than trusted operators.** Bind it
  to `127.0.0.1` (see the `--host 127.0.0.1` above) or a private network, and put a
  real access-control layer (VPN, reverse proxy with SSO, mTLS, cloud IAM, etc.) in
  front of it before running it anywhere reachable by untrusted clients. Do not put it
  on the public internet as-is.
- The money/publication safety property of this system lives entirely in the Slack
  approval flow, which *is* verified end-to-end. The REST API can queue arbitrary
  agent work but still cannot itself approve a gated spend or publish action — those
  still require a signed, allowlisted Slack interaction.

`POST /tasks` also validates that `agent_id` refers to an existing, enabled `Agent`
row and returns `404`/`400` otherwise, so a typo'd or disabled agent id fails loudly
at request time instead of silently queuing a run nothing will ever pick up.

## Schedules

Recurring work is created once and fired by the scheduler on a cron cadence, instead
of via one-off `POST /tasks` calls:

```bash
curl -X POST http://127.0.0.1:8000/schedules \
  -H 'Content-Type: application/json' \
  -d '{
        "agent_id": "00000000-0000-0000-0000-000000000000",
        "name": "hourly scan",
        "cron": "0 * * * *",
        "timezone": "UTC",
        "title_template": "Hourly scan",
        "goal_template": "Scan and report."
      }'
```

`GET /schedules` lists them, `PATCH /schedules/{id}` updates the cadence or
templates (or disables one), and `DELETE /schedules/{id}` removes it without
touching tasks it already created. The `worker + sweeper + scheduler` loop
(`python -m aicom.main`) fires each due schedule at most once per tick, skipping
(and advancing the clock past) a slot whose previous cycle is still open.

## Packs

A pack is not a subsystem — it is one `agent` row and one or more `schedule` rows,
shipped as data plus an idempotent seed command, since there is no UI yet for creating
an agent by hand. Seed one with:

```bash
python -m aicom.packs.seed opportunity
```

Safe to run repeatedly: it upserts by agent name and by `(agent, schedule name)`, so
running it again prints the same agent id and updates the existing rows rather than
creating new ones.

The one pack that ships today, `opportunity`, watches a beat — a standing instruction
like "watch the AI agent tooling space, report what's new, changed, or gone" — and
writes what it finds to a report in the artifact repo. The beat itself lives entirely
in the schedule's `goal_template`; there is no separate "beat" table. It reaches no
gated action: it does not spend, publish, or contact anyone, so it never needs an
operator's sign-off. See `docs/superpowers/specs/2026-08-13-opportunity-pack-design.md`
for the design and `src/aicom/packs/AGENTS.md` for how to add a second pack.

## The approval gate

The `gate` MCP server (`aicom.gate.server`) is injected into every run **in code** by the
worker — operators do not put it in `agent.mcp_config`, and an entry of that name there is
overridden. It is the only route to a gated action.

`agent.gated_tools` lists tools that exist for the agent but are reachable only through a
signed-off approval. They must **not** also appear in `agent.allowed_tools`: that would make
them permanently callable and silently disable the boundary, so the worker refuses to run
such an agent, fails the run, and reports it. When a run resumes with an APPROVED decision
whose `payload["tool"]` names a member of `gated_tools`, that one tool is added to
`--allowedTools` for **that single execution only** — never persisted, never carried into a
retry, a later run, or another agent. A payload naming anything outside `gated_tools` is
refused (the payload is agent-supplied, and the operator signed off on a proposal, not on a
tool string the agent picked). S1 ships no gated tools; the S5 agent packs plug into this.

## Operations notes

- **A failed Slack dispatch loses the approval's thread anchor, not the approval.** The
  worker sends the approval message and then persists the returned channel/ts via
  `attach_slack_ref`. If the send raises, no `slack_channel`/`slack_ts` is stored. Nothing is
  lost — the approval row is already committed and still pending — but the sweeper's 30-minute
  reminder then posts as a **new message** instead of threading onto the original. Expect a
  standalone reminder rather than a thread reply after a Slack outage.

## Worker + sweeper + scheduler loop

`python -m aicom.main` runs `aicom.main.run_worker_forever`, a single long-lived loop
that each iteration: recovers stale runs, sends due approval reminders, fires any due
schedules, and lets the worker claim and execute one queued run. An unhandled
exception from any of those steps is logged and the loop continues on the next
iteration rather than exiting — there is no process supervisor restarting this
service, so a single bad iteration (e.g. a transient DB error) must not take down the
ability to ever again pick up queued work, Slack reminders, or due schedules.
