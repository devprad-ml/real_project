''' One handler = one state transition. Nothing else may move a document's status.

Every handler opens with a guard on `doc.status`. That guard is what makes the whole
at-least-once design safe: a job replayed after a crash, a duplicate delivery, or a
stale sweep finds the document already past that state and returns without doing
anything. Remove the guard and a retry doubles the side effect.
'''

from sqlalchemy.orm import Session

from app.audit.log import write_audit
from app.enums import AuditAction, DocumentStatus, JobKind
from app.jobs.queue import enqueue
from app.models.document import Document
from app.models.document_page import DocumentPages
from app.processing import normalize, ocr
from app.storage import get_blob_store


def handle_normalize(session: Session, doc: Document) -> None:
    if doc.status != DocumentStatus.RECEIVED:
        return

    blob = get_blob_store()
    raw = blob.get(doc.blob_key)
    pdfa, pages = normalize.run(raw)

    # Derived key is content-addressed on the RECORD's sha256, so a re-run after a
    # crash overwrites the same key instead of orphaning a half-written blob.
    derived_key = f"derived/{doc.tenant_id}/{doc.sha256}/normalized.pdf"

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
    enqueue(session, tenant_id=doc.tenant_id, document_id=doc.id, kind=JobKind.OCR)


def handle_ocr(session: Session, doc: Document) -> None:
    if doc.status != DocumentStatus.NORMALIZED:
        return

    pdfa = get_blob_store().get(doc.normalized_blob_key)
    count = 0
    for page_no, text in ocr.pages(pdfa):
        session.add(
            DocumentPages(
                tenant_id=doc.tenant_id,
                document_id=doc.id,
                page_no=page_no,
                ocr_text=text,
            )
        )
        count += 1

    doc.status = DocumentStatus.OCR_DONE
    write_audit(
        session,
        tenant_id=doc.tenant_id,
        document_id=doc.id,
        actor="system",
        action=AuditAction.STATE_CHANGE,
        detail={"to": "OCR_DONE", "pages": count},
    )
    # The pipeline stops here this phase. `embed` lands with its handler; the future
    # hybrid-search PR hangs a parallel `index` job off this same fan-out point.


HANDLERS = {
    JobKind.NORMALIZE: handle_normalize,
    JobKind.OCR: handle_ocr,
}
