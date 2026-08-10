from aicom.domain.enums import RunStatus

TERMINAL: frozenset[RunStatus] = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.TIMED_OUT, RunStatus.CANCELLED}
)

_ALLOWED: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.TIMED_OUT,
            RunStatus.AWAITING_APPROVAL,
            RunStatus.QUEUED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.AWAITING_APPROVAL: frozenset({RunStatus.QUEUED, RunStatus.CANCELLED}),
}


class IllegalTransition(Exception):
    def __init__(self, frm: RunStatus, to: RunStatus) -> None:
        super().__init__(f"illegal run transition: {frm} -> {to}")
        self.frm = frm
        self.to = to


def can_transition(frm: RunStatus, to: RunStatus) -> bool:
    return to in _ALLOWED.get(frm, frozenset())


def assert_transition(frm: RunStatus, to: RunStatus) -> None:
    if not can_transition(frm, to):
        raise IllegalTransition(frm, to)
