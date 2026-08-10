"""MCP server exposing the single gated-action tool.

This is the ONLY route an agent has to money, publication, or third-party
contact. Dangerous tools are absent from --allowedTools, so an agent cannot
reach them directly no matter what its prompt says.
"""

import os
import uuid
from datetime import UTC, datetime

from mcp.server.fastmcp import FastMCP

from aicom.config import load_settings
from aicom.gate.service import request_approval
from aicom.store.db import make_engine, session_factory

mcp = FastMCP("gate")
_settings = load_settings()
_sessions = session_factory(make_engine(_settings.database_url))


@mcp.tool()
def request_approval_tool(kind: str, proposal: str, payload: dict | None = None) -> str:
    """Submit a completed proposal for human sign-off, then terminate.

    kind: one of spend, publish, contact, execute_order.
    proposal: markdown stating WHAT, WHY, HOW MUCH, ALTERNATIVES, and
        whether the action is REVERSIBLE. Never ask an open question here.
    payload: structured details, e.g. {"amount_usd": 20, "vendor": "X"}.
    """
    run_id = uuid.UUID(os.environ["AICOM_RUN_ID"])
    with _sessions() as session:
        result = request_approval(
            session,
            run_id=run_id,
            kind=kind,
            proposal=proposal,
            payload=payload or {},
            now=datetime.now(UTC),
        )
        session.commit()
    return result.message


if __name__ == "__main__":
    mcp.run()
