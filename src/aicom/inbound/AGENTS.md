<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-11 | Updated: 2026-08-11 -->

# inbound

## Purpose
The FastAPI app that receives Slack's interaction callbacks (button clicks on approval messages) and turns them into approval resolutions. This endpoint is **public on the internet** and its effect, when successful, is authorizing real spending, order execution, publication, or third-party contact — it is the other half of the security boundary that `gate/` starts. Everything here exists to make sure only a genuine, freshly-signed, allowlisted Slack click can release a gated action.

## Key Files
| File | Description |
|------|-------------|
| `verify.py` | `verify_slack_signature` — HMAC verification of the raw request body with a replay window. |
| `app.py` | `create_app` / `app_factory` — the FastAPI app, `/slack/interactions` route, nonce resolution logic. |

## Public Interface
```python
# verify.py
REPLAY_WINDOW_SECONDS = 300

def verify_slack_signature(
    *, signing_secret: str, timestamp: str, body: bytes, signature: str, now: datetime
) -> bool: ...

# app.py
class ResolveOutcome(StrEnum):
    NOT_FOUND = "not_found"
    REQUEUED = "requeued"
    REQUEUE_FAILED = "requeue_failed"

def resolve_approval(
    session: Session, *, nonce: str, decided_by: str, approved: bool, now: datetime
) -> ResolveOutcome: ...

def create_app(sessions: sessionmaker[Session], settings: Settings) -> FastAPI: ...
def app_factory() -> FastAPI:  # zero-arg entry point for `uvicorn aicom.inbound.app:app_factory --factory`
```

## For AI Agents

### Working In This Directory
- **Signature verification runs before any parsing or DB work, on the raw body.** In `app.py`'s `interactions` handler, `await request.body()` is hashed exactly as received and checked via `verify_slack_signature` *before* `await request.form()` is ever called. Never move body-reading, form-parsing, or a DB query ahead of this check — anything that touches the payload before verification is a forgery surface.
- **Verification uses `hmac.compare_digest`, not `==`.** A plain string comparison leaks timing information that can be used to forge a valid signature byte-by-byte. Do not "simplify" this comparison.
- **Both failure modes fail closed.** An empty `signing_secret` returns `False` immediately (`if not signing_secret: return False`), and an empty `slack_approver_ids` allowlist means no `user_id` can ever match. Never change either to fail open (e.g. "if no secret configured, skip verification") — that would let anyone on the internet approve spending.
- **The replay window is ±5 minutes (`REPLAY_WINDOW_SECONDS = 300`)**, checked against `abs(now - timestamp)`. This bounds how long a captured-and-replayed request stays valid even with a correct signature.
- **The approver allowlist check happens after signature verification, deliberately.** A valid Slack signature only proves the request came from Slack's infrastructure — it says nothing about which Slack user clicked the button. `settings.slack_approver_ids` is the actual authorization check; it must always run, and it must always come after signature verification (checking identity in an unverified request is meaningless).
- **`ResolveOutcome` has three variants but the HTTP response is identical across all of them (still `200`).** Do not let `not_found` / `requeued` / `requeue_failed` leak into the response body, status code, or headers — doing so would give an attacker a nonce-validity oracle (the ability to probe whether a given nonce string is real). The distinction exists only for server-side logging (`REQUEUE_FAILED` logs at `error` level because it means a run may be stuck parked with a burned nonce and needs operator attention).
- **Malformed Slack actions are skipped and logged, never allowed to 500 the request.** `_collect_nonces` defensively parses each action in `payload["actions"]`; one bad action (missing `nonce`, non-dict `value`, unrecognized `action_id`) must not block other valid actions in the same click or crash the endpoint — Slack's payload shape is not something this service controls.
- **`approve`/`reject` vs `approve_all` have different value shapes** — see `notify/AGENTS.md`. `_collect_nonces` branches on `action_id`, expecting a single `"nonce"` key for `approve`/`reject` and a `"nonces"` list for `approve_all`. If you add a new action type, keep the shapes distinguishable by `action_id`, not by guessing from the value's contents.
- Nonce consumption itself is atomic and lives in `store.approvals.consume_nonce` (single `UPDATE ... WHERE consumed_at IS NULL RETURNING *`), not in this module — `resolve_approval` here only orchestrates consume-then-requeue and must not attempt to re-implement single-use semantics locally.

### Testing Requirements
- `tests/inbound/test_verify.py` — valid / forged / replayed / empty-secret signature cases.
- `tests/inbound/test_app.py` — allowlist rejection, nonce reuse, malformed actions, `approve_all` batching, `ResolveOutcome` branches.
- `tests/inbound/conftest.py` sets up fixtures; this suite needs Docker running (testcontainers spins up a real Postgres 16).
- Run with `.venv/bin/pytest tests/inbound`.

### Common Patterns
- FastAPI routes return `Response(status_code=...)` directly rather than raising `HTTPException`, keeping the response shape fully explicit and uniform (see the oracle-avoidance point above).
- `app_factory()` builds its own `Settings`/session factory from process env so it can be pointed at directly by `uvicorn --factory`; `create_app()` is the version to call from tests, with fakes/fixtures injected.

## Dependencies

### Internal
- Imports `aicom.domain.enums.RunStatus`, `aicom.inbound.verify.verify_slack_signature`, `aicom.store.approvals.consume_nonce`, `aicom.store.runs.transition`, `aicom.store.models.Run`, `aicom.store.db`, `aicom.config`, and mounts `aicom.api.routes.make_router`.
- Not imported elsewhere in `aicom` — this is an ASGI entry point (`app_factory`) run directly by `uvicorn`, not a library other modules call into.

### External
- `fastapi` (`FastAPI`, `Request`, `Response`), `sqlalchemy.orm` (`Session`, `sessionmaker`), stdlib `hmac`/`hashlib`.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
