''' GET /documents/{id}/file: the normalized PDF, for a reviewer to read. '''

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.log import write_audit
from app.db import get_session
from app.deps import actor, require_role
from app.enums import AuditAction
from app.models.document import Document
from app.models.principal import Principal
from app.storage import get_blob_store

router = APIRouter(tags=["Documents"])


def tenant_doc(
    db: Session, principal: Principal, document_id: uuid.UUID, *, lock: bool = False
) -> Document:
    ''' The caller's document or 404. Another tenant's document is a 404 too, not a
    403: a 403 would confirm the id exists. '''
    stmt = select(Document).where(
        Document.id == document_id, Document.tenant_id == principal.tenant_id
    )
    doc = db.scalar(stmt.with_for_update() if lock else stmt)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="document not found")
    return doc


@router.get("/documents/{document_id}/file")
def document_file(
    document_id: uuid.UUID,
    principal: Principal = Depends(require_role()),
    db: Session = Depends(get_session),
) -> Response:
    doc = tenant_doc(db, principal, document_id)
    if doc.normalized_blob_key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="not normalized yet")
    data = get_blob_store().get(doc.normalized_blob_key)
    write_audit(
        db,
        tenant_id=doc.tenant_id,
        document_id=doc.id,
        actor=actor(principal),
        action=AuditAction.VIEW,
        detail={"via": "file"},
    )
    # Committed before the bytes leave: a view with no audit row must never happen.
    db.commit()
    return Response(data, media_type="application/pdf")
