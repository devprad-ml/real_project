''' Append-only audit trail. Every state change goes through here. '''

import uuid

from sqlalchemy.orm import Session

from app.enums import AuditAction
from app.models.audit_log import Log


def write_audit(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    actor: str,
    action: AuditAction,
    detail: dict,
) -> None:
    ''' Stage an audit row. Commits with the caller's transaction, not separately --
    an audit row for a state change that got rolled back would be a lie. '''
    session.add(
        Log(
            tenant_id=tenant_id,
            document_id=document_id,
            actor=actor,
            action=action,
            detail=detail,
        )
    )
