''' GET /search: semantic search over one tenant's documents. '''

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_session
from app.models.client import Client
from app.search.retrieve import SearchHit, search

router = APIRouter(tags=["Search"])


def current_tenant(
    x_tenant_id: uuid.UUID = Header(), db: Session = Depends(get_session)
) -> uuid.UUID:
    # ponytail: trusted header, so anyone who can reach the API can read any tenant.
    # Fine on localhost only. When auth lands this body becomes "tenant for this API
    # key" and nothing else in the read path changes.
    if db.get(Client, x_tenant_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown tenant")
    return x_tenant_id


@router.get("/search", response_model=list[SearchHit])
def search_documents(
    # MiniLM truncates at 256 tokens, so a longer q does not search better; it only
    # lands a large string in audit_log on every hit.
    q: str = Query(min_length=1, max_length=1000),
    # An uncapped limit dumps a tenant's whole collection in one request.
    limit: int = Query(10, ge=1, le=50),
    tenant_id: uuid.UUID = Depends(current_tenant),
    db: Session = Depends(get_session),
) -> list[SearchHit]:
    # ponytail: the first search per process loads torch and the model (seconds).
    # Warm it in lifespan, or move encoding to its own service, when that matters.
    hits = search(db, tenant_id=tenant_id, query=q, limit=limit)
    # Same session as current_tenant's (FastAPI caches a dependency per request). If
    # this commit fails the caller gets a 500, never results without the audit rows.
    db.commit()
    return hits
