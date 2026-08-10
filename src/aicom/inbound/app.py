import json
from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response
from sqlalchemy.orm import Session, sessionmaker

from aicom.config import Settings
from aicom.domain.enums import RunStatus
from aicom.inbound.verify import verify_slack_signature
from aicom.store.approvals import consume_nonce
from aicom.store.runs import transition


def resolve_approval(
    session: Session, *, nonce: str, decided_by: str, approved: bool, now: datetime
) -> bool:
    approval = consume_nonce(
        session, nonce=nonce, decided_by=decided_by, approved=approved, now=now
    )
    if approval is None:
        return False
    note = "APPROVED" if approved else "REJECTED"
    transition(
        session,
        approval.run_id,
        RunStatus.AWAITING_APPROVAL,
        RunStatus.QUEUED,
        resume_pending=True,
        resume_note=f"Sign-off decision for approval {approval.id}: {note}.",
    )
    return True


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
            continue
        action_id = action.get("action_id")
        try:
            value = json.loads(action.get("value") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        if action_id == "approve_all":
            raw_nonces = value.get("nonces", [])
            if isinstance(raw_nonces, list):
                nonces += [(n, True) for n in raw_nonces if isinstance(n, str)]
        elif action_id in {"approve", "reject"}:
            nonce = value.get("nonce")
            if isinstance(nonce, str):
                nonces.append((nonce, action_id == "approve"))
        # any other action_id is unrecognized and is silently ignored
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
                resolve_approval(
                    session, nonce=nonce, decided_by=user_id, approved=approved, now=now
                )
            session.commit()
        return Response(status_code=200)

    return app
