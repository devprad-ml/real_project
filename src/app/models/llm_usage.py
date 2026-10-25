import datetime
import uuid

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base


class LlmUsage(Base):
    ''' One row per Claude call that returned. Tokens, never dollars: prices change,
    tokens do not. Append-only for app_user, like audit_log. See m13.md §3.3. '''

    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_llm_usage_tenant_at", "tenant_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clients.id"))
    # Null for /ask: a question spans several documents.
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id"), nullable=True
    )
    purpose: Mapped[str] = mapped_column(String(12))  # classify | extract | ask
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer)
    # Includes thinking tokens: that is what the API bills.
    output_tokens: Mapped[int] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())
