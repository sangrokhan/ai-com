"""MCP server exposing the single gated-action tool.

This is the ONLY route an agent has to money, publication, or third-party
contact. Dangerous tools are absent from --allowedTools, so an agent cannot
reach them directly no matter what its prompt says.
"""

import logging
import os
import uuid
from datetime import UTC, datetime

# mcp.server.fastmcp.FastMCP was removed in mcp 2.0.0 (renamed/restructured to
# mcp.server.mcpserver.MCPServer). pyproject.toml pins "mcp>=1.0,<2.0" to keep
# this import working. Migrate this module to the 2.x API before that pin is
# ever relaxed/bumped, or this - the sole route to gated actions - breaks at
# import time.
from mcp.server.fastmcp import FastMCP

from aicom.config import load_settings
from aicom.gate.service import request_approval
from aicom.store.db import make_engine, session_factory

logger = logging.getLogger(__name__)

mcp = FastMCP("gate")
_settings = load_settings()
_sessions = session_factory(make_engine(_settings.database_url))


class ApprovalRecordingFailed(Exception):
    """Raised when an approval could not be durably recorded.

    Deliberately distinct from whatever underlying exception caused the
    failure (a foreign-key violation, a lost connection, a transition race,
    ...): the model calling this tool must be able to tell, from the
    exception alone and without inspecting server internals, that it did
    NOT obtain sign-off and must not proceed as though it did. Swallowing
    this into the tool's return value (a "success" string) is exactly the
    failure mode this exists to prevent -- it previously let an agent's run
    finish `succeeded` with zero approval rows and zero Slack notification.
    """


@mcp.tool()
def request_approval_tool(kind: str, proposal: str, payload: dict | None = None) -> str:
    """Submit a completed proposal for human sign-off, then terminate.

    kind: one of spend, publish, contact, execute_order.
    proposal: markdown stating WHAT, WHY, HOW MUCH, ALTERNATIVES, and
        whether the action is REVERSIBLE. Never ask an open question here.
    payload: structured details, e.g. {"amount_usd": 20, "vendor": "X"}.

    Raises ApprovalRecordingFailed, never returns a success message, if the
    approval could not be durably recorded -- e.g. this MCP server's database
    connection does not match the one the run actually lives in (see
    `gate_server_spec` in aicom.orchestrator.worker), or the run was no
    longer RUNNING when this was called. Either way, no half-state is left
    behind: the underlying session is never committed on this path, so any
    flushed-but-uncommitted approval row is discarded when the session
    closes.
    """
    run_id = uuid.UUID(os.environ["AICOM_RUN_ID"])
    try:
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
    except Exception as exc:
        logger.exception(
            "run %s: failed to record approval (kind=%s); no sign-off was recorded",
            run_id,
            kind,
        )
        raise ApprovalRecordingFailed(
            f"Approval was NOT recorded for run {run_id} (kind={kind}): {exc}. "
            "You do not have sign-off. Do not proceed with the gated action and "
            "do not treat this as approval obtained; report this failure instead "
            "of terminating as if the request succeeded."
        ) from exc
    return result.message


if __name__ == "__main__":
    mcp.run()
