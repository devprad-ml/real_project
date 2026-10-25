''' The review queue: what M7's confidence gate or M9's MPI match sent to NEEDS_REVIEW,
and the two ways a reviewer clears it. See m8.md §2, m9.md §3. '''

import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.audit.log import write_audit
from app.api.documents import tenant_doc
from app.db import get_session
from app.deps import actor, require_role
from app.enums import AuditAction, DocumentStatus, DocumentType, JobKind, Role
from app.jobs.queue import enqueue
from app.models.audit_log import Log
from app.models.document import Document
from app.models.extracted_field import ExtractedFieldRow
from app.models.member import Association
from app.models.principal import Principal
from app.processing.confidence import VALIDATORS
from app.processing.extract import FIELD_SCHEMAS
from app.storage import get_blob_store, move_normalized

router = APIRouter(prefix="/validation", tags=["Review"])
reviewer = require_role(Role.REVIEWER)


class FieldOut(BaseModel):
    name: str
    value: str | None
    confidence: float
    human_verified: bool


class QueueItem(BaseModel):
    id: uuid.UUID
    filename: str | None
    doc_type: DocumentType
    classify_confidence: float | None
    summary: str | None
    received_at: datetime.datetime
    reasons: list[str]
    fields: list[FieldOut]


class ResolveIn(BaseModel):
    fields: dict[str, str | None] = {}
    # The member is genuinely not in the MPI (a new enrollee not loaded yet). Skips
    # matching, which would only send it straight back here; routing sends an
    # unbound document to member services.
    no_mpi_match: bool = False


class ReclassifyIn(BaseModel):
    doc_type: DocumentType


@router.get("", response_model=list[QueueItem])
def queue(
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(reviewer),
    db: Session = Depends(get_session),
) -> list[QueueItem]:
    docs = db.scalars(
        select(Document)
        .where(
            Document.tenant_id == principal.tenant_id,
            Document.status == DocumentStatus.NEEDS_REVIEW,
        )
        .order_by(Document.received_at, Document.id)  # waited longest first
        .limit(limit)
        .offset(offset)
    ).all()
    ids = [d.id for d in docs]

    fields: dict[uuid.UUID, list[ExtractedFieldRow]] = {i: [] for i in ids}
    for f in db.scalars(
        select(ExtractedFieldRow).where(ExtractedFieldRow.document_id.in_(ids))
    ):
        fields[f.document_id].append(f)

    # Why it is here: the audit row that sent it (M7 writes `reasons`). The latest
    # one, in case it has been through review before.
    reasons = dict(
        db.execute(
            select(Log.document_id, Log.detail["reasons"])
            .where(
                Log.document_id.in_(ids),
                Log.action == AuditAction.STATE_CHANGE,
                Log.detail["to"].astext == DocumentStatus.NEEDS_REVIEW.value,
            )
            .order_by(Log.document_id, Log.at.desc())
            .distinct(Log.document_id)
        ).all()
    )

    out = []
    for d in docs:
        order = FIELD_SCHEMAS.get(d.doc_type, [])
        rows = sorted(
            fields[d.id],
            key=lambda f: order.index(f.name) if f.name in order else len(order),
        )
        out.append(
            QueueItem(
                id=d.id,
                filename=d.source_filename,
                doc_type=d.doc_type,
                classify_confidence=d.classify_confidence,
                summary=d.summary,
                received_at=d.received_at,
                reasons=reasons.get(d.id) or [],
                fields=[
                    FieldOut(
                        name=f.name,
                        value=f.value,
                        confidence=f.confidence,
                        human_verified=f.human_verified,
                    )
                    for f in rows
                ],
            )
        )
    return out


def _in_review(db: Session, principal: Principal, document_id: uuid.UUID) -> Document:
    ''' Locked, so two reviewers on the same document serialize: the second one
    finds it no longer in review and gets a 409 instead of overwriting the first. '''
    doc = tenant_doc(db, principal, document_id, lock=True)
    if doc.status != DocumentStatus.NEEDS_REVIEW:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="not in review")
    return doc


@router.post("/{document_id}/resolve", status_code=status.HTTP_204_NO_CONTENT)
def resolve(
    document_id: uuid.UUID,
    body: ResolveIn,
    principal: Principal = Depends(reviewer),
    db: Session = Depends(get_session),
) -> None:
    doc = _in_review(db, principal, document_id)
    if doc.doc_type is DocumentType.UNKNOWN:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="doc_type is unknown: reclassify first"
        )

    expected = FIELD_SCHEMAS.get(doc.doc_type, [])
    unknown = sorted(set(body.fields) - set(expected))
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"not a {doc.doc_type.value} field: {', '.join(unknown)}",
        )
    invalid = sorted(
        name
        for name, value in body.fields.items()
        if value is not None and name in VALIDATORS and not VALIDATORS[name](value)
    )
    if invalid:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"invalid value for: {', '.join(invalid)}",
        )

    changed = {}
    for row in db.scalars(
        select(ExtractedFieldRow).where(ExtractedFieldRow.document_id == doc.id)
    ):
        if row.name in body.fields and body.fields[row.name] != row.value:
            changed[row.name] = [row.value, body.fields[row.name]]
            row.value = body.fields[row.name]
        # The reviewer saw the whole list: untouched fields are confirmed, not skipped.
        row.confidence = 1.0
        row.human_verified = True

    detail = {"changed": changed}
    if body.no_mpi_match:
        # A binding from an earlier pass (before a downstream rejection) would make
        # route treat the document as bound.
        db.execute(delete(Association).where(Association.document_id == doc.id))
        doc.status, next_kind = DocumentStatus.ASSOCIATED, JobKind.ROUTE
        detail["no_mpi_match"] = True
    else:
        doc.status, next_kind = DocumentStatus.EXTRACTED, JobKind.ASSOCIATE
    write_audit(
        db,
        tenant_id=doc.tenant_id,
        document_id=doc.id,
        actor=actor(principal),
        action=AuditAction.RESOLVE,
        detail={"to": doc.status.value, **detail},
    )
    enqueue(db, tenant_id=doc.tenant_id, document_id=doc.id, kind=next_kind)
    db.commit()


@router.post("/{document_id}/reclassify", status_code=status.HTTP_202_ACCEPTED)
def reclassify(
    document_id: uuid.UUID,
    body: ReclassifyIn,
    principal: Principal = Depends(reviewer),
    db: Session = Depends(get_session),
) -> None:
    ''' 202: the new type is set now; the worker re-embeds and re-extracts. '''
    if body.doc_type is DocumentType.UNKNOWN:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="cannot reclassify to unknown"
        )
    doc = _in_review(db, principal, document_id)

    old_fields = {
        f.name: f.value
        for f in db.scalars(
            select(ExtractedFieldRow).where(ExtractedFieldRow.document_id == doc.id)
        )
    }
    old_type = doc.doc_type

    doc.doc_type = body.doc_type
    # A human chose the type. Left at classify's low score, the gate would send the
    # re-extracted document straight back here.
    doc.classify_confidence = 1.0
    move_normalized(db, doc, get_blob_store().get(doc.normalized_blob_key))
    # The old type's fields belong to the old schema. Their values live on below.
    db.execute(delete(ExtractedFieldRow).where(ExtractedFieldRow.document_id == doc.id))
    # Back through embed (overwrites the same point ids, so qdrant's doc_type payload
    # is updated, not duplicated) and extract (new schema, gate runs again).
    doc.status = DocumentStatus.CLASSIFIED
    write_audit(
        db,
        tenant_id=doc.tenant_id,
        document_id=doc.id,
        actor=actor(principal),
        action=AuditAction.RECLASSIFY,
        detail={"from": old_type.value, "to": body.doc_type.value, "fields": old_fields},
    )
    enqueue(db, tenant_id=doc.tenant_id, document_id=doc.id, kind=JobKind.EMBED)
    db.commit()
