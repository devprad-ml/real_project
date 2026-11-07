''' GET /admin/stats. '''

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_session
from app.deps import require_role
from app.enums import Role
from app.models.principal import Principal
from app.observability import stats as stats_mod
from app.storage import get_vector_store

router = APIRouter(prefix="/admin", tags=["Admin"])
admin = require_role(Role.ADMIN)


@router.get("/stats")
def stats(
    principal: Principal = Depends(admin), db: Session = Depends(get_session)
) -> dict:
    ''' Document counts and Claude spend for the caller's tenant. Tenant-scoped on
    purpose: a global view would show tenant A the counts of tenant B. '''
    body = stats_mod.collect(db, principal.tenant_id)
    try:
        get_vector_store().ping()
        body["vector_store"] = "ok"
    except Exception:
        body["vector_store"] = "unreachable"
    return body
