''' One handler = one state transition. Nothing else may move a document's status.

Every handler opens with a guard on `doc.status`. That guard is what makes the whole
at-least-once design safe: a job replayed after a crash, a duplicate delivery, or a
stale sweep finds the document already past that state and returns without doing
anything. Remove the guard and a retry doubles the side effect.

RECEIVED -normalize-> NORMALIZED -classify-> CLASSIFIED -embed-> EMBEDDED -extract->
EXTRACTED (or NEEDS_REVIEW, decided by the confidence gate inside handle_extract)
-associate-> ASSOCIATED (or NEEDS_REVIEW, no MPI match) -route-> ROUTED -push-> PUSHED
'''

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.audit.log import write_audit
from app.enums import AuditAction, DocumentStatus, JobKind
from app.integrations import downstream
from app.jobs.queue import enqueue
from app.models.audit_log import Log
from app.models.document import Document
from app.models.extracted_field import ExtractedFieldRow
from app.models.member import Association, Member
from app.processing import bm25, chunk, classify, confidence, embed, extract
from app.processing import mpi, normalize, pdftext, routing
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
    to replay separately. A document that passes it goes on to associate; one that
    fails waits for a reviewer, whose resolve enqueues associate instead. '''
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
    if doc.status == DocumentStatus.EXTRACTED:
        enqueue(
            session, tenant_id=doc.tenant_id, document_id=doc.id, kind=JobKind.ASSOCIATE
        )


def handle_associate(session: Session, doc: Document) -> None:
    ''' Bind the document to its member in the tenant's MPI, or send it to review.
    Fields are trusted by now (the gate passed, or a reviewer verified them), so a
    miss here is about the MPI, not about extraction. '''
    if doc.status != DocumentStatus.EXTRACTED:
        return

    # A document back from review (or a downstream rejection) may carry a binding
    # from its last pass. It must not survive a re-match; the audit rows keep it.
    session.execute(delete(Association).where(Association.document_id == doc.id))
    detail: dict = {}
    if "member_id" in extract.FIELD_SCHEMAS.get(doc.doc_type, []):
        fields = {
            f.name: f
            for f in session.scalars(
                select(ExtractedFieldRow).where(ExtractedFieldRow.document_id == doc.id)
            )
        }
        id_field, dob_field = fields.get("member_id"), fields.get("dob")
        member_id = id_field.value if id_field else None
        member = mpi.lookup(session, doc.tenant_id, member_id) if member_id else None
        reasons = mpi.mismatches(member_id, dob_field and dob_field.value, member)
        if reasons:
            doc.status = DocumentStatus.NEEDS_REVIEW
            detail["reasons"] = reasons
        else:
            # match_confidence is the id's own: the match itself is exact.
            session.add(
                Association(
                    tenant_id=doc.tenant_id,
                    document_id=doc.id,
                    member_pk=member.id,
                    match_confidence=id_field.confidence,
                )
            )
            doc.status = DocumentStatus.ASSOCIATED
            detail["member"] = str(member.id)
    else:
        # Correspondence: nothing in the schema to match on. Associated with nobody,
        # which is the honest answer, not a failure.
        doc.status = DocumentStatus.ASSOCIATED
        detail["member"] = None

    write_audit(
        session,
        tenant_id=doc.tenant_id,
        document_id=doc.id,
        actor="system",
        action=AuditAction.STATE_CHANGE,
        detail={"to": doc.status.value, **detail},
    )
    if doc.status == DocumentStatus.ASSOCIATED:
        enqueue(session, tenant_id=doc.tenant_id, document_id=doc.id, kind=JobKind.ROUTE)


def handle_route(session: Session, doc: Document) -> None:
    ''' Pick the departmental queue. '''
    if doc.status != DocumentStatus.ASSOCIATED:
        return

    bound = session.scalar(
        select(Association.id).where(Association.document_id == doc.id)
    ) is not None
    doc.route_queue = routing.route(doc.doc_type, bound)
    doc.status = DocumentStatus.ROUTED
    write_audit(
        session,
        tenant_id=doc.tenant_id,
        document_id=doc.id,
        actor="system",
        action=AuditAction.STATE_CHANGE,
        detail={"to": "ROUTED", "queue": doc.route_queue.value},
    )
    enqueue(session, tenant_id=doc.tenant_id, document_id=doc.id, kind=JobKind.PUSH)


def handle_push(session: Session, doc: Document) -> None:
    ''' Hand the document to the downstream system. The one handler whose side effect
    lives in someone else's system, so it is the idempotency key, not the status
    guard, that stops a replay from submitting twice.

    The key counts this document's earlier successful pushes. A retry of this push
    (crash after the downstream accepted, before our commit) computes the same key
    and is deduped there; a re-push after a rejection and review gets the next one,
    so the downstream treats the corrected document as new. '''
    if doc.status != DocumentStatus.ROUTED:
        return

    pushes = session.scalar(
        select(func.count()).where(
            Log.document_id == doc.id,
            Log.action == AuditAction.STATE_CHANGE,
            Log.detail["to"].astext == DocumentStatus.PUSHED.value,
        )
    )
    key = f"{doc.id}:{pushes}"
    fields = dict(
        session.execute(
            select(ExtractedFieldRow.name, ExtractedFieldRow.value).where(
                ExtractedFieldRow.document_id == doc.id
            )
        ).all()
    )
    member_id = session.scalar(
        select(Member.member_id)
        .join(Association, Association.member_pk == Member.id)
        .where(Association.document_id == doc.id)
    )
    metadata = {
        "external_id": str(doc.id),
        "tenant_id": str(doc.tenant_id),
        "doc_type": doc.doc_type.value,
        "queue": doc.route_queue.value,
        "member_id": member_id,
        "summary": doc.summary,
        # Every field passed the gate or was verified by a human to get here.
        "fields": fields,
    }
    pdf = get_blob_store().get(doc.normalized_blob_key)

    # The call before the status flip, as with every other side effect: a raise
    # leaves the document ROUTED for the retry.
    status_code = downstream.push(
        metadata, pdf, f"{doc.doc_type.value}.pdf", idempotency_key=key
    )
    doc.status = DocumentStatus.PUSHED
    write_audit(
        session,
        tenant_id=doc.tenant_id,
        document_id=doc.id,
        actor="system",
        action=AuditAction.STATE_CHANGE,
        detail={"to": "PUSHED", "idempotency_key": key, "status_code": status_code},
    )


HANDLERS = {
    JobKind.NORMALIZE: handle_normalize,
    JobKind.CLASSIFY: handle_classify,
    JobKind.EMBED: handle_embed,
    JobKind.EXTRACT: handle_extract,
    JobKind.ASSOCIATE: handle_associate,
    JobKind.ROUTE: handle_route,
    JobKind.PUSH: handle_push,
}

# The status each handler's guard requires. Requeue puts a FAILED document back here
# and re-enqueues the kind that failed. Kept beside HANDLERS so a new handler and its
# precondition are written together.
REQUIRES = {
    JobKind.NORMALIZE: DocumentStatus.RECEIVED,
    JobKind.CLASSIFY: DocumentStatus.NORMALIZED,
    JobKind.EMBED: DocumentStatus.CLASSIFIED,
    JobKind.EXTRACT: DocumentStatus.EMBEDDED,
    JobKind.ASSOCIATE: DocumentStatus.EXTRACTED,
    JobKind.ROUTE: DocumentStatus.ASSOCIATED,
    JobKind.PUSH: DocumentStatus.ROUTED,
}
