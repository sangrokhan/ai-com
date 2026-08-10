from datetime import datetime

from sqlalchemy.orm import Session

from aicom.store.models import SystemState


def _row(session: Session) -> SystemState:
    state = session.get(SystemState, 1)
    if state is None:
        state = SystemState(id=1)
        session.add(state)
        session.flush()
    return state


def get_pause(session: Session) -> datetime | None:
    return _row(session).llm_paused_until


def set_pause(session: Session, until: datetime, reason: str) -> None:
    state = _row(session)
    state.llm_paused_until = until
    state.pause_reason = reason
    state.consecutive_pauses += 1


def mark_pause_notified(session: Session, at: datetime) -> None:
    _row(session).pause_notified_at = at


def pause_notified_at(session: Session) -> datetime | None:
    return _row(session).pause_notified_at


def clear_pause(session: Session) -> None:
    state = _row(session)
    state.llm_paused_until = None
    state.pause_reason = None
    state.pause_notified_at = None
    state.consecutive_pauses = 0
