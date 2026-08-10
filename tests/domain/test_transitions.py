import pytest

from aicom.domain.enums import RunStatus
from aicom.domain.transitions import IllegalTransition, assert_transition, can_transition


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (RunStatus.QUEUED, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.SUCCEEDED),
        (RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL),
        (RunStatus.RUNNING, RunStatus.QUEUED),  # usage-limit requeue, stale recovery
        (RunStatus.AWAITING_APPROVAL, RunStatus.QUEUED),  # resume after sign-off
        (RunStatus.AWAITING_APPROVAL, RunStatus.CANCELLED),
    ],
)
def test_legal_transitions(frm: RunStatus, to: RunStatus) -> None:
    assert can_transition(frm, to) is True


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (RunStatus.QUEUED, RunStatus.SUCCEEDED),
        (RunStatus.SUCCEEDED, RunStatus.RUNNING),
        (RunStatus.FAILED, RunStatus.QUEUED),
        (RunStatus.AWAITING_APPROVAL, RunStatus.RUNNING),  # must go through queued
        (RunStatus.RUNNING, RunStatus.RUNNING),
    ],
)
def test_illegal_transitions(frm: RunStatus, to: RunStatus) -> None:
    assert can_transition(frm, to) is False
    with pytest.raises(IllegalTransition):
        assert_transition(frm, to)


def test_terminal_states_have_no_successors() -> None:
    from aicom.domain.transitions import TERMINAL

    assert TERMINAL == {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.TIMED_OUT,
        RunStatus.CANCELLED,
    }
    for status in TERMINAL:
        assert all(not can_transition(status, other) for other in RunStatus)
