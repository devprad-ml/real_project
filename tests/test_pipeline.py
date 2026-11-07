"""The ingest pipeline: stored attachment to searchable document, side by side."""

import datetime
import threading
import time

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.enums import DocumentType
from app.errors import PermanentError
from app.ingestion import pipeline
from app.models import Document, Log
from app.rag import normalize
from app.storage.vectors import tenant_collection

from conftest import FAKE_PDFA


def _fresh(doc):
    with SessionLocal() as s:
        return s.get(Document, doc.id)


def test_a_document_is_normalized_classified_and_embedded(
    tenant, make_doc, blob, vectors, fake_tools, fake_classifier, fake_embedder
):
    doc = make_doc()

    pipeline.process(doc.id)

    done = _fresh(doc)
    assert done.processed_at is not None
    assert (done.attempts, done.error) == (1, None)
    assert done.doc_type == DocumentType.CLAIMS
    assert done.normalized_blob_key.endswith(f"/{doc.id}/claims.pdf")
    assert blob.data[done.normalized_blob_key] == FAKE_PDFA
    assert blob.data[done.blob_key] == b"original record bytes"
    assert vectors.points(tenant_collection(tenant))
    with SessionLocal() as s:
        [row] = s.scalars(select(Log).where(Log.document_id == doc.id)).all()
    assert row.detail["event"] == "processed"


def test_a_transient_failure_keeps_the_error_and_can_retry(
    make_doc, blob, vectors, fake_tools, fake_classifier, fake_embedder, monkeypatch
):
    doc = make_doc()
    good = normalize.run

    def fail_once(data):
        monkeypatch.setattr(normalize, "run", good)
        raise ConnectionError("Jane Roe")

    monkeypatch.setattr(normalize, "run", fail_once)

    pipeline.process(doc.id)
    failed = _fresh(doc)
    assert failed.processed_at is None
    assert failed.attempts == 1
    assert "Jane Roe" in failed.error

    pipeline.process(doc.id)
    done = _fresh(doc)
    assert done.processed_at is not None
    assert (done.attempts, done.error) == (2, None)


def test_a_permanent_failure_uses_up_every_attempt(make_doc, blob, monkeypatch):
    def refuse(data):
        raise PermanentError("encrypted PDF")

    monkeypatch.setattr(normalize, "run", refuse)
    doc = make_doc()

    pipeline.process(doc.id)

    assert _fresh(doc).attempts == get_settings().max_attempts
    calls = []
    monkeypatch.setattr(pipeline, "_run", lambda d: calls.append(d))
    pipeline.process(doc.id)
    assert calls == []


def test_an_attempt_is_counted_before_the_work_starts(make_doc, monkeypatch):
    """A document that kills the process must not get a free retry on every restart."""
    seen = []
    doc = make_doc()

    def run(d):
        seen.append(_fresh(doc).attempts)
        return {}

    monkeypatch.setattr(pipeline, "_run", run)

    pipeline.process(doc.id)

    assert seen == [1]


def test_a_processed_document_is_never_run_again(make_doc, monkeypatch):
    doc = make_doc()
    calls = []
    monkeypatch.setattr(pipeline, "_run", lambda d: calls.append(d.id) or {})

    pipeline.process(doc.id)
    pipeline.process(doc.id)

    assert calls == [doc.id]


def test_documents_run_side_by_side(make_doc, monkeypatch):
    """All three are inside _run at once, or the barrier times out and raises."""
    barrier = threading.Barrier(3, timeout=10)
    docs = [make_doc() for _ in range(3)]

    def run(d):
        barrier.wait()
        return {}

    monkeypatch.setattr(pipeline, "_run", run)

    for doc in docs:
        pipeline.submit(doc.id)

    for doc in docs:
        _wait_processed(doc)


def test_a_queued_document_is_not_queued_twice(make_doc, monkeypatch):
    release = threading.Event()
    calls = []
    doc = make_doc()

    def run(d):
        calls.append(d.id)
        release.wait(10)
        return {}

    monkeypatch.setattr(pipeline, "_run", run)

    pipeline.submit(doc.id)
    pipeline.submit(doc.id)
    release.set()
    _wait_processed(doc)

    assert calls == [doc.id]


def test_retry_unfinished_skips_finished_and_given_up_documents(make_doc, monkeypatch):
    pending = make_doc(attempts=1)
    make_doc(attempts=get_settings().max_attempts)
    make_doc(processed_at=datetime.datetime(2026, 11, 1))
    submitted = []
    monkeypatch.setattr(pipeline, "submit", submitted.append)

    pipeline.retry_unfinished()

    assert pending.id in submitted
    with SessionLocal() as s:
        rows = s.scalars(select(Document).where(Document.id.in_(submitted))).all()
    assert all(r.processed_at is None for r in rows)
    assert all(r.attempts < get_settings().max_attempts for r in rows)


def _wait_processed(doc, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _fresh(doc).processed_at is not None:
            return
        time.sleep(0.05)
    raise AssertionError("document was not processed in time")
