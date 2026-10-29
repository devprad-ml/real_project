''' The queue is a Postgres table. Claim with SELECT ... FOR UPDATE SKIP LOCKED. '''

import datetime
import os
import random
import socket
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import AuditAction, DocumentStatus, JobKind, JobState
from app.audit.log import write_audit
from app.models.document import Document
from app.models.job import Job

# Exponential backoff stops growing here: past it, a longer wait only delays noticing
# that the downstream came back.
BACKOFF_CAP_SECONDS = 15 * 60


def _utcnow() -> datetime.datetime:
    # Columns are TIMESTAMP WITHOUT TIME ZONE and the DB's server_default now() writes
    # the server clock. Naive-UTC on both sides only agrees because the Postgres
    # container runs UTC -- that is an assumption, not a guarantee.
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def enqueue(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    kind: JobKind,
    run_after: datetime.datetime | None = None,
) -> Job:
    ''' Stage a job. Caller commits -- enqueueing in the same transaction as the row
    the job is about to act on is the whole reason the queue lives in Postgres. '''
    job = Job(
        tenant_id=tenant_id,
        document_id=document_id,
        kind=kind,
        state=JobState.PENDING,
        attempts=0,
        run_after=run_after or _utcnow(),
    )
    session.add(job)
    return job


def owner() -> str:
    ''' This process, as written to jobs.locked_by. Computed per call, not at import:
    a forked child has its own pid. '''
    return f"{socket.gethostname()}:{os.getpid()}"


def claim_one(session: Session) -> Job | None:
    ''' Take the oldest due pending job, locking it against other workers.

    SKIP LOCKED is the concurrency primitive: a row another transaction already holds
    is passed over instead of blocking, so N workers each get a different job and
    none of them queue up behind the slowest one. '''
    return session.scalar(
        select(Job)
        .where(Job.state == JobState.PENDING, Job.run_after <= _utcnow())
        .order_by(Job.run_after)
        .with_for_update(skip_locked=True)
        .limit(1)
    )


def sweep_stale(session: Session, lease_seconds: int | None = None) -> int:
    ''' Return jobs whose worker died mid-run to the pending pool.

    The crash window: a worker marks RUNNING, commits, then dies. Nothing else will
    ever touch that row -- claim_one only looks at PENDING. Without this the job is
    lost silently. Handlers guard on doc.status, so a job that actually finished its
    side effect before the crash re-runs as a no-op. '''
    # A running job refreshes locked_at every heartbeat (jobs/worker.py), so the lease
    # only has to cover missed heartbeats, not the slowest handler.
    lease = lease_seconds or get_settings().job_lease_seconds
    # The database clock on both sides of the comparison: locked_at is written by
    # now() too, so skew between worker hosts cannot make a healthy lock look stale.
    cutoff = func.now() - datetime.timedelta(seconds=lease)
    result = session.execute(
        update(Job)
        .where(Job.state == JobState.RUNNING, Job.locked_at < cutoff)
        .values(state=JobState.PENDING, locked_at=None, locked_by=None)
    )
    return result.rowcount


def fail_with_backoff(
    session: Session, job: Job, err: str, *, permanent: bool = False
) -> None:
    ''' Record a failed attempt. Retry with exponential backoff until max_attempts,
    then park the document in FAILED for a human. `permanent` skips the retries --
    an encrypted PDF is not going to decrypt itself on attempt four. '''
    settings = get_settings()
    # Push outlives downstream outages; everything else fails fast (m10.md §3).
    limit = (
        settings.push_max_attempts if job.kind == JobKind.PUSH else settings.max_attempts
    )
    job.attempts += 1
    job.last_error = err[:2000]
    job.locked_at = None
    job.locked_by = None

    if permanent or job.attempts >= limit:
        job.state = JobState.ERROR
        doc = session.get(Document, job.document_id)
        doc.status = DocumentStatus.FAILED
        write_audit(
            session,
            tenant_id=job.tenant_id,
            document_id=job.document_id,
            actor="system",
            action=AuditAction.STATE_CHANGE,
            detail={
                "to": "FAILED",
                "kind": job.kind.value,
                "permanent": permanent,
                "err": err[:500],
            },
        )
    else:
        job.state = JobState.PENDING
        # Jitter: every push that failed during an outage would otherwise retry at
        # the same instant and stampede the downstream the moment it comes back.
        delay = min(2**job.attempts, BACKOFF_CAP_SECONDS) * random.uniform(0.5, 1.0)
        job.run_after = _utcnow() + datetime.timedelta(seconds=delay)
