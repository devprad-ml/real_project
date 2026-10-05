''' Bytes in, RECEIVED document out. No IMAP in here -- the poller is one caller, an API
upload endpoint would be another. '''

import hashlib
import mimetypes
import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.log import write_audit
from app.enums import AuditAction, DocumentStatus, DocumentType, JobKind
from app.jobs.queue import enqueue
from app.models.document import Document
from app.storage import get_blob_store


def safe_filename(name: str) -> str:
    ''' A sender controls this string and it becomes part of a blob key: strip any
    path, control characters and leading dots so `../../x` cannot climb out of raw/. '''
    base = re.split(r"[\\/]", name)[-1]
    base = re.sub(r"[\x00-\x1f]", "", base).lstrip(".").strip()
    return base[:200] or "unnamed"


def intake_bytes(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    data: bytes,
    filename: str,
    source_id: uuid.UUID | None,
) -> Document | None:
    ''' The whole RECEIVED transition. Returns None for a duplicate. Does not commit. '''
    filename = safe_filename(filename)
    sha = hashlib.sha256(data).hexdigest()  # of the record bytes, never a derived copy

    # autoflush is off on SessionLocal: without this, a second identical attachment in
    # the same message would not see the first one's pending row.
    session.flush()
    existing = session.scalar(
        select(Document).where(Document.tenant_id == tenant_id, Document.sha256 == sha)
    )
    if existing is not None:
        # Not an error: the same denial letter forwarded by three people is normal.
        # ponytail: STATE_CHANGE is the closest AuditAction; add a DUPLICATE member (and
        # widen the VARCHAR) if audit queries need to tell the two apart.
        write_audit(
            session,
            tenant_id=tenant_id,
            document_id=existing.id,
            actor="system",
            action=AuditAction.STATE_CHANGE,
            detail={"event": "duplicate_intake", "filename": filename},
        )
        return None

    # Blob before row, same discipline as handle_normalize: a crash leaves an orphan
    # blob (cheap) rather than a RECEIVED row pointing at nothing (a poison job).
    key = f"raw/{tenant_id}/{sha}/{filename}"
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    get_blob_store().put(key, data, content_type)

    doc = Document(
        tenant_id=tenant_id,
        sha256=sha,
        doc_type=DocumentType.UNKNOWN,
        blob_key=key,
        status=DocumentStatus.RECEIVED,
        source_id=source_id,
        source_filename=filename,
    )
    session.add(doc)
    session.flush()  # doc.id is needed by the audit row and the job

    write_audit(
        session,
        tenant_id=tenant_id,
        document_id=doc.id,
        actor="system",
        action=AuditAction.STATE_CHANGE,
        detail={"to": "RECEIVED", "filename": filename},
    )
    enqueue(session, tenant_id=tenant_id, document_id=doc.id, kind=JobKind.NORMALIZE)
    return doc
