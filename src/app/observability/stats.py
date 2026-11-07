''' The numbers behind GET /admin/stats: document counts for one tenant. Claude spend and
latency are in Langfuse. '''

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.document import Document


def collect(db: Session, tenant_id: uuid.UUID) -> dict:
    max_attempts = get_settings().max_attempts
    ready, failed, processing = db.execute(
        select(
            func.count().filter(Document.processed_at.is_not(None)),
            func.count().filter(
                Document.processed_at.is_(None), Document.attempts >= max_attempts
            ),
            func.count().filter(
                Document.processed_at.is_(None), Document.attempts < max_attempts
            ),
        ).where(Document.tenant_id == tenant_id)
    ).one()
    return {"documents": {"ready": ready, "processing": processing, "failed": failed}}
