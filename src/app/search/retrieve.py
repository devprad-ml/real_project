''' Query text -> ranked chunks for one tenant. The read side of `handle_embed`.

Text and page number come from the vector payload, written at embed time for exactly
this read. Postgres is touched once, for the filename and status, and that same query
re-checks that every hit belongs to the caller's tenant.
'''

import uuid

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.log import write_audit
from app.enums import AuditAction, DocumentStatus
from app.models.document import Document
from app.processing import embed
from app.storage import get_vector_store
from app.storage.vectors import tenant_collection


class SearchHit(BaseModel):
    document_id: uuid.UUID
    filename: str | None
    status: DocumentStatus
    page_no: int
    score: float
    text: str


def search(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    query: str,
    limit: int = 10,
    actor: str = "api",
) -> list[SearchHit]:
    ''' Does not commit: the caller owns the transaction, and the audit rows staged
    here must commit with it. Results without their audit rows must never happen. '''
    # The same encode handle_embed uses: vectors from two different models are not
    # comparable, so there is exactly one place a vector can come from.
    vector = embed.encode([query])[0]
    hits = get_vector_store().search(tenant_collection(tenant_id), vector, limit)
    if not hits:
        return []

    # Filtered on tenant_id as well as id. The collection is already per tenant; this
    # is the second lock, for a point in the wrong collection or a document row that
    # is gone while its vectors linger. A hit with no row here is dropped.
    ids = {uuid.UUID(h.payload["document_id"]) for h in hits}
    docs = {
        d.id: d
        for d in session.scalars(
            select(Document).where(Document.id.in_(ids), Document.tenant_id == tenant_id)
        )
    }

    results = [
        SearchHit(
            document_id=doc.id,
            filename=doc.source_filename,
            status=doc.status,
            page_no=h.payload["page_no"],
            score=h.score,
            text=h.payload["text"],
        )
        for h in hits
        if (doc := docs.get(uuid.UUID(h.payload["document_id"]))) is not None
    ]

    # A search returns chart text, which is a disclosure. One row per document, in
    # rank order: "which documents did they see" is the question an auditor asks.
    for document_id in dict.fromkeys(r.document_id for r in results):
        write_audit(
            session,
            tenant_id=tenant_id,
            document_id=document_id,
            actor=actor,
            action=AuditAction.VIEW,
            detail={"via": "search", "query": query},
        )
    return results
