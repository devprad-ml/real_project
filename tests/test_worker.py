"""The worker loop.

`run_once` is the only place the two-transaction split, the rollback-then-record-the
-failure path, and the permanent/retryable decision actually run. Handlers are stubbed
here so a failure points at the worker rather than at OCR.
"""

import datetime
import time

import pytest
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.db import SessionLocal
from app.enums import DocumentStatus, JobKind, JobState
from app.errors import PermanentError
from app.jobs import worker
from app.jobs.queue import enqueue
from app.models import Document, Job
from app.processing import pdftext


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
    assert f"derived/{tenant}/{doc.id}/unknown.pdf" in blob.data


def test_run_once_drives_the_real_classify_handler(
    tenant, make_doc, blob, fake_tools, fake_classifier
):
    """The blob move deletes the old key in an after-commit hook, so the worker's own
    commit is the one that has to trigger it."""
    old_key = f"derived/{tenant}/x/unknown.pdf"
    blob.data[old_key] = b"pdf"
    doc = make_doc(status=DocumentStatus.NORMALIZED, normalized_blob_key=old_key)
    job_id = _seed_job(tenant, doc, kind=JobKind.CLASSIFY)

    assert worker.run_once() is True

    with SessionLocal() as s:
        assert s.get(Job, job_id).state == JobState.DONE
        assert s.get(Document, doc.id).status == DocumentStatus.CLASSIFIED
    assert f"derived/{tenant}/{doc.id}/claims.pdf" in blob.data
    assert old_key not in blob.data


def test_run_once_drives_the_real_embed_handler(
    tenant, make_doc, blob, vectors, fake_embedder, monkeypatch
):
    """The last transition this phase, through the worker rather than called directly
    -- the embed handler talks to a store outside the transaction, so its wiring is
    worth proving at the dispatch level too."""
    blob.data["derived/x/denial.pdf"] = b"pdf"
    monkeypatch.setattr(pdftext, "pages", lambda pdf: iter([(1, "a page")]))
    doc = make_doc(
        status=DocumentStatus.CLASSIFIED, normalized_blob_key="derived/x/denial.pdf"
    )
    job_id = _seed_job(tenant, doc, kind=JobKind.EMBED)

    assert worker.run_once() is True

    with SessionLocal() as s:
        assert s.get(Job, job_id).state == JobState.DONE
        assert s.get(Document, doc.id).status == DocumentStatus.EMBEDDED
    assert len(vectors.points(f"chunks_{tenant}")) == 1


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


def test_a_kind_with_no_handler_is_permanent(tenant, make_doc, monkeypatch):
    """A JobKind with no registered handler -- a typo'd or not-yet-built kind -- must
    not spend MAX_ATTEMPTS discovering it still does not exist. Every JobKind has a
    handler today, so one is unregistered here to reach that path."""
    monkeypatch.delitem(worker.HANDLERS, JobKind.EMBED)
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


def _state(job_id):
    with SessionLocal() as s:
        return s.get(Job, job_id).state


def test_a_heartbeat_keeps_a_job_alive_past_the_lease_and_silence_loses_it(
    tenant, make_doc, monkeypatch
):
    """The point of the heartbeat: the lease can be short because a live worker keeps
    proving it, while a silent one is reclaimed (m13.md 2.1)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "job_lease_seconds", 1)
    monkeypatch.setattr(settings, "job_heartbeat_seconds", 0.2)
    monkeypatch.setattr(settings, "sweep_seconds", 0.2)
    monkeypatch.setattr(worker.Housekeeper, "TICK", 0.05)
    doc = make_doc()
    with SessionLocal() as s:
        job = enqueue(s, tenant_id=tenant, document_id=doc.id, kind=JobKind.NORMALIZE)
        job.state = JobState.RUNNING
        job.locked_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        s.commit()
        job_id = job.id

    hk = worker.Housekeeper()
    hk.start()
    hk.hold(job_id)
    try:
        time.sleep(1.6)  # well past the 1 s lease, and the sweeper has run many times
        assert _state(job_id) == JobState.RUNNING

        hk.hold(None)  # the worker "dies": no more heartbeats
        deadline = time.monotonic() + 5
        while _state(job_id) != JobState.PENDING and time.monotonic() < deadline:
            time.sleep(0.1)
        assert _state(job_id) == JobState.PENDING
    finally:
        hk.stop()
        hk.join(2)


def test_the_loop_survives_a_database_outage_and_a_bookkeeping_bug(monkeypatch):
    """Before M13 one Postgres restart raised out of claim_one and ended the process."""
    monkeypatch.setattr(worker, "_stop", False)
    monkeypatch.setattr(worker, "housekeeper", None)
    monkeypatch.setattr(worker, "_poll_mailbox", lambda: None)
    monkeypatch.setattr(worker.signal, "signal", lambda *a: None)
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("SELECT 1", {}, Exception("connection closed"))
        if len(calls) == 2:
            raise RuntimeError("a bug outside any handler")
        worker._stop = True
        return False

    monkeypatch.setattr(worker, "run_once", flaky)

    worker.run_forever()

    assert len(calls) == 3  # kept going through both, then drained cleanly
