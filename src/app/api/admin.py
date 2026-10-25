''' Admin recovery. See m10.md §5.

`ponytail:` one document per call; a bulk requeue (every FAILED push after an outage)
when someone has to click this hundreds of times.
'''

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.documents import tenant_doc
from app.audit.log import write_audit
from app.db import get_session
from app.deps import actor, require_role
from app.enums import AuditAction, DocumentStatus, JobState, Role
from app.jobs.handlers import REQUIRES
from app.jobs.queue import enqueue
from app.models.job import Job
from app.models.principal import Principal
from app.observability import stats as stats_mod
from app.storage import get_vector_store

router = APIRouter(prefix="/admin", tags=["Admin"])
admin = require_role(Role.ADMIN)


@router.post("/documents/{document_id}/requeue", status_code=status.HTTP_202_ACCEPTED)
def requeue(
    document_id: uuid.UUID,
    principal: Principal = Depends(admin),
    db: Session = Depends(get_session),
) -> None:
    ''' Put a FAILED document back where the failed step starts, and run that step
    again with a fresh attempt count. 202: the worker does the work. '''
    doc = tenant_doc(db, principal, document_id, lock=True)
    if doc.status != DocumentStatus.FAILED:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="not failed")
    failed = db.scalar(
        select(Job)
        .where(Job.document_id == doc.id, Job.state == JobState.ERROR)
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    if failed is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="no failed job to retry")

    doc.status = REQUIRES[failed.kind]
    write_audit(
        db,
        tenant_id=doc.tenant_id,
        document_id=doc.id,
        actor=actor(principal),
        action=AuditAction.REQUEUE,
        detail={"kind": failed.kind.value, "from": "FAILED", "to": doc.status.value},
    )
    enqueue(db, tenant_id=doc.tenant_id, document_id=doc.id, kind=failed.kind)
    db.commit()


@router.get("/stats")
def stats(
    principal: Principal = Depends(admin), db: Session = Depends(get_session)
) -> dict:
    ''' Queue lag, dead letters, stuck jobs and Claude spend for the caller's tenant.
    Tenant-scoped on purpose: ADMIN is a tenant administrator, and a global view would
    show tenant A the counts of tenant B (m13.md §3.4). '''
    body = stats_mod.collect(db, principal.tenant_id)
    try:
        get_vector_store().ping()
        body["vector_store"] = "ok"
    except Exception:
        body["vector_store"] = "unreachable"
    return body
