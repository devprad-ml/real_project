import uuid

from sqlalchemy import Boolean, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ExtractedFieldRow(Base):
    __tablename__ = "extracted_fields"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clients.id"))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"))
    name: Mapped[str] = mapped_column(String(100))
    # Nullable: an expected field Claude could not find is a row, not a missing row --
    # the review queue needs something concrete to show ("could not find a service
    # date") instead of silence.
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float)
    human_verified: Mapped[bool] = mapped_column(Boolean, default=False)
