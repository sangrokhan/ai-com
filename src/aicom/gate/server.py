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
from aicom.store.runs import record_gate_error

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


def _try_record_gate_error(run_id: uuid.UUID, kind: str, exc: Exception) -> None:
    """Best-effort: stamp `Run.gate_error` so the failure is discoverable on
    the orchestrator side, not just inside this MCP subprocess's own stderr
    (which the `claude` CLI absorbs into its MCP logs, and which
    `ClaudeCliExecutor` otherwise discards). The worker checks this column at
    finalize time and, if set, surfaces it via its normal Slack run-report
    path and its own logs -- channels an operator actually watches.

    IMPORTANT SCOPE LIMIT: this uses the SAME `_sessions` the rest of this
    module uses, so it can only help when this gate server's DB connection
    is sound but recording still failed for some other reason (a transition
    race, a transient error, ...). It does NOT and CANNOT cover this gate
    server being connected to the WRONG database (the original defect this
    whole feature responds to): in that case `record_gate_error`'s UPDATE
    matches zero rows in *that* database (the run doesn't exist there) and
    there is nothing further this function can do about it from inside that
    same wrong connection -- it can only log that it happened. The fix for
    the wrong-database case is `gate_server_spec`/`_build_request` in
    aicom.orchestrator.worker forwarding `settings.database_url` explicitly,
    which removes the possibility rather than reporting it after the fact.

    Uses a FRESH session, deliberately separate from the one that just
    failed above (which may itself be unusable, e.g. mid-rollback). Any
    failure here is swallowed: this is a best-effort secondary signal, and
    must never replace the logger.exception call or the raised
    ApprovalRecordingFailed as the primary evidence trail.

    The message stored deliberately excludes `str(exc)`: a driver-level or
    DSN-parsing error can carry the raw connection string -- credentials and
    all -- and this value is forwarded verbatim into a Slack message by the
    worker. Only the exception's type name is safe to carry that far; the
    full, unsanitized exception is already captured server-side (this
    process's own logs only) by the logger.exception call in the caller.
    """
    safe_message = (
        f"{kind}: {type(exc).__name__} (see this gate server's own logs for "
        f"run {run_id} for the full error -- deliberately not repeated here, "
        "since it may contain connection credentials)"
    )
    try:
        with _sessions() as session:
            found = record_gate_error(session, run_id, safe_message)
            session.commit()
    except Exception:
        logger.exception(
            "run %s: also failed to persist gate_error for operator visibility", run_id
        )
        return
    if not found:
        logger.error(
            "run %s: gate_error UPDATE matched no rows -- this gate server's DB "
            "connection does not contain a run with this id at all, so the "
            "approval failure above was NOT recorded anywhere an operator can "
            "see it. This is very likely the gate server being connected to the "
            "wrong database (see gate_server_spec in aicom.orchestrator.worker).",
            run_id,
        )


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
    `gate_server_spec` in aicom.orchestrator.worker), the run was no longer
    RUNNING when this was called, or AICOM_RUN_ID itself is missing/malformed.
    Either way, no half-state is left behind for the approval flow: the
    underlying session is never committed on this path, so any
    flushed-but-uncommitted approval row is discarded when the session
    closes. Also makes a best-effort attempt to stamp `Run.gate_error` (see
    `_try_record_gate_error`) so the failure reaches the operator, not just
    the model -- EXCEPT when this server's own DB connection is the wrong
    one, in which case that attempt necessarily also fails silently (there
    is nowhere on the wrong database to write to); only a log line on this
    subprocess's own stderr survives that case. See `_try_record_gate_error`
    for why, and `gate_server_spec` for how the wrong-database case is
    supposed to be prevented rather than reported.
    """
    run_id: uuid.UUID | None = None
    try:
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
    except Exception as exc:
        logger.exception(
            "run %s: failed to record approval (kind=%s); no sign-off was recorded",
            run_id,
            kind,
        )
        if run_id is not None:
            _try_record_gate_error(run_id, kind, exc)
        raise ApprovalRecordingFailed(
            f"Approval was NOT recorded for run {run_id} (kind={kind}): {exc}. "
            "You do not have sign-off. Do not proceed with the gated action and "
            "do not treat this as approval obtained; report this failure instead "
            "of terminating as if the request succeeded."
        ) from exc
    return result.message


if __name__ == "__main__":
    mcp.run()
