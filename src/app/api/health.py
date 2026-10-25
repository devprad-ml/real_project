''' /healthz, /readyz endpoints for health checks.'''

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_session

router = APIRouter(tags=["Health"])

@router.get("/healthz")
def liveness() -> dict[str, str]:
    return {"status":"ok"}


@router.get("/readyz")
def readiness(db: Session = Depends(get_session)) -> dict[str, str]:
    ''' Postgres only: every route needs it. Not qdrant: an unready pod is pulled from
    the load balancer, and qdrant is shared, so a qdrant outage would pull every pod
    and take review, downloads and webhooks down with search. /search and /ask fail on
    their own; /admin/stats reports the vector store. '''
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Database unavailable")
    return {"status": "ok"}
    

