''' GET /search: semantic search over the caller's tenant's documents. '''

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_session
from app.deps import actor, require_role
from app.models.principal import Principal
from app.search.retrieve import SearchHit, search

router = APIRouter(tags=["Search"])


@router.get("/search", response_model=list[SearchHit])
def search_documents(
    # MiniLM truncates at 256 tokens, so a longer q does not search better; it only
    # lands a large string in audit_log on every hit.
    q: str = Query(min_length=1, max_length=1000),
    # An uncapped limit dumps a tenant's whole collection in one request.
    limit: int = Query(10, ge=1, le=50),
    principal: Principal = Depends(require_role()),
    db: Session = Depends(get_session),
) -> list[SearchHit]:
    # ponytail: the first search per process loads torch and the model (seconds).
    # Warm it in lifespan, or move encoding to its own service, when that matters.
    hits = search(
        db, tenant_id=principal.tenant_id, query=q, limit=limit, actor=actor(principal)
    )
    # Same session as the auth dependency's (FastAPI caches a dependency per request).
    # If this commit fails the caller gets a 500, never results without the audit rows.
    db.commit()
    return hits
