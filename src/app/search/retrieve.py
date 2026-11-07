''' Query text -> ranked chunks for one tenant. The read side of `index_document`.

Hybrid: the query becomes a dense vector (meaning) and a bm25 sparse vector (exact
tokens), and the store fuses both rankings with RRF. A cross-encoder then reorders those
candidates, and a chunk naming an identifier from the query goes first.

Text and page number come from the vector payload, written at embed time for exactly
this read. Postgres is touched once, for filename and type, and that same
query re-checks that every hit belongs to the caller's tenant.
'''

import re
import uuid

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.log import write_audit
from app.enums import AuditAction, DocumentType
from app.models.document import Document
from app.rag import bm25, embed, rerank
from app.storage import get_vector_store
from app.storage.vectors import Hit, tenant_collection

# Reranked per query. More finds more and costs latency on CPU.
CANDIDATES = 20
# ponytail: names past this are left out, counts stay complete. Roughly 2,000 tokens, sent
# with every question. Send it only for listing questions if that cost starts to matter.
INVENTORY_LIMIT = 100
# A claim number, member id, denial code or CPT code: C-9779, M-1001, CO-197, 72148.
_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]{1,3}-?\d[\d-]*|\d{4,})(?![A-Za-z0-9])"
)


class SearchHit(BaseModel):
    document_id: uuid.UUID
    filename: str | None
    doc_type: DocumentType
    page_no: int
    score: float  # cross-encoder score: comparable within one result list only
    text: str


def _matches(identifier: str, text: str) -> bool:
    # Bounded, so C-977 does not match C-9779.
    pattern = rf"(?<![A-Za-z0-9]){re.escape(identifier)}(?![A-Za-z0-9])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _rank(query: str, hits: list[Hit]) -> list[Hit]:
    ''' Chunks of documents naming the query's identifiers first, then by score. Neither
    embeddings nor the cross-encoder treat C-9779 and C-9778 as different things.

    Credit goes to the whole document: page 2 of a denial often holds the reason without
    repeating the claim number that page 1 carries. '''
    identifiers = _IDENTIFIER.findall(query)
    named: dict[str, set[str]] = {}
    for h in hits:
        found = {i for i in identifiers if _matches(i, h.payload["text"])}
        named.setdefault(h.payload["document_id"], set()).update(found)
    scored = [
        Hit(h.id, s, h.payload)
        for h, s in zip(hits, rerank.score(query, [h.payload["text"] for h in hits]))
    ]
    return sorted(scored, key=lambda h: (-len(named[h.payload["document_id"]]), -h.score))


def retrieve(
    session: Session, *, tenant_id: uuid.UUID, query: str, limit: int = 10
) -> list[SearchHit]:
    ''' Ranked hits, nothing audited. Every caller must audit what it discloses:
    `search` below for /search, `api.ask` for /ask. '''
    # The same encode index_document uses: vectors from two different models are not
    # comparable, so there is exactly one place a vector can come from.
    vector = embed.encode([query])[0]
    hits = get_vector_store().search(
        tenant_collection(tenant_id),
        vector,
        bm25.encode_query(query),
        max(limit, CANDIDATES),
        query,
    )
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

    return [
        SearchHit(
            document_id=doc.id,
            filename=doc.source_filename,
            # From Postgres, not the payload: the payload copy can lag a reclassify.
            doc_type=doc.doc_type,
            page_no=h.payload["page_no"],
            score=h.score,
            text=h.payload["text"],
        )
        for h in _rank(query, hits)
        if (doc := docs.get(uuid.UUID(h.payload["document_id"]))) is not None
    ][:limit]


def inventory(session: Session, tenant_id: uuid.UUID) -> str:
    ''' What this tenant has received, for questions a handful of chunks cannot answer,
    like "how many denials". '''
    counts = session.execute(
        select(Document.doc_type, func.count())
        .where(Document.tenant_id == tenant_id, Document.processed_at.is_not(None))
        .group_by(Document.doc_type)
    ).all()
    if not counts:
        return ""
    newest = session.execute(
        select(Document.source_filename, Document.doc_type, Document.received_at)
        .where(Document.tenant_id == tenant_id, Document.processed_at.is_not(None))
        .order_by(Document.received_at.desc(), Document.id)
        .limit(INVENTORY_LIMIT)
    ).all()
    by_type = ", ".join(sorted(f"{t.value} {n}" for t, n in counts))
    return "\n".join(
        [
            f"{sum(n for _, n in counts)} documents received and searchable.",
            f"By type: {by_type}",
            f"Newest {len(newest)}:",
            *(f"- {name} ({t.value}, {at:%Y-%m-%d})" for name, t, at in newest),
        ]
    )


def audit_views(
    session: Session,
    results: list[SearchHit],
    *,
    tenant_id: uuid.UUID,
    actor: str,
    detail: dict,
) -> None:
    ''' One VIEW row per document, in rank order: "which documents did they see" is
    the question an auditor asks. '''
    for document_id in dict.fromkeys(r.document_id for r in results):
        write_audit(
            session,
            tenant_id=tenant_id,
            document_id=document_id,
            actor=actor,
            action=AuditAction.VIEW,
            detail=detail,
        )


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
    results = retrieve(session, tenant_id=tenant_id, query=query, limit=limit)
    # A search returns chart text, which is a disclosure.
    audit_views(
        session,
        results,
        tenant_id=tenant_id,
        actor=actor,
        detail={"via": "search", "query": query},
    )
    return results
