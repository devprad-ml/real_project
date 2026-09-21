import uuid

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import Base


class Src(Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("ref", name="unique_ref"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clients.id"))
    ref: Mapped[str] = mapped_column(String(100))
    channel: Mapped[str] = mapped_column(String(10))
    raw_meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
