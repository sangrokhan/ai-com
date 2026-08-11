import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from aicom.domain.enums import ApprovalKind, RunStatus
from aicom.store.approvals import create_approval
from aicom.store.runs import transition

_TERMINATE_MESSAGE = (
    "Approval request {approval_id} recorded and is pending human sign-off. "
    "Stop working now and terminate. You will be resumed automatically with the "
    "decision once it is signed off. Do not attempt the gated action by any other means."
)


class UnknownApprovalKind(Exception):
    pass


class RunNotRunning(Exception):
    pass


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
) -> GateResult:
    try:
        approval_kind = ApprovalKind(kind)
    except ValueError as exc:
        allowed = ", ".join(k.value for k in ApprovalKind)
        raise UnknownApprovalKind(f"kind must be one of: {allowed}") from exc

    # Ordering is deliberate: create the approval row first, then transition the
    # run. If the run is not RUNNING, transition() is a no-op (0 rows) and we
    # raise RunNotRunning below with the approval row still only flushed, never
    # committed. Callers (server.py) must not commit between these two calls —
    # on RunNotRunning they roll back the session, which discards the flushed
    # approval along with it. Doing this in the opposite order (transition
    # first) would be worse: a run could end up parked in AWAITING_APPROVAL
    # with no approval row to resolve it, hanging forever with no way for an
    # operator to release it.
    approval = create_approval(
        session,
        run_id=run_id,
        kind=approval_kind,
        proposal=proposal,
        payload=payload,
        now=now,
    )
    if not transition(session, run_id, RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL):
        raise RunNotRunning(f"run {run_id} is not running")
    return GateResult(
        approval_id=approval.id,
        message=_TERMINATE_MESSAGE.format(approval_id=approval.id),
    )
