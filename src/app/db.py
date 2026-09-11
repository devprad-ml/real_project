''' DB engine '''

from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

# create the db engine

s = get_settings()

engine = create_engine(
    s.database_url.get_secret_value(),
    pool_pre_ping=True    # testing pooled connection
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


