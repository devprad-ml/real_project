"""Queue mechanics. Each test fails for exactly one reason."""

import datetime

import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.enums import DocumentStatus, JobKind, JobState
from app.jobs.queue import claim_one, enqueue, fail_with_backoff, sweep_stale
from app.models import Document, Job


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def test_claim_one_skips_future_jobs(db, tenant, make_doc):
    doc = make_doc()
    with SessionLocal() as s:
        enqueue(
            s,
            tenant_id=tenant,
            document_id=doc.id,
            kind=JobKind.NORMALIZE,
            run_after=_utcnow() + datetime.timedelta(minutes=5),
        )
        s.commit()

    assert claim_one(db) is None


def test_two_workers_never_claim_the_same_job(tenant, make_doc):
    """The SKIP LOCKED proof.

    Two live sessions, two pending jobs. Session A holds a row lock on the first;
    session B must step over it and take the second rather than blocking. Without
    SKIP LOCKED, B would hang until A commits -- which is what makes N workers
    serialize behind the slowest job.
    """
    doc = make_doc()
    with SessionLocal() as s:
        for i in range(2):
            enqueue(
                s,
                tenant_id=tenant,
                document_id=doc.id,
                kind=JobKind.NORMALIZE,
                run_after=_utcnow() - datetime.timedelta(seconds=10 - i),
            )
        s.commit()

    with SessionLocal() as a, SessionLocal() as b:
        job_a = claim_one(a)
        job_b = claim_one(b)

        assert job_a is not None
        assert job_b is not None
        assert job_a.id != job_b.id

        # And a third claimant gets nothing -- both rows are locked, not merely
        # reordered.
        with SessionLocal() as c:
            assert claim_one(c) is None


def test_failure_backs_off_then_gives_up(db, tenant, make_doc):
    doc = make_doc()
    with SessionLocal() as s:
        job = enqueue(s, tenant_id=tenant, document_id=doc.id, kind=JobKind.NORMALIZE)
        s.commit()
        job_id = job.id

    max_attempts = get_settings().max_attempts

    with SessionLocal() as s:
        job = s.get(Job, job_id)
        fail_with_backoff(s, job, "boom")
        s.commit()
        assert job.attempts == 1
        assert job.state == JobState.PENDING
        # 2 ** 1 seconds out, so it is not immediately re-claimable.
        assert job.run_after > _utcnow()

    with SessionLocal() as s:
        assert claim_one(s) is None

    for _ in range(max_attempts - 1):
        with SessionLocal() as s:
            fail_with_backoff(s, s.get(Job, job_id), "boom")
            s.commit()

    with SessionLocal() as s:
        job = s.get(Job, job_id)
        assert job.attempts == max_attempts
        assert job.state == JobState.ERROR
        assert s.get(Document, doc.id).status == DocumentStatus.FAILED


def test_permanent_failure_skips_the_retries(db, tenant, make_doc):
    doc = make_doc()
    with SessionLocal() as s:
        job = enqueue(s, tenant_id=tenant, document_id=doc.id, kind=JobKind.NORMALIZE)
        s.commit()
        job_id = job.id

    with SessionLocal() as s:
        fail_with_backoff(s, s.get(Job, job_id), "encrypted PDF", permanent=True)
        s.commit()

    with SessionLocal() as s:
        assert s.get(Job, job_id).attempts == 1
        assert s.get(Job, job_id).state == JobState.ERROR
        assert s.get(Document, doc.id).status == DocumentStatus.FAILED


def test_sweep_reclaims_a_job_whose_worker_died(tenant, make_doc):
    """A worker that marks RUNNING and then dies leaves a row nothing else looks at.
    claim_one only reads PENDING, so without the sweeper the job is lost forever."""
    doc = make_doc()
    with SessionLocal() as s:
        job = enqueue(s, tenant_id=tenant, document_id=doc.id, kind=JobKind.NORMALIZE)
        job.state = JobState.RUNNING
        job.locked_at = _utcnow() - datetime.timedelta(hours=2)
        s.commit()
        job_id = job.id

    with SessionLocal() as s:
        assert claim_one(s) is None  # invisible to the claim scan

    with SessionLocal() as s:
        assert sweep_stale(s) == 1
        s.commit()

    with SessionLocal() as s:
        reclaimed = claim_one(s)
        assert reclaimed is not None
        assert reclaimed.id == job_id


def test_sweep_leaves_a_live_job_alone(tenant, make_doc):
    """A healthy long-running OCR must not be stolen mid-flight -- that is how one
    document gets processed twice."""
    doc = make_doc()
    with SessionLocal() as s:
        job = enqueue(s, tenant_id=tenant, document_id=doc.id, kind=JobKind.NORMALIZE)
        job.state = JobState.RUNNING
        job.locked_at = _utcnow()
        s.commit()

    with SessionLocal() as s:
        assert sweep_stale(s) == 0
        s.commit()


@pytest.mark.parametrize("attempts,expected", [(0, 2), (1, 4), (2, 8)])
def test_backoff_is_exponential(db, tenant, make_doc, attempts, expected):
    doc = make_doc()
    with SessionLocal() as s:
        job = enqueue(s, tenant_id=tenant, document_id=doc.id, kind=JobKind.NORMALIZE)
        job.attempts = attempts
        s.commit()
        before = _utcnow()
        fail_with_backoff(s, job, "boom")
        delay = (job.run_after - before).total_seconds()
        s.commit()

    assert expected - 1 <= delay <= expected + 1
