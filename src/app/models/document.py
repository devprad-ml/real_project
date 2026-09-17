import uuid
import datetime

from sqlalchemy import String, UniqueConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import ForeignKey, Enum as SQLEnum
from sqlalchemy.sql import func

from app.models.base import Base
from app.enums import DocumentStatus, DocumentType

class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("sha256", name="unique_sha256"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clients.id"))  for later
    sha256: Mapped[str] = mapped_column(String(64))
    doc_type: Mapped[DocumentType] = mapped_column(SQLEnum(DocumentType, native_enum=False))  # unknown type is weak excuse for enterprise app
    blob_key: Mapped[str] = mapped_column(String(1024))
    normalized_blob_key: Mapped[str | None] = mapped_column(nullable=True)
    page_count: Mapped[int | None] = mapped_column(nullable=True)
    received_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    status: Mapped[DocumentStatus] = mapped_column(SQLEnum(DocumentStatus, native_enum=False))  # cant have default for this
    classify_confidence: Mapped[float | None] = mapped_column(nullable=True)
    summary: Mapped[str | None] = mapped_column(Text)

