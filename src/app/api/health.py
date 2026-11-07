''' /healthz, /readyz endpoints for health checks.'''

import anyio.to_thread
from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import text

from app.db import probe_engine

router = APIRouter(tags=["Health"])

@router.get("/healthz")
def liveness() -> dict[str, str]:
    return {"status":"ok"}


def ping_database() -> None:
    with probe_engine.connect() as connection:
        connection.execute(text("SELECT 1"))


@router.get("/readyz")
async def readiness(request: Request) -> dict[str, str]:
    ''' Postgres only: every route needs it. Not qdrant: an unready pod is pulled from
    the load balancer, and qdrant is shared, so a qdrant outage would pull every pod
    and take review, downloads and webhooks down with search. /search and /ask fail on
    their own; /admin/stats reports the vector store.

    Answers even when the pod is saturated: it skips admission control (main.py), runs
    on its own thread slot and its own connection. Otherwise every pod under the same
    load fails readiness together and the balancer pulls them all: a slow service
    becomes no service. '''
    try:
        await anyio.to_thread.run_sync(
            ping_database, limiter=request.app.state.probe_slot
        )
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Database unavailable")
    return {"status": "ok"}
