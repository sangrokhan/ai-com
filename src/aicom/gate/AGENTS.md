<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-11 | Updated: 2026-08-11 -->

# gate

## Purpose
An MCP server exposing exactly one tool, `request_approval_tool`, which is the **only** route an agent process has to money, order execution, external publication, or contacting a third party. Dangerous tools are absent from `--allowedTools` (see `executor/`), so an agent cannot reach a gated action by any other means, no matter what its prompt says or how it is jailbroken. Calling the tool records a pending `approval` row, parks the run in `AWAITING_APPROVAL`, and tells the agent to terminate — the CLI process must exit rather than sit waiting for a decision that may take days.

## Key Files
| File | Description |
|------|-------------|
| `service.py` | `request_approval` — the DB-facing logic: create the approval, then transition the run. |
| `server.py` | The MCP server itself (`FastMCP("gate")`) exposing `request_approval_tool`, reading `AICOM_RUN_ID` from the environment. |

## Public Interface
```python
# service.py
@dataclass(frozen=True, slots=True)
class GateResult:
    approval_id: uuid.UUID
    message: str

def request_approval(
    session: Session,
    *,
    run_id: uuid.UUID,
    kind: str,
    proposal: str,
    payload: dict,
    now: datetime,
) -> GateResult: ...  # raises UnknownApprovalKind, RunNotRunning

class UnknownApprovalKind(Exception): ...
class RunNotRunning(Exception): ...

# server.py (MCP tool, not a plain function call)
@mcp.tool()
def request_approval_tool(kind: str, proposal: str, payload: dict | None = None) -> str: ...
```

## For AI Agents

### Working In This Directory
- **This module IS the security boundary.** Every gated action (spend, `execute_order`, `publish`, `contact`) exists on the other side of `request_approval_tool`. Do not add a second way to create an approval, do not add a tool that performs a gated action directly, and do not widen what `kind` accepts without updating `ApprovalKind` in `domain/enums.py` first — `request_approval` validates against that enum and rejects anything else via `UnknownApprovalKind`.
- **The returned message is what makes the CLI process exit.** `_TERMINATE_MESSAGE` explicitly instructs the agent to stop and terminate now. If you edit this string, preserve the instruction to terminate and to not attempt the gated action by any other means — a model that ignores this and keeps working leaves an inconsistent run parked in `AWAITING_APPROVAL` while still executing.
- **Nonce is the capability.** The approval's `nonce` (`secrets.token_urlsafe(32)`, generated in `store/approvals.py`'s `create_approval`) is the only thing that later authorizes releasing real spending via the inbound Slack endpoint. Treat it as a bearer credential — never log it in full, never echo it back through any channel other than the Slack approval message.
- **Approval-then-transition ordering in `request_approval` is deliberate and must not be reordered.** The approval row is created *before* the run is transitioned to `AWAITING_APPROVAL`. If the run turns out not to be `RUNNING`, `transition()` no-ops (0 rows updated) and `RunNotRunning` is raised with the approval only flushed, never committed — the caller (`server.py`) must roll back rather than commit in that case. Doing it in the opposite order (transition first) would risk a run parked in `AWAITING_APPROVAL` with no approval row able to release it — a permanently stuck run with no operator recourse. **Never commit the session between the `create_approval` and `transition` calls.**
- **The `mcp>=1.0,<2.0` pin in `pyproject.toml` is load-bearing for this module specifically.** `mcp.server.fastmcp.FastMCP` was removed/renamed in mcp 2.0. Since this is the sole route to gated actions, an unreviewed bump of that pin breaks import at server startup — migrate `server.py` to the 2.x API deliberately before ever relaxing the pin.
- `payload` is caller-supplied structured data (e.g. `{"amount_usd": 20}`) and is never validated beyond being a `dict` — it is rendered into the Slack message as-is by `notify/blocks.py`. Do not assume any particular shape here.

### Testing Requirements
- `tests/gate/test_service.py` — covers valid kinds, `UnknownApprovalKind`, `RunNotRunning`, and the approval-before-transition ordering.
- `tests/gate/conftest.py` sets up fixtures; this test suite needs Docker running (testcontainers spins up a real Postgres 16), since `service.py` talks to `aicom.store`.
- Run with `.venv/bin/pytest tests/gate`.

### Common Patterns
- `service.py` is the only place with real logic; `server.py` is a thin MCP adapter that opens a session, calls `service.py`, and commits. Keep new logic in `service.py` so it stays testable without an MCP runtime.
- Exceptions are the control-flow signal for the FastAPI/MCP layer to interpret (`UnknownApprovalKind`, `RunNotRunning`) rather than sentinel return values.

## Dependencies

### Internal
- Imports `aicom.domain.enums.ApprovalKind`, `aicom.domain.enums.RunStatus`, `aicom.store.approvals.create_approval`, `aicom.store.runs.transition`, `aicom.config.load_settings`, `aicom.store.db`.
- Imported by nothing inside `aicom` — `server.py` is a standalone entry point (`python -m aicom.gate.server` style, invoked by the CLI via `--mcp-config` pointing at it), not imported by the orchestrator/worker code.

### External
- `mcp.server.fastmcp.FastMCP` (pinned `mcp>=1.0,<2.0`), `sqlalchemy.orm.Session`.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
