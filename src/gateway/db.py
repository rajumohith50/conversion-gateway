"""Engine and session construction.

Synchronous SQLAlchemy on purpose. FastAPI runs `def` endpoints in a thread
pool, so sync sessions cost nothing in throughput at this scale and avoid
the second set of APIs (AsyncSession, async drivers, async context managers)
that async SQLAlchemy brings. The worker and CLI are plain sync programs and
share the same code.
"""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str) -> Engine:
    # pool_pre_ping: a connection that died while idle (Postgres restart,
    # network blip) is detected and replaced instead of surfacing as an
    # error on the next request.
    return create_engine(database_url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: after a transaction commits we still read
    # attributes off the ORM objects to build the HTTP response. With the
    # default, each attribute access after commit would issue a new SELECT.
    return sessionmaker(bind=engine, expire_on_commit=False)


__all__ = ["Session", "make_engine", "make_session_factory"]
