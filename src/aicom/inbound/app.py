import json
import logging
from datetime import UTC, datetime
from enum import StrEnum

from fastapi import FastAPI, Request, Response
from sqlalchemy.orm import Session, sessionmaker

from aicom.config import Settings, load_settings
from aicom.domain.enums import RunStatus
from aicom.inbound.verify import verify_slack_signature
from aicom.store.approvals import consume_nonce
from aicom.store.db import make_engine, session_factory
from aicom.store.models import Run
from aicom.store.runs import transition

logger = logging.getLogger(__name__)


class ResolveOutcome(StrEnum):
    """Distinguishes the three ways resolving a nonce can go, so a caller can
    tell a benign duplicate-click no-op apart from a genuine stall -- without
    that distinction ever reaching the HTTP response, which would hand an
    attacker an oracle for probing nonce validity."""

    NOT_FOUND = "not_found"  # unknown or already-consumed nonce: a safe no-op
    REQUEUED = "requeued"  # consumed and the parked run was requeued
    REQUEUE_FAILED = "requeue_failed"  # consumed but the run was not requeued


def resolve_approval(
    session: Session, *, nonce: str, decided_by: str, approved: bool, now: datetime
) -> ResolveOutcome:
    approval = consume_nonce(
        session, nonce=nonce, decided_by=decided_by, approved=approved, now=now
    )
    if approval is None:
        return ResolveOutcome.NOT_FOUND
    note = "APPROVED" if approved else "REJECTED"
    requeued = transition(
        session,
        approval.run_id,
        RunStatus.AWAITING_APPROVAL,
        RunStatus.QUEUED,
        resume_pending=True,
        resume_note=f"Sign-off decision for approval {approval.id}: {note}.",
    )
    if not requeued:
        # The nonce is already burned and the approval already marked
        # APPROVED/REJECTED (consume_nonce is a single atomic UPDATE that has
        # already committed to that outcome), but the run was not in
        # AWAITING_APPROVAL at that moment, so it was never requeued. That
        # run is now parked forever unless an operator intervenes -- this
        # must never happen silently.
        run = session.get(Run, approval.run_id, populate_existing=True)
        actual_status = run.status if run is not None else "<run not found>"
        logger.error(
            "approval %s consumed (approved=%s) but run %s was not requeued: "
            "expected status AWAITING_APPROVAL, actual status %s",
            approval.id,
            approved,
            approval.run_id,
            actual_status,
        )
        return ResolveOutcome.REQUEUE_FAILED
    return ResolveOutcome.REQUEUED


def _collect_nonces(payload: dict) -> list[tuple[str, bool]]:
    """Extract (nonce, approved) pairs from the payload's actions.

    Slack's signature only proves the request came from Slack, not that its
    contents are shaped the way we expect. A single malformed or unrecognized
    action must not crash the whole request (and must not block other, valid
    actions in the same click) -- so each action is parsed defensively and
    anything that doesn't parse is skipped rather than raising.
    """
    nonces: list[tuple[str, bool]] = []
    for action in payload.get("actions", []):
        if not isinstance(action, dict):
            logger.warning("skipping malformed action (not an object): %r", action)
            continue
        action_id = action.get("action_id")
        try:
            value = json.loads(action.get("value") or "{}")
        except (TypeError, ValueError):
            logger.warning("skipping action %r with unparseable value: %r", action_id, action)
            continue
        if not isinstance(value, dict):
            logger.warning(
                "skipping action %r whose value did not decode to an object: %r",
                action_id,
                action,
            )
            continue
        if action_id == "approve_all":
            raw_nonces = value.get("nonces", [])
            if isinstance(raw_nonces, list):
                nonces += [(n, True) for n in raw_nonces if isinstance(n, str)]
            else:
                logger.warning("skipping approve_all action with non-list nonces: %r", action)
        elif action_id in {"approve", "reject"}:
            nonce = value.get("nonce")
            if isinstance(nonce, str):
                nonces.append((nonce, action_id == "approve"))
            else:
                logger.warning(
                    "skipping action %r with missing/invalid nonce: %r", action_id, action
                )
        else:
            logger.warning("skipping unrecognized action_id %r: %r", action_id, action)
    return nonces


def create_app(sessions: sessionmaker[Session], settings: Settings) -> FastAPI:
    app = FastAPI()

    @app.post("/slack/interactions")
    async def interactions(request: Request) -> Response:
        # Verification must run before any parsing of the payload and before
        # any database work: the raw body is hashed exactly as received, so
        # nothing may decode or re-encode it first.
        body = await request.body()
        now = datetime.now(UTC)
        if not verify_slack_signature(
            signing_secret=settings.slack_signing_secret,
            timestamp=request.headers.get("X-Slack-Request-Timestamp", ""),
            body=body,
            signature=request.headers.get("X-Slack-Signature", ""),
            now=now,
        ):
            return Response(status_code=401)

        form = await request.form()
        try:
            payload = json.loads(str(form.get("payload", "{}")))
        except ValueError:
            return Response(status_code=400)
        if not isinstance(payload, dict):
            return Response(status_code=400)

        # A valid Slack signature only proves the request came from Slack, not
        # that the clicking user is authorised to spend real money -- check
        # the approver allowlist next.
        user_id = str(payload.get("user", {}).get("id", ""))
        if user_id not in settings.slack_approver_ids:
            return Response(status_code=403)

        nonces = _collect_nonces(payload)

        with sessions() as session:
            for nonce, approved in nonces:
                outcome = resolve_approval(
                    session, nonce=nonce, decided_by=user_id, approved=approved, now=now
                )
                if outcome is not ResolveOutcome.REQUEUED:
                    logger.warning(
                        "nonce resolution for user %s did not requeue a run: outcome=%s",
                        user_id,
                        outcome,
                    )
            session.commit()
        return Response(status_code=200)

    from aicom.auth.middleware import AuthMiddleware
    from aicom.auth.routes import make_auth_router

    app.add_middleware(
        AuthMiddleware,
        secret=settings.session_secret,
        max_age_seconds=settings.session_max_age_seconds,
    )
    app.include_router(make_auth_router(settings))

    from aicom.api.routes import make_router

    app.include_router(make_router(sessions))

    from aicom.console.routes import make_console_router

    app.include_router(make_console_router(sessions, settings))

    return app


def app_factory() -> FastAPI:
    """Zero-argument entry point for `uvicorn aicom.inbound.app:app_factory --factory`.

    `create_app` needs a session factory and settings, which uvicorn's
    `--factory` calling convention has no way to supply -- it always calls
    the target with no arguments. This wrapper builds both from the process
    environment (via `AICOM_*` settings) so the ASGI server can still be
    pointed at a plain import path.
    """
    settings = load_settings()
    sessions = session_factory(make_engine(settings.database_url))
    return create_app(sessions, settings)
