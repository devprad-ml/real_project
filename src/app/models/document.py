import uuid
import datetime

from sqlalchemy import String, UniqueConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import ForeignKey, Enum as SQLEnum
from sqlalchemy.sql import func

from app.models.base import Base
from app.enums import DocumentType

class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("tenant_id","sha256", name="unique_tenant_sha256"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clients.id"))
    sha256: Mapped[str] = mapped_column(String(64))
    doc_type: Mapped[DocumentType] = mapped_column(SQLEnum(DocumentType, native_enum=False))  # unknown type is weak excuse for enterprise app
    blob_key: Mapped[str] = mapped_column(String(1024))
    # Nullable: a later API intake has no email behind it. One message -> many
    # documents, so the filename lives here, not on the shared sources row.
    source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    source_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    normalized_blob_key: Mapped[str | None] = mapped_column(nullable=True)
    page_count: Mapped[int | None] = mapped_column(nullable=True)
    received_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    classify_confidence: Mapped[float | None] = mapped_column(nullable=True)
    # Null until the pipeline finishes. Null with attempts used up means it gave up.
    processed_at: Mapped[datetime.datetime | None] = mapped_column(nullable=True)
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    # Full exception text: may carry document content, so it stays out of logs.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
