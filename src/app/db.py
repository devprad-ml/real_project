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
    # Ceiling = threadpool_size, exactly. Below it: with the default 5 + 10 and 40
    # threads, 40 requests held 15 connections waiting for threads while 25 threads
    # waited for connections, a deadlock the 30 s pool timeout turned into 500s. Above
    # it is waste: limit_inflight admits threadpool_size requests and each holds one
    # connection. Connections open on demand, so a worker (about 4 in use) never
    # reaches it.
    # Budget: api_processes * threadpool_size + workers * 4 < Postgres max_connections
    # (100 by default). Past that, lower threadpool_size or put PgBouncer in front;
    # transaction mode is fine, claim_one's FOR UPDATE lives in one transaction.
    pool_size=min(s.db_pool_size, s.threadpool_size),
    max_overflow=max(0, s.threadpool_size - s.db_pool_size),
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


