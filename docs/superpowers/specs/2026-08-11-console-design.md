# Live 3D Console (S4a) — Design Spec

Date: 2026-08-11
Status: Approved for planning
Scope: S4a — the read-only live console
Builds on: `2026-08-10-agent-orchestrator-design.md` (S1+S2), `2026-08-11-scheduler-design.md` (S3)

---

## 1. Purpose

The system now runs agents, gates their dangerous actions behind Slack sign-off, and
starts itself on a schedule. What it cannot do is show the operator what is happening.
Today that means reading Postgres by hand.

S4a is the window: a single screen showing the whole organisation as an isometric 3D
office, each agent at a desk with a name pill, a status dot, and a speech bubble saying
what it is doing right now. It is read-only. Creating and editing come later.

The visual reference is `twin.quantlabnote.com` (screenshot at `twin_sample.png` in the
repository root): a low-poly pastel isometric building, agents seated at desks, dark
rounded name pills floating above them with a coloured status dot, and a white speech
bubble for whichever agent has something to say.

## 2. Decomposition

| ID | Scope | State |
|----|-------|-------|
| **S4a** | **Live 3D console: scene, name pills, status dots, speech bubbles, login** | This spec |
| S4b | Editing: persona, `allowed_tools`, `gated_tools`, `mcp_config`, schedule forms | Later |
| S4c | Web sign-off: pending approvals with approve/reject, alongside Slack rather than replacing it | Later |

S4a stands alone: it delivers the operator a working view of the running system without
any of the write paths, and every write path is a separate risk surface that deserves its
own review.

## 3. Key decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Rendering | three.js, real 3D scene | Matches the reference, which is three.js-based. A pre-rendered backdrop was cheaper but caps what the scene can ever become |
| 3D assets | Kenney CC0 kits (City, Furniture, Mini Characters) as glTF | CC0 so licensing is a non-issue, and the low-poly pastel style is already close to the reference |
| Labels and bubbles | DOM overlay projected from 3D coordinates, not in-scene text | Crisp text at any zoom, Korean fonts work without loading a 3D font atlas, and it is how the reference appears to do it |
| Frontend stack | React + Vite, built to static files served by FastAPI | The operator asked for a real app rather than server-rendered pages; S4b's forms will want components |
| Live updates | Server-sent events, one delta every 10 seconds | The worker, scheduler, and API are separate processes, so in-memory pub/sub cannot work. A poll-and-diff on the server side is the honest mechanism |
| Access control | Single password, signed session cookie, applied to every route | One operator; account management would be ceremony. This also closes the "no authentication" hole S1 and S3 shipped with |
| Network exposure | Bind `0.0.0.0`, reachable on the LAN | The operator wants it on a phone. See §7 for what this costs |

## 4. The scene

### 4.1 Layout

Room positions are generated, not hand-authored. The scene builder takes the agent list
and lays out desks on a grid inside a floor plate, growing the plate as agents are added.
Each agent's desk slot is derived deterministically from its `agent.id`, so an agent keeps
its place across reloads and restarts — an operator learns where to look.

Kenney models are loaded once as glTF and instanced per desk. The camera is a fixed
isometric view with drag-to-rotate and scroll-to-zoom; there is no free-fly camera.

### 4.2 What is drawn per agent

- A seated character at the desk.
- A dark rounded **name pill** above it, showing `agent.name`.
- A **status dot** on the pill:

| Colour | Meaning | Derived from |
|--------|---------|--------------|
| Green | Working | a run in `RUNNING` |
| Amber | Waiting on the operator | a run in `AWAITING_APPROVAL` |
| Red | Failed | most recent run terminal in `FAILED` or `TIMED_OUT` |
| Blue | Paused | `system_state.llm_paused_until` is in the future |
| Grey | Idle | anything else |

- A white **speech bubble** when the agent has a running run, containing a one-line
  summary of that run's most recent event — "reading market data", "running the backtest".
  The summary is computed server-side from the existing `event` rows; no new storage.

Characters do not walk. Motion in S4a is the pills, dots, and bubbles updating, plus the
subtle idle animation Kenney's character models already carry if present.

## 5. Components

| Path | Responsibility |
|------|----------------|
| `src/aicom/console/state.py` | Builds the console snapshot from the database: per-agent status, current run, speech-bubble line, pending-approval count |
| `src/aicom/console/routes.py` | `GET /console/state`, `GET /console/stream` (SSE) |
| `src/aicom/auth/password.py` | Constant-time password check, cookie signing and verification |
| `src/aicom/auth/middleware.py` | Rejects unauthenticated requests to every route except the Slack webhook, the login endpoint, and the health check |
| `web/` | Vite + React + three.js application |
| `web/src/scene/` | Scene builder, model loading, deterministic desk assignment, camera |
| `web/src/overlay/` | Name pills, status dots, speech bubbles, projected from 3D coordinates |
| `web/src/api/` | Snapshot fetch, SSE subscription, login form |

`console/state.py` holds all the derivation logic and is a pure function of database
contents, so it is testable without HTTP or a browser.

## 6. Data flow

```
browser loads / ──> 401 unless session cookie ──> login form
                                │
                         POST /auth/login (password)
                                │
                      signed session cookie set
                                │
              GET /console/state ──> full snapshot, scene built
                                │
              GET /console/stream (SSE) ──> delta every 10s
                                │
                  pills, dots, bubbles updated in place
```

The SSE endpoint recomputes the snapshot on its interval and sends only what changed. Ten
seconds is deliberately slow: this is a status view, not a terminal, and every tick is a
database query whose cost grows with the agent count.

## 7. Access control, stated plainly

A single password lives in `AICOM_CONSOLE_PASSWORD`; a separate `AICOM_SESSION_SECRET`
signs the cookie. The check is constant-time. The cookie is `HttpOnly` and `SameSite=Lax`.
Authentication is enforced by middleware on every route, with four exemptions: the Slack
interaction webhook (which has its own signature verification and cannot present a
cookie), `POST /auth/login`, `GET /health` (the container healthcheck), and the built
console frontend itself (the SPA shell at `/` and its `/assets/*` bundle).

The frontend exemption exists because the other three are not enough to reach a working
login: a cookie-less browser needs the page in order to get the login form, and needs the
login form in order to get a cookie. The bundle it serves unauthenticated carries no
data — only the login form and application code, which call the same protected data
endpoints (`/console/state`, `/console/stream`, `/tasks`, `/runs/*`, `/approvals`,
`/schedules`) any other client would, and get the same `401` without a session. The
exemption is filesystem-backed (`aicom.auth.middleware.is_static_bundle_request`), not a
path-prefix rule: a request is only exempt if it names a file that actually exists inside
the mounted build output, so a future route that happens to share the `/assets/` prefix
is not accidentally waved through — it still needs a session like everything else.

This closes the hole S1 and S3 shipped with, where `POST /tasks` — an endpoint that queues
work for an autonomous agent — was reachable by anyone who could reach the port.

**What it does not fix.** The operator has chosen to bind `0.0.0.0` so the console is
reachable from a phone on the same network. Served over plain HTTP, the password and the
session cookie cross the LAN in clear text, and any device on that network can reach the
port and attempt the password. On a home network the practical risk is low, but "it has a
password now" is not the same as "it is safe to expose". Before this is reachable from
anywhere less trusted it needs TLS and something stronger in front of it. The `secure`
cookie flag is configurable and defaults to off precisely because there is no TLS here;
turning TLS on should turn that flag on with it. Logout is also only a client-side
courtesy: it clears the cookie on the caller's browser, but the token is a stateless
signed credential with no server-side record, so it remains valid until it expires on
its own -- real revocation would need server-side session state that does not exist yet.

## 8. Error handling

| Failure | Handling |
|---------|----------|
| SSE connection drops | The client reconnects with backoff and refetches the full snapshot, so a missed delta cannot leave the scene permanently stale |
| A model file fails to load | The scene renders with a placeholder box for that model and logs it; a missing desk must not blank the whole view |
| Snapshot query fails inside the SSE loop | Log and skip that tick; the stream stays open and the next tick recovers |
| WebGL unavailable | Fall back to a plain table of agents and their statuses, so the console is never a blank page |
| Wrong password | 401 with no detail about whether a password is even configured |

## 9. Testing

- `console/state.py` — unit tests against a real Postgres covering each status colour, the
  speech-bubble summary, and an agent with no runs at all.
- Auth — a correct password sets a usable cookie; a wrong one is rejected; an
  unauthenticated request to `POST /tasks` is rejected; the Slack webhook still works
  without a cookie; `GET /health` still answers for the container healthcheck.
- SSE — the endpoint emits an initial payload and a subsequent delta, and survives a
  failing tick.
- One Playwright smoke test: log in, the canvas mounts, and an agent's name pill appears
  on screen. The three.js scene itself is not unit tested — the cost outweighs the value.

## 10. Out of scope

- Editing anything (S4b) and web sign-off (S4c).
- Walking or animated characters; office decoration beyond desks and rooms.
- Historical playback, charts, or the reference's perimeter billboards.
- Multiple users, roles, or account management.
- TLS and anything beyond the single shared password.
- Camera controls. §4.1 above describes drag-to-rotate and scroll-to-zoom, but S4a does
  not implement them: `web/src/scene/Scene.ts` positions the camera once and only moves
  it in response to an agent-count change or a window resize, never user input. Not
  implemented in this task; the spec text is aspirational, not shipped.
