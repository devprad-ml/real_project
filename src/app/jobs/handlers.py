''' One handler = one state transition. Nothing else may move a document's status.

Every handler opens with a guard on `doc.status`. That guard is what makes the whole
at-least-once design safe: a job replayed after a crash, a duplicate delivery, or a
stale sweep finds the document already past that state and returns without doing
anything. Remove the guard and a retry doubles the side effect.

RECEIVED -normalize-> NORMALIZED -classify-> CLASSIFIED -embed-> EMBEDDED -extract->
EXTRACTED (or NEEDS_REVIEW, decided by the confidence gate inside handle_extract)
'''

from sqlalchemy.orm import Session

from app.audit.log import write_audit
from app.enums import AuditAction, DocumentStatus, JobKind
from app.jobs.queue import enqueue
from app.models.document import Document
from app.models.extracted_field import ExtractedFieldRow
from app.processing import bm25, chunk, classify, confidence, embed, extract
from app.processing import normalize, pdftext
from app.storage import get_blob_store, get_vector_store, move_normalized
from app.storage.blob import normalized_key
from app.storage.vectors import Point, point_id, tenant_collection


def handle_normalize(session: Session, doc: Document) -> None:
    if doc.status != DocumentStatus.RECEIVED:
        return

    blob = get_blob_store()
    raw = blob.get(doc.blob_key)
    pdfa, pages = normalize.run(raw)

    # Deterministic key (`unknown.pdf` until classify renames it), so a re-run after a
    # crash overwrites the same key instead of orphaning a half-written blob.
    derived_key = normalized_key(doc.tenant_id, doc.id, doc.doc_type)

    # Order matters: blob first, then status, then commit. Crash before the status
    # flip leaves the doc at RECEIVED and the job replays harmlessly. The reverse
    # order would mark a document NORMALIZED with no derived artifact behind it.
    blob.put(derived_key, pdfa, "application/pdf")
    doc.normalized_blob_key = derived_key
    doc.page_count = pages
    doc.status = DocumentStatus.NORMALIZED

    write_audit(
        session,
        tenant_id=doc.tenant_id,
        document_id=doc.id,
        actor="system",
        action=AuditAction.STATE_CHANGE,
        detail={"to": "NORMALIZED", "pages": pages},
    )
    enqueue(session, tenant_id=doc.tenant_id, document_id=doc.id, kind=JobKind.CLASSIFY)


def handle_classify(session: Session, doc: Document) -> None:
    if doc.status != DocumentStatus.NORMALIZED:
        return

    pdfa = get_blob_store().get(doc.normalized_blob_key)
    result = classify.run(text for _, text in pdftext.pages(pdfa))

    doc.doc_type = result.doc_type
    # Recorded, not acted on: the confidence gate that sends a document to review
    # arrives with extract (M7) and looks at this together with the field scores.
    doc.classify_confidence = result.confidence
    move_normalized(session, doc, pdfa)
    doc.status = DocumentStatus.CLASSIFIED

    write_audit(
        session,
        tenant_id=doc.tenant_id,
        document_id=doc.id,
        actor="system",
        action=AuditAction.STATE_CHANGE,
        detail={
            "to": "CLASSIFIED",
            "doc_type": result.doc_type.value,
            "confidence": result.confidence,
        },
    )
    enqueue(session, tenant_id=doc.tenant_id, document_id=doc.id, kind=JobKind.EMBED)


def index_document(doc: Document) -> int:
    ''' Chunk the normalized PDF's text and upsert dense + bm25 vectors. Returns the
    chunk count. Shared by `handle_embed` and the reindex command: the vector store is
    rebuilt from blob storage exactly the way it was first written. '''
    pdfa = get_blob_store().get(doc.normalized_blob_key)
    chunks = chunk.chunk_pages(pdftext.pages(pdfa))

    # A document whose every page is blank (a fax of a blank sheet) has nothing to
    # embed. That is a finished document, not a failure -- raising here would burn
    # MAX_ATTEMPTS to reach the same answer and then lie about it with FAILED.
    if not chunks:
        return 0

    embeddings = embed.encode([c.text for c in chunks])
    get_vector_store().upsert(
        tenant_collection(doc.tenant_id),
        [
            Point(
                id=point_id(doc.id, c.page_no, c.idx),
                vector=vector,
                sparse=bm25.encode_doc(c.text),
                # text rides along in the payload: a RAG read gets its context from
                # one vector-DB hit, with no fan-out back into Postgres. doc_type is
                # a copy of the Postgres column for filtering; Postgres stays the
                # truth, so a reclassify must update these points too.
                payload={
                    "tenant_id": str(doc.tenant_id),
                    "document_id": str(doc.id),
                    "page_no": c.page_no,
                    "doc_type": doc.doc_type.value,
                    "text": c.text,
                },
            )
            for c, vector in zip(chunks, embeddings)
        ],
    )
    return len(chunks)


def handle_embed(session: Session, doc: Document) -> None:
    ''' Two stores have to agree here, and only one of them has transactions. '''
    if doc.status != DocumentStatus.CLASSIFIED:
        return

    count = index_document(doc)

    # Vectors first, then the status flip, then commit -- same ordering discipline as
    # handle_normalize. A raise mid-upsert leaves the doc at CLASSIFIED and the retry
    # overwrites the same point ids, so a half-written collection self-heals. The
    # reverse order would mark a document EMBEDDED that no RAG query can ever find.
    doc.status = DocumentStatus.EMBEDDED
    write_audit(
        session,
        tenant_id=doc.tenant_id,
        document_id=doc.id,
        actor="system",
        action=AuditAction.STATE_CHANGE,
        detail={"to": "EMBEDDED", "chunks": count},
    )
    enqueue(session, tenant_id=doc.tenant_id, document_id=doc.id, kind=JobKind.EXTRACT)


def handle_extract(session: Session, doc: Document) -> None:
    ''' The confidence gate lives here, not in a separate job: no second Claude call
    to replay separately, and no `associate` job enqueued -- M9 doesn't have a
    handler for it yet, and an unregistered kind would fail a correctly-extracted
    document straight to FAILED. A document that passes the gate just stays at
    EXTRACTED; M9 will scan for that status directly. '''
    if doc.status != DocumentStatus.EMBEDDED:
        return

    pdfa = get_blob_store().get(doc.normalized_blob_key)
    result = extract.run(doc.doc_type, (text for _, text in pdftext.pages(pdfa)))
    # Only schema names are stored, so a name Claude invented never reaches the DB.
    found = {f.name: f for f in result.fields} if result else {}

    rows = []
    for name in extract.FIELD_SCHEMAS.get(doc.doc_type, []):
        f = found.get(name)
        value = f.value if f else None
        rows.append(
            ExtractedFieldRow(
                tenant_id=doc.tenant_id,
                document_id=doc.id,
                name=name,
                value=value,
                confidence=confidence.score(name, value, f.confidence if f else 0.0),
            )
        )
    session.add_all(rows)

    reasons = confidence.review_reasons(doc, rows)
    if result is None:
        reasons.insert(0, "extract_refused")
    else:
        doc.summary = result.summary
    doc.status = DocumentStatus.NEEDS_REVIEW if reasons else DocumentStatus.EXTRACTED

    write_audit(
        session,
        tenant_id=doc.tenant_id,
        document_id=doc.id,
        actor="system",
        action=AuditAction.STATE_CHANGE,
        detail={"to": doc.status.value, "fields": len(rows), "reasons": reasons},
    )


HANDLERS = {
    JobKind.NORMALIZE: handle_normalize,
    JobKind.CLASSIFY: handle_classify,
    JobKind.EMBED: handle_embed,
    JobKind.EXTRACT: handle_extract,
}
