''' GET /: the start page, every document with the email it came from. '''

import datetime
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.deps import require_role
from app.enums import DocumentType
from app.models.document import Document
from app.models.principal import Principal
from app.models.source import Src

router = APIRouter(tags=["Dashboard"])
PAGE = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"


class IntakeRow(BaseModel):
    id: uuid.UUID
    filename: str | None
    doc_type: DocumentType
    state: str  # processing | ready | failed
    error: bool
    pages: int | None
    sender: str | None
    subject: str | None
    sent_at: str | None  # the email's Date header, when it had a usable one
    received_at: datetime.datetime


def _state(doc: Document, max_attempts: int) -> str:
    if doc.processed_at is not None:
        return "ready"
    return "failed" if doc.attempts >= max_attempts else "processing"


@router.get("/", include_in_schema=False)
def dashboard_page() -> FileResponse:
    return FileResponse(PAGE)


@router.get("/intake", response_model=list[IntakeRow])
def intake(
    limit: int = Query(200, ge=1, le=500),
    principal: Principal = Depends(require_role()),
    db: Session = Depends(get_session),
) -> list[IntakeRow]:
    max_attempts = get_settings().max_attempts
    rows = db.execute(
        select(Document, Src.raw_meta)
        .outerjoin(Src, Src.id == Document.source_id)
        .where(Document.tenant_id == principal.tenant_id)
        .order_by(Document.received_at.desc(), Document.id)
        .limit(limit)
    ).all()
    out = []
    for doc, meta in rows:
        meta = meta or {}
        out.append(
            IntakeRow(
                id=doc.id,
                filename=doc.source_filename,
                doc_type=doc.doc_type,
                state=_state(doc, max_attempts),
                # A flag, not the text: exception text can carry document content.
                error=doc.error is not None,
                pages=doc.page_count,
                sender=meta.get("from"),
                subject=meta.get("subject"),
                sent_at=meta.get("sent_at"),
                received_at=doc.received_at,
            )
        )
    return out
