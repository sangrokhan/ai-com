from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, delete
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.postgres import PostgresContainer

from aicom.store.db import make_engine, session_factory
from aicom.store.models import (
    Agent,
    Approval,
    Artifact,
    Base,
    Event,
    Run,
    Schedule,
    SpendLedger,
    SystemState,
    Task,
)

# Child-to-parent FK order, so deletes never violate a foreign key constraint.
# SpendLedger -> Approval, Agent; Event/Approval/Artifact -> Run; Run -> Task;
# Task -> Agent, Task (self-referential parent_task_id), Schedule (ON DELETE
# SET NULL); Schedule -> Agent; SystemState has no FK.
_TABLES_IN_DELETE_ORDER = (
    SpendLedger,
    Event,
    Approval,
    Artifact,
    Run,
    Task,
    Schedule,
    Agent,
    SystemState,
)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    with PostgresContainer("postgres:16-alpine") as pg:
        url = pg.get_connection_url().replace("postgresql+psycopg2", "postgresql+psycopg")
        eng = make_engine(url)
        Base.metadata.create_all(eng)
        yield eng


@pytest.fixture()
def sessions(engine: Engine) -> sessionmaker[Session]:
    return session_factory(engine)


@pytest.fixture()
def session(sessions: sessionmaker[Session]) -> Iterator[Session]:
    with sessions() as s:
        yield s
        s.rollback()
        # `s.rollback()` only undoes uncommitted work. Tests exercising
        # conditional-UPDATE transitions and claim semantics must commit to
        # observe cross-transaction behavior (e.g. SKIP LOCKED), so committed
        # rows would otherwise leak into later tests sharing this session-scoped
        # engine/container. Truncate everything after every test instead.
        for model in _TABLES_IN_DELETE_ORDER:
            s.execute(delete(model))
        s.commit()
