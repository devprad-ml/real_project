"""Handler transitions.

The OCR toolchain (Tesseract, Ghostscript) is faked out here on purpose: these tests
are about the state machine -- what status the document lands in, what audit row is
written, what job comes next -- not about whether Tesseract can read a fax.
"""

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.enums import DocumentStatus, JobKind, JobState
from app.jobs import handlers
from app.models import Document, DocumentPages, Job, Log
from app.processing import normalize, ocr

FAKE_PDFA = b"%PDF-1.7 fake pdfa"
PAGE_TEXTS = ["page one text", "page two text", ""]


class FakeBlobStore:
    """In-memory BlobStore. Structurally compatible -- Protocol, not inheritance."""

    def __init__(self, seed=None):
        self.data = dict(seed or {})

    def put(self, key, data, content_type):
        self.data[key] = data

    def get(self, key):
        return self.data[key]

    def url(self, key):
        return f"memory://{key}"


@pytest.fixture
def blob(monkeypatch):
    store = FakeBlobStore({"raw/test/file.pdf": b"original record bytes"})
    monkeypatch.setattr(handlers, "get_blob_store", lambda: store)
    return store


@pytest.fixture
def fake_tools(monkeypatch):
    monkeypatch.setattr(normalize, "run", lambda data: (FAKE_PDFA, len(PAGE_TEXTS)))
    monkeypatch.setattr(ocr, "pages", lambda pdf: enumerate(PAGE_TEXTS, start=1))


def test_normalize_writes_a_derived_artifact_and_enqueues_ocr(
    tenant, make_doc, blob, fake_tools
):
    doc = make_doc()
    with SessionLocal() as s:
        handlers.handle_normalize(s, s.get(Document, doc.id))
        s.commit()

    expected_key = f"derived/{tenant}/{doc.sha256}/normalized.pdf"
    assert blob.data[expected_key] == FAKE_PDFA
    # The record is untouched -- normalize writes next to it, never over it.
    assert blob.data["raw/test/file.pdf"] == b"original record bytes"

    with SessionLocal() as s:
        fresh = s.get(Document, doc.id)
        assert fresh.status == DocumentStatus.NORMALIZED
        assert fresh.normalized_blob_key == expected_key
        assert fresh.page_count == 3

        jobs = s.scalars(select(Job).where(Job.document_id == doc.id)).all()
        assert [j.kind for j in jobs] == [JobKind.OCR]
        assert jobs[0].state == JobState.PENDING

        logs = s.scalars(select(Log).where(Log.document_id == doc.id)).all()
        assert [log.detail["to"] for log in logs] == ["NORMALIZED"]


def test_normalize_is_a_noop_once_past_received(tenant, make_doc, blob, fake_tools):
    """The replay guard. A job re-delivered after a crash must not enqueue a second
    OCR job or rewrite the derived artifact."""
    doc = make_doc(status=DocumentStatus.NORMALIZED)
    with SessionLocal() as s:
        handlers.handle_normalize(s, s.get(Document, doc.id))
        s.commit()

    with SessionLocal() as s:
        assert s.scalars(select(Job).where(Job.document_id == doc.id)).all() == []
    assert f"derived/{tenant}/{doc.sha256}/normalized.pdf" not in blob.data


def test_ocr_writes_one_row_per_page(tenant, make_doc, blob, fake_tools):
    doc = make_doc(
        status=DocumentStatus.NORMALIZED,
        normalized_blob_key="derived/x/normalized.pdf",
        page_count=3,
    )
    blob.data["derived/x/normalized.pdf"] = FAKE_PDFA

    with SessionLocal() as s:
        handlers.handle_ocr(s, s.get(Document, doc.id))
        s.commit()

    with SessionLocal() as s:
        pages = s.scalars(
            select(DocumentPages)
            .where(DocumentPages.document_id == doc.id)
            .order_by(DocumentPages.page_no)
        ).all()
        # An empty page still gets a row: page_no has to stay aligned with the
        # physical document or later page citations point at the wrong sheet.
        assert [p.page_no for p in pages] == [1, 2, 3]
        assert [p.ocr_text for p in pages] == PAGE_TEXTS
        assert s.get(Document, doc.id).status == DocumentStatus.OCR_DONE
        # No embed handler this phase, so the pipeline stops here.
        assert s.scalars(select(Job).where(Job.document_id == doc.id)).all() == []


def test_ocr_is_a_noop_before_normalize_ran(tenant, make_doc, blob, fake_tools):
    doc = make_doc(status=DocumentStatus.RECEIVED)
    with SessionLocal() as s:
        handlers.handle_ocr(s, s.get(Document, doc.id))
        s.commit()

    with SessionLocal() as s:
        assert (
            s.scalars(
                select(DocumentPages).where(DocumentPages.document_id == doc.id)
            ).all()
            == []
        )
        assert s.get(Document, doc.id).status == DocumentStatus.RECEIVED
