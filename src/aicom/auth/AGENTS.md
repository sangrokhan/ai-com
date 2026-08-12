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
| `middleware.py` | `AuthMiddleware`, `EXEMPT_PATHS` |

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

class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, *, secret: str, max_age_seconds: int) -> None: ...
```

## For AI Agents

### An empty configured password rejects every login

`check_password` returns `False` unconditionally when `expected` (i.e.
`settings.console_password`) is empty — it never falls through to comparing an empty
string against the candidate. An unset `AICOM_CONSOLE_PASSWORD` therefore locks the
console entirely rather than accepting any password (including an empty one). This is
intentional: "not configured" must never mean "open".

### The exemption list is deliberately three paths, and no more

`EXEMPT_PATHS` in `middleware.py` contains exactly three entries, matching spec §7:

- `/slack/interactions` — authenticates itself via Slack's own signature verification
  (`aicom.inbound.verify.verify_slack_signature`); it cannot present a cookie because
  Slack, not a logged-in operator, is the caller.
- `/auth/login` — this is how a session cookie is obtained in the first place; requiring
  a cookie to reach it would make login impossible.
- `/health` — polled by the container runtime's healthcheck, which has no browser and no
  password.

Every other path — including the console's own static frontend mounted at `/` in
`aicom.inbound.app.create_app` — requires a valid session. Do not add paths to this set
casually; each entry is a hole in "every route requires a session", and the three above
are the only ones the design accepts. (See the root `AGENTS.md` roadmap note and
`docs/superpowers/specs/2026-08-11-console-design.md` §7 for the consequence this has for
a first-time, cookie-less browser visit.)

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
