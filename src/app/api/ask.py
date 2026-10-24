''' POST /ask: a question answered from the caller's tenant's documents, with
citations. See m11.md. '''

import uuid

import anthropic
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session
from app.deps import actor, require_role
from app.enums import DocumentType
from app.models.principal import Principal
from app.processing import answer
from app.search.retrieve import audit_views, retrieve

router = APIRouter(tags=["Ask"])


class AskIn(BaseModel):
    # Same cap as /search's q: the embedder truncates at 256 tokens, and the
    # question lands in audit_log once per retrieved document.
    question: str = Field(min_length=1, max_length=1000)


class Source(BaseModel):
    document_id: uuid.UUID
    filename: str | None
    page_no: int
    doc_type: DocumentType


class AskOut(BaseModel):
    answer: list[answer.Segment]
    sources: list[Source]
    answered: bool


@router.post("/ask", response_model=AskOut)
def ask(
    body: AskIn,
    principal: Principal = Depends(require_role()),
    db: Session = Depends(get_session),
) -> AskOut:
    hits = retrieve(
        db,
        tenant_id=principal.tenant_id,
        query=body.question,
        limit=get_settings().ask_top_k,
    )
    if not hits:
        return AskOut(answer=[], sources=[], answered=False)  # nothing to pay for

    # Before the call, and committed: the chunk text reaches Anthropic whether or not
    # the call then succeeds, so the record of that must not depend on it.
    audit_views(
        db,
        hits,
        tenant_id=principal.tenant_id,
        actor=actor(principal),
        detail={"via": "ask", "question": body.question},
    )
    db.commit()
    # Give the pooled connection back for the seconds Claude takes; held, ~15
    # concurrent asks would drain the pool and stall every other route.
    db.close()

    try:
        segments = answer.run(body.question, hits)
    except anthropic.BadRequestError:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail="the model rejected the request"
        ) from None
    except (anthropic.APIStatusError, anthropic.APIConnectionError):
        # 429/5xx/network, after the SDK's own retries.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="the model is unavailable"
        ) from None

    sources = [
        Source(
            document_id=h.document_id,
            filename=h.filename,
            page_no=h.page_no,
            doc_type=h.doc_type,
        )
        for h in hits
    ]
    return AskOut(
        answer=segments or [], sources=sources, answered=segments is not None
    )
