<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-11 | Updated: 2026-08-11 -->

# auth

## Purpose

Everything the console needs to gate access behind one shared password: constant-time
password check, a signed session cookie, and the ASGI middleware that enforces a session
on (almost) every request. One operator, one password — no accounts, no roles.

## Key Files

| File | Description |
|------|-------------|
| `password.py` | `check_password`, `issue_session`, `verify_session`, `SESSION_COOKIE` |
| `routes.py` | `make_auth_router(settings)` — `POST /auth/login`, `POST /auth/logout` |
| `middleware.py` | `AuthMiddleware`, `EXEMPT_PATHS`, `is_static_bundle_request` |

## Public Interface

```python
# password.py
SESSION_COOKIE: str  # "aicom_session"

def check_password(candidate: str, expected: str) -> bool: ...
def issue_session(secret: str) -> str: ...
def verify_session(token: str, secret: str, max_age_seconds: int) -> bool: ...

# routes.py
def make_auth_router(settings: Settings) -> APIRouter: ...

# middleware.py
EXEMPT_PATHS: frozenset[str]  # {"/slack/interactions", "/auth/login", "/health"}

def is_static_bundle_request(method: str, path: str, static_dir: Path) -> bool: ...

class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(
        self, app: object, *, secret: str, max_age_seconds: int, static_dir: Path
    ) -> None: ...
```

## For AI Agents

### An empty configured password rejects every login

`check_password` returns `False` unconditionally when `expected` (i.e.
`settings.console_password`) is empty — it never falls through to comparing an empty
string against the candidate. An unset `AICOM_CONSOLE_PASSWORD` therefore locks the
console entirely rather than accepting any password (including an empty one). This is
intentional: "not configured" must never mean "open".

### The exemption list is deliberately three fixed paths, plus the static bundle

`EXEMPT_PATHS` in `middleware.py` contains exactly three entries, matching spec §7:

- `/slack/interactions` — authenticates itself via Slack's own signature verification
  (`aicom.inbound.verify.verify_slack_signature`); it cannot present a cookie because
  Slack, not a logged-in operator, is the caller.
- `/auth/login` — this is how a session cookie is obtained in the first place; requiring
  a cookie to reach it would make login impossible.
- `/health` — polled by the container runtime's healthcheck, which has no browser and no
  password.

A fourth, structurally different exemption sits alongside it: `is_static_bundle_request`
lets an unauthenticated `GET`/`HEAD` through when it names a real file inside
`settings.console_static_dir` (the built console — `/` and `/assets/*`, mounted by
`aicom.inbound.app.create_app`). Without it, a cookie-less browser could never load
enough of the app to reach `<Login />` in the first place: `/` and `/assets/*` would 401
before the SPA shell was ever fetched, meaning there'd be no way to obtain a cookie
without one already. The bundle is safe to serve openly — it is the login form plus
application code, no data — and every endpoint it actually calls
(`/console/state`, `/console/stream`, `/tasks`, `/runs/*`, `/approvals`, `/schedules`)
still goes through the normal session check and 401s without a cookie exactly as before.

This exemption is intentionally **not** `path.startswith("/assets/")`: that shape would
silently cover any future route added under the same prefix. `is_static_bundle_request`
instead resolves the request path against `static_dir` and only exempts it if that exact
file is really there (and the resolved path can't escape `static_dir` via `..`), so a
same-shaped path that is actually an API route — not a built asset — still needs a
session. See `tests/auth/test_middleware.py` for the case that specifically constructs
such a route to prove it stays gated.

Do not add paths to `EXEMPT_PATHS` casually, and do not widen
`is_static_bundle_request` into a prefix match; each is a hole in "every route requires a
session by default", and these four are the only ones the design accepts.

### Session mechanics

- `verify_session` never raises: any bad signature, expiry, or garbage token is caught
  and treated as "not logged in" (a 401), never a 500.
- `check_password` compares UTF-8 bytes, not `str`, with `hmac.compare_digest` — comparing
  `str` directly raises `TypeError` on non-ASCII input, which would turn a non-ASCII
  password attempt into a 500 instead of a 401.
- `POST /auth/logout` only clears the cookie on the caller's browser. The token is a
  stateless signed credential with no server-side record, so a previously-issued cookie
  value remains valid (for anyone still holding it) until `session_max_age_seconds`
  elapses. There is no server-side revocation.

## Dependencies

### External

`itsdangerous` (`URLSafeTimedSerializer` signs and verifies the session token) ·
Starlette's `BaseHTTPMiddleware` · FastAPI.

### Internal

`aicom.config.Settings` for `console_password`, `session_secret`,
`session_cookie_secure`, `session_max_age_seconds`. Mounted by
`aicom.inbound.app.create_app`, which installs `AuthMiddleware` before including any
router.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->

### Limitation: `AuthMiddleware` does not cover WebSocket routes

`AuthMiddleware` subclasses Starlette's `BaseHTTPMiddleware`, which only runs for
`scope["type"] == "http"`. A WebSocket route (`scope["type"] == "websocket"`) would skip
this middleware entirely — including its session check — and be reachable with no
authentication at all, regardless of `EXEMPT_PATHS`.

There are no WebSocket routes in this codebase today, so nothing is currently
exploitable. But "every route requires a session by default" is only true for HTTP
routes. If a WebSocket route is ever added (e.g. to push console updates instead of SSE),
it needs its own explicit session check in the `websocket_endpoint` handler (verify the
session cookie from the handshake before `accept()`), or `AuthMiddleware` needs to be
rewritten as a pure ASGI middleware that inspects `scope["type"]` itself rather than
relying on `BaseHTTPMiddleware`. Neither change is made here — this note exists so the
gap is visible before someone adds that route.
