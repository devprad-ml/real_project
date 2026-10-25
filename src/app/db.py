''' DB engine '''

from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

# create the db engine

s = get_settings()

engine = create_engine(
    s.database_url.get_secret_value(),
    pool_pre_ping=True,    # testing pooled connection
    pool_size=s.db_pool_size,
    # Every thread that can run a route must be able to get a connection. With the
    # default 5 + 10 and 40 threads, 40 concurrent requests held 15 connections while
    # waiting for threads and the other 25 threads waited for connections: a deadlock
    # that only ended when the 30 s pool timeout turned it into 500s. Connections are
    # opened on demand, so the ceiling costs nothing until load needs it.
    max_overflow=s.threadpool_size,
    pool_timeout=10,
)

#create the local session

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,   # to prevent uncommitted instert into next select queries
    expire_on_commit=False
)

# create the get_db function to not keep it open.

def get_session() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


