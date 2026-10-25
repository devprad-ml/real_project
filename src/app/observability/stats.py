''' The numbers behind GET /admin/stats: what is queued, stuck, failed, and spent.
See m13.md §3.4. Always scoped to one tenant. '''

import datetime
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import AuditAction, JobKind, JobState
from app.jobs.queue import _utcnow
from app.models.audit_log import Log
from app.models.document import Document
from app.models.job import Job
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
    now = _utcnow()
    lease = datetime.timedelta(seconds=get_settings().job_lease_seconds)

    documents = {
        status.value: n
        for status, n in db.execute(
            select(Document.status, func.count())
            .where(Document.tenant_id == tenant_id)
            .group_by(Document.status)
        )
    }

    jobs = {k.value: {"pending": 0, "running": 0, "error": 0} for k in JobKind}
    for kind, state, n in db.execute(
        select(Job.kind, Job.state, func.count())
        .where(Job.tenant_id == tenant_id)
        .group_by(Job.kind, Job.state)
    ):
        if state != JobState.DONE:  # done rows only ever accumulate
            jobs[kind.value][state.value] = n

    # Queue lag: how long the oldest job that is *due* has been waiting. A backoff
    # job not yet due is not lag, so it is not counted.
    oldest_due = {
        kind.value: int((now - oldest).total_seconds())
        for kind, oldest in db.execute(
            select(Job.kind, func.min(Job.run_after))
            .where(
                Job.tenant_id == tenant_id,
                Job.state == JobState.PENDING,
                Job.run_after <= now,
            )
            .group_by(Job.kind)
        )
    }

    running, stale = db.execute(
        select(
            func.count(),
            func.count().filter(Job.locked_at < func.now() - lease),  # the DB clock
        ).where(Job.tenant_id == tenant_id, Job.state == JobState.RUNNING)
    ).one()

    transitions = dict(
        db.execute(
            select(Log.detail["to"].as_string(), func.count())
            .where(
                Log.tenant_id == tenant_id,
                Log.action == AuditAction.STATE_CHANGE,
                Log.at >= now - datetime.timedelta(hours=1),
                Log.detail["to"].as_string().is_not(None),
            )
            .group_by(Log.detail["to"].as_string())
        ).all()
    )

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
        .where(
            LlmUsage.tenant_id == tenant_id,
            LlmUsage.created_at >= now - datetime.timedelta(hours=24),
        )
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
        "documents_by_status": documents,
        "jobs": jobs,
        "oldest_due_pending_s": oldest_due,
        "running_jobs": running,
        "stale_running": stale,
        "dead_letter": sum(j["error"] for j in jobs.values()),
        "transitions_last_hour": transitions,
        "llm_24h": llm,
    }
