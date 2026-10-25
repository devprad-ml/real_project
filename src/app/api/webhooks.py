''' The downstream system's verdict on a pushed document. See m10.md §4.

No API key here: the caller is a system, authenticated by an HMAC over the raw body.
'''

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.log import write_audit
from app.config import get_settings
from app.db import get_session
from app.enums import AuditAction, DocumentStatus
from app.integrations import signing
from app.models.document import Document

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


class Verdict(BaseModel):
    external_id: uuid.UUID
    status: Literal["accepted", "rejected"]
    reason: str | None = None


async def raw_body(request: Request) -> bytes:
    # An async dependency so the route itself can stay sync (the DB session is sync).
    # The signature is over these exact bytes; re-serialized JSON is not what was signed.
    return await request.body()


@router.post("/downstream", status_code=status.HTTP_204_NO_CONTENT)
def downstream_verdict(
    body: bytes = Depends(raw_body),
    x_timestamp: str = Header(""),
    x_signature: str = Header(""),
    db: Session = Depends(get_session),
) -> None:
    secret = get_settings().downstream_webhook_secret.get_secret_value()
    if not signing.verify(secret, x_timestamp, body, x_signature):
        # Nothing written: an audit row per unauthenticated request would let anyone
        # fill the audit log.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="bad signature")
    try:
        verdict = Verdict.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.errors()
        ) from None

    # Found across tenants: the signature, not a principal, says who is asking.
    doc = db.scalar(
        select(Document).where(Document.id == verdict.external_id).with_for_update()
    )
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown external_id")
    if doc.status == DocumentStatus.ROUTED:
        # The verdict beat our push's commit. A non-2xx makes the sender retry; by
        # then the document is PUSHED and the verdict lands where it belongs.
        raise HTTPException(status.HTTP_409_CONFLICT, detail="push not recorded yet")

    detail = {"verdict": verdict.status, "reason": verdict.reason}
    if verdict.status == "rejected" and doc.status == DocumentStatus.PUSHED:
        doc.status = DocumentStatus.NEEDS_REVIEW
        action = AuditAction.STATE_CHANGE
        # `to` + `reasons`: the shape the review queue reads its reasons from.
        detail |= {
            "to": doc.status.value,
            "reasons": [f"downstream_rejected: {verdict.reason or 'no reason given'}"],
        }
    else:
        # Accepted, or a duplicate / late rejection for a document that has moved on:
        # recorded, nothing changes.
        action = AuditAction.VERDICT
    write_audit(
        db,
        tenant_id=doc.tenant_id,
        document_id=doc.id,
        actor="downstream",
        action=action,
        detail=detail,
    )
    db.commit()
