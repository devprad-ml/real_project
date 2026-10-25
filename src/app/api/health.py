''' /healthz, /readyz endpoints for health checks.'''

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_session
from app.storage import get_vector_store

router = APIRouter(tags=["Health"])

@router.get("/healthz")
def liveness() -> dict[str, str]:
    return {"status":"ok"}


@router.get("/readyz")
def readiness(db: Session = Depends(get_session)) -> dict[str, str]:
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Database unavailable")
    try:
        # /ask and /search are down when the vector store is; ready must say so.
        get_vector_store().ping()
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Vector store unavailable")
    return {"status": "ok"}
    

