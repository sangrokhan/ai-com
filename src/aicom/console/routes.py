import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, sessionmaker

from aicom.config import Settings
from aicom.console.state import ConsoleSnapshot, build_snapshot

logger = logging.getLogger(__name__)


def snapshot_payload(snapshot: ConsoleSnapshot) -> dict:
    return asdict(snapshot)


def make_console_router(
    sessions: sessionmaker[Session], settings: Settings
) -> APIRouter:
    router = APIRouter()

    def _current() -> dict:
        with sessions() as session:
            return snapshot_payload(build_snapshot(session, now=datetime.now(UTC)))

    @router.get("/console/state")
    def console_state() -> dict:
        return _current()

    @router.get("/console/stream")
    async def console_stream() -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            previous: dict | None = None
            while True:
                try:
                    payload = await asyncio.to_thread(_current)
                except Exception:
                    # A failed tick must not close the stream; the next one recovers.
                    logger.exception("console snapshot failed")
                else:
                    if payload != previous:
                        previous = payload
                        yield f"data: {json.dumps(payload)}\n\n"
                    else:
                        yield ": keep-alive\n\n"
                await asyncio.sleep(settings.sse_interval_seconds)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
