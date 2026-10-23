import datetime
import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class Member(Base):
    ''' One row of the tenant's Master Patient Index, loaded by `python -m
    app.members load`. Stands in for the enterprise MPI service. '''

    __tablename__ = "members"
    __table_args__ = (
        UniqueConstraint("tenant_id", "member_id", name="unique_tenant_member"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clients.id"))
    # Normalized (mpi.normalize_id): uppercase, alphanumerics only.
    member_id: Mapped[str] = mapped_column(String(32))
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    dob: Mapped[datetime.date | None]


class Association(Base):
    ''' A document bound to a member. At most one per document: a document with no
    row here was routed unmatched (no member_id in its schema, or a reviewer said
    the member is not in the MPI). '''

    __tablename__ = "associations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clients.id"))
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id"), unique=True
    )
    member_pk: Mapped[uuid.UUID] = mapped_column(ForeignKey("members.id"))
    match_confidence: Mapped[float]
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())
