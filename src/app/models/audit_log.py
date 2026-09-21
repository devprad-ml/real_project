import uuid
import datetime

from sqlalchemy import String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import ForeignKey, Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from app.models.base import Base
from app.enums import AuditAction

class Log(Base):
    __tablename__= "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clients.id"))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"))
    actor: Mapped[str] = mapped_column(String(50))
    action: Mapped[AuditAction] = mapped_column(SQLEnum(AuditAction, native_enum=False))
    detail: Mapped[dict] = mapped_column(JSONB)
    at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())

