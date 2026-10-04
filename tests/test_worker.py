"""The worker loop.

`run_once` is the only place the two-transaction split, the rollback-then-record-the
-failure path, and the permanent/retryable decision actually run. Handlers are stubbed
here so a failure points at the worker rather than at OCR.
"""

import datetime

import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.enums import DocumentStatus, JobKind, JobState
from app.errors import PermanentError
from app.jobs import worker
from app.jobs.queue import enqueue
from app.models import Document, Job


def _seed_job(tenant, doc, kind=JobKind.NORMALIZE):
    with SessionLocal() as s:
        job = enqueue(s, tenant_id=tenant, document_id=doc.id, kind=kind)
        s.commit()
        return job.id


def _stub(monkeypatch, kind, fn):
    """Swap one handler. worker.HANDLERS is the same dict object as
    handlers.HANDLERS, so setitem reaches the worker's lookup."""
    monkeypatch.setitem(worker.HANDLERS, kind, fn)


def test_run_once_returns_false_on_an_empty_queue(tenant):
    assert worker.run_once() is False


def test_run_once_drives_the_real_normalize_handler(tenant, make_doc, blob, fake_tools):
    """One end-to-end pass with the actual handler, to prove the wiring: claim,
    dispatch, commit, and the follow-on job all line up."""
    doc = make_doc()
    job_id = _seed_job(tenant, doc)

    assert worker.run_once() is True

    with SessionLocal() as s:
        assert s.get(Job, job_id).state == JobState.DONE
        assert s.get(Document, doc.id).status == DocumentStatus.NORMALIZED
    assert f"derived/{tenant}/{doc.sha256}/normalized.pdf" in blob.data


def test_successful_job_is_marked_done_and_unlocked(tenant, make_doc, monkeypatch):
    doc = make_doc()
    job_id = _seed_job(tenant, doc)
    _stub(monkeypatch, JobKind.NORMALIZE, lambda session, d: None)

    assert worker.run_once() is True

    with SessionLocal() as s:
        job = s.get(Job, job_id)
        assert job.state == JobState.DONE
        assert job.locked_at is None
        assert job.attempts == 0


def test_a_raising_handler_leaves_the_document_untouched(tenant, make_doc, monkeypatch):
    """The rollback matters: the handler mutates the document and *then* raises. If
    the worker committed anything from that session, the document would be left in a
    state no handler can recover from."""
    doc = make_doc()
    job_id = _seed_job(tenant, doc)

    def explode(session, d):
        d.status = DocumentStatus.NORMALIZED  # partial work, must not survive
        raise RuntimeError("boom")

    _stub(monkeypatch, JobKind.NORMALIZE, explode)

    assert worker.run_once() is True

    with SessionLocal() as s:
        job = s.get(Job, job_id)
        assert job.state == JobState.PENDING  # queued for another attempt
        assert job.attempts == 1
        assert "boom" in job.last_error
        assert job.locked_at is None
        assert job.run_after > datetime.datetime.utcnow()  # backed off
        assert s.get(Document, doc.id).status == DocumentStatus.RECEIVED


def test_permanent_error_fails_the_document_immediately(tenant, make_doc, monkeypatch):
    doc = make_doc()
    job_id = _seed_job(tenant, doc)

    def refuse(session, d):
        raise PermanentError("encrypted PDF")

    _stub(monkeypatch, JobKind.NORMALIZE, refuse)

    assert worker.run_once() is True

    with SessionLocal() as s:
        job = s.get(Job, job_id)
        assert job.state == JobState.ERROR
        assert job.attempts == 1  # no retries burned
        assert s.get(Document, doc.id).status == DocumentStatus.FAILED


def test_a_kind_with_no_handler_is_permanent(tenant, make_doc):
    """`embed` is a real JobKind with no handler yet. A typo'd or not-yet-built kind
    must not spend MAX_ATTEMPTS discovering it still does not exist."""
    doc = make_doc()
    job_id = _seed_job(tenant, doc, kind=JobKind.EMBED)

    assert worker.run_once() is True

    with SessionLocal() as s:
        job = s.get(Job, job_id)
        assert job.state == JobState.ERROR
        assert job.attempts == 1
        assert "no handler" in job.last_error


def test_retries_eventually_exhaust_and_fail_the_document(
    tenant, make_doc, monkeypatch
):
    """Drive the whole retry ladder through the worker, clearing run_after between
    attempts so the backoff does not make the test sleep."""
    doc = make_doc()
    job_id = _seed_job(tenant, doc)
    _stub(
        monkeypatch,
        JobKind.NORMALIZE,
        lambda session, d: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    max_attempts = get_settings().max_attempts
    for _ in range(max_attempts):
        assert worker.run_once() is True
        with SessionLocal() as s:  # make the next attempt due immediately
            job = s.get(Job, job_id)
            if job.state == JobState.PENDING:
                job.run_after = datetime.datetime.utcnow()
                s.commit()

    with SessionLocal() as s:
        assert s.get(Job, job_id).state == JobState.ERROR
        assert s.get(Job, job_id).attempts == max_attempts
        assert s.get(Document, doc.id).status == DocumentStatus.FAILED


def test_claimed_job_is_invisible_to_a_second_worker(tenant, make_doc, monkeypatch):
    """While a handler runs, the job is RUNNING rather than locked -- the claim
    transaction already committed and released the row. A second worker must still
    pass over it."""
    seen = {}

    def check_mid_flight(session, d):
        seen["claimable"] = worker.run_once()  # a second worker, mid-handler

    doc = make_doc()
    _seed_job(tenant, doc)
    _stub(monkeypatch, JobKind.NORMALIZE, check_mid_flight)

    assert worker.run_once() is True
    assert seen["claimable"] is False


@pytest.mark.parametrize("signum", [2, 15])
def test_signals_request_a_drain_rather_than_an_exit(signum, monkeypatch):
    """SIGINT/SIGTERM only set a flag. Exiting mid-job would strand a RUNNING row for
    a full lease on every deploy."""
    monkeypatch.setattr(worker, "_stop", False)
    worker._request_stop(signum, None)
    assert worker._stop is True
