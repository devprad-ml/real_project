''' DB engine '''

from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import get_settings

# create the db engine

s = get_settings()

engine = create_engine(
    s.database_url.get_secret_value(),
    pool_pre_ping=True,    # testing pooled connection
    # Ceiling = one connection per request thread, per ingest thread, plus the timer's.
    # Below it: with the default 5 + 10 and 40 threads, 40 requests held 15 connections
    # waiting for threads while 25 threads waited for connections, a deadlock the 30 s
    # pool timeout turned into 500s. Keep the total under Postgres max_connections
    # (100 by default).
    pool_size=min(s.db_pool_size, s.threadpool_size),
    max_overflow=max(0, s.threadpool_size + s.ingest_workers + 1 - s.db_pool_size),
    pool_timeout=10,
)

# /readyz's own connection, outside the pool above: under full load every pooled
# connection is in use, and a probe that waits for one makes a busy pod read as a broken
# one. NullPool opens one short connection per probe (+1 to the budget above).
probe_engine = create_engine(
    s.database_url.get_secret_value(),
    poolclass=NullPool,
    connect_args={"connect_timeout": 3},
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


