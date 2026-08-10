from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.postgres import PostgresContainer

from aicom.store.db import make_engine, session_factory
from aicom.store.models import Base


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
