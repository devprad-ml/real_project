''' The numbers behind GET /admin/stats: documents and Claude spend for one tenant. '''

import datetime
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.document import Document
from app.models.llm_usage import LlmUsage

# USD per million tokens, (input, output). ponytail: as of 2026-10-25, typed in by hand.
# Haiku 5.5 bills 5x above a 100k-token prompt; ours are ~3k, so one tier. Move to
# config when a price changes in a way that matters.
PRICE_PER_MTOK = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


def est_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    ''' None for a model not in the table: tokens are facts, a guessed price is not. '''
    price = PRICE_PER_MTOK.get(model)
    if price is None:
        return None
    return round((input_tokens * price[0] + output_tokens * price[1]) / 1e6, 6)


def collect(db: Session, tenant_id: uuid.UUID) -> dict:
    ready, failed, processing = db.execute(
        select(
            func.count().filter(Document.processed_at.is_not(None)),
            func.count().filter(
                Document.processed_at.is_(None),
                Document.attempts >= get_settings().max_attempts,
            ),
            func.count().filter(
                Document.processed_at.is_(None),
                Document.attempts < get_settings().max_attempts,
            ),
        ).where(Document.tenant_id == tenant_id)
    ).one()

    since = datetime.datetime.now(datetime.timezone.utc).replace(
        tzinfo=None
    ) - datetime.timedelta(hours=24)
    llm = []
    for purpose, model, calls, tin, tout, p50, p95 in db.execute(
        select(
            LlmUsage.purpose,
            LlmUsage.model,
            func.count(),
            func.sum(LlmUsage.input_tokens),
            func.sum(LlmUsage.output_tokens),
            func.percentile_cont(0.5).within_group(LlmUsage.latency_ms),
            func.percentile_cont(0.95).within_group(LlmUsage.latency_ms),
        )
        .where(LlmUsage.tenant_id == tenant_id, LlmUsage.created_at >= since)
        .group_by(LlmUsage.purpose, LlmUsage.model)
        .order_by(LlmUsage.purpose, LlmUsage.model)
    ):
        llm.append(
            {
                "purpose": purpose,
                "model": model,
                "calls": calls,
                "input_tokens": int(tin),
                "output_tokens": int(tout),
                "p50_ms": int(p50),
                "p95_ms": int(p95),
                "est_usd": est_usd(model, int(tin), int(tout)),
            }
        )

    return {
        "documents": {"ready": ready, "processing": processing, "failed": failed},
        "llm_24h": llm,
    }
