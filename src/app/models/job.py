import uuid
import datetime

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import ForeignKey, Enum as SQLEnum
from sqlalchemy.sql import func

from app.models.base import Base
from app.enums import JobKind, JobState


class Job(Base):
    __tablename__ = "jobs"
    # SQLEnum(native_enum=False) stores the member NAME, so the predicate is 'PENDING'.
    # Partial: only pending rows are ever scanned by claim_one, and done/error rows
    # accumulate forever -- a full index would carry the whole history.
    __table_args__ = (
        Index(
            "ix_jobs_claim",
            "run_after",
            postgresql_where="state = 'PENDING'",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clients.id"))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"))
    kind: Mapped[JobKind] = mapped_column(SQLEnum(JobKind, native_enum=False))
    state: Mapped[JobState] = mapped_column(
        SQLEnum(JobState, native_enum=False), default=JobState.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime.datetime] = mapped_column(server_default=func.now())
    # Database clock (now()), never a worker's: workers on different hosts compare
    # each other's locks, and a 30 s lease cannot absorb clock skew.
    locked_at: Mapped[datetime.datetime | None] = mapped_column(nullable=True)
    # Which worker holds a RUNNING job (host:pid). Every write a worker makes to a job
    # it claimed is conditional on this, so one whose lease was swept cannot finish,
    # fail or heartbeat the run that another worker took over.
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())
