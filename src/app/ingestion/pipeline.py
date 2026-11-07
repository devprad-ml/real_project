''' Stored attachment -> searchable PDF -> classified -> embedded, on a small thread pool
inside the API process, so documents run side by side without blocking requests.

ponytail: assumes one API process. `_inflight` is per process, so two would duplicate
work; row locks or a shared queue are the upgrade if that changes.
'''

import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor

from sqlalchemy import func, select

from app.audit.log import write_audit
from app.config import get_settings
from app.db import SessionLocal
from app.enums import AuditAction
from app.errors import PermanentError
from app.models.document import Document
from app.observability import tracing
from app.observability.logging import log
from app.rag import bm25, chunk, classify, embed, normalize, pdftext
from app.storage import get_blob_store, get_vector_store
from app.storage.blob import normalized_key
from app.storage.vectors import Point, point_id, tenant_collection

_pool = ThreadPoolExecutor(
    max_workers=get_settings().ingest_workers, thread_name_prefix="ingest"
)
_inflight: set[uuid.UUID] = set()
_lock = threading.Lock()


def submit(document_id: uuid.UUID) -> None:
    ''' Queue one document unless it is already queued or running. Callable from any
    thread: nothing here needs the event loop. '''
    with _lock:
        if document_id in _inflight:
            return
        _inflight.add(document_id)
    _pool.submit(process, document_id).add_done_callback(
        lambda future: _finished(document_id, future)
    )


def _finished(document_id: uuid.UUID, future: Future) -> None:
    with _lock:
        _inflight.discard(document_id)
    if not future.cancelled() and (exc := future.exception()) is not None:
        # Only bookkeeping can land here (the database was down); retry_unfinished
        # picks the document up on a later tick.
        log.error(
            "ingest_crashed",
            extra={"document_id": document_id, "error_type": type(exc).__name__},
        )


def retry_unfinished() -> int:
    ''' Queue every document with attempts left: failures, and whatever a restart cut
    short. Runs on a timer, which also spaces the retries out. '''
    with SessionLocal() as session:
        ids = session.scalars(
            select(Document.id).where(
                Document.processed_at.is_(None),
                Document.attempts < get_settings().max_attempts,
            )
        ).all()
    for document_id in ids:
        submit(document_id)
    return len(ids)


def process(document_id: uuid.UUID) -> None:
    max_attempts = get_settings().max_attempts
    with SessionLocal() as session:
        doc = session.get(Document, document_id)
        if doc is None or doc.processed_at is not None or doc.attempts >= max_attempts:
            return
        tenant_id = doc.tenant_id
        # Committed before the work: a document that kills the process still uses up
        # an attempt, so it cannot crash every restart forever.
        doc.attempts += 1
        session.commit()

        try:
            with tracing.observe(
                "ingest",
                tenant_id=tenant_id,
                input=doc.source_filename,
                metadata={"document_id": str(document_id), "attempt": doc.attempts},
            ) as traced:
                detail = _run(doc)
                tracing.finish(traced, output=detail, root=True, metadata=detail)
            doc.processed_at = func.now()
            doc.error = None
            write_audit(
                session,
                tenant_id=tenant_id,
                document_id=document_id,
                actor="system",
                action=AuditAction.STATE_CHANGE,
                detail={"event": "processed", **detail},
            )
            session.commit()
        except Exception as exc:
            session.rollback()
            # The full text stays on the row; exception text can carry document
            # content, so logs and the append-only audit log get only the class name.
            doc.error = repr(exc)[:2000]
            if isinstance(exc, PermanentError):
                doc.attempts = max_attempts
            exhausted = doc.attempts >= max_attempts
            if exhausted:
                write_audit(
                    session,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    actor="system",
                    action=AuditAction.STATE_CHANGE,
                    detail={"event": "failed", "error_type": type(exc).__name__},
                )
            session.commit()
            log.warning(
                "ingest_failed" if exhausted else "ingest_retry",
                extra={
                    "tenant_id": tenant_id,
                    "document_id": document_id,
                    "attempts": doc.attempts,
                    "error_type": type(exc).__name__,
                },
            )


def _run(doc: Document) -> dict:
    ''' Every side effect overwrites a fixed key or point id, so a retry from the top
    is safe. '''
    blob = get_blob_store()
    with tracing.observe("normalize"):
        pdfa, pages = normalize.run(blob.get(doc.blob_key))
    result = classify.run(text for _, text in pdftext.pages(pdfa))

    doc.doc_type = result.doc_type
    doc.classify_confidence = result.confidence
    doc.page_count = pages
    doc.normalized_blob_key = normalized_key(doc.tenant_id, doc.id, doc.doc_type)
    blob.put(doc.normalized_blob_key, pdfa, "application/pdf")

    with tracing.observe("embed"):
        chunks = index_document(doc)
    return {
        "doc_type": result.doc_type.value,
        "confidence": result.confidence,
        "pages": pages,
        "chunks": chunks,
    }


def index_document(doc: Document) -> int:
    ''' Chunk the normalized PDF and upsert its dense + bm25 vectors. Returns the chunk
    count. Shared with the reindex command, which rebuilds the store from blobs. '''
    pdfa = get_blob_store().get(doc.normalized_blob_key)
    chunks = chunk.chunk_pages(pdftext.pages(pdfa))
    # A fax of a blank sheet has nothing to embed; that is a finished document.
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
                # Text rides in the payload so a RAG read needs no fan-out to Postgres.
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


def shutdown() -> None:
    _pool.shutdown(wait=False, cancel_futures=True)
