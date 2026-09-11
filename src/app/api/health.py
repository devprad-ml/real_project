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
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Database unavailable")
    

