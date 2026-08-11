import uuid

from sqlalchemy.orm import Session

from aicom.store.models import Event


def append_event(session: Session, run_id: uuid.UUID, seq: int, type_: str, payload: dict) -> None:
    session.add(Event(id=uuid.uuid4(), run_id=run_id, seq=seq, type=type_, payload=payload))
